from types import SimpleNamespace

from pa_agent.config.settings import Settings
from pa_agent.notify import feishu_notifier


def test_webhook_and_secret_send_signed_text_card(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, payload=json, headers=headers, timeout=timeout)
        return SimpleNamespace(json=lambda: {"code": 0})

    monkeypatch.setattr("requests.post", fake_post)
    settings = Settings()
    settings.feishu.webhook_url = "https://example.test/feishu-hook"
    settings.feishu.secret = "signing-secret"

    ok = feishu_notifier.send_order_signal(
        decision_inner={
            "order_type": "市价单",
            "order_direction": "做多",
            "reasoning": "test",
        },
        stage2_full={},
        symbol="ETHUSDT",
        timeframe="15m",
        exchange="OKX",
        bar_time_ms=1_700_000_000_000,
        analysis_id="analysis-1",
        settings=settings,
    )

    assert ok is True
    assert captured["url"] == settings.feishu.webhook_url
    assert captured["payload"]["timestamp"]
    assert captured["payload"]["sign"]
    assert captured["payload"]["msg_type"] == "interactive"
    payload_text = str(captured["payload"])
    assert "OKX" in payload_text
    assert "analysis-1" in payload_text
    assert "img_key" not in payload_text
