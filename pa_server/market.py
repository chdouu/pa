from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Iterable

from pa_agent.data.bar_close_wait import has_forming_bar_at_head
from pa_agent.data.base import KlineBar
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS
from pa_agent.data.tradingview import TradingViewSource


def fetch_bars(request: dict, count: int) -> list[KlineBar]:
    source = TradingViewSource("", "", extended_session=bool(request.get("extended_session", False)))
    try:
        source.set_exchange(request["exchange"])
        source.connect()
        source.subscribe(request["symbol"], request["timeframe"])
        return source.latest_snapshot(count)
    finally:
        try:
            source.disconnect()
        except Exception:
            pass


def closed_bars(bars: list[KlineBar], request: dict) -> list[KlineBar]:
    if not bars:
        return []
    skip = 1 if has_forming_bar_at_head(
        bars, request["timeframe"], symbol=request["symbol"]
    ) else 0
    if request["timeframe"] == "1M":
        opened = datetime.fromtimestamp(float(bars[0].ts_open) / 1000).astimezone()
        if opened.month == 12:
            next_month = opened.replace(year=opened.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            next_month = opened.replace(month=opened.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        skip = 1 if datetime.now().astimezone() < next_month else 0
    return bars[skip:]


def serialize_snapshot(bars: Iterable[KlineBar]) -> list[dict]:
    result = []
    for seq, bar in enumerate(bars, 1):
        item = dataclasses.asdict(bar)
        item["seq"] = seq
        item["closed"] = True
        result.append(item)
    return result


def discover_snapshots(watch: dict) -> list[tuple[int, list[dict]]]:
    request = watch_request(watch)
    fetch_count = min(5000, request["bar_count"] + INDICATOR_WARMUP_BARS + 1001)
    closed = closed_bars(fetch_bars(request, fetch_count), request)
    minimum = request["bar_count"] + INDICATOR_WARMUP_BARS
    if len(closed) < minimum:
        raise RuntimeError("TradingView did not return enough closed bars")
    last_seen = watch.get("last_seen_ts")
    if last_seen is None:
        targets = [0]
    else:
        timestamps = [int(bar.ts_open) for bar in closed]
        if int(last_seen) not in timestamps and int(last_seen) < timestamps[-1]:
            raise RuntimeError("Historical bar gap exceeds the TradingView retrieval window")
        targets = [i for i, bar in enumerate(closed) if int(bar.ts_open) > int(last_seen)]
        targets.reverse()
    snapshots: list[tuple[int, list[dict]]] = []
    for index in targets:
        selected = closed[index : index + minimum]
        if len(selected) < minimum:
            raise RuntimeError("Historical snapshot lacks indicator warm-up bars")
        snapshots.append((int(closed[index].ts_open), serialize_snapshot(selected)))
    return snapshots


def latest_closed_ts(request: dict) -> int:
    bars = closed_bars(fetch_bars(request, 3), request)
    if not bars:
        raise RuntimeError("TradingView returned no closed bars")
    return int(bars[0].ts_open)


def watch_request(watch: dict) -> dict:
    return {
        "source": "tradingview",
        "exchange": watch["exchange"],
        "symbol": watch["symbol"],
        "timeframe": watch["timeframe"],
        "bar_count": watch["bar_count"],
        "extended_session": bool(watch.get("extended_session", False)),
    }
