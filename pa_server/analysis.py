from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.router import route_strategy_files
from pa_agent.data.base import KlineBar
from pa_agent.data.snapshot import build_analysis_frame
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.analysis_history import compute_incremental_bar_delta
from pa_agent.records.schema import AnalysisRecord
from pa_agent.util.threading import CancelToken, OrchestratorEvent


class AnalysisError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def scrub_secrets(value, settings):
    secrets = [settings.provider.api_key]
    secrets.extend(group.api_key for group in settings.provider.fallback_groups)
    secrets.extend((
        settings.feishu.webhook_url,
        settings.feishu.secret,
    ))
    secrets = [secret for secret in secrets if secret]

    def walk(node):
        if isinstance(node, str):
            for secret in secrets:
                node = node.replace(secret, "****")
            return node
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node
    return walk(value)


class StrictWriter(PendingWriter):
    def __init__(self, *args, secrets: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._secrets = [value for value in (secrets or []) if value]

    def _write_json(self, path: Path, data: dict) -> None:
        def scrub(value):
            if isinstance(value, str):
                for secret in self._secrets:
                    value = value.replace(secret, "****")
                return value
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        data = scrub(data)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def run_snapshot_analysis(
    job: dict,
    settings,
    root: Path,
    progress: Callable[[str], None],
    timezone_name: str = "Asia/Taipei",
    client=None,
) -> tuple[dict, dict]:
    request = job["request"]
    snapshot = job["snapshot"]
    bars = [KlineBar(**item) for item in snapshot]
    frame = build_analysis_frame(bars, request["bar_count"], request["symbol"], request["timeframe"])
    if frame is None:
        raise AnalysisError("insufficient_data", "Snapshot does not contain enough closed bars")
    job_dir = root / "records" / job["id"]
    secrets = [settings.provider.api_key] + [group.api_key for group in settings.provider.fallback_groups]
    writer = StrictWriter(pending_dir=job_dir, api_key=settings.provider.api_key, secrets=secrets)
    previous = None
    previous_raw = job.get("previous_record")
    incremental_count = None
    if previous_raw:
        try:
            previous = AnalysisRecord.model_validate(previous_raw)
            delta = compute_incremental_bar_delta(frame, previous)
            max_new = int(getattr(settings.general, "incremental_max_new_bars", 10))
            if delta and 0 < delta.new_count <= max_new:
                incremental_count = delta.new_count
            else:
                previous = None
        except Exception:
            previous = None
    analysis_client = client or create_ai_client(settings.provider)
    begin_trace = getattr(analysis_client, "begin_trace", None)
    if begin_trace is not None:
        begin_trace()
    orchestrator = TwoStageOrchestrator(
        client=analysis_client,
        assembler=PromptAssembler(prompt_dir=root / "prompt_engineering", experience_reader=ExperienceReader(root / "experience"), prompt_settings=settings.prompt),
        router=route_strategy_files,
        validator=JsonValidator(settings),
        pending_writer=writer,
        exp_reader=ExperienceReader(root / "experience"),
        settings=settings,
        persist_fallback_settings=False,
    )

    def on_event(event: OrchestratorEvent) -> None:
        if event == OrchestratorEvent.Stage1Started:
            set_stage = getattr(analysis_client, "set_stage", None)
            if set_stage is not None:
                set_stage("stage1")
            progress("stage1")
        elif event == OrchestratorEvent.Stage2Started:
            set_stage = getattr(analysis_client, "set_stage", None)
            if set_stage is not None:
                set_stage("stage2")
            progress("stage2")

    set_progress = getattr(analysis_client, "set_progress_callback", None)
    if set_progress is not None:
        set_progress(progress)
    try:
        record = orchestrator.submit(
            frame, CancelToken(), on_event,
            previous_record=previous,
            incremental_new_bar_count=incremental_count,
            active_thesis=job.get("active_thesis") or None,
        )
    finally:
        if set_progress is not None:
            set_progress(None)
    if record.exception or not record.stage1_diagnosis or not record.stage2_decision:
        raise AnalysisError("model_error", "Two-stage analysis did not complete")
    get_last_success = getattr(analysis_client, "get_last_success", None)
    selected_ai = get_last_success() if get_last_success is not None else None
    if selected_ai is None:
        selected_ai = {
            "api_group": "primary",
            "api_group_index": 1,
            "model": settings.provider.model,
            "model_id": settings.provider.model,
            "stage": "analysis",
        }
    get_success_history = getattr(analysis_client, "get_success_history", None)
    ai_history = get_success_history() if get_success_history is not None else [selected_ai]
    ai_by_stage = {}
    for item in ai_history:
        if item.get("stage"):
            ai_by_stage[item["stage"]] = item
    result = {
        "analysis_id": job["id"],
        "watch_id": job.get("watch_id"),
        "request": request,
        "target_bar_time_ms": int(job["target_ts"]),
        "target_bar_time": datetime.fromtimestamp(
            int(job["target_ts"]) / 1000, tz=ZoneInfo(timezone_name)
        ).isoformat(),
        "snapshot_time_ms": frame.snapshot_ts_local_ms,
        "settings_version": job["settings_version"],
        "model": selected_ai["model_id"],
        "ai_provider": selected_ai,
        "ai_providers_by_stage": ai_by_stage,
        "stage1_diagnosis": record.stage1_diagnosis,
        "stage2_decision": record.stage2_decision,
        "active_thesis": job.get("active_thesis") or None,
        "usage_total": record.usage_total,
        "stale": False,
        "notification_status": "not_required",
    }
    return scrub_secrets(result, settings), scrub_secrets(record.model_dump(), settings)


def has_order_opportunity(stage2: dict, threshold: int) -> bool:
    decision = stage2.get("decision") if isinstance(stage2, dict) else None
    if not isinstance(decision, dict) or decision.get("order_type") not in {"限价单", "突破单", "市价单"}:
        return False
    if threshold <= 0:
        return True
    try:
        return float(decision.get("trade_confidence")) >= threshold
    except (TypeError, ValueError):
        return False
