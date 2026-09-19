from types import SimpleNamespace

import pytest

from pa_agent.ai import fallback_client
from pa_agent.ai.fallback_client import AllCandidatesFailed, FallbackClient, _failure_kind
from pa_agent.config.settings import AIProviderSettings, FallbackAPIGroup, default_fallback_models


class ProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _settings() -> AIProviderSettings:
    return AIProviderSettings(
        fallback_enabled=True,
        fallback_groups=[
            FallbackAPIGroup(name="Gemini API 1", api_key="secret-key-one"),
            FallbackAPIGroup(name="Gemini API 2", api_key="secret-key-two"),
        ],
    )


def test_all_nine_models_are_tried_before_next_key_and_success_is_preferred(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, settings, **kwargs):
            self.settings = settings

        def chat(self, messages, **kwargs):
            calls.append((self.settings.api_key, self.settings.model))
            if self.settings.api_key == "secret-key-one":
                raise ProviderError("rate limited", 429)
            return SimpleNamespace(content="ok")

    monkeypatch.setattr(fallback_client, "DeepSeekClient", FakeClient)
    client = FallbackClient(_settings())

    client.chat([{"role": "user", "content": "first"}])
    expected_models = [model.model_id for model in default_fallback_models()]
    assert calls == [
        *[("secret-key-one", model) for model in expected_models],
        ("secret-key-two", expected_models[0]),
    ]
    assert client.get_last_success() == {
        "api_group": "Gemini API 2",
        "api_group_index": 2,
        "model": "Gemini 2.5 Flash",
        "model_id": "gemini-2.5-flash",
    }

    calls.clear()
    client.chat([{"role": "user", "content": "second"}])
    assert calls == [("secret-key-two", "gemini-2.5-flash")]


def test_invalid_key_skips_rest_of_group(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, settings, **kwargs):
            self.settings = settings

        def chat(self, messages, **kwargs):
            calls.append((self.settings.api_key, self.settings.model))
            if self.settings.api_key == "secret-key-one":
                raise ProviderError("api_key_invalid", 401)
            return SimpleNamespace(content="ok")

    monkeypatch.setattr(fallback_client, "DeepSeekClient", FakeClient)
    FallbackClient(_settings()).chat([{"role": "user", "content": "hello"}])
    assert calls == [
        ("secret-key-one", "gemini-2.5-flash"),
        ("secret-key-two", "gemini-2.5-flash"),
    ]


def test_failed_stream_does_not_emit_partial_candidate_output(monkeypatch):
    class FakeClient:
        def __init__(self, settings, **kwargs):
            self.settings = settings

        def stream_chat(self, messages, **kwargs):
            if self.settings.api_key == "secret-key-one":
                kwargs["on_content_token"]("discard-me")
                raise ConnectionError("stream disconnected")
            kwargs["on_reasoning_token"]("reasoning")
            kwargs["on_content_token"]("final")
            return SimpleNamespace(content="final")

    monkeypatch.setattr(fallback_client, "DeepSeekClient", FakeClient)
    settings = _settings()
    settings.fallback_groups[0].models = settings.fallback_groups[0].models[:1]
    settings.fallback_groups[1].models = settings.fallback_groups[1].models[:1]
    reasoning, content = [], []

    FallbackClient(settings).stream_chat(
        [{"role": "user", "content": "hello"}],
        on_reasoning_token=reasoning.append,
        on_content_token=content.append,
    )
    assert reasoning == ["reasoning"]
    assert content == ["final"]


def test_all_failures_are_aggregated_without_keys(monkeypatch):
    class FakeClient:
        def __init__(self, settings, **kwargs):
            self.settings = settings

        def chat(self, messages, **kwargs):
            raise ProviderError(f"quota exceeded for {self.settings.api_key}", 429)

    monkeypatch.setattr(fallback_client, "DeepSeekClient", FakeClient)
    settings = _settings()
    for group in settings.fallback_groups:
        group.models = group.models[:1]

    with pytest.raises(AllCandidatesFailed) as raised:
        FallbackClient(settings).chat([{"role": "user", "content": "hello"}])
    message = str(raised.value)
    assert "Gemini API 1 / Gemini 2.5 Flash" in message
    assert "Gemini API 2 / Gemini 2.5 Flash" in message
    assert "secret-key-one" not in message
    assert "secret-key-two" not in message


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderError("rate limit", 429), "請求受限或額度用盡"),
        (ProviderError("out of credits", 402), "帳戶額度不足"),
        (ProviderError("model not found", 404), "模型不可用"),
        (ProviderError("server error", 503), "HTTP 503"),
        (TimeoutError("timeout"), "連線逾時或中斷"),
    ],
)
def test_recoverable_failure_classification(error, expected):
    assert _failure_kind(error)[0] == expected
