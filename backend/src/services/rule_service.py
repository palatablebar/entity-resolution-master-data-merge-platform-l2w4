"""MergeRule 服务：读取、创建、更新、参数校验与运行规则快照。

运行规则快照（frozen snapshot）是解释历史候选 / 簇 / 黄金档案的唯一依据：
  - 运行创建时把当时的规则值完整写进 ResolutionRun.params_json；
  - worker 执行时只读这份快照，绝不再读可变的 MergeRule 行；
  - 失败重试、重跑都沿用原快照；
  - 规则后续调整只影响之后创建的新运行。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import FIELD_LABELS, SOURCE_SYSTEM_LABELS, SourceSystem
from src.constants.weights import DEFAULT_FIELD_WEIGHTS, DEFAULT_SOURCE_RELIABILITY
from src.models.merge_rule import MergeRule
from src.models.resolution_run import ResolutionRun
from src.schemas.rules import RuleUpdateRequest, serialize_rule
from src.utils.errors import AppError, not_found
from src.utils.normalize import DEFAULT_NORMALIZATION

# 决定解析结果（判定/仲裁）的规则值；只有这些字段参与「是否受影响」的比较。
# 决定解析结果（判定/仲裁）的规则值；只有这些字段参与「是否受影响」的比较。
RULE_VALUE_KEYS: tuple[str, ...] = (
    "field_weights",
    "source_reliability",
    "auto_threshold",
    "review_threshold",
)
# 快照中除规则值外还保存的元信息（不参与变更比较）。
RULE_META_KEYS: tuple[str, ...] = (
    "rule_id",
    "rule_name",
    "rule_version",
    "normalization",
    "frozen_at",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_default_rule(db: Session) -> MergeRule:
    rule = db.query(MergeRule).filter(MergeRule.is_active.is_(True)).order_by(MergeRule.created_at.asc()).first()
    if rule is not None:
        return rule
    rule = MergeRule(
        name="康和会员主数据 v1",
        field_weights=dict(DEFAULT_FIELD_WEIGHTS),
        source_reliability=dict(DEFAULT_SOURCE_RELIABILITY),
        auto_threshold=0.92,
        review_threshold=0.75,
        normalization=dict(DEFAULT_NORMALIZATION),
        is_active=True,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def list_rules(db: Session) -> list[dict[str, Any]]:
    rows = db.query(MergeRule).order_by(MergeRule.is_active.desc(), MergeRule.created_at.asc()).all()
    return [serialize_rule(row) for row in rows]


def get_rule(db: Session, rule_id: str) -> MergeRule:
    row = db.get(MergeRule, rule_id)
    if row is None:
        raise not_found("MergeRule", rule_id)
    return row


def create_rule(db: Session, name: str, auto_threshold: float, review_threshold: float) -> dict[str, Any]:
    if review_threshold >= auto_threshold:
        raise AppError(codes.RULE_INVALID, "review 阈值必须小于 auto 阈值", "review >= auto")
    if db.query(MergeRule).filter(MergeRule.name == name).first():
        raise AppError(codes.RULE_INVALID, "规则名已存在", f"name={name}")
    row = MergeRule(
        name=name,
        field_weights=dict(DEFAULT_FIELD_WEIGHTS),
        source_reliability=dict(DEFAULT_SOURCE_RELIABILITY),
        auto_threshold=auto_threshold,
        review_threshold=review_threshold,
        normalization=dict(DEFAULT_NORMALIZATION),
        is_active=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return serialize_rule(row)


def _validate_weights(weights: dict[str, float]) -> dict[str, float]:
    allowed = set(DEFAULT_FIELD_WEIGHTS)
    if set(weights) - allowed:
        raise AppError(codes.RULE_INVALID, "包含未知字段权重", f"keys={sorted(set(weights) - allowed)}")
    cleaned: dict[str, float] = {}
    for field, value in weights.items():
        numeric = float(value)
        if numeric < 0 or numeric > 1:
            raise AppError(codes.RULE_INVALID, f"字段 {field} 权重必须在 0~1", f"value={numeric}")
        cleaned[field] = round(numeric, 4)
    if sum(cleaned.values()) <= 0:
        raise AppError(codes.RULE_INVALID, "字段权重之和必须大于 0", "sum=0")
    return cleaned


def _validate_reliability(values: dict[str, float]) -> dict[str, float]:
    allowed = {system.value for system in SourceSystem}
    if set(values) - allowed:
        raise AppError(codes.RULE_INVALID, "包含未知来源权重", f"keys={sorted(set(values) - allowed)}")
    cleaned: dict[str, float] = {}
    for source, value in values.items():
        numeric = float(value)
        if numeric < 0 or numeric > 1:
            raise AppError(codes.RULE_INVALID, f"来源 {source} 权重必须在 0~1", f"value={numeric}")
        cleaned[source] = round(numeric, 4)
    return cleaned


def update_rule(db: Session, rule_id: str, payload: RuleUpdateRequest) -> dict[str, Any]:
    row = get_rule(db, rule_id)
    if payload.field_weights is not None:
        row.field_weights = _validate_weights(payload.field_weights)
    if payload.source_reliability is not None:
        row.source_reliability = _validate_reliability(payload.source_reliability)
    if payload.auto_threshold is not None:
        row.auto_threshold = round(float(payload.auto_threshold), 4)
    if payload.review_threshold is not None:
        row.review_threshold = round(float(payload.review_threshold), 4)
    if row.review_threshold >= row.auto_threshold:
        raise AppError(codes.RULE_INVALID, "review 阈值必须小于 auto 阈值", "review >= auto")
    if payload.is_active is not None:
        if payload.is_active:
            db.query(MergeRule).filter(MergeRule.id != row.id).update({MergeRule.is_active: False})
        row.is_active = payload.is_active
    row.version += 1
    db.commit()
    db.refresh(row)
    return serialize_rule(row)


# ------------------------------------------------------------------ 规则快照 ----


def rule_values(rule: MergeRule) -> dict[str, Any]:
    """当前可变规则行的规则值（不含冻结元信息）。"""
    return {
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "rule_version": int(rule.version),
        "field_weights": dict(rule.field_weights or {}),
        "source_reliability": dict(rule.source_reliability or {}),
        "auto_threshold": float(rule.auto_threshold),
        "review_threshold": float(rule.review_threshold),
        "normalization": dict(rule.normalization or {}),
    }


def snapshot(db: Session, rule: MergeRule, *, freeze_at: datetime | None = None) -> dict[str, Any]:
    """运行规则快照，写进 ResolutionRun.params_json。

    一旦落库，这份快照就是该运行的「唯一合法规则」；后续规则升版不会改写它。
    """
    frozen_at = freeze_at or _utcnow()
    return {
        **rule_values(rule),
        "frozen_at": frozen_at.isoformat(),
    }


def has_snapshot(params: dict[str, Any] | None) -> bool:
    if not isinstance(params, dict):
        return False
    values = values_from_params(params)
    return values["field_weights"] is not None and values["auto_threshold"] is not None


def values_from_params(params: dict[str, Any] | None) -> dict[str, Any]:
    params = params or {}
    return {
        "rule_id": params.get("rule_id"),
        "rule_name": params.get("rule_name"),
        "rule_version": params.get("rule_version"),
        "field_weights": params.get("field_weights"),
        "source_reliability": params.get("source_reliability"),
        "auto_threshold": params.get("auto_threshold"),
        "review_threshold": params.get("review_threshold"),
        "normalization": params.get("normalization"),
    }


def resolve_live_rule_for_run(db: Session, run: ResolutionRun) -> MergeRule | None:
    """对照基准：优先使用该运行引用的规则集当前版本，否则用生效规则。"""
    if run.rule_id:
        rule = db.get(MergeRule, run.rule_id)
        if rule is not None:
            return rule
    return (
        db.query(MergeRule)
        .filter(MergeRule.is_active.is_(True))
        .order_by(MergeRule.created_at.asc())
        .first()
    )


def freeze_run(db: Session, run: ResolutionRun) -> dict[str, Any]:
    """取得运行冻结快照；旧运行若没有快照，则在首次执行时补冻结并落库。

    返回的字典是本次执行唯一允许使用的规则参数。
    """
    params = dict(run.params_json or {})
    if has_snapshot(params):
        return params

    rule = resolve_live_rule_for_run(db, run)
    if rule is None:
        raise AppError(codes.RULE_INVALID, "没有可用的合并规则", "no merge rule for run")
    frozen = snapshot(db, rule)
    merged = {**params, **frozen, "frozen_from": "legacy_live_rule"}
    run.params_json = merged
    db.commit()
    db.refresh(run)
    return dict(run.params_json)


def copy_frozen_params(params: dict[str, Any], *, rerun_of: str | None = None) -> dict[str, Any]:
    """重跑：原样复制上一卷的冻结快照，只补來源标记，不重新取当前规则。"""
    copied = dict(params or {})
    # 保持原 rule_version / 权重 / 阈值；仅记录血缘。
    if rerun_of:
        copied["rerun_of"] = rerun_of
    return copied


def _as_float(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def build_comparison(
    frozen_params: dict[str, Any],
    current_rule: MergeRule | None,
    *,
    snapshot_missing: bool = False,
) -> dict[str, Any]:
    """对比运行冻结版本与同一规则集的当前版本，列出每个规则值的变化。"""
    frozen = values_from_params(frozen_params)
    current = rule_values(current_rule) if current_rule is not None else None

    changes: list[dict[str, Any]] = []
    if current is not None:
        for key, label in (("auto_threshold", "auto 阈值"), ("review_threshold", "review 阈值")):
            before = _as_float(frozen.get(key))
            after = _as_float(current.get(key))
            if before != after:
                changes.append(
                    {
                        "key": key,
                        "group": "threshold",
                        "label": label,
                        "before": before,
                        "after": after,
                    }
                )

        for group, group_label, label_map in (
            ("field_weights", "字段权重", FIELD_LABELS),
            ("source_reliability", "来源可靠性", SOURCE_SYSTEM_LABELS),
        ):
            before_values = frozen.get(group) or {}
            after_values = current.get(group) or {}
            for item in sorted(set(before_values) | set(after_values)):
                before = _as_float(before_values.get(item))
                after = _as_float(after_values.get(item))
                if before != after:
                    changes.append(
                        {
                            "key": f"{group}.{item}",
                            "group": group,
                            "item": item,
                            "label": f"{group_label} · {label_map.get(item, item)}",
                            "before": before,
                            "after": after,
                        }
                    )

    version_changed = (
        frozen.get("rule_version") is not None
        and current is not None
        and int(frozen.get("rule_version") or 0) != int(current.get("rule_version") or 0)
    )
    rule_set_changed = (
        current is not None
        and frozen.get("rule_id") is not None
        and str(frozen.get("rule_id")) != str(current.get("rule_id"))
    )
    params_changed = bool(changes)

    return {
        "snapshot_missing": snapshot_missing,
        "run_rule_id": str(frozen.get("rule_id")) if frozen.get("rule_id") else None,
        "run_rule_name": frozen.get("rule_name"),
        "run_rule_version": int(frozen["rule_version"]) if frozen.get("rule_version") is not None else None,
        "current_rule_id": str(current.get("rule_id")) if current and current.get("rule_id") else None,
        "current_rule_name": current.get("rule_name") if current else None,
        "current_rule_version": (
            int(current["rule_version"]) if current and current.get("rule_version") is not None else None
        ),
        "current_available": current is not None,
        "version_changed": bool(version_changed),
        "rule_set_changed": bool(rule_set_changed),
        "params_changed": bool(params_changed),
        "rule_changed": bool(version_changed or params_changed or rule_set_changed),
        # 「受影响」只看是否真的改了规则值或换了规则集；仅版本号自增不算。
        "affected_basis": bool(params_changed or rule_set_changed),
        "changes": changes,
        "changed_keys": [change["key"] for change in changes],
        "frozen_values": frozen,
        "current_values": current,
    }


def context_for_run(db: Session, run: ResolutionRun, *, rule: MergeRule | None = None) -> dict[str, Any]:
    """读路径上的规则上下文：运行冻结值 + 同一规则集当前值 + 差异。"""
    params = dict(run.params_json or {})
    snapshot_missing = not has_snapshot(params)
    if snapshot_missing:
        # 只读回退：历史运行没有快照时，用其引用规则的当前值作为可解释基线。
        rule = rule or resolve_live_rule_for_run(db, run)
        params = {**params, **rule_values(rule)} if rule is not None else params
    else:
        rule = rule or resolve_live_rule_for_run(db, run)
    comparison = build_comparison(params, rule, snapshot_missing=snapshot_missing)
    return {
        "frozen": values_from_params(params),
        "frozen_params": params,
        "current": rule_values(rule) if rule is not None else None,
        "comparison": comparison,
    }


def contexts_for_runs(
    db: Session, runs: list[ResolutionRun]
) -> dict[str, dict[str, Any]]:
    """批量构造上下文；相同 rule_id 的当前规则只查一次。"""
    rule_cache: dict[str, MergeRule | None] = {}
    active_rule: MergeRule | None = None
    active_loaded = False
    contexts: dict[str, dict[str, Any]] = {}
    for run in runs:
        key = str(run.rule_id) if run.rule_id else ""
        if key:
            if key not in rule_cache:
                rule_cache[key] = db.get(MergeRule, run.rule_id) if run.rule_id else None
            current_rule = rule_cache[key]
        else:
            if not active_loaded:
                active_rule = current_active_rule(db)
                active_loaded = True
            current_rule = active_rule
        contexts[str(run.id)] = context_for_run(db, run, rule=current_rule)
    return contexts


def current_active_rule(db: Session) -> MergeRule | None:
    return (
        db.query(MergeRule)
        .filter(MergeRule.is_active.is_(True))
        .order_by(MergeRule.created_at.asc())
        .first()
    )

