import sqlite3

import pytest

from pa_agent.trading.thesis_store import ThesisStore


def values(account="a", profile="demo", inst="BTC-USDT-SWAP", bar=1):
    return {
        "account_id": account, "profile": profile, "inst_id": inst, "timeframe": "15m",
        "status": "PENDING_SUBMIT", "direction": "long", "order_type": "限价单",
        "source_bar_ts": bar, "last_bar_ts": bar, "entry_zone_low": "99",
        "entry_zone_high": "101", "original_entry": "100", "current_entry": "100",
        "original_stop": "90", "current_stop": "90", "tp1": "110", "tp2": "120",
        "total_size": "10", "tp1_size": "5", "tp2_size": "5",
        "cl_ord_id": f"pa{account}{profile}{bar}", "reason": "test", "confidence": 80,
    }


def test_only_one_active_thesis_per_account_profile_instrument(tmp_path):
    store = ThesisStore(tmp_path / "okx.db")
    first = store.create(values())
    with pytest.raises(sqlite3.IntegrityError):
        store.create(values(bar=2))
    store.update(first["id"], status="COMPLETED")
    second = store.create(values(bar=2))
    assert second["id"] != first["id"]


def test_accounts_and_profiles_are_isolated(tmp_path):
    store = ThesisStore(tmp_path / "okx.db")
    store.create(values(account="a", profile="demo", bar=1))
    store.create(values(account="b", profile="demo", bar=2))
    store.create(values(account="a", profile="live", bar=3))
    assert len(store.active_all()) == 3


def test_revision_is_idempotent_for_same_bar_action(tmp_path):
    store = ThesisStore(tmp_path / "okx.db")
    thesis = store.create(values())
    assert store.revision(thesis["id"], 10, "15m", {"order_type": "不下单"}, "UPDATE")
    assert not store.revision(thesis["id"], 10, "15m", {"order_type": "不下单"}, "UPDATE")
