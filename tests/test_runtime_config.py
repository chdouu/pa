import json
from pathlib import Path

from pa_agent.config.settings import GEMINI_FALLBACK_MODELS
from pa_server.config import GEMINI_OPENAI_BASE_URL, ServerConfig, load_runtime_settings


def _config(tmp_path: Path) -> ServerConfig:
    settings_path = tmp_path / "settings.json"
    source = Path(__file__).resolve().parents[1] / "settings.example.json"
    settings_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return ServerConfig(
        root=tmp_path,
        data_dir=tmp_path / "data",
        settings_path=settings_path,
        api_token="token",
        bind_host="127.0.0.1",
        port=8765,
        poll_seconds=30,
        timezone="Asia/Taipei",
    )


def test_gemini_keys_build_ordered_groups_with_all_models(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_GEMINI_API_KEYS", " key-one, key-two ,,key-three ")
    monkeypatch.delenv("PA_AI_API_KEY", raising=False)
    monkeypatch.delenv("PA_FALLBACK_API_KEYS", raising=False)

    settings = load_runtime_settings(_config(tmp_path))

    assert settings.provider.fallback_enabled is True
    assert [group.name for group in settings.provider.fallback_groups] == [
        "Gemini API 1",
        "Gemini API 2",
        "Gemini API 3",
    ]
    assert [group.api_key for group in settings.provider.fallback_groups] == [
        "key-one",
        "key-two",
        "key-three",
    ]
    expected_models = [model_id for _, model_id in GEMINI_FALLBACK_MODELS]
    for group in settings.provider.fallback_groups:
        assert group.base_url == GEMINI_OPENAI_BASE_URL
        assert group.thinking is True
        assert group.reasoning_effort == "high"
        assert [model.model_id for model in group.models] == expected_models


def test_legacy_fallback_key_mapping_remains_supported(tmp_path, monkeypatch):
    monkeypatch.delenv("PA_GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("PA_AI_API_KEY", raising=False)
    monkeypatch.setenv(
        "PA_FALLBACK_API_KEYS",
        json.dumps({"Gemini API 1": "legacy-key"}),
    )

    settings = load_runtime_settings(_config(tmp_path))

    assert settings.provider.fallback_groups[0].api_key == "legacy-key"


def test_legacy_primary_key_remains_supported(tmp_path, monkeypatch):
    monkeypatch.delenv("PA_GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("PA_FALLBACK_API_KEYS", raising=False)
    monkeypatch.setenv("PA_AI_API_KEY", "legacy-primary-key")

    settings = load_runtime_settings(_config(tmp_path))

    assert settings.provider.fallback_enabled is False
    assert settings.provider.api_key == "legacy-primary-key"
