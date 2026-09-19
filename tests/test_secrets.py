from pa_agent.config.settings import Settings
from pa_server.analysis import scrub_secrets


def test_all_configured_secrets_are_scrubbed_recursively():
    settings = Settings()
    settings.provider.api_key = "primary-secret"
    settings.feishu.webhook_url = "https://example.test/hook/private-token"
    settings.feishu.app_secret = "app-secret"
    value = {
        "response": ["primary-secret", "failed at https://example.test/hook/private-token"],
        "nested": {"value": "app-secret"},
    }
    cleaned = scrub_secrets(value, settings)
    assert "primary-secret" not in str(cleaned)
    assert "private-token" not in str(cleaned)
    assert "app-secret" not in str(cleaned)
