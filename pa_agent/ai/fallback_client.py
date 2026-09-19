"""Ordered, request-local fallback across OpenAI-compatible API groups."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from pa_agent.ai.deepseek_client import CancelledError, DeepSeekClient
from pa_agent.config.settings import AIProviderSettings


class AllCandidatesFailed(RuntimeError):
    """Every configured candidate failed with a recoverable provider error."""


def _failure_kind(exc: Exception) -> tuple[str, bool] | None:
    """Return a safe label and whether the entire API group should be skipped."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if (status in (401, 403) or "authenticationerror" in name
            or "permissiondenied" in name or "api_key_invalid" in message):
        return "API Key 或帳戶權限無效", True
    if status == 429 or "ratelimit" in name:
        return "請求受限或額度用盡", False
    if status == 402 or "out of credits" in message or "payment required" in message:
        return "帳戶額度不足", True
    if any(s in message for s in ("insufficient quota", "quota exceeded", "积分不足")):
        return "額度不足", False
    if status in (404, 410) or "not found" in message or "model does not exist" in message:
        return "模型不可用", False
    if status in (408, 409, 425) or (isinstance(status, int) and status >= 500):
        return f"HTTP {status}", False
    if ("timeout" in name or "connection" in name or "protocolerror" in name
            or "connecterror" in name or "readerror" in name
            or any(s in message for s in (
                "timeout", "connection", "disconnected", "connection reset",
            ))):
        return "連線逾時或中斷", False
    if status == 400 and any(s in message for s in (
        "model not found", "unsupported model", "invalid model", "model is not supported",
        "model does not exist", "unknown model", "unsupported reasoning_effort",
        "reasoning_effort is not supported", "thinking is not supported",
    )):
        return "模型或模型參數不支援", False
    return None


class FallbackClient:
    """Preserve the regular client API while trying configured candidates."""

    def __init__(self, settings: AIProviderSettings, logger_: logging.Logger | None = None) -> None:
        self._settings = settings.model_copy(deep=True)
        self._log = logger_ or logging.getLogger(__name__)
        self._preferred: tuple[int, int] | None = None
        self._last_success: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._local = threading.local()

    def update_provider(self, settings: AIProviderSettings) -> None:
        with self._lock:
            self._settings = settings.model_copy(deep=True)
            self._preferred = None
            self._last_success = None

    def get_last_success(self) -> dict[str, Any] | None:
        """Return safe metadata for the most recently successful candidate."""
        with self._lock:
            return dict(self._last_success) if self._last_success else None

    def set_progress_callback(self, callback: Callable[[str], None] | None) -> None:
        """Attach a UI status sink to the calling worker thread."""
        self._local.progress = callback

    def _ordered_candidates(self, settings: AIProviderSettings) -> list[tuple[int, int]]:
        candidates = [
            (group_index, model_index)
            for group_index, group in enumerate(settings.fallback_groups)
            if group.enabled and group.api_key.strip() and group.base_url.strip()
            for model_index, model in enumerate(group.models)
            if model.enabled and model.model_id.strip()
        ]
        with self._lock:
            preferred = self._preferred
        if preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)
        return candidates

    def _call(self, method: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        with self._lock:
            settings = self._settings.model_copy(deep=True)
        candidates = self._ordered_candidates(settings)
        if not candidates:
            raise ValueError("自動切換已啟用，但沒有可用的 API 組別和模型。")
        failures: list[str] = []
        skipped_groups: set[int] = set()
        cancel_token = kwargs.get("cancel_token")
        progress = getattr(self._local, "progress", None)
        for position, (group_index, model_index) in enumerate(candidates, 1):
            if group_index in skipped_groups:
                continue
            if cancel_token is not None and cancel_token.is_set():
                raise CancelledError("Request cancelled before API call")
            group = settings.fallback_groups[group_index]
            model = group.models[model_index]
            label = f"{group.name or f'API {group_index + 1}'} / {model.name or model.model_id}"
            for secret in [settings.api_key] + [item.api_key for item in settings.fallback_groups]:
                if secret:
                    label = label.replace(secret, "[已隱藏]")
            if progress is not None:
                progress(f"AI 切換：嘗試 {label}（{position}/{len(candidates)}）")
            candidate = settings.model_copy(update={
                "model": model.model_id,
                "base_url": group.base_url,
                "api_key": group.api_key,
                "thinking": group.thinking,
                "reasoning_effort": group.reasoning_effort,
                "fallback_enabled": False,
                "fallback_groups": [],
            })
            client = DeepSeekClient(candidate, logger_=self._log, max_retries=0)
            call_kwargs = dict(kwargs)
            # A failed stream must never leak partial text into the UI or history.
            reasoning_chunks: list[str] = []
            content_chunks: list[str] = []
            if method == "stream_chat":
                call_kwargs["on_reasoning_token"] = reasoning_chunks.append
                call_kwargs["on_content_token"] = content_chunks.append
            call_kwargs["thinking"] = group.thinking
            call_kwargs["reasoning_effort"] = group.reasoning_effort
            try:
                reply = getattr(client, method)(messages, **call_kwargs)
            except CancelledError:
                raise
            except Exception as exc:
                kind = _failure_kind(exc)
                if kind is None:
                    safe = str(exc)
                    secrets = [settings.api_key] + [
                        item.api_key for item in settings.fallback_groups
                    ]
                    for secret in secrets:
                        if secret:
                            safe = safe.replace(secret, "[已隱藏]")
                    raise RuntimeError(f"AI 請求失敗：{safe}") from None
                reason, skip_group = kind
                failures.append(f"{label}：{reason}")
                self._log.warning("AI fallback candidate failed: %s (%s)", label, reason)
                if skip_group:
                    skipped_groups.add(group_index)
                continue
            if cancel_token is not None and cancel_token.is_set():
                raise CancelledError("Request cancelled after API call")
            with self._lock:
                self._preferred = (group_index, model_index)
                self._last_success = {
                    "api_group": group.name or f"API {group_index + 1}",
                    "api_group_index": group_index + 1,
                    "model": model.name or model.model_id,
                    "model_id": model.model_id,
                }
            if method == "stream_chat":
                on_reasoning = kwargs.get("on_reasoning_token")
                on_content = kwargs.get("on_content_token")
                if on_reasoning is not None:
                    for chunk in reasoning_chunks:
                        on_reasoning(chunk)
                if on_content is not None:
                    for chunk in content_chunks:
                        on_content(chunk)
            if progress is not None:
                progress(f"AI 已使用：{label}")
            return reply
        raise AllCandidatesFailed("所有 API 與模型均無法使用：\n" + "\n".join(failures))

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self._call("chat", messages, **kwargs)

    def stream_chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self._call("stream_chat", messages, **kwargs)
