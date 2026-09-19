"""Active Thesis state machine and OKX execution coordinator."""
from __future__ import annotations

import hashlib
import logging
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from pa_agent.config.paths import OKX_TRADING_DB_PATH
from pa_agent.config.settings import OKXSettings, Settings
from pa_agent.trading.okx_client import OKX_API_BASE_URLS, OKXClient, OKXCredentials, OKXError
from pa_agent.trading.secrets import unprotect_secret
from pa_agent.trading.sizing import D, InstrumentSpec, SizingError, calculate_contracts, decimal_text
from pa_agent.trading.thesis_store import ThesisStore

logger = logging.getLogger(__name__)
TERMINAL_ORDER_STATES = {"filled", "canceled", "mmp_canceled"}


def _price(value: Any) -> Decimal:
    result = D(value)
    if not result.is_finite() or result <= 0:
        raise ValueError("价格必须是正数")
    return result


def _same_price(left: Any, right: Any) -> bool:
    try:
        return D(left) == D(right)
    except Exception:  # noqa: BLE001
        return False


def _direction(value: Any) -> str:
    text = str(value or "").lower()
    if any(word in text for word in ("多", "long", "buy", "bull")):
        return "long"
    if any(word in text for word in ("空", "short", "sell", "bear")):
        return "short"
    raise ValueError("无法识别交易方向")


def _client_id(profile: str, inst_id: str, bar_ts: int) -> str:
    digest = hashlib.sha256(f"{profile}:{inst_id}:{bar_ts}".encode()).hexdigest()[:20]
    return f"pa{digest}"[:32]


def _amendment_id(profile: str, inst_id: str, bar_ts: int, action: str) -> str:
    digest = hashlib.sha256(
        f"{profile}:{inst_id}:{bar_ts}:{action}".encode()
    ).hexdigest()[:20]
    return f"pt{digest}"[:32]


