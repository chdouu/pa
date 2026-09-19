"""Decimal-only OKX perpetual contract sizing."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


class SizingError(ValueError):
    pass


def D(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class InstrumentSpec:
    inst_id: str
    ct_val: Decimal
    lot_size: Decimal
    min_size: Decimal
    tick_size: Decimal

    @classmethod
    def from_okx(cls, row: dict) -> "InstrumentSpec":
        return cls(
            inst_id=str(row["instId"]), ct_val=D(row.get("ctVal") or "0"),
            lot_size=D(row.get("lotSz") or "1"), min_size=D(row.get("minSz") or "1"),
            tick_size=D(row.get("tickSz") or "0.00000001"),
        )

    def floor_size(self, size: Decimal) -> Decimal:
        return (size / self.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.lot_size

    def floor_price(self, price: Decimal) -> Decimal:
        return (price / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size

    def round_price(self, price: Decimal) -> Decimal:
        return (price / self.tick_size).to_integral_value(rounding=ROUND_HALF_UP) * self.tick_size


def calculate_contracts(
    *, spec: InstrumentSpec, mode: str, entry: Decimal, stop: Decimal,
    leverage: int, fixed_notional: Decimal = Decimal(0), fixed_margin: Decimal = Decimal(0),
    risk_percent: Decimal = Decimal(0), equity: Decimal = Decimal(0),
    max_notional: Decimal = Decimal(0),
) -> tuple[Decimal, Decimal, Decimal]:
    if entry <= 0 or spec.ct_val <= 0:
        raise SizingError("入场价或合约面值无效")
    if mode == "fixed_notional":
        notional = fixed_notional
    elif mode == "fixed_margin":
        notional = fixed_margin * D(leverage)
    elif mode == "risk_percent":
        distance = abs(entry - stop)
        if equity <= 0 or risk_percent <= 0 or distance <= 0:
            raise SizingError("风险比例模式需要有效权益、风险比例与止损距离")
        risk_budget = equity * risk_percent / D(100)
        raw = risk_budget / (distance * spec.ct_val)
        notional = raw * spec.ct_val * entry
    else:
        raise SizingError("未知部位计算模式")
    if notional <= 0:
        raise SizingError("交易金额必须大于 0")
    if max_notional > 0:
        notional = min(notional, max_notional)
    total = spec.floor_size(notional / (entry * spec.ct_val))
    if total < spec.min_size:
        raise SizingError("计算后的张数低于 OKX 最小下单量")
    tp1 = spec.floor_size(total / D(2))
    tp2 = total - tp1
    if tp1 < spec.min_size or tp2 < spec.min_size:
        raise SizingError("张数不足以拆分为两段止盈")
    return total, tp1, tp2


def decimal_text(value: Decimal) -> str:
    return format(value, "f")
