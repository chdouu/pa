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


def test_schema_migrates_current_tp_and_journals_amendment(tmp_path):
    store = ThesisStore(tmp_path / "okx.db")
    thesis = store.create(values())
    loaded = store.recent(1)[0]
    assert loaded["current_tp1"] == "110"
    assert loaded["current_tp2"] == "120"
    assert loaded["watch_id"] == ""
    first = store.queue_tp_amendment(
        thesis["id"], 2, "lower_tp1", "108", "req1", "barbwire"
    )
    second = store.queue_tp_amendment(
        thesis["id"], 2, "lower_tp1", "107", "req2", "duplicate"
    )
    assert first["id"] == second["id"]
    assert second["target_price"] == "108"


def test_legacy_database_is_upgraded_and_existing_targets_are_backfilled(tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE theses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            profile TEXT NOT NULL, inst_id TEXT NOT NULL, timeframe TEXT NOT NULL,
            status TEXT NOT NULL, direction TEXT NOT NULL, order_type TEXT NOT NULL,
            source_bar_ts INTEGER NOT NULL, last_bar_ts INTEGER NOT NULL,
            elapsed_bars INTEGER NOT NULL DEFAULT 0, entry_zone_low TEXT NOT NULL,
            entry_zone_high TEXT NOT NULL, original_entry TEXT NOT NULL,
            current_entry TEXT NOT NULL, original_stop TEXT NOT NULL,
            current_stop TEXT NOT NULL, tp1 TEXT NOT NULL, tp2 TEXT NOT NULL,
            total_size TEXT NOT NULL, tp1_size TEXT NOT NULL, tp2_size TEXT NOT NULL,
            filled_size TEXT NOT NULL DEFAULT '0', protected_size TEXT NOT NULL DEFAULT '0',
            position_seen INTEGER NOT NULL DEFAULT 0,
            zero_position_checks INTEGER NOT NULL DEFAULT 0,
            entry_order_id TEXT NOT NULL DEFAULT '', entry_algo_id TEXT NOT NULL DEFAULT '',
            cl_ord_id TEXT NOT NULL UNIQUE, reason TEXT NOT NULL DEFAULT '',
            confidence INTEGER, last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE exchange_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, thesis_id INTEGER NOT NULL,
            role TEXT NOT NULL, ord_id TEXT NOT NULL DEFAULT '',
            algo_id TEXT NOT NULL DEFAULT '', client_id TEXT NOT NULL DEFAULT '',
            size TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    legacy = values()
    now = "2026-01-01T00:00:00+00:00"
    columns = ",".join((*legacy.keys(), "created_at", "updated_at"))
    params = ",".join("?" for _ in range(len(legacy) + 2))
    db.execute(
        f"INSERT INTO theses ({columns}) VALUES ({params})",
        (*legacy.values(), now, now),
    )
    db.commit()
    db.close()

    store = ThesisStore(path)
    loaded = store.recent(1)[0]
    assert loaded["current_tp1"] == "110"
    assert loaded["current_tp2"] == "120"
    order_columns = {
        row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(exchange_orders)")
    }
    assert {"protection_leg", "trigger_price", "stop_price"} <= order_columns
    thesis_columns = {
        row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(theses)")
    }
    assert "watch_id" in thesis_columns
