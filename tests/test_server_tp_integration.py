from pathlib import Path
from types import SimpleNamespace

from pa_agent.config.settings import Settings
from pa_server.config import ServerConfig
from pa_server.service import Runtime
from pa_server.store import Store
from pa_server.trading import ServerTradingController


REQUEST = {
    "source": "tradingview", "exchange": "OKX", "symbol": "BTCUSDT",
    "timeframe": "15m", "bar_count": 100, "extended_session": False,
    "trading_enabled": False, "okx_instrument": "BTC-USDT-SWAP",
}


class FakeTrading:
    def __init__(self):
        self.calls = []

    def submit_job(self, job, watch):
        self.calls.append((job["id"], watch["id"]))
        return {"action": "updated", "thesis_id": 7}


def _snapshot():
    return [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1,
             "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]


def test_disabled_switches_still_manage_captured_active_thesis(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3")
    watch, _ = store.create_watch(REQUEST, "v1", None)
    store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, _snapshot())], "v1")
    job = store.claim_job()
    job = store.capture_job_active_thesis(job["id"], {
        "id": 7, "watch_id": watch["id"], "timeframe": "15m",
    })
    store.finish_job(job["id"], {
        "stage2_decision": {"decision": {"order_type": "不下单"}},
        "trading_status": "pending",
    }, {"ok": True}, False, True)
    task = store.claim_trade_task()

    runtime = object.__new__(Runtime)
    runtime.store = store
    runtime.settings = Settings()
    runtime.settings.okx.enabled = False
    runtime.version = "v1"
    runtime.trading = FakeTrading()
    runtime._safe_error = lambda exc: str(exc)
    monkeypatch.setattr("pa_server.service.latest_closed_ts", lambda _request: 1000)

    runtime._run_trade_task(task)
    assert runtime.trading.calls == [(job["id"], watch["id"])]
    assert store.get_job(job["id"])["result"]["trading_status"] == "succeeded"


def test_historical_job_captures_no_current_thesis(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3")
    watch, _ = store.create_watch(REQUEST, "v1", None)
    store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, _snapshot())], "v1")
    job = store.claim_job()

    runtime = object.__new__(Runtime)
    runtime.store = store
    runtime.trading = SimpleNamespace(
        active_thesis_context=lambda _watch: (_ for _ in ()).throw(
            AssertionError("historical jobs must not read the current thesis")
        )
    )
    monkeypatch.setattr("pa_server.service.latest_closed_ts", lambda _request: 2000)

    captured = runtime._capture_active_thesis(job)
    assert captured["active_thesis_captured"] is True
    assert captured["active_thesis"] == {}


def _controller(tmp_path: Path, store: Store) -> ServerTradingController:
    config = ServerConfig(
        root=tmp_path, data_dir=tmp_path, settings_path=tmp_path / "settings.json",
        api_token="token", bind_host="127.0.0.1", port=8765,
        poll_seconds=30, timezone="Asia/Taipei",
    )
    return ServerTradingController(config, Settings(), store)


def _thesis_values(controller: ServerTradingController, bar: int = 1):
    return {
        "account_id": controller.service.account_id(), "profile": "demo",
        "inst_id": "BTC-USDT-SWAP", "watch_id": "", "timeframe": "15m",
        "status": "OPEN", "direction": "long", "order_type": "限价单",
        "source_bar_ts": bar, "last_bar_ts": bar, "entry_zone_low": "99",
        "entry_zone_high": "101", "original_entry": "100", "current_entry": "100",
        "original_stop": "90", "current_stop": "90", "tp1": "110", "tp2": "120",
        "total_size": "10", "tp1_size": "5", "tp2_size": "5",
        "filled_size": "10", "protected_size": "10", "cl_ord_id": f"pa{bar}",
        "reason": "test", "confidence": 80,
    }


def test_legacy_thesis_is_bound_only_to_unique_matching_watch(tmp_path: Path):
    store = Store(tmp_path / "server.db")
    watch, _ = store.create_watch(REQUEST, "v1", None)
    controller = _controller(tmp_path, store)
    thesis = controller.service.store.create(_thesis_values(controller))

    context = controller.active_thesis_context(watch)
    assert context["id"] == thesis["id"]
    assert context["watch_id"] == watch["id"]
    assert controller.service.store.recent(1)[0]["watch_id"] == watch["id"]


def test_legacy_thesis_is_not_bound_when_watch_mapping_is_ambiguous(tmp_path: Path):
    store = Store(tmp_path / "server.db")
    first, _ = store.create_watch(REQUEST, "v1", None)
    store.create_watch({**REQUEST, "symbol": "BTCUSDT.P"}, "v1", None)
    controller = _controller(tmp_path, store)
    controller.service.store.create(_thesis_values(controller))

    assert controller.active_thesis_context(first) is None
    assert controller.service.store.recent(1)[0]["watch_id"] == ""
