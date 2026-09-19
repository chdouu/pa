from importlib import import_module


def test_headless_runtime_source_packages_are_importable():
    modules = (
        "pa_agent.data.base",
        "pa_agent.data.snapshot",
        "pa_agent.data.tradingview",
        "pa_agent.records.analysis_history",
        "pa_agent.records.experience_reader",
        "pa_agent.records.pending_writer",
        "pa_agent.records.schema",
        "pa_server.api",
    )
    for module in modules:
        assert import_module(module) is not None
