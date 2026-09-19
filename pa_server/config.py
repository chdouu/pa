from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pa_agent.config.settings import (
    FallbackAPIGroup,
    Settings,
    default_fallback_models,
    provider_api_key_configured,
)


GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass(frozen=True)
class ServerConfig:
    root: Path
    data_dir: Path
    settings_path: Path
    api_token: str
    bind_host: str
    port: int
    poll_seconds: int
    timezone: str
    okx_demo_api_key: str = ""
    okx_demo_secret_key: str = ""
    okx_demo_passphrase: str = ""
    okx_live_api_key: str = ""
    okx_live_secret_key: str = ""
    okx_live_passphrase: str = ""

    @classmethod
    def from_env(cls) -> "ServerConfig":
        root = Path(os.getenv("PA_ROOT", Path(__file__).resolve().parents[1]))
        data_dir = Path(os.getenv("PA_DATA_DIR", root / "data"))
        return cls(
            root=root,
            data_dir=data_dir,
            settings_path=Path(os.getenv("PA_SETTINGS_PATH", root / "config" / "settings.json")),
            api_token=os.getenv("PA_API_TOKEN", "").strip(),
            bind_host=os.getenv("PA_BIND_HOST", "127.0.0.1"),
            port=int(os.getenv("PA_PORT", "8765")),
            poll_seconds=max(5, int(os.getenv("PA_POLL_SECONDS", "30"))),
            timezone=os.getenv("PA_TIMEZONE", "Asia/Taipei"),
            okx_demo_api_key=os.getenv("PA_OKX_DEMO_API_KEY", "").strip(),
            okx_demo_secret_key=os.getenv("PA_OKX_DEMO_SECRET_KEY", "").strip(),
            okx_demo_passphrase=os.getenv("PA_OKX_DEMO_PASSPHRASE", "").strip(),
            okx_live_api_key=os.getenv("PA_OKX_LIVE_API_KEY", "").strip(),
            okx_live_secret_key=os.getenv("PA_OKX_LIVE_SECRET_KEY", "").strip(),
            okx_live_passphrase=os.getenv("PA_OKX_LIVE_PASSPHRASE", "").strip(),
        )


def load_runtime_settings(cfg: ServerConfig) -> Settings:
    if not cfg.settings_path.is_file():
        raise RuntimeError(f"Settings file is missing: {cfg.settings_path}")
    settings = Settings.model_validate_json(cfg.settings_path.read_text(encoding="utf-8"))
    provider_key = os.getenv("PA_AI_API_KEY", "").strip()
    if provider_key:
        settings.provider.api_key = provider_key
    gemini_keys = [
        value.strip()
        for value in os.getenv("PA_GEMINI_API_KEYS", "").split(",")
        if value.strip()
    ]
    if gemini_keys:
        settings.provider.fallback_enabled = True
        settings.provider.fallback_groups = [
            FallbackAPIGroup(
                name=f"Gemini API {index}",
                base_url=GEMINI_OPENAI_BASE_URL,
                api_key=api_key,
                enabled=True,
                thinking=True,
                reasoning_effort="high",
                models=default_fallback_models(),
            )
            for index, api_key in enumerate(gemini_keys, 1)
        ]
    fallback_keys_raw = os.getenv("PA_FALLBACK_API_KEYS", "").strip()
    if fallback_keys_raw and not gemini_keys:
        fallback_keys = json.loads(fallback_keys_raw)
        for group in settings.provider.fallback_groups:
            if group.name in fallback_keys:
                group.api_key = str(fallback_keys[group.name])
    if provider_key and not gemini_keys and not any(
        group.enabled and group.api_key.strip()
        for group in settings.provider.fallback_groups
    ):
        settings.provider.fallback_enabled = False
    webhook = os.getenv("PA_FEISHU_WEBHOOK_URL", "").strip()
    if webhook:
        settings.feishu.webhook_url = webhook
    for env_name, field in (("PA_FEISHU_SECRET", "secret"),):
        value = os.getenv(env_name, "").strip()
        if value:
            setattr(settings.feishu, field, value)
    if not provider_api_key_configured(settings):
        raise RuntimeError(
            "No AI API key configured (set PA_GEMINI_API_KEYS or legacy PA_AI_API_KEY/PA_FALLBACK_API_KEYS)"
        )
    model = settings.provider.model.lower()
    if model.startswith(("openclaw_", "cursor", "trae", "qoder")):
        raise RuntimeError("Desktop-integrated AI providers are not supported by PA Server")
    return settings


def settings_version(settings: Settings) -> str:
    data = settings.model_dump()
    provider = data.get("provider", {})
    provider.pop("api_key", None)
    provider.pop("api_key_encrypted", None)
    for group in provider.get("fallback_groups", []):
        group.pop("api_key", None)
    feishu = data.get("feishu", {})
    for name in ("webhook_url", "secret"):
        feishu.pop(name, None)
    okx = data.get("okx", {})
    okx.pop("demo_credentials", None)
    okx.pop("live_credentials", None)
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
