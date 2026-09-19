import base64
import hashlib
import hmac
import json

import httpx
import pytest

from pa_agent.trading.okx_client import OKXClient, OKXCredentials, OKXError


def test_demo_header_and_signature_cover_exact_request_path_and_body(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"code": "0", "data": [{"ordId": "1", "sCode": "0"}]})

    monkeypatch.setattr(OKXClient, "_timestamp", staticmethod(lambda: "2026-01-01T00:00:00.000Z"))
    client = OKXClient(
        OKXCredentials("key", "secret", "pass"), profile="demo",
        transport=httpx.MockTransport(handler),
    )
    payload = {"instId": "BTC-USDT-SWAP", "side": "buy"}
    client.place_order(payload)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    expected = base64.b64encode(hmac.new(
        b"secret", f"2026-01-01T00:00:00.000ZPOST/api/v5/trade/order{body}".encode(), hashlib.sha256
    ).digest()).decode()
    assert captured["headers"]["x-simulated-trading"] == "1"
    assert captured["headers"]["ok-access-sign"] == expected
    assert captured["body"] == body
    client.close()


def test_401_json_preserves_okx_code_and_adds_environment_hint():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "50119", "msg": "API key doesn't exist"})

    client = OKXClient(
        OKXCredentials("key", "secret", "pass"), profile="demo",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OKXError) as caught:
        client.account_config()
    assert caught.value.code == "50119"
    assert "模拟盘／实盘" in str(caught.value)
    assert "profile: demo" in str(caught.value)
    client.close()


def test_plain_401_has_actionable_authentication_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    client = OKXClient(
        OKXCredentials("key", "secret", "pass"), profile="live",
        base_url="https://eea.okx.com", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OKXError, match="Passphrase"):
        client.account_config()
    client.close()


def test_client_uses_selected_regional_host():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        return httpx.Response(200, json={"code": "0", "data": [{}]})

    client = OKXClient(
        OKXCredentials("key", "secret", "pass"), profile="live",
        base_url="https://us.okx.com", transport=httpx.MockTransport(handler),
    )
    client.account_config()
    assert captured["host"] == "us.okx.com"
    client.close()


def test_amend_algo_order_sends_tp_only_and_preserves_original_on_failure_policy():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "code": "0", "data": [{"algoId": "a1", "reqId": "r1", "sCode": "0"}]
        })

    client = OKXClient(
        OKXCredentials("key", "secret", "pass"), profile="demo",
        transport=httpx.MockTransport(handler),
    )
    client.amend_algo_order("BTC-USDT-SWAP", "a1", tp_trigger_px="105", req_id="r1")
    assert captured["path"] == "/api/v5/trade/amend-algos"
    assert captured["payload"] == {
        "instId": "BTC-USDT-SWAP", "algoId": "a1",
        "newTpTriggerPx": "105", "newTpOrdPx": "-1",
        "newTpTriggerPxType": "mark", "cxlOnFail": False, "reqId": "r1",
    }
    client.close()
