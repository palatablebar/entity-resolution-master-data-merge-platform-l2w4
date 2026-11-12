"""解析运行编排：blocking → 打分 → 并查集聚类 → golden record 仲裁 → 落库。

backend 只负责创建运行并投递 Celery；worker 调用本模块真正执行计算。
seed 也复用同一函数，保证演示数据与真实运行结果同源、可复现。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.algorithms.blocking import generate_blocked_pairs, prune_pairs
from src.algorithms.clustering import (
    arbitrate_golden,
    build_clusters,
    build_histogram,
    entity_key_for,
)
from src.algorithms.scoring import score_pair
from src.algorithms.types import RecordView
from src.config.settings import settings
from src.constants import error_codes as codes
from src.constants.enums import (
    BLOCKING_KEY_LABELS,
    CandidateDecision,
    ClusterStatus,
    GoldenStatus,
    RunStatus,
)
from src.constants.limits import MAX_RECORDS_PER_RUN, SCORE_BINS
from src.models.entity_cluster import EntityCluster
from src.models.golden_record import GoldenRecord
from src.models.match_candidate import MatchCandidate
from src.models.merge_rule import MergeRule
from src.models.resolution_run import ResolutionRun
from src.models.source_record import SourceRecord
from src.services import rule_service
from src.utils.errors import AppError
from src.utils.logger import get_logger

logger = get_logger("entresolv.resolution")


def _edge_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _clear_derived(db: Session, run_id: Any) -> None:
    """删除该运行的全部派生产物（候选 / 簇 / 黄金档案）。

    失败后恢复或重复执行同一个 run 时先清空再重建，保证同一 run 的派生产物
    不会因重试而翻倍；review 痕迹只对成功的运行有意义。
    """
    db.query(GoldenRecord).filter(GoldenRecord.run_id == run_id).delete(synchronize_session=False)
    db.query(EntityCluster).filter(EntityCluster.run_id == run_id).delete(synchronize_session=False)
    db.query(MatchCandidate).filter(MatchCandidate.run_id == run_id).delete(synchronize_session=False)


def execute_run(
    db: Session,
    run: ResolutionRun,
    rule: MergeRule | None = None,
    *,
    reference_time: datetime | None = None,
) -> ResolutionRun:
    """同步执行一次运行；调用方（worker / seed）负责事务边界之外的重试与日志。

    规则参数只从 run.params_json 的冻结快照读取（rule 参数仅为旧调用签名兼容，
    不再参与计算）。这样排队期间或失败恢复期间规则升版，都不会改变本 run 的口径。
    """
    # 同一 run 已成功：直接返回，不重建派生产物（重复执行不新增候选/簇/档案）。
    if run.status == RunStatus.SUCCEEDED.value:
        logger.info("run=%s already succeeded, skip re-execution", run.id)
        return run

    try:
        frozen = rule_service.freeze_run(db, run)
        field_weights = dict(frozen["field_weights"])
        source_reliability = dict(frozen["source_reliability"])
        auto_threshold = float(frozen["auto_threshold"])
        review_threshold = float(frozen["review_threshold"])
    except AppError as exc:
        _mark_failed(db, run, exc.code, exc.message)
        raise
    except (KeyError, TypeError, ValueError) as exc:
        _mark_failed(db, run, codes.RULE_INVALID, f"运行冻结的规则参数不完整：{exc}")
        raise AppError(codes.RULE_INVALID, "运行冻结的规则参数不完整，无法执行", str(exc)) from exc

    wall_start = time.perf_counter()
    started_at = reference_time or run.started_at or datetime.now(timezone.utc)
    run.status = RunStatus.RUNNING.value
    run.started_at = started_at
    run.error_message = None
    run.error_code = None
    run.blocking_keys = list(BLOCKING_KEY_LABELS.keys())

    # 允许失败后对同一 run 重跑：先清掉上一次的派生产物
    _clear_derived(db, run.id)
    db.commit()

    try:
        records = (
            db.query(SourceRecord)
            .order_by(SourceRecord.source_system.asc(), SourceRecord.external_id.asc())
            .all()
        )
        if not records:
            raise AppError(codes.RUN_EMPTY, "没有可解析的来源记录，请先导入数据", "source_records=0")
        if len(records) > MAX_RECORDS_PER_RUN:
            raise AppError(
                codes.RUN_EMPTY,
                f"单次运行记录数超过上限 {MAX_RECORDS_PER_RUN}",
                f"records={len(records)}",
            )

        views = [RecordView.from_model(row) for row in records]
        view_by_id = {view.id: view for view in views}

        blocked_pairs, raw_count = generate_blocked_pairs(views)
        kept_pairs, pruned_count = prune_pairs(blocked_pairs, views, view_by_id)

        auto_edges: list[tuple[str, str]] = []
        review_edges: list[tuple[str, str]] = []
        edge_scores: dict[tuple[str, str], float] = {}
        stats = {"auto": 0, "review": 0, "reject": 0}
        scores: list[float] = []

        for pair in kept_pairs:
            left = view_by_id[pair.left_id]
            right = view_by_id[pair.right_id]
            scored = score_pair(left, right, field_weights, auto_threshold, review_threshold)
            score = float(scored["score"])
            decision = str(scored["decision"])
            stats[decision] = stats.get(decision, 0) + 1
            scores.append(score)
            edge_key = _edge_key(pair.left_id, pair.right_id)
            edge_scores[edge_key] = score
            if decision == CandidateDecision.AUTO.value:
                auto_edges.append(edge_key)
            elif decision == CandidateDecision.REVIEW.value:
                review_edges.append(edge_key)

            db.add(
                MatchCandidate(
                    run_id=run.id,
                    left_record_id=left.id,
                    right_record_id=right.id,
                    score=score,
                    decision=decision,
                    status="pending",
                    blocking_keys=list(pair.keys),
                    features_json={
                        "features": scored["features"],
                        "available_weight": scored["available_weight"],
                        "thresholds": scored["thresholds"],
                    },
                )
            )

        clusters = build_clusters(auto_edges, review_edges, edge_scores)
        golden_count = 0
        field_names = list(field_weights.keys())

        for edge_type, groups in (("auto", clusters["auto"]), ("review", clusters["review"])):
            for group in groups:
                members: list[str] = group["members"]
                status = (
                    ClusterStatus.AUTO_MERGED.value
                    if edge_type == "auto"
                    else ClusterStatus.PENDING_REVIEW.value
                )
                cluster = EntityCluster(
                    run_id=run.id,
                    cluster_key=group["cluster_key"],
                    member_ids=members,
                    member_keys=[view_by_id[mid].display_key() for mid in members],
                    record_count=len(members),
                    confidence=group["confidence"],
                    status=status,
                    edge_type=edge_type,
                )
                db.add(cluster)
                db.flush()

                arbitration = arbitrate_golden(
                    members=members,
                    records_by_id=view_by_id,
                    source_reliability=source_reliability,
                    fields=field_names,
                    reference_time=started_at or datetime.now(),
                )
                db.add(
                    GoldenRecord(
                        run_id=run.id,
                        cluster_id=cluster.id,
                        entity_key=entity_key_for(members),
                        field_values=arbitration["field_values"],
                        conflict_fields=arbitration["conflict_fields"],
                        confidence=group["confidence"],
                        status=(
                            GoldenStatus.ACTIVE.value
                            if edge_type == "auto"
                            else GoldenStatus.PROVISIONAL.value
                        ),
                    )
                )
                golden_count += 1

        wall_duration = int((time.perf_counter() - wall_start) * 1000)
        run.records_scanned = len(views)
        run.blocked_pairs_raw = raw_count
        run.pruned_pairs = pruned_count
        run.candidate_pairs = len(kept_pairs)
        run.auto_pairs = stats.get("auto", 0)
        run.review_pairs = stats.get("review", 0)
        run.rejected_pairs = stats.get("reject", 0)
        run.clusters_auto = len(clusters["auto"])
        run.clusters_review = len(clusters["review"])
        run.golden_records = golden_count
        run.duration_ms = wall_duration
        run.distribution = build_histogram(scores, SCORE_BINS)
        run.status = RunStatus.SUCCEEDED.value
        run.finished_at = started_at + timedelta(milliseconds=wall_duration)
        db.commit()
        db.refresh(run)
        logger.info(
            "run=%s status=succeeded records=%s candidates=%s auto=%s review=%s clusters=%s/%s golden=%s ms=%s",
            run.id,
            run.records_scanned,
            run.candidate_pairs,
            run.auto_pairs,
            run.review_pairs,
            run.clusters_auto,
            run.clusters_review,
            run.golden_records,
            run.duration_ms,
        )
        return run
    except AppError as exc:
        _mark_failed(db, run, exc.code, exc.message)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("run failed run=%s error=%s", run.id, exc, exc_info=True)
        _mark_failed(db, run, codes.INTERNAL_ERROR, str(exc))
        raise


def _mark_failed(db: Session, run: ResolutionRun, code: str, message: str) -> None:
    # 丢弃本次执行未提交的半成品，避免失败运行留下不完整的候选/簇/档案。
    run_id = run.id
    db.rollback()
    row = db.get(ResolutionRun, run_id)
    if row is None:
        return
    row.status = RunStatus.FAILED.value
    row.error_code = code
    row.error_message = message[:500]
    row.finished_at = datetime.now(timezone.utc)
    db.commit()


def run_summary(run: ResolutionRun) -> dict[str, Any]:
    return {
        "records_scanned": run.records_scanned,
        "blocked_pairs_raw": run.blocked_pairs_raw,
        "pruned_pairs": run.pruned_pairs,
        "candidate_pairs": run.candidate_pairs,
        "auto_pairs": run.auto_pairs,
        "review_pairs": run.review_pairs,
        "rejected_pairs": run.rejected_pairs,
        "clusters_auto": run.clusters_auto,
        "clusters_review": run.clusters_review,
        "golden_records": run.golden_records,
        "duration_ms": run.duration_ms,
    }


def default_rule(db: Session) -> MergeRule | None:
    return (
        db.query(MergeRule)
        .filter(MergeRule.is_active.is_(True))
        .order_by(MergeRule.created_at.asc())
        .first()
    )


def settings_defaults() -> dict[str, float]:
    return {"auto": settings.auto_threshold, "review": settings.review_threshold}
