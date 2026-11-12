"""首页/导航概览统计。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.constants.enums import (
    BLOCKING_KEY_LABELS,
    CandidateDecision,
    CandidateStatus,
    ClusterStatus,
    FIELD_LABELS,
    GoldenStatus,
    RunStatus,
    SOURCE_SYSTEM_LABELS,
)
from src.constants.roles import PERMISSIONS, ROLE_LABELS
from src.constants.weights import TIME_DECAY_HALF_LIFE_DAYS
from src.models.entity_cluster import EntityCluster
from src.models.golden_record import GoldenRecord
from src.models.match_candidate import MatchCandidate
from src.models.merge_rule import MergeRule
from src.models.resolution_run import ResolutionRun
from src.models.source_record import SourceRecord
from src.schemas.rules import serialize_rule
from src.schemas.runs import serialize_run
from src.services import rule_service
from src.services.run_service import latest_succeeded_run


def overview(db: Session) -> dict[str, Any]:
    latest = latest_succeeded_run(db)
    pending_query = db.query(func.count(MatchCandidate.id)).filter(
        MatchCandidate.status == CandidateStatus.PENDING.value,
        MatchCandidate.decision == CandidateDecision.REVIEW.value,
    )
    if latest is not None:
        pending_query = pending_query.filter(MatchCandidate.run_id == latest.id)
    pending_candidates = pending_query.scalar() or 0
    counts = {
        "source_records": db.query(func.count(SourceRecord.id)).scalar() or 0,
        "match_candidates": db.query(func.count(MatchCandidate.id)).scalar() or 0,
        "pending_reviews": pending_candidates,
        "clusters": db.query(func.count(EntityCluster.id)).scalar() or 0,
        "golden_records": db.query(func.count(GoldenRecord.id)).scalar() or 0,
        "merge_rules": db.query(func.count(MergeRule.id)).scalar() or 0,
        "resolution_runs": db.query(func.count(ResolutionRun.id)).scalar() or 0,
    }
    latest_payload = None
    if latest is not None:
        latest_payload = serialize_run(latest)
        latest_payload["rule_context"] = rule_service.context_for_run(db, latest)
    active_rule = rule_service.current_active_rule(db)
    return {
        "counts": counts,
        "latest_run": latest_payload,
        "active_rule": serialize_rule(active_rule) if active_rule is not None else None,
        "labels": {
            "source_system": SOURCE_SYSTEM_LABELS,
            "blocking_keys": BLOCKING_KEY_LABELS,
            "fields": FIELD_LABELS,
            "roles": ROLE_LABELS,
        },
        "enums": {
            "run_status": [status.value for status in RunStatus],
            "candidate_decision": [value.value for value in CandidateDecision],
            "candidate_status": [value.value for value in CandidateStatus],
            "cluster_status": [value.value for value in ClusterStatus],
            "golden_status": [value.value for value in GoldenStatus],
        },
        "roles": {role: list(actions) for role, actions in PERMISSIONS.items()},
        "time_decay_half_life_days": TIME_DECAY_HALF_LIFE_DAYS,
    }
