"""规则变更影响判定：用冻结快照解释历史产物，并对照当前规则标出会变的候选 / 档案。

判定口径（只做可解释、确定性的重算，不猜测）：
  - 候选：相似度已固化在 features_json 里，分数只是「权重 × 相似度 / 可用权重」的
    纯函数，因此可用当前权重与阈值精确重算决策；判定变化 → 受影响。
  - golden record：reliability = 来源权重 × 完整度 × 时间衰减，完整度恒为 1.0，
    因此可用旧 reliability 的权重比例换算新 reliability，重排字段胜者；
    胜出值严格易主 → 受影响（并列时不误报）。
  - 只要冻结值与当前值没有任何规则值差异，一律不算受影响（版本号自增不算）。
"""
from __future__ import annotations

from typing import Any

from src.constants.enums import CandidateDecision, FIELD_LABELS

EPSILON = 1e-9


def _weight(rule_values: dict[str, Any] | None, field: str) -> float:
    weights = (rule_values or {}).get("field_weights") or {}
    try:
        return float(weights.get(field, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _thresholds(rule_values: dict[str, Any] | None) -> tuple[float, float] | None:
    try:
        return (
            float((rule_values or {}).get("auto_threshold")),
            float((rule_values or {}).get("review_threshold")),
        )
    except (TypeError, ValueError):
        return None


def _decide(score: float, auto_threshold: float, review_threshold: float) -> str:
    if score >= auto_threshold:
        return CandidateDecision.AUTO.value
    if score >= review_threshold:
        return CandidateDecision.REVIEW.value
    return CandidateDecision.REJECT.value


def rescore_candidate(
    features: dict[str, Any] | None,
    current_values: dict[str, Any] | None,
) -> tuple[float, str] | None:
    """按当前字段权重与阈值重算候选分数与判定；features 不可用时返回 None。"""
    if not isinstance(features, dict) or not features or not current_values:
        return None
    thresholds = _thresholds(current_values)
    if thresholds is None:
        return None
    auto_threshold, review_threshold = thresholds

    numerator = 0.0
    available_weight = 0.0
    available_fields = 0
    for field, detail in features.items():
        if not isinstance(detail, dict):
            continue
        similarity = detail.get("similarity")
        if similarity is None:
            continue
        try:
            similarity_value = float(similarity)
        except (TypeError, ValueError):
            continue
        available_fields += 1
        weight = _weight(current_values, field)
        numerator += weight * similarity_value
        available_weight += weight

    if available_fields == 0:
        # 与算法口径一致：没有任何可用字段时总分为 0。
        score = 0.0
    else:
        score = round(numerator / available_weight, 4) if available_weight > 0 else 0.0
    return score, _decide(score, auto_threshold, review_threshold)


def candidate_impact(
    features: dict[str, Any] | None,
    stored_score: float | None,
    stored_decision: str,
    *,
    comparison: dict[str, Any] | None,
    frozen_values: dict[str, Any] | None,
) -> dict[str, Any]:
    comparison = comparison or {}
    if not comparison.get("affected_basis"):
        return {
            "affected": False,
            "reason": "规则未变化",
            "new_decision": stored_decision,
            "new_score": stored_score,
            "score_delta": None,
        }

    rescored = rescore_candidate(features, comparison.get("current_values"))
    if rescored is None:
        # 无法精确重算（例如历史候选缺 features）：按规则已变化保守标记。
        return {
            "affected": bool(comparison.get("rule_changed")),
            "reason": "规则已变化，该候选无法精确重算",
            "new_decision": None,
            "new_score": None,
            "score_delta": None,
        }

    new_score, new_decision = rescored
    old_score = float(stored_score) if stored_score is not None else None
    delta = round(new_score - old_score, 4) if old_score is not None else None
    affected = new_decision != stored_decision
    if affected:
        reason = f"判定由 {stored_decision} 变为 {new_decision}"
    elif delta is not None and abs(delta) > EPSILON:
        reason = f"分数变化 {delta:+.4f}（判定不变）"
    else:
        reason = "规则值变化但不影响该候选"

    return {
        "affected": affected,
        "reason": reason,
        "new_decision": new_decision,
        "new_score": new_score,
        "score_delta": delta,
        "decision_changed": affected,
    }


def _reliability_options(field_detail: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    if not isinstance(field_detail, dict):
        return options
    if field_detail.get("value") is not None and field_detail.get("source_system") is not None:
        options.append(
            {
                "value": field_detail.get("value"),
                "source_system": field_detail.get("source_system"),
                "reliability": field_detail.get("reliability"),
            }
        )
    for alternative in field_detail.get("alternatives") or []:
        if not isinstance(alternative, dict):
            continue
        if alternative.get("value") is None or alternative.get("source_system") is None:
            continue
        options.append(
            {
                "value": alternative.get("value"),
                "source_system": alternative.get("source_system"),
                "reliability": alternative.get("reliability"),
            }
        )
    return options


def _source_weight(values: dict[str, Any] | None, source: str) -> float:
    reliability = (values or {}).get("source_reliability") or {}
    try:
        return float(reliability.get(source, 0.5))
    except (TypeError, ValueError):
        return 0.5


def _scale_reliability(
    reliability: float,
    source: str,
    frozen_values: dict[str, Any],
    current_values: dict[str, Any],
) -> float | None:
    """把旧 reliability 换算到当前来源权重下的等价 reliability。"""
    old_weight = _source_weight(frozen_values, source)
    new_weight = _source_weight(current_values, source)
    if old_weight <= 0:
        # 旧权重为 0 时无法从 reliability 反推原始分值；权重未变则沿用，变了则跳过该字段。
        return reliability if abs(new_weight - old_weight) <= EPSILON else None
    return reliability * (new_weight / old_weight)


def golden_impact(
    field_values: dict[str, Any] | None,
    *,
    comparison: dict[str, Any] | None,
    frozen_values: dict[str, Any] | None,
) -> dict[str, Any]:
    comparison = comparison or {}
    if not comparison.get("affected_basis"):
        return {"affected": False, "reason": "规则未变化", "changed_fields": [], "field_changes": []}

    keys = set(comparison.get("changed_keys") or [])
    if not any(key.startswith("source_reliability.") for key in keys):
        return {
            "affected": False,
            "reason": "来源可靠性未变化",
            "changed_fields": [],
            "field_changes": [],
        }

    current_values = comparison.get("current_values") or {}
    frozen_values = frozen_values or comparison.get("frozen_values") or {}

    changed_fields: list[str] = []
    field_changes: list[dict[str, Any]] = []
    for field, detail in (field_values or {}).items():
        options = _reliability_options(detail)
        if len(options) < 2:
            continue

        ranked: list[dict[str, Any]] = []
        for option in options:
            source = str(option["source_system"])
            try:
                old_reliability = float(option.get("reliability") or 0.0)
            except (TypeError, ValueError):
                old_reliability = 0.0
            # 旧 reliability 已含时间衰减；完整度恒为 1.0，按来源权重比例换算新 reliability。
            new_reliability = _scale_reliability(old_reliability, source, frozen_values, current_values)
            ranked.append(
                {
                    "value": str(option["value"]),
                    "source": source,
                    "old": old_reliability,
                    "new": new_reliability,
                }
            )
        if any(item["new"] is None for item in ranked):
            continue

        old_winner = max(ranked, key=lambda item: item["old"])
        best_new = max(item["new"] for item in ranked)
        best_items = [item for item in ranked if abs(item["new"] - best_new) <= EPSILON]
        # 并列时沿用旧胜者，避免因无法还原的 tie-break 误报。
        if any(item["value"] == old_winner["value"] for item in best_items):
            new_winner = old_winner
        else:
            new_winner = best_items[0]

        if new_winner["value"] != old_winner["value"]:
            changed_fields.append(field)
            field_changes.append(
                {
                    "field": field,
                    "label": FIELD_LABELS.get(field, field),
                    "before": old_winner["value"],
                    "after": new_winner["value"],
                }
            )

    return {
        "affected": bool(changed_fields),
        "reason": (
            f"来源可靠性变化导致 {len(changed_fields)} 个字段胜出值改变"
            if changed_fields
            else "来源可靠性变化但不改变本档案字段胜者"
        ),
        "changed_fields": changed_fields,
        "field_changes": field_changes,
    }
