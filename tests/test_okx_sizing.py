from decimal import Decimal

import pytest

from pa_agent.trading.sizing import InstrumentSpec, SizingError, calculate_contracts


SPEC = InstrumentSpec(
    inst_id="BTC-USDT-SWAP",
    ct_val=Decimal("0.01"),
    lot_size=Decimal("1"),
    min_size=Decimal("1"),
    tick_size=Decimal("0.1"),
)


def test_fixed_notional_and_uneven_tp_split_round_down():
    total, tp1, tp2 = calculate_contracts(
        spec=SPEC, mode="fixed_notional", entry=Decimal("100"), stop=Decimal("90"),
        leverage=3, fixed_notional=Decimal("5"), max_notional=Decimal("100"),
    )
    assert (total, tp1, tp2) == (Decimal("5"), Decimal("2"), Decimal("3"))


def test_fixed_margin_multiplies_leverage_and_honors_cap():
    total, _, _ = calculate_contracts(
        spec=SPEC, mode="fixed_margin", entry=Decimal("100"), stop=Decimal("90"),
        leverage=10, fixed_margin=Decimal("10"), max_notional=Decimal("20"),
    )
    assert total == Decimal("20")


def test_risk_percent_uses_stop_distance():
    total, _, _ = calculate_contracts(
        spec=SPEC, mode="risk_percent", entry=Decimal("100"), stop=Decimal("90"),
        leverage=1, risk_percent=Decimal("1"), equity=Decimal("1000"),
        max_notional=Decimal("1000"),
    )
    assert total == Decimal("100")


def test_rejects_size_that_cannot_be_split_into_two_minimum_orders():
    with pytest.raises(SizingError, match="拆分"):
        calculate_contracts(
            spec=SPEC, mode="fixed_notional", entry=Decimal("100"), stop=Decimal("90"),
            leverage=1, fixed_notional=Decimal("1"), max_notional=Decimal("1"),
        )
