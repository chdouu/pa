from pa_agent.config.settings import Settings
from pa_server.analysis import scrub_secrets


def test_all_configured_secrets_are_scrubbed_recursively():
    settings = Settings()
    settings.provider.api_key = "primary-secret"
    settings.feishu.webhook_url = "https://example.test/hook/private-token"
    settings.feishu.secret = "signing-secret"
    value = {
        "response": ["primary-secret", "failed at https://example.test/hook/private-token"],
        "nested": {"value": "signing-secret"},
    }
    cleaned = scrub_secrets(value, settings)
    assert "primary-secret" not in str(cleaned)
    assert "private-token" not in str(cleaned)
    assert "signing-secret" not in str(cleaned)
