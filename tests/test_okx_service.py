from pathlib import Path

from pa_agent.config.settings import Settings
from pa_agent.trading.okx_client import OKXCredentials
from pa_agent.trading.okx_service import OKXTradingService


class FakeClient:
    def __init__(self):
        self.fill = "0"
        self.order_state = "live"
        self.position = "0"
        self.orders = []
        self.algos = []
        self.canceled = []
        self.amended = []
        self.closed = False

    def close(self): self.closed = True
    def account_config(self): return {"posMode": "net_mode"}
    def public_instrument(self, _inst):
        return {"instId": "BTC-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctVal": "0.01", "lotSz": "1", "minSz": "1", "tickSz": "0.1"}
    def positions(self, _inst): return [] if self.position == "0" else [{"pos": self.position}]
    def set_leverage(self, *_args): return {}
    def usdt_equity(self): return "1000"
    def place_order(self, payload):
        self.orders.append(payload)
        if payload.get("reduceOnly"):
            self.position = "0"
        return {"ordId": str(len(self.orders)), "sCode": "0"}
    def get_order(self, *_args, **_kwargs):
        return {"ordId": "1", "state": self.order_state, "accFillSz": self.fill}
    def amend_order(self, *args): self.amended.append(args); return {}
    def cancel_order(self, _inst, order_id): self.canceled.append(order_id); self.order_state = "canceled"; return {}
    def place_algo_order(self, payload):
        self.algos.append(payload); return {"algoId": f"a{len(self.algos)}", "sCode": "0"}
    def cancel_algos(self, items): self.canceled.extend(x["algoId"] for x in items); return []
    def get_algo_order(self, _id): return {"state": "live"}
    def find_pending_algo(self, _id): return {}


def service(tmp_path: Path, fake: FakeClient):
    settings = Settings()
    cfg = settings.okx
    cfg.enabled = True
    cfg.symbol_mappings = {"BTCUSDT": "BTC-USDT-SWAP"}
    cfg.fixed_notional_usdt = 10
    cfg.max_notional_usdt = 100
    result = OKXTradingService(settings, db_path=tmp_path / "okx.db", client_factory=lambda _c, _p: fake)
    result.credentials = lambda: OKXCredentials("key", "secret", "pass")  # type: ignore[method-assign]
    return result


def decision(**updates):
    value = {
        "order_type": "限价单", "order_direction": "做多", "entry_price": 100,
        "entry_zone_low": 99, "entry_zone_high": 101, "stop_loss_price": 90,
        "take_profit_price": 110, "take_profit_price_2": 120,
        "reasoning": "test", "trade_confidence": 80,
    }
    value.update(updates)
    return value


def submit(svc, bar, dec=None, high=105, low=95):
    return svc.submit_analysis(
        exchange="OKX", symbol="BTCUSDT", timeframe="15m", bar_ts=bar,
        decision=dec or decision(), bar_high=high, bar_low=low,
    )


def test_create_is_persisted_before_entry_and_duplicate_does_not_add(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    assert submit(svc, 100)["action"] == "created"
    assert len(fake.orders) == 1
    submit(svc, 100)
    submit(svc, 100)
    assert len(fake.orders) == 1
    assert len(svc.store.active_all()) == 1


def test_below_confidence_threshold_does_not_create_thesis(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    result = submit(svc, 100, decision(trade_confidence=39))
    assert result["action"] == "observed"
    assert not fake.orders
    assert not svc.store.recent()


def test_partial_fills_are_protected_and_converge_to_equal_tp_split(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    thesis = svc.store.active_all()[0]
    fake.fill = "2"; fake.position = "2"
    svc._reconcile_one(fake, thesis)
    assert [(a["sz"], a["tpTriggerPx"]) for a in fake.algos] == [("2", "110")]
    thesis = svc.store.active_all()[0]
    fake.fill = "10"; fake.position = "10"; fake.order_state = "filled"
    svc._reconcile_one(fake, thesis)
    assert [(a["sz"], a["tpTriggerPx"]) for a in fake.algos] == [
        ("2", "110"), ("3", "110"), ("5", "120")
    ]


def test_pending_thesis_expires_after_three_new_bars(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    assert submit(svc, 200)["action"] == "updated"
    assert submit(svc, 300)["action"] == "updated"
    assert submit(svc, 400)["action"] == "expired"
    assert svc.store.recent(1)[0]["status"] == "INVALIDATED"


def test_opposite_ai_view_does_not_invalidate_active_thesis(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    result = submit(svc, 200, decision(order_direction="做空", entry_price=100, stop_loss_price=110, take_profit_price=90, take_profit_price_2=80))
    assert result["action"] == "updated"
    assert svc.store.active_all()[0]["direction"] == "long"


def test_original_stop_touch_cancels_entry_and_invalidates(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    result = submit(svc, 200, low=89)
    assert result["action"] == "invalidated"
    assert fake.canceled == ["1"]
    assert svc.store.recent(1)[0]["status"] == "INVALIDATED"


def test_pending_entry_update_stays_in_zone_and_cannot_increase_risk(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    submit(svc, 200, decision(proposed_entry_price=101))
    assert fake.amended == []  # farther from the fixed stop
    submit(svc, 300, decision(proposed_entry_price=99))
    assert fake.amended[-1][-1] == "99"


def test_completion_waits_for_seen_position_and_two_zero_confirmations(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    fake.fill = "10"; fake.order_state = "filled"; fake.position = "10"
    thesis = svc.store.active_all()[0]
    svc._reconcile_one(fake, thesis)
    assert svc.store.active_all()[0]["position_seen"] == 1
    fake.position = "0"
    svc._reconcile_one(fake, svc.store.active_all()[0])
    assert svc.store.active_all()
    svc._reconcile_one(fake, svc.store.active_all()[0])
    assert not svc.store.active_all()
    assert svc.store.recent(1)[0]["status"] == "COMPLETED"


def test_completed_thesis_requires_next_closed_bar_before_reentry(tmp_path):
    fake = FakeClient(); svc = service(tmp_path, fake)
    submit(svc, 100)
    thesis = svc.store.active_all()[0]
    svc.store.update(thesis["id"], status="COMPLETED")
    assert submit(svc, 100)["action"] == "wait_next_bar"
    assert submit(svc, 200)["action"] == "created"
