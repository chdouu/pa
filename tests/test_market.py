from pa_agent.data.base import KlineBar
from pa_server import market


def bar(ts: int, closed: bool = True) -> KlineBar:
    return KlineBar(seq=1, ts_open=ts, open=1, high=2, low=1, close=2, volume=1, closed=closed)


def test_discovery_only_enqueues_latest_for_new_watch(monkeypatch):
    bars = [bar(ts) for ts in range(200_000, 0, -1_000)]
    monkeypatch.setattr(market, "fetch_bars", lambda request, count: bars[:count])
    watch = {"id": "w", "exchange": "OKX", "symbol": "ETHUSDT", "timeframe": "15m", "bar_count": 20, "extended_session": False, "last_seen_ts": None}
    snapshots = market.discover_snapshots(watch)
    assert [item[0] for item in snapshots] == [200_000]
    assert snapshots[0][1][0]["ts_open"] == 200_000


def test_backlog_is_oldest_first_and_fixed_to_target(monkeypatch):
    bars = [bar(ts) for ts in range(300_000, 0, -1_000)]
    monkeypatch.setattr(market, "fetch_bars", lambda request, count: bars[:count])
    watch = {"id": "w", "exchange": "OKX", "symbol": "ETHUSDT", "timeframe": "15m", "bar_count": 20, "extended_session": False, "last_seen_ts": 295_000}
    snapshots = market.discover_snapshots(watch)
    assert [item[0] for item in snapshots] == [296_000, 297_000, 298_000, 299_000, 300_000]
    assert all(target == snapshot[0]["ts_open"] for target, snapshot in snapshots)