class OKXTradingService:
    """Thread-safe coordinator. One instance may be shared by the GUI."""

    def __init__(
        self, settings: Settings, *, db_path: Path = OKX_TRADING_DB_PATH,
        client_factory: Callable[[OKXCredentials, str], OKXClient] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = ThesisStore(db_path)
        self._client_factory = client_factory or (
            lambda credentials, profile: OKXClient(
                credentials,
                profile=profile,
                base_url=OKX_API_BASE_URLS[self.cfg.api_region],
            )
        )
        self._event_callback = event_callback
        self._lock = threading.RLock()

    @property
    def cfg(self) -> OKXSettings:
        return self.settings.okx

    def _emit(self, event: str, **data: Any) -> None:
        logger.info("OKX %s %s", event, {k: v for k, v in data.items() if "secret" not in k and "key" not in k})
        if self._event_callback:
            try:
                self._event_callback(event, data)
            except Exception:  # noqa: BLE001
                logger.warning("OKX event callback failed", exc_info=True)

    def credentials(self) -> OKXCredentials:
        encrypted = self.cfg.demo_credentials if self.cfg.profile == "demo" else self.cfg.live_credentials
        return OKXCredentials(
            unprotect_secret(encrypted.api_key_encrypted),
            unprotect_secret(encrypted.secret_key_encrypted),
            unprotect_secret(encrypted.passphrase_encrypted),
        )

    def client(self) -> OKXClient:
        credentials = self.credentials()
        if not credentials.complete():
            raise OKXError("当前环境的 OKX API 凭证不完整")
        return self._client_factory(credentials, self.cfg.profile)

    def account_id(self) -> str:
        """Stable, non-secret local account key derived from the API key."""
        return hashlib.sha256(self.credentials().api_key.encode()).hexdigest()[:16]

    def validate_configuration(self, client: OKXClient | None = None) -> dict[str, Any]:
        if self.cfg.sizing_mode == "fixed_notional" and self.cfg.fixed_notional_usdt <= 0:
            raise ValueError("固定名目金额必须大于 0")
        if self.cfg.sizing_mode == "fixed_margin" and self.cfg.fixed_margin_usdt <= 0:
            raise ValueError("固定保证金必须大于 0")
        if self.cfg.sizing_mode == "risk_percent" and self.cfg.risk_percent <= 0:
            raise ValueError("风险比例必须大于 0")
        if self.cfg.max_notional_usdt <= 0:
            raise ValueError("每笔名目金额上限必须大于 0")
        own = client or self.client()
        close = client is None
        try:
            account = own.account_config()
            if str(account.get("posMode") or "") != "net_mode":
                raise ValueError("OKX 帐户必须切换为单向持仓模式（net_mode）")
            if not self.cfg.symbol_mappings:
                raise ValueError("至少需要一条 TradingView → OKX 商品映射")
            for symbol, inst_id in self.cfg.symbol_mappings.items():
                if not str(inst_id).upper().endswith("-USDT-SWAP"):
                    raise ValueError(f"{symbol} 的映射不是 USDT 永续合约")
                instrument = own.public_instrument(str(inst_id).upper())
                if instrument.get("state") != "live" or instrument.get("settleCcy") != "USDT":
                    raise ValueError(f"{symbol} 映射的合约目前不可交易")
            return account
        finally:
            if close:
                own.close()

    def mapped_instrument(self, symbol: str) -> str:
        mapping = {str(k).upper(): str(v).upper() for k, v in self.cfg.symbol_mappings.items()}
        inst_id = mapping.get(symbol.strip().upper(), "")
        if not inst_id.endswith("-USDT-SWAP"):
            raise ValueError(f"{symbol} 未映射到 OKX USDT 永续合约")
        return inst_id

    def active_thesis_context(self, exchange: str, symbol: str) -> dict[str, Any] | None:
        """Return a prompt-safe snapshot of the exchange-backed active thesis."""
        if exchange.strip().upper() != "OKX":
            return None
        try:
            inst_id = self.mapped_instrument(symbol)
            thesis = self.store.active(self.cfg.profile, inst_id, self.account_id())
        except Exception:  # noqa: BLE001
            return None
        if not thesis:
            return None
        keys = (
            "id", "profile", "inst_id", "watch_id", "timeframe", "status", "direction",
            "original_entry", "current_entry", "original_stop", "current_stop",
            "tp1", "tp2", "current_tp1", "current_tp2", "total_size",
            "filled_size", "protected_size", "reason", "confidence",
        )
        return {key: thesis.get(key) for key in keys}

    def submit_analysis(
        self, *, exchange: str, symbol: str, timeframe: str, bar_ts: int,
        decision: dict[str, Any], stage2_full: dict[str, Any] | None = None,
        bar_high: Any = None, bar_low: Any = None, bar_close: Any = None,
        watch_id: str = "",
    ) -> dict[str, Any]:
        """Apply one closed-bar analysis to the single active thesis."""
        if exchange.strip().upper() != "OKX":
            return {"action": "ignored", "reason": "自动交易只接受 TradingView OKX 行情"}
        if not self.cfg.enabled:
            try:
                inst_id = self.mapped_instrument(symbol)
                account_id = self.account_id()
                active = self.store.active(self.cfg.profile, inst_id, account_id)
            except Exception:  # noqa: BLE001
                return {"action": "disabled"}
            if not active:
                return {"action": "disabled"}
        else:
            inst_id = self.mapped_instrument(symbol)
            account_id = self.account_id()
        with self._lock:
            client = self.client()
            try:
                active = self.store.active(self.cfg.profile, inst_id, account_id)
                if active:
                    if watch_id and active.get("watch_id") and active["watch_id"] != watch_id:
                        return {"action": "ignored", "reason": "Active Thesis 属于其他监控"}
                    return self._update_active(
                        client, active, timeframe, bar_ts, decision,
                        bar_high, bar_low, bar_close,
                    )
                latest = self.store.latest(account_id, self.cfg.profile, inst_id)
                if latest and bar_ts <= int(latest["last_bar_ts"]):
                    return {"action": "wait_next_bar", "reason": "上一 Thesis 结束后需等待下一根收盘 K 棒"}
                order_type = str(decision.get("order_type") or "")
                if order_type not in {"限价单", "突破单", "市价单"}:
                    return {"action": "observed", "reason": "本次分析不下单"}
                threshold = int(self.settings.general.decision_confidence_threshold)
                try:
                    confidence = int(float(decision.get("trade_confidence")))
                except (TypeError, ValueError):
                    confidence = -1
                if confidence < threshold:
                    return {
                        "action": "observed",
                        "reason": f"交易置信度 {confidence} 低于门槛 {threshold}",
                    }
                return self._create_thesis(
                    client, inst_id, timeframe, bar_ts, decision, watch_id=watch_id
                )
            finally:
                client.close()

    def _create_thesis(
        self, client: OKXClient, inst_id: str, timeframe: str,
        bar_ts: int, decision: dict[str, Any], *, watch_id: str = "",
    ) -> dict[str, Any]:
        self.validate_configuration(client)
        instrument = client.public_instrument(inst_id)
        if instrument.get("state") != "live" or instrument.get("settleCcy") != "USDT":
            raise ValueError("映射商品不是可交易的 USDT 永续合约")
        positions = [p for p in client.positions(inst_id) if D(p.get("pos") or 0) != 0]
        if positions:
            raise ValueError("检测到现有持仓；PA Agent 不接管人工或未知持仓")
        spec = InstrumentSpec.from_okx(instrument)
        direction = _direction(decision.get("order_direction"))
        entry = _price(decision.get("entry_price"))
        stop = _price(decision.get("stop_loss_price"))
        tp1 = _price(decision.get("take_profit_price"))
        tp2 = _price(decision.get("take_profit_price_2"))
        if direction == "long" and not (stop < entry < tp1 < tp2):
            raise ValueError("做多价格结构必须为 stop < entry < TP1 < TP2")
        if direction == "short" and not (tp2 < tp1 < entry < stop):
            raise ValueError("做空价格结构必须为 TP2 < TP1 < entry < stop")
        entry = spec.round_price(entry)
        stop = spec.round_price(stop)
        tp1 = spec.round_price(tp1)
        tp2 = spec.round_price(tp2)
        if direction == "long" and not (stop < entry < tp1 < tp2):
            raise ValueError("价格按 OKX 精度取整后不再满足做多结构")
        if direction == "short" and not (tp2 < tp1 < entry < stop):
            raise ValueError("价格按 OKX 精度取整后不再满足做空结构")
        zone_low_raw = decision.get("entry_zone_low")
        zone_high_raw = decision.get("entry_zone_high")
        if zone_low_raw is None or zone_high_raw is None:
            zone_low = zone_high = entry
        else:
            zone_low, zone_high = _price(zone_low_raw), _price(zone_high_raw)
            zone_low = spec.round_price(zone_low)
            zone_high = spec.round_price(zone_high)
            if zone_low > entry or zone_high < entry or zone_low > zone_high:
                zone_low = zone_high = entry
        equity = D(client.usdt_equity()) if self.cfg.sizing_mode == "risk_percent" else D(0)
        total, size1, size2 = calculate_contracts(
            spec=spec, mode=self.cfg.sizing_mode, entry=entry, stop=stop,
            leverage=self.cfg.leverage, fixed_notional=D(self.cfg.fixed_notional_usdt),
            fixed_margin=D(self.cfg.fixed_margin_usdt), risk_percent=D(self.cfg.risk_percent),
            equity=equity, max_notional=D(self.cfg.max_notional_usdt),
        )
        cl_ord_id = _client_id(self.cfg.profile, inst_id, bar_ts)
        values = {
            "account_id": self.account_id(), "profile": self.cfg.profile,
            "inst_id": inst_id, "watch_id": watch_id, "timeframe": timeframe,
            "status": "PENDING_SUBMIT", "direction": direction,
            "order_type": str(decision["order_type"]), "source_bar_ts": bar_ts,
            "last_bar_ts": bar_ts, "entry_zone_low": decimal_text(zone_low),
            "entry_zone_high": decimal_text(zone_high), "original_entry": decimal_text(entry),
            "current_entry": decimal_text(entry), "original_stop": decimal_text(stop),
            "current_stop": decimal_text(stop), "tp1": decimal_text(tp1),
            "tp2": decimal_text(tp2), "current_tp1": decimal_text(tp1),
            "current_tp2": decimal_text(tp2), "total_size": decimal_text(total),
            "tp1_size": decimal_text(size1), "tp2_size": decimal_text(size2),
            "cl_ord_id": cl_ord_id, "reason": str(decision.get("reasoning") or ""),
            "confidence": decision.get("trade_confidence"),
        }
        thesis = self.store.create(values)
        self.store.revision(thesis["id"], bar_ts, timeframe, decision, "CREATE")
        try:
            client.set_leverage(inst_id, self.cfg.leverage, self.cfg.margin_mode)
        except OKXError as exc:
            self.store.update(thesis["id"], status="INVALIDATED", last_error=f"杠杆设置失败：{exc}")
            raise
        try:
            result = self._place_entry(client, thesis)
            raw = result.pop("raw", {})
            self.store.update(thesis["id"], status="PENDING_ENTRY", **result)
            self.store.order(
                thesis["id"], "ENTRY", ord_id=result.get("entry_order_id", ""),
                algo_id=result.get("entry_algo_id", ""), client_id=cl_ord_id,
                size=decimal_text(total), state="live", raw=raw,
            )
            self._emit("ORDER_ACCEPTED", thesis_id=thesis["id"], inst_id=inst_id, profile=self.cfg.profile)
            self._reconcile_one(client, {**thesis, **result, "status": "PENDING_ENTRY"})
            return {"action": "created", "thesis_id": thesis["id"], "profile": self.cfg.profile}
        except OKXError as exc:
            if exc.uncertain:
                self.store.update(thesis["id"], status="RECONCILE", last_error=str(exc))
                return {"action": "reconcile", "thesis_id": thesis["id"]}
            self.store.update(thesis["id"], status="INVALIDATED", last_error=str(exc))
            raise

    def _place_entry(self, client: OKXClient, thesis: dict[str, Any]) -> dict[str, Any]:
        side = "buy" if thesis["direction"] == "long" else "sell"
        if thesis["order_type"] == "突破单":
            row = client.place_algo_order({
                "instId": thesis["inst_id"], "tdMode": self.cfg.margin_mode,
                "side": side, "ordType": "trigger", "sz": thesis["total_size"],
                "triggerPx": thesis["current_entry"], "orderPx": "-1",
                "triggerPxType": "last", "algoClOrdId": thesis["cl_ord_id"],
            })
            return {"entry_algo_id": str(row.get("algoId") or ""), "raw": row}
        payload = {
            "instId": thesis["inst_id"], "tdMode": self.cfg.margin_mode,
            "side": side, "ordType": "market" if thesis["order_type"] == "市价单" else "limit",
            "sz": thesis["total_size"], "clOrdId": thesis["cl_ord_id"],
        }
        if payload["ordType"] == "limit":
            payload["px"] = thesis["current_entry"]
        row = client.place_order(payload)
        return {"entry_order_id": str(row.get("ordId") or ""), "raw": row}

    def _update_active(
        self, client: OKXClient, thesis: dict[str, Any], timeframe: str, bar_ts: int,
        decision: dict[str, Any], bar_high: Any, bar_low: Any, bar_close: Any,
    ) -> dict[str, Any]:
        self._reconcile_one(client, thesis)
        thesis = self.store.active(thesis["profile"], thesis["inst_id"], thesis["account_id"])
        if thesis is None:
            return {"action": "completed"}
        action = "OBSERVE_OTHER_TIMEFRAME" if timeframe != thesis["timeframe"] else "UPDATE"
        is_new_bar = bar_ts > int(thesis["last_bar_ts"])
        inserted = self.store.revision(thesis["id"], bar_ts, timeframe, decision, action)
        if not inserted:
            return {"action": "duplicate", "thesis_id": thesis["id"]}
        self.store.update(
            thesis["id"], reason=str(decision.get("reasoning") or thesis["reason"]),
            confidence=decision.get("trade_confidence", thesis["confidence"]),
        )
        if timeframe != thesis["timeframe"]:
            return {"action": "recorded_other_timeframe", "thesis_id": thesis["id"]}
        stop = D(thesis["original_stop"])
        invalid = False
        if bar_high is not None and bar_low is not None:
            invalid = (thesis["direction"] == "long" and D(bar_low) <= stop) or (
                thesis["direction"] == "short" and D(bar_high) >= stop
            )
        if invalid:
            self._exit_thesis(client, thesis, "原始止损/结构失效价被触及")
            return {"action": "invalidated", "thesis_id": thesis["id"]}
        elapsed = int(thesis["elapsed_bars"])
        if is_new_bar:
            elapsed += 1
            self.store.update(thesis["id"], elapsed_bars=elapsed, last_bar_ts=bar_ts)
        if thesis["status"] in {"PENDING_ENTRY", "RECONCILE"} and elapsed >= self.cfg.pending_expiry_bars:
            self._exit_thesis(client, thesis, "未成交入场单到期")
            return {"action": "expired", "thesis_id": thesis["id"]}
        if thesis["status"] == "PENDING_ENTRY":
            self._maybe_amend_entry(client, thesis, decision)
        if thesis["status"] == "OPEN":
            self._maybe_tighten_stop(client, thesis, decision)
            self._maybe_amend_take_profit(
                client, thesis, decision, bar_ts=bar_ts, bar_close=bar_close
            )
        return {"action": "updated", "thesis_id": thesis["id"]}

    def _maybe_amend_take_profit(
        self, client: OKXClient, thesis: dict[str, Any], decision: dict[str, Any],
        *, bar_ts: int, bar_close: Any,
    ) -> None:
        action = str(decision.get("tp_update_action") or "none").strip().lower()
        if action not in {"lower_tp1", "raise_tp2"}:
            return
        field = (
            "proposed_take_profit_price"
            if action == "lower_tp1"
            else "proposed_take_profit_price_2"
        )
        if decision.get(field) is None:
            return
        spec = InstrumentSpec.from_okx(client.public_instrument(thesis["inst_id"]))
        target = spec.round_price(_price(decision[field]))
        entry = D(thesis["original_entry"])
        current_tp1 = D(thesis.get("current_tp1") or thesis["tp1"])
        current_tp2 = D(thesis.get("current_tp2") or thesis["tp2"])
        close = _price(bar_close) if bar_close is not None else None
        direction = thesis["direction"]
        valid = False
        if action == "lower_tp1":
            if direction == "long":
                floor = max(entry, close) if close is not None else entry
                valid = floor < target < current_tp1 and target < current_tp2
            else:
                ceiling = min(entry, close) if close is not None else entry
                valid = current_tp1 < target < ceiling and current_tp2 < target
        elif direction == "long":
            valid = target > current_tp2 and target > current_tp1
        else:
            valid = target < current_tp2 and target < current_tp1
        if not valid:
            self._emit(
                "TP_AMEND_REJECTED", thesis_id=thesis["id"], action=action,
                target=decimal_text(target), reason="建议价格不符合只收近 TP1／扩展 TP2 的限制",
            )
            return
        req_id = _amendment_id(thesis["profile"], thesis["inst_id"], bar_ts, action)
        self.store.queue_tp_amendment(
            thesis["id"], bar_ts, action, decimal_text(target), req_id,
            str(decision.get("tp_update_reason") or decision.get("reasoning") or ""),
        )
        self._apply_pending_tp_amendments(client, thesis)

    def _apply_pending_tp_amendments(
        self, client: OKXClient, thesis: dict[str, Any]
    ) -> None:
        for amendment in self.store.pending_tp_amendments(thesis["id"]):
            action = amendment["action"]
            leg = "TP1" if action == "lower_tp1" else "TP2"
            target = str(amendment["target_price"])
            matched = 0
            unresolved = False
            for order in self.store.orders(thesis["id"]):
                if order["role"] != "PROTECTION" or not order["algo_id"]:
                    continue
                try:
                    detail = client.get_algo_order(order["algo_id"])
                except OKXError as exc:
                    unresolved = True
                    self.store.update_tp_amendment(
                        amendment["id"], status="RECONCILE", last_error=str(exc)
                    )
                    continue
                state = str(detail.get("state") or order["state"] or "")
                actual_tp = str(detail.get("tpTriggerPx") or order.get("trigger_price") or "")
                order_leg = str(order.get("protection_leg") or "")
                if not order_leg:
                    tp1_values = {
                        str(thesis["tp1"]),
                        str(thesis.get("current_tp1") or thesis["tp1"]),
                    }
                    tp2_values = {
                        str(thesis["tp2"]),
                        str(thesis.get("current_tp2") or thesis["tp2"]),
                    }
                    if any(_same_price(actual_tp, value) for value in tp1_values):
                        order_leg = "TP1"
                    elif any(_same_price(actual_tp, value) for value in tp2_values):
                        order_leg = "TP2"
                    elif _same_price(actual_tp, target):
                        order_leg = leg
                self.store.update_order(
                    order["id"], state=state, protection_leg=order_leg,
                    trigger_price=actual_tp,
                    stop_price=str(detail.get("slTriggerPx") or order.get("stop_price") or ""),
                )
                if order_leg != leg or state not in {"live", "pause"}:
                    continue
                matched += 1
                if _same_price(actual_tp, target):
                    continue
                try:
                    order_req_id = "pt" + hashlib.sha256(
                        f"{amendment['req_id']}:{order['algo_id']}".encode()
                    ).hexdigest()[:20]
                    client.amend_algo_order(
                        thesis["inst_id"], order["algo_id"],
                        tp_trigger_px=target, req_id=order_req_id,
                    )
                    check = client.get_algo_order(order["algo_id"])
                except OKXError as exc:
                    # A timed-out write may still have reached OKX. Query before
                    # leaving it for the next reconciliation pass.
                    try:
                        check = client.get_algo_order(order["algo_id"])
                    except OKXError:
                        check = {}
                    if not _same_price(check.get("tpTriggerPx") or actual_tp, target):
                        unresolved = True
                        self.store.update_tp_amendment(
                            amendment["id"], status="RECONCILE", last_error=str(exc)
                        )
                        continue
                if not _same_price(check.get("tpTriggerPx") or actual_tp, target):
                    unresolved = True
                    self.store.update_tp_amendment(
                        amendment["id"], status="RECONCILE",
                        last_error=f"{leg} 修改已受理但交易所状态尚未更新",
                    )
                    continue
                self.store.update_order(order["id"], trigger_price=target)
            if unresolved:
                self.store.update(
                    thesis["id"], last_error=f"{leg} 修改待核对"
                )
                continue
            if matched == 0:
                self.store.update_tp_amendment(
                    amendment["id"], status="OBSOLETE", last_error=f"{leg} 已无有效委托"
                )
                continue
            column = "current_tp1" if leg == "TP1" else "current_tp2"
            self.store.update(thesis["id"], **{column: target, "last_error": ""})
            thesis = {**thesis, column: target}
            self.store.update_tp_amendment(
                amendment["id"], status="APPLIED", last_error=""
            )
            self._emit(
                "TP1_LOWERED" if leg == "TP1" else "TP2_RAISED",
                thesis_id=thesis["id"], inst_id=thesis["inst_id"],
                price=target, reason=str(amendment.get("reason") or "PA Active Thesis 更新"),
            )

    def _maybe_amend_entry(self, client: OKXClient, thesis: dict[str, Any], decision: dict[str, Any]) -> None:
        proposed = decision.get("proposed_entry_price")
        if proposed is None or not thesis.get("entry_order_id") or D(thesis["filled_size"]) > 0:
            return
        new_price = _price(proposed)
        spec = InstrumentSpec.from_okx(client.public_instrument(thesis["inst_id"]))
        new_price = spec.round_price(new_price)
        if not (D(thesis["entry_zone_low"]) <= new_price <= D(thesis["entry_zone_high"])):
            return
        if abs(new_price - D(thesis["original_stop"])) > abs(
            D(thesis["original_entry"]) - D(thesis["original_stop"])
        ):
            return
        if new_price == D(thesis["current_entry"]):
            return
        client.amend_order(thesis["inst_id"], thesis["entry_order_id"], decimal_text(new_price))
        self.store.update(thesis["id"], current_entry=decimal_text(new_price))
        self._emit("ENTRY_AMENDED", thesis_id=thesis["id"], price=decimal_text(new_price))

    def _maybe_tighten_stop(self, client: OKXClient, thesis: dict[str, Any], decision: dict[str, Any]) -> None:
        proposed = decision.get("proposed_stop_loss_price")
        if proposed is None:
            return
        new_stop = _price(proposed)
        spec = InstrumentSpec.from_okx(client.public_instrument(thesis["inst_id"]))
        new_stop = spec.round_price(new_stop)
        current = D(thesis["current_stop"])
        safer = new_stop > current if thesis["direction"] == "long" else new_stop < current
        entry = D(thesis["original_entry"])
        valid_side = new_stop < entry if thesis["direction"] == "long" else new_stop > entry
        if not safer or not valid_side:
            return
        # Install replacement protection before removing the old protection so
        # a rejected request never leaves the position naked.
        old_items = self._live_protection_items(thesis)
        self._protect_filled(
            client,
            {**thesis, "current_stop": decimal_text(new_stop), "protected_size": "0"},
        )
        self.store.update(thesis["id"], current_stop=decimal_text(new_stop))
        self._cancel_protection_items(client, thesis["id"], old_items)
        self._emit("STOP_TIGHTENED", thesis_id=thesis["id"], stop=decimal_text(new_stop))

    def _entry_snapshot(self, client: OKXClient, thesis: dict[str, Any]) -> dict[str, Any]:
        if thesis.get("entry_order_id"):
            return client.get_order(thesis["inst_id"], ord_id=thesis["entry_order_id"])
        if thesis.get("entry_algo_id"):
            algo = client.get_algo_order(thesis["entry_algo_id"])
            ord_id = str(algo.get("ordId") or algo.get("actualOrdId") or "")
            if ord_id:
                self.store.update(thesis["id"], entry_order_id=ord_id)
                return client.get_order(thesis["inst_id"], ord_id=ord_id)
            return {"state": algo.get("state", "live"), "accFillSz": "0"}
        if thesis.get("order_type") == "突破单":
            algo = client.find_pending_algo(thesis["cl_ord_id"])
            if algo:
                algo_id = str(algo.get("algoId") or "")
                self.store.update(thesis["id"], entry_algo_id=algo_id, status="PENDING_ENTRY")
                return {"state": algo.get("state", "live"), "accFillSz": "0"}
            raise OKXError("未能确认突破触发单状态")
        return client.get_order(thesis["inst_id"], cl_ord_id=thesis["cl_ord_id"])

    def _reconcile_one(self, client: OKXClient, thesis: dict[str, Any]) -> None:
        try:
            snapshot = self._entry_snapshot(client, thesis)
        except OKXError as exc:
            self.store.update(thesis["id"], status="RECONCILE", last_error=str(exc))
            return
        filled = D(snapshot.get("accFillSz") or snapshot.get("fillSz") or thesis["filled_size"])
        if filled != D(thesis["filled_size"]):
            self.store.update(thesis["id"], filled_size=decimal_text(filled))
            thesis = {**thesis, "filled_size": decimal_text(filled)}
            self._emit("FILL", thesis_id=thesis["id"], size=decimal_text(filled))
        if filled > D(thesis["protected_size"]):
            try:
                self._protect_filled(client, thesis)
            except Exception as exc:  # noqa: BLE001
                self.store.update(thesis["id"], last_error=f"保护委托失败：{exc}")
                self._emergency_flatten(client, thesis, filled)
                raise
        self._apply_pending_tp_amendments(client, thesis)
        state = str(snapshot.get("state") or "")
        if filled > 0:
            self.store.update(thesis["id"], status="OPEN", last_error="")
        positions = [p for p in client.positions(thesis["inst_id"]) if D(p.get("pos") or 0) != 0]
        if positions and abs(D(positions[0].get("pos") or 0)) > D(thesis["total_size"]):
            self.store.update(thesis["id"], status="RECONCILE", last_error="检测到超出 Thesis 的未知/人工持仓")
            self._emit("FROZEN", thesis_id=thesis["id"], reason="检测到未知/人工持仓")
            return
        if positions:
            self.store.update(thesis["id"], position_seen=1, zero_position_checks=0)
        elif filled > 0 and state in TERMINAL_ORDER_STATES and int(thesis.get("position_seen", 0)):
            checks = int(thesis.get("zero_position_checks", 0)) + 1
            if checks >= 2:
                self.store.update(thesis["id"], status="COMPLETED", zero_position_checks=checks)
                self._emit("COMPLETED", thesis_id=thesis["id"], inst_id=thesis["inst_id"])
            else:
                self.store.update(thesis["id"], zero_position_checks=checks)
        elif filled == 0 and state in {"canceled", "mmp_canceled"}:
            self.store.update(thesis["id"], status="INVALIDATED", last_error="入场委托已取消")

    def _protect_filled(self, client: OKXClient, thesis: dict[str, Any]) -> None:
        filled, protected = D(thesis["filled_size"]), D(thesis["protected_size"])
        delta = filled - protected
        if delta <= 0:
            return
        side = "sell" if thesis["direction"] == "long" else "buy"
        spec = InstrumentSpec.from_okx(client.public_instrument(thesis["inst_id"]))
        # Allocate every partial fill immediately, while converging to the
        # original TP1/TP2 split as more contracts fill.
        tp1_remaining = max(D(0), D(thesis["tp1_size"]) - protected)
        to_tp1 = min(delta, tp1_remaining)
        to_tp2 = delta - to_tp1
        chunks: list[tuple[Decimal, str, str]] = []
        if to_tp1 >= spec.min_size:
            chunks.append((to_tp1, str(thesis.get("current_tp1") or thesis["tp1"]), "TP1"))
        if to_tp2 >= spec.min_size:
            chunks.append((to_tp2, str(thesis.get("current_tp2") or thesis["tp2"]), "TP2"))
        if sum((size for size, _, _ in chunks), D(0)) != delta:
            raise SizingError("部分成交量无法按 OKX 最小张数建立保护单")
        created: list[dict[str, str]] = []
        try:
            for size, tp, leg in chunks:
                client_id = _client_id(thesis["profile"], thesis["inst_id"], int(thesis["source_bar_ts"]) + len(self.store.orders(thesis["id"])) + 1)
                row = client.place_algo_order({
                    "instId": thesis["inst_id"], "tdMode": self.cfg.margin_mode,
                    "side": side, "ordType": "oco", "sz": decimal_text(size),
                    "reduceOnly": True, "tpTriggerPx": tp, "tpOrdPx": "-1",
                    "slTriggerPx": thesis["current_stop"], "slOrdPx": "-1",
                    "tpTriggerPxType": "mark", "slTriggerPxType": "mark",
                    "algoClOrdId": client_id,
                })
                algo_id = str(row.get("algoId") or "")
                created.append({"instId": thesis["inst_id"], "algoId": algo_id})
                self.store.order(
                    thesis["id"], "PROTECTION", algo_id=algo_id, client_id=client_id,
                    size=decimal_text(size), state="live", raw=row,
                    protection_leg=leg, trigger_price=tp,
                    stop_price=str(thesis["current_stop"]),
                )
        except Exception:
            if created:
                client.cancel_algos(created)
            raise
        self.store.update(thesis["id"], protected_size=decimal_text(filled), status="OPEN")

    def _live_protection_items(self, thesis: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"instId": thesis["inst_id"], "algoId": row["algo_id"]}
            for row in self.store.orders(thesis["id"])
            if row["role"] == "PROTECTION" and row["algo_id"] and row["state"] == "live"
        ]

    def _cancel_protection_items(
        self, client: OKXClient, thesis_id: int, items: list[dict[str, str]],
    ) -> None:
        if items:
            for start in range(0, len(items), 10):
                client.cancel_algos(items[start:start + 10])
            self.store.mark_protection_canceled(
                thesis_id, [item["algoId"] for item in items]
            )

    def _cancel_protection(self, client: OKXClient, thesis: dict[str, Any]) -> None:
        self._cancel_protection_items(client, thesis["id"], self._live_protection_items(thesis))

    def _emergency_flatten(self, client: OKXClient, thesis: dict[str, Any], size: Decimal) -> None:
        if thesis.get("entry_order_id"):
            try:
                client.cancel_order(thesis["inst_id"], thesis["entry_order_id"])
            except OKXError:
                pass
        client.place_order({
            "instId": thesis["inst_id"], "tdMode": self.cfg.margin_mode,
            "side": "sell" if thesis["direction"] == "long" else "buy",
            "ordType": "market", "sz": decimal_text(size), "reduceOnly": True,
        })
        self.store.update(thesis["id"], status="EXITING", position_seen=1, zero_position_checks=0)

    def _exit_thesis(self, client: OKXClient, thesis: dict[str, Any], reason: str) -> None:
        self.store.update(thesis["id"], status="EXITING", last_error=reason)
        if thesis.get("entry_order_id"):
            try:
                client.cancel_order(thesis["inst_id"], thesis["entry_order_id"])
            except OKXError as exc:
                logger.info("Entry cancel during exit: %s", exc)
        if thesis.get("entry_algo_id"):
            try:
                client.cancel_algos([{"instId": thesis["inst_id"], "algoId": thesis["entry_algo_id"]}])
            except OKXError as exc:
                logger.info("Trigger cancel during exit: %s", exc)
        self._cancel_protection(client, thesis)
        positions = [p for p in client.positions(thesis["inst_id"]) if D(p.get("pos") or 0) != 0]
        if positions:
            actual = abs(D(positions[0].get("pos") or 0))
            if actual > D(thesis["total_size"]):
                self.store.update(thesis["id"], status="RECONCILE", last_error="未知持仓阻止自动平仓")
                self._emit("FROZEN", thesis_id=thesis["id"], reason="未知持仓阻止自动平仓")
                return
            size = actual
            self._emergency_flatten(client, thesis, size)
        else:
            self.store.update(thesis["id"], status="INVALIDATED")
        self._emit("INVALIDATED", thesis_id=thesis["id"], reason=reason)

    def reconcile_all(self) -> None:
        account_id = self.account_id()
        if not self.cfg.enabled and not self.store.active_all(self.cfg.profile, account_id):
            return
        with self._lock:
            client = self.client()
            try:
                for thesis in self.store.active_all(self.cfg.profile, account_id):
                    self._reconcile_one(client, thesis)
            finally:
                client.close()

    def close_thesis(self, thesis_id: int) -> None:
        with self._lock:
            thesis = next((
                row for row in self.store.active_all(self.cfg.profile, self.account_id())
                if row["id"] == thesis_id
            ), None)
            if not thesis:
                raise ValueError("找不到进行中的 Thesis")
            client = self.client()
            try:
                self._exit_thesis(client, thesis, "使用者手动结束 Thesis")
            finally:
                client.close()
