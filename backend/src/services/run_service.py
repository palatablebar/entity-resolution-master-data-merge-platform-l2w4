"""解析运行服务：创建并投递 Celery、查询列表与详情、重跑。

规则冻结规则：
  - 新建运行在「创建瞬间」把规则快照写进 params_json，之后 worker 只读快照；
  - 重跑原样沿用上一卷的冻结快照（不重新取当前规则）；
  - 同一 run 被重复投递时，worker 清空并重建派生产物，绝不累加候选/簇/档案。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.config.celery_app import TASK_RESOLVE_RUN, celery_app
from src.constants import error_codes as codes
from src.constants.enums import RunStatus
from src.models.resolution_run import ResolutionRun
from src.models.user import User
from src.schemas.runs import serialize_run
from src.services import audit_service, rule_impact, rule_service
from src.utils.errors import AppError, not_found
from src.utils.logger import get_logger
from src.utils.pagination import normalize_page

logger = get_logger("entresolv.run")

ACTIVE_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)


def list_runs(db: Session, *, status: str | None, page: int, page_size: int) -> dict[str, Any]:
    safe_page, safe_size = normalize_page(page, page_size)
    query = db.query(ResolutionRun)
    if status:
        query = query.filter(ResolutionRun.status == status)
    total = query.with_entities(func.count(ResolutionRun.id)).scalar() or 0
    rows = (
        query.order_by(ResolutionRun.created_at.desc())
        .offset((safe_page - 1) * safe_size)
        .limit(safe_size)
        .all()
    )
    contexts = rule_service.contexts_for_runs(db, rows)
    items = []
    for row in rows:
        payload = serialize_run(row)
        payload["rule_context"] = contexts.get(str(row.id))
        items.append(payload)
    return {
        "items": items,
        "page": safe_page,
        "page_size": safe_size,
        "total": total,
        "total_pages": max(1, -(-total // safe_size)) if total else 1,
    }


def get_run(db: Session, run_id: str) -> Any:
    row = db.get(ResolutionRun, run_id)
    if row is None:
        raise not_found("ResolutionRun", run_id)
    return row


def get_run_payload(db: Session, run_id: str) -> dict[str, Any]:
    row = get_run(db, run_id)
    payload = serialize_run(row)
    context = rule_service.context_for_run(db, row)
    payload["rule_context"] = context
    payload["rule_impact_summary"] = summarize_impact(db, row, context)
    return payload


def summarize_impact(db: Session, run: ResolutionRun, context: dict[str, Any]) -> dict[str, Any]:
    """统计该运行的候选 / 黄金档案中，按当前规则会改变结果的数量与清单。"""
    from src.models.golden_record import GoldenRecord
    from src.models.match_candidate import MatchCandidate

    comparison = context.get("comparison") or {}
    frozen = comparison.get("frozen_values") or {}

    candidates_affected: list[str] = []
    candidates_total = 0
    candidate_rows = (
        db.query(MatchCandidate)
        .filter(MatchCandidate.run_id == run.id)
        .order_by(MatchCandidate.score.desc())
        .all()
    )
    for row in candidate_rows:
        candidates_total += 1
        features = (row.features_json or {}).get("features") if isinstance(row.features_json, dict) else None
        impact = rule_impact.candidate_impact(
            features,
            row.score,
            row.decision,
            comparison=comparison,
            frozen_values=frozen,
        )
        if impact.get("affected"):
            candidates_affected.append(str(row.id))

    golden_affected: list[str] = []
    golden_rows = (
        db.query(GoldenRecord)
        .filter(GoldenRecord.run_id == run.id)
        .order_by(GoldenRecord.entity_key.asc())
        .all()
    )
    for row in golden_rows:
        impact = rule_impact.golden_impact(
            row.field_values,
            comparison=comparison,
            frozen_values=frozen,
        )
        if impact.get("affected"):
            golden_affected.append(str(row.id))

    return {
        "basis": "affected_basis" if comparison.get("affected_basis") else "unchanged",
        "rule_changed": bool(comparison.get("rule_changed")),
        "candidates_total": candidates_total,
        "candidates_affected": len(candidates_affected),
        "candidate_ids": candidates_affected,
        "golden_total": len(golden_rows),
        "golden_affected": len(golden_affected),
        "golden_ids": golden_affected,
    }


def latest_succeeded_run(db: Session) -> ResolutionRun | None:
    return (
        db.query(ResolutionRun)
        .filter(ResolutionRun.status == RunStatus.SUCCEEDED.value)
        .order_by(ResolutionRun.created_at.desc())
        .first()
    )


def _ensure_no_active_run(db: Session) -> None:
    active = db.query(ResolutionRun).filter(ResolutionRun.status.in_(ACTIVE_STATUSES)).first()
    if active is not None:
        raise AppError(
            codes.RUN_CONFLICT,
            "已有解析任务在排队或运行中，请稍后重试",
            f"active_run={active.id}",
            extra={"active_run_id": str(active.id)},
        )


def _dispatch(db: Session, run: ResolutionRun) -> None:
    try:
        celery_app.send_task(TASK_RESOLVE_RUN, args=[str(run.id)])
        logger.info("run dispatched run=%s", run.id)
    except Exception as exc:  # noqa: BLE001
        logger.error("dispatch failed run=%s error=%s", run.id, exc)
        run.status = RunStatus.FAILED.value
        run.error_code = codes.INTERNAL_ERROR
        run.error_message = f"任务队列不可用：{exc}"[:500]
        db.commit()
        raise AppError(codes.INTERNAL_ERROR, "任务队列暂不可用，运行未启动", str(exc)) from exc


def create_run(
    db: Session,
    *,
    user: User,
    rule_id: str | None,
    run_label: str,
    ip: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    _ensure_no_active_run(db)

    if rule_id:
        rule = rule_service.get_rule(db, rule_id)
    else:
        rule = rule_service.ensure_default_rule(db)

    run = ResolutionRun(
        rule_id=rule.id,
        run_label=run_label or f"人工触发 · {rule.name}",
        status=RunStatus.QUEUED.value,
        params_json=rule_service.snapshot(db, rule),
        blocking_keys=["phone_suffix", "name_city", "email_domain"],
        triggered_by=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    audit_service.record(
        db,
        action="run.create",
        entity_type="ResolutionRun",
        entity_id=str(run.id),
        actor_id=str(user.id),
        actor_email=user.email,
        payload={
            "rule_id": str(rule.id),
            "rule_version": rule.version,
            "run_label": run.run_label,
        },
        ip=ip,
        trace_id=trace_id,
    )

    _dispatch(db, run)
    payload = serialize_run(run)
    payload["rule_context"] = rule_service.context_for_run(db, run, rule=rule)
    return payload


def retry_failed(
    db: Session, run_id: str, *, user: User, ip: str | None, trace_id: str | None
) -> dict[str, Any]:
    """失败恢复：就地重投同一个 run，沿用其冻结快照，不新建卷、不清除人工复核痕迹以外的产物。

    worker 再次执行时会先清空并重建该 run 的候选 / 簇 / 档案，因此重复恢复不会累加产物。
    """
    run = get_run(db, run_id)
    if run.status == RunStatus.QUEUED.value or run.status == RunStatus.RUNNING.value:
        raise AppError(
            codes.RUN_CONFLICT,
            "该运行仍在排队或运行中，无需恢复",
            f"status={run.status}",
            extra={"active_run_id": str(run.id)},
        )
    if run.status == RunStatus.SUCCEEDED.value:
        raise AppError(
            codes.RUN_CONFLICT,
            "该运行已成功，不能按失败恢复；如需重算请使用重跑",
            f"status={run.status}",
        )

    # 旧运行如果没有冻结快照，补冻结一次；已有快照绝不重取当前规则。
    if not rule_service.has_snapshot(run.params_json):
        rule = rule_service.resolve_live_rule_for_run(db, run)
        if rule is None:
            raise AppError(codes.RULE_INVALID, "没有可用的合并规则，无法恢复", "no merge rule")
        run.params_json = {
            **dict(run.params_json or {}),
            **rule_service.snapshot(db, rule),
            "frozen_from": "legacy_live_rule",
        }
        db.commit()
        db.refresh(run)

    run.status = RunStatus.QUEUED.value
    run.error_code = None
    run.error_message = None
    run.started_at = None
    run.finished_at = None
    db.commit()
    db.refresh(run)

    audit_service.record(
        db,
        action="run.retry",
        entity_type="ResolutionRun",
        entity_id=str(run.id),
        actor_id=str(user.id),
        actor_email=user.email,
        payload={
            "rule_id": run.params_json.get("rule_id"),
            "rule_version": run.params_json.get("rule_version"),
            "run_label": run.run_label,
        },
        ip=ip,
        trace_id=trace_id,
    )

    _dispatch(db, run)
    payload = serialize_run(run)
    payload["rule_context"] = rule_service.context_for_run(db, run)
    return payload


def rerun(db: Session, run_id: str, *, user: User, ip: str | None, trace_id: str | None) -> dict[str, Any]:
    """重跑：新建一卷，沿用被重跑运行的冻结规则快照，不读取当前规则。"""
    previous = get_run(db, run_id)
    _ensure_no_active_run(db)

    params = dict(previous.params_json or {})
    if not rule_service.has_snapshot(params):
        # 旧运行没有冻结快照：在重跑时补冻结一次，并标记来源，后续一律沿用。
        rule = rule_service.resolve_live_rule_for_run(db, previous)
        if rule is None:
            raise AppError(codes.RULE_INVALID, "没有可用的合并规则，无法重跑", "no merge rule")
        params = {**params, **rule_service.snapshot(db, rule), "frozen_from": "legacy_live_rule"}
    else:
        params = rule_service.copy_frozen_params(params, rerun_of=str(previous.id))

    run = ResolutionRun(
        rule_id=previous.rule_id,
        run_label=f"重跑 · {previous.run_label}"[:160],
        status=RunStatus.QUEUED.value,
        params_json=params,
        # 重跑沿用被重跑运行的 blocking 键口径，而不是重算当前默认值。
        blocking_keys=list(previous.blocking_keys or ["phone_suffix", "name_city", "email_domain"]),
        triggered_by=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    audit_service.record(
        db,
        action="run.rerun",
        entity_type="ResolutionRun",
        entity_id=str(run.id),
        actor_id=str(user.id),
        actor_email=user.email,
        payload={
            "rerun_of": str(previous.id),
            "rule_id": params.get("rule_id"),
            "rule_version": params.get("rule_version"),
            "run_label": run.run_label,
        },
        ip=ip,
        trace_id=trace_id,
    )

    _dispatch(db, run)
    payload = serialize_run(run)
    payload["rule_context"] = rule_service.context_for_run(db, run)
    return payload
