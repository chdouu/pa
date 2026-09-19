from pa_server.analysis import has_order_opportunity


def test_order_opportunity_uses_type_and_confidence():
    assert has_order_opportunity({"decision": {"order_type": "限价单", "trade_confidence": 60}}, 40)
    assert not has_order_opportunity({"decision": {"order_type": "限价单", "trade_confidence": 20}}, 40)
    assert not has_order_opportunity({"decision": {"order_type": "不下单", "trade_confidence": 99}}, 40)
