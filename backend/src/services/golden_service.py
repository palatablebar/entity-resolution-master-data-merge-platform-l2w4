"""golden record 服务：列表、详情、CSV 导出。"""
from __future__ import annotations

import csv
import io
import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.constants.enums import FIELD_LABELS
from src.models.entity_cluster import EntityCluster
from src.models.golden_record import GoldenRecord
from src.models.resolution_run import ResolutionRun
from src.schemas.golden import serialize_golden
from src.services import rule_impact, rule_service
from src.utils.errors import not_found
from src.utils.pagination import normalize_page

EXPORT_FIELDS = ("name", "phone", "email", "address")
EXPORT_HEADERS = ["entity_key", "置信度", "状态", "姓名", "手机", "邮箱", "地址", "冲突字段", "来源引用"]


def list_golden_records(
    db: Session,
    *,
    run_id: str | None,
    query: str | None,
    status: str | None,
    cluster_id: str | None,
    page: int,
    page_size: int,
    affected_only: bool = False,
) -> dict[str, Any]:
    safe_page, safe_size = normalize_page(page, page_size)
    base = db.query(GoldenRecord)
    if run_id:
        base = base.filter(GoldenRecord.run_id == run_id)
    if cluster_id:
        base = base.filter(GoldenRecord.cluster_id == cluster_id)
    if status:
        base = base.filter(GoldenRecord.status == status)
    if query:
        pattern = f"%{query.strip()}%"
        base = base.filter(GoldenRecord.entity_key.ilike(pattern))
    total = base.with_entities(func.count(GoldenRecord.id)).scalar() or 0
    if affected_only and run_id:
        # 「规则变化后受影响」需要逐份重算，先取本 run 的档案再在内存里过滤分页。
        all_rows = base.order_by(GoldenRecord.confidence.desc(), GoldenRecord.entity_key.asc()).all()
        contexts = _rule_contexts(db, all_rows)
        items: list[dict[str, Any]] = []
        for row in all_rows:
            payload = serialize_golden(row)
            payload["display"] = {
                field: (row.field_values.get(field) or {}).get("value") for field in EXPORT_FIELDS
            }
            _attach_impact(payload, row, contexts.get(str(row.run_id)))
            if payload["rule_impact"].get("affected"):
                items.append(payload)
        total = len(items)
        offset = (safe_page - 1) * safe_size
        return {
            "items": items[offset : offset + safe_size],
            "page": safe_page,
            "page_size": safe_size,
            "total": total,
            "total_pages": max(1, -(-total // safe_size)) if total else 1,
        }

    rows = (
        base.order_by(GoldenRecord.confidence.desc(), GoldenRecord.entity_key.asc())
        .offset((safe_page - 1) * safe_size)
        .limit(safe_size)
        .all()
    )
    items = []
    contexts = _rule_contexts(db, rows)
    for row in rows:
        payload = serialize_golden(row)
        payload["display"] = {
            field: (row.field_values.get(field) or {}).get("value") for field in EXPORT_FIELDS
        }
        _attach_impact(payload, row, contexts.get(str(row.run_id)))
        items.append(payload)
    return {
        "items": items,
        "page": safe_page,
        "page_size": safe_size,
        "total": total,
        "total_pages": max(1, -(-total // safe_size)) if total else 1,
    }


def _rule_contexts(db: Session, rows: list[GoldenRecord]) -> dict[str, dict[str, Any]]:
    if not rows:
        return {}
    runs = (
        db.query(ResolutionRun)
        .filter(ResolutionRun.id.in_({row.run_id for row in rows}))
        .all()
    )
    return rule_service.contexts_for_runs(db, runs)


def _attach_impact(payload: dict[str, Any], row: GoldenRecord, context: dict[str, Any] | None) -> None:
    context = context or {}
    frozen = (context.get("comparison") or {}).get("frozen_values") or {}
    payload["rule_context"] = context
    payload["rule_impact"] = rule_impact.golden_impact(
        row.field_values,
        comparison=context.get("comparison"),
        frozen_values=frozen,
    )


def get_golden(db: Session, golden_id: str) -> dict[str, Any]:
    row = db.get(GoldenRecord, golden_id)
    if row is None:
        raise not_found("GoldenRecord", golden_id)
    cluster = db.get(EntityCluster, row.cluster_id)
    payload = serialize_golden(row, cluster=cluster)
    run = db.get(ResolutionRun, row.run_id)
    context = rule_service.context_for_run(db, run) if run is not None else None
    _attach_impact(payload, row, context)
    return payload


def export_csv(db: Session, run_id: str) -> tuple[str, str]:
    """返回 (CSV 文本, 文件名)。字段口径固定，供营销系统直接导入。"""
    rows = (
        db.query(GoldenRecord)
        .filter(GoldenRecord.run_id == run_id)
        .order_by(GoldenRecord.entity_key.asc())
        .all()
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_HEADERS)
    for row in rows:
        values = row.field_values or {}
        references = []
        for field in EXPORT_FIELDS:
            detail = values.get(field) or {}
            if detail.get("source_system"):
                references.append(f"{FIELD_LABELS.get(field, field)}:{detail.get('source_system')}")
        writer.writerow(
            [
                row.entity_key,
                f"{row.confidence:.4f}",
                row.status,
                (values.get("name") or {}).get("value") or "",
                (values.get("phone") or {}).get("value") or "",
                (values.get("email") or {}).get("value") or "",
                (values.get("address") or {}).get("value") or "",
                "|".join(row.conflict_fields or []),
                "|".join(references),
            ]
        )
    filename = f"golden_records_{str(run_id)[:8]}.csv"
    return buffer.getvalue(), filename
