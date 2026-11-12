"""规则版本冻结与影响标注的集成测试。

运行方式（需要 PostgreSQL；用例会建独立 schema 并清理）：
    cd backend && python -m pytest tests/test_rule_freezing.py -q

覆盖：
  1. 运行创建后改规则，worker 执行仍用创建瞬间的冻结版本；
  2. 失败后重试同一 run，沿用原冻结版本且派生产物不累加；
  3. 重跑沿用被重跑 run 的冻结版本，而不是当前规则；
  4. 页面读路径能给出「冻结版本 ↔ 当前版本」对照与受影响的候选 / 黄金档案。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "5432")

from src.models import Base  # noqa: E402
from src.models.entity_cluster import EntityCluster  # noqa: E402
from src.models.golden_record import GoldenRecord  # noqa: E402
from src.models.match_candidate import MatchCandidate  # noqa: E402
from src.models.merge_rule import MergeRule  # noqa: E402
from src.models.resolution_run import ResolutionRun  # noqa: E402
from src.models.source_record import SourceRecord  # noqa: E402
from src.models.user import User  # noqa: E402
from src.constants.enums import RunStatus  # noqa: E402
from src.constants.weights import DEFAULT_FIELD_WEIGHTS, DEFAULT_SOURCE_RELIABILITY  # noqa: E402
from src.services import candidate_service, golden_service, resolution_service, rule_service, run_service  # noqa: E402
from src.utils.normalize import (  # noqa: E402
    DEFAULT_NORMALIZATION,
    normalize_address,
    normalize_city,
    normalize_email,
    normalize_name,
    normalize_phone,
)

DSN = (
    f"postgresql+psycopg://{os.environ.get('DB_USER', 'entresolv')}:"
    f"{os.environ.get('DB_PASSWORD', 'entresolv_pw_2026')}@"
    f"{os.environ.get('DB_HOST', '127.0.0.1')}:{os.environ.get('DB_PORT', '5432')}/"
    f"{os.environ.get('DB_NAME', 'entresolv')}"
)

SCHEMA = f"test_rule_freeze_{uuid.uuid4().hex[:8]}"
REFERENCE = datetime(2026, 11, 9, 2, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(DSN, future=True)
    with eng.connect() as conn:
        try:
            conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - 环境未提供 PG 时跳过
            pytest.skip(f"PostgreSQL 不可用：{exc}")
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        conn.execute(text(f"SET search_path TO {SCHEMA}"))
        conn.commit()
    with eng.connect() as conn:
        conn.execute(text(f"SET search_path TO {SCHEMA}"))
        Base.metadata.create_all(bind=conn)
        conn.commit()
    yield eng
    with eng.connect() as conn:
        conn.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
        conn.commit()
    eng.dispose()


@pytest.fixture()
def db(engine):
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {SCHEMA}"))
        conn.commit()
    session = SessionLocal()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    # 每个用例清空业务表
    for model in (GoldenRecord, EntityCluster, MatchCandidate, ResolutionRun, MergeRule, SourceRecord, User):
        session.query(model).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _rule(db: Session, *, name="测试规则", version=1, auto=0.92, review=0.75, weights=None, reliability=None) -> MergeRule:
    rule = MergeRule(
        name=name,
        field_weights=dict(weights or DEFAULT_FIELD_WEIGHTS),
        source_reliability=dict(reliability or DEFAULT_SOURCE_RELIABILITY),
        auto_threshold=auto,
        review_threshold=review,
        normalization=dict(DEFAULT_NORMALIZATION),
        version=version,
        is_active=True,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def _records(db: Session, count: int = 3) -> list[SourceRecord]:
    """同一会员的三条跨系统记录（姓名/手机/邮箱一致，地址轻微差异）。"""
    rows = []
    for index, source in enumerate(("pos", "ecommerce", "service")):
        address = "成都市武侯区人民南路四段100号1栋1502室"
        if source == "ecommerce":
            address = "成都市 武侯区 人民南路四段100号 1栋 1502室"
        if source == "service":
            address = "成都市武侯区人民南路四段100号"
        row = SourceRecord(
            source_system=source,
            external_id=f"{source}-{index:02d}",
            full_name="张伟",
            phone="+86 138-0000-0001" if source == "ecommerce" else "13800000001",
            email="member001@163.com",
            address=address,
            city="成都市",
            normalized_name=normalize_name("张伟"),
            normalized_phone=normalize_phone("13800000001"),
            normalized_email=normalize_email("member001@163.com"),
            normalized_address=normalize_address(address),
            normalized_city=normalize_city("成都市"),
            source_updated_at=REFERENCE - timedelta(days=index * 3),
            raw_payload={},
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def _create_run(db: Session, rule: MergeRule, user: User, label="冻结测试") -> ResolutionRun:
    run = ResolutionRun(
        rule_id=rule.id,
        run_label=label,
        status=RunStatus.QUEUED.value,
        params_json=rule_service.snapshot(db, rule),
        blocking_keys=["phone_suffix", "name_city", "email_domain"],
        triggered_by=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _user(db: Session) -> User:
    user = User(
        email=f"tester-{uuid.uuid4().hex[:6]}@master.local",
        password_hash="x",
        display_name="测试管理员",
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _counts(db: Session, run_id) -> tuple[int, int, int]:
    return (
        db.query(MatchCandidate).filter(MatchCandidate.run_id == run_id).count(),
        db.query(EntityCluster).filter(EntityCluster.run_id == run_id).count(),
        db.query(GoldenRecord).filter(GoldenRecord.run_id == run_id).count(),
    )


# ---------------------------------------------------------------- 1. 排队期间改规则 ----


def test_queued_run_keeps_creation_snapshot(db: Session) -> None:
    user = _user(db)
    _records(db)
    rule = _rule(db, auto=0.75, review=0.6)
    run = _create_run(db, rule, user)
    frozen_before = dict(run.params_json)

    # 运行排队期间规则被改（升版 + 改阈值 + 改权重）
    rule.auto_threshold = 0.99
    rule.review_threshold = 0.88
    rule.field_weights = {"name": 0.1, "phone": 0.1, "email": 0.1, "address": 0.1}
    rule.version = 2
    db.commit()

    resolution_service.execute_run(db, run)
    db.refresh(run)

    assert run.params_json == frozen_before
    assert run.params_json["auto_threshold"] == 0.75
    assert run.params_json["review_threshold"] == 0.6
    # 若误用新规则（0.99/0.88），这条高度相似的三源记录不会有 auto 边。
    assert run.auto_pairs >= 1
    candidate = db.query(MatchCandidate).filter(MatchCandidate.run_id == run.id).first()
    assert candidate.features_json["thresholds"] == {"auto": 0.75, "review": 0.6}


# ---------------------------------------------------------------- 2. 失败后恢复 ----


def test_retry_after_failure_uses_same_snapshot_without_duplicates(db: Session, monkeypatch) -> None:
    user = _user(db)
    _records(db)
    rule = _rule(db, auto=0.75, review=0.6)
    run = _create_run(db, rule, user)

    rule.auto_threshold = 0.99
    rule.version = 2
    db.commit()

    # 让第一次执行在写入派生产物后失败
    original_build = resolution_service.build_clusters

    def boom(*args, **kwargs):
        raise RuntimeError("模拟中断")

    monkeypatch.setattr(resolution_service, "build_clusters", boom)
    with pytest.raises(RuntimeError):
        resolution_service.execute_run(db, run)
    db.refresh(run)
    assert run.status == RunStatus.FAILED.value
    assert _counts(db, run.id) == (0, 0, 0)  # 失败不留半成品

    monkeypatch.setattr(resolution_service, "build_clusters", original_build)
    resolution_service.execute_run(db, run)
    db.refresh(run)

    assert run.status == RunStatus.SUCCEEDED.value
    assert run.params_json["auto_threshold"] == 0.75  # 恢复仍用原冻结版本
    assert run.auto_pairs >= 1
    candidate_count, cluster_count, golden_count = _counts(db, run.id)
    assert candidate_count == run.candidate_pairs
    assert cluster_count == run.clusters_auto + run.clusters_review
    assert golden_count == run.golden_records

    # 重复执行同一 run：不得新增候选 / 簇 / 档案
    before = _counts(db, run.id)
    resolution_service.execute_run(db, run)
    db.refresh(run)
    assert _counts(db, run.id) == before


# ---------------------------------------------------------------- 3. 重跑继承版本 ----


def test_rerun_inherits_frozen_snapshot_not_current_rule(db: Session, monkeypatch) -> None:
    user = _user(db)
    _records(db)
    rule = _rule(db, auto=0.75, review=0.6)
    original = _create_run(db, rule, user, label="原始运行")
    expected = dict(original.params_json)

    rule.auto_threshold = 0.99
    rule.review_threshold = 0.88
    rule.version = 3
    db.commit()

    # 原始运行已完成，不构成「排队中」冲突；同时把 rerun 的投递替换为无副作用。
    original.status = RunStatus.SUCCEEDED.value
    db.commit()
    monkeypatch.setattr(run_service, "_dispatch", lambda db, run: None)
    payload = run_service.rerun(db, str(original.id), user=user, ip=None, trace_id=None)
    # 避免真的投递 Celery：直接读库断言
    rerun_row = db.get(ResolutionRun, payload["id"])
    assert rerun_row.params_json["auto_threshold"] == expected["auto_threshold"]
    assert rerun_row.params_json["review_threshold"] == expected["review_threshold"]
    assert rerun_row.params_json["rule_version"] == expected["rule_version"]
    assert rerun_row.params_json["rerun_of"] == str(original.id)

    resolution_service.execute_run(db, rerun_row)
    db.refresh(rerun_row)
    assert rerun_row.auto_pairs >= 1
    candidate = db.query(MatchCandidate).filter(MatchCandidate.run_id == rerun_row.id).first()
    assert candidate.features_json["thresholds"] == {"auto": 0.75, "review": 0.6}


# ---------------------------------------------------------------- 4. 版本对照与影响 ----


def test_views_compare_versions_and_flag_affected_artifacts(db: Session) -> None:
    user = _user(db)
    _records(db)
    rule = _rule(db, auto=0.75, review=0.6)
    run = _create_run(db, rule, user, label="版本对照运行")
    resolution_service.execute_run(db, run)
    db.refresh(run)

    # 初始：规则未变化，不应标受影响
    context = rule_service.context_for_run(db, run)
    assert context["comparison"]["affected_basis"] is False
    assert context["comparison"]["run_rule_version"] == 1

    # 只升版本、不改值 → 版本变化但不受影响
    rule.version = 2
    db.commit()
    context = rule_service.context_for_run(db, run)
    assert context["comparison"]["version_changed"] is True
    assert context["comparison"]["affected_basis"] is False

    # 收紧 auto 阈值 → 候选判定应受影响
    rule.auto_threshold = 0.99
    rule.version = 3
    db.commit()
    context = rule_service.context_for_run(db, run)
    assert context["comparison"]["affected_basis"] is True
    summary = run_service.summarize_impact(db, run, context)
    assert summary["candidates_affected"] >= 1
    assert summary["candidates_total"] == run.candidate_pairs

    candidates = candidate_service.list_candidates(
        db, run_id=str(run.id), decision=None, status=None, page=1, page_size=100, affected_only=True
    )
    assert candidates["total"] >= 1
    for item in candidates["items"]:
        assert item["rule_impact"]["affected"] is True
        assert item["rule_context"]["comparison"]["run_rule_version"] == 1
        assert item["rule_context"]["comparison"]["current_rule_version"] == 3

    # 改动来源可靠性并让电商权重反超 POS → 黄金档案字段胜者受影响
    rule.auto_threshold = 0.75
    rule.source_reliability = {"pos": 0.2, "ecommerce": 0.95, "service": 0.1}
    rule.version = 4
    db.commit()
    golden = golden_service.list_golden_records(
        db, run_id=str(run.id), query=None, status=None, cluster_id=None, page=1, page_size=100, affected_only=True
    )
    assert golden["total"] >= 1
    assert any(item["rule_impact"]["changed_fields"] for item in golden["items"])


def test_retry_failed_run_in_place_keeps_snapshot(db: Session, monkeypatch) -> None:
    """失败恢复：就地重投同一 run，仍用原冻结快照；重复恢复不新增派生产物。"""
    user = _user(db)
    _records(db)
    rule = _rule(db, auto=0.75, review=0.6)
    run = _create_run(db, rule, user, label="失败恢复")
    expected_threshold = run.params_json["auto_threshold"]

    rule.auto_threshold = 0.99
    rule.version = 2
    db.commit()

    # 模拟一次失败
    run.status = RunStatus.FAILED.value
    run.error_code = "INTERNAL_ERROR"
    run.error_message = "模拟失败"
    db.commit()

    monkeypatch.setattr(run_service, "_dispatch", lambda db, run: None)
    payload = run_service.retry_failed(db, str(run.id), user=user, ip=None, trace_id=None)
    row = db.get(ResolutionRun, payload["id"])
    assert row.id == run.id  # 就地恢复，不新建卷
    assert row.status == RunStatus.QUEUED.value
    assert row.params_json["auto_threshold"] == expected_threshold

    resolution_service.execute_run(db, row)
    db.refresh(row)
    assert row.status == RunStatus.SUCCEEDED.value
    assert row.auto_pairs >= 1
    first_counts = _counts(db, row.id)

    # 再恢复一次（先人为改成失败），产物数量不累加
    row.status = RunStatus.FAILED.value
    db.commit()
    run_service.retry_failed(db, str(row.id), user=user, ip=None, trace_id=None)
    resolution_service.execute_run(db, row)
    db.refresh(row)
    assert _counts(db, row.id) == first_counts


def test_legacy_run_without_snapshot_freezes_on_first_execution(db: Session) -> None:
    user = _user(db)
    _records(db)
    rule = _rule(db, auto=0.75, review=0.6)
    run = ResolutionRun(
        rule_id=rule.id,
        run_label="旧运行",
        status=RunStatus.QUEUED.value,
        params_json={"rule_id": str(rule.id), "rule_name": rule.name, "rule_version": 1},
        blocking_keys=["phone_suffix"],
        triggered_by=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    rule.auto_threshold = 0.999
    db.commit()

    resolution_service.execute_run(db, run)
    db.refresh(run)
    assert run.status == RunStatus.SUCCEEDED.value
    assert run.params_json["frozen_from"] == "legacy_live_rule"
    assert run.params_json["auto_threshold"] == 0.999  # 首次执行时补冻结

    # 之后的规则变化不再影响该运行
    rule.auto_threshold = 0.7
    db.commit()
    context = rule_service.context_for_run(db, run)
    assert context["comparison"]["affected_basis"] is True  # 只作对照展示
    assert run.params_json["auto_threshold"] == 0.999
