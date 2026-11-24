"""投放复盘接口：归因权重有效归属、领用、更正、核对与月末导出。"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.domain.attribution_ledger import (
    GROUPS,
    KINDS,
    ledger,
    normalize_columns,
    normalize_group,
    normalize_kind,
)

router = APIRouter(prefix="/api/attribution-review", tags=["投放复盘"])


class ClaimPayload(BaseModel):
    order_id: str
    group: str
    claimed_at: str | None = None
    claim_id: str | None = None


class RestoreClaimPayload(BaseModel):
    claim_id: str
    operated_at: str | None = None


class CorrectionPayload(BaseModel):
    effective_at: str
    correction_id: str | None = None
    operated_at: str | None = None
    reason: str | None = None
    channel: str | None = None


class ProcessPendingPayload(BaseModel):
    group: str | None = None
    order_id: str | None = None
    operated_at: str | None = None


class ResetPayload(BaseModel):
    channel: str | None = None


def _csv_text(rows: list[dict[str, Any]], columns: list[str]) -> str:
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        values = [row.get(column, "") for column in columns]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return output.getvalue()


def _export_filename(group: str, kind: str) -> str:
    return f"{ledger.channel}-{group}-{kind}.csv"


def _invalid_group(group: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=f"组别 {group} 不是本期有效归属组，可选：{'、'.join(GROUPS)}",
    )


def _check_group(group: str | None) -> None:
    if group and normalize_group(group) is None:
        raise _invalid_group(group)


def _selected_columns(raw: str | None) -> list[str]:
    try:
        return normalize_columns(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("")
@router.get("/overview")
def overview(
    channel: str = Query(default="C1"),
    group: str | None = Query(default=None),
) -> dict[str, Any]:
    if str(channel).strip().upper() != ledger.channel:
        raise HTTPException(status_code=404, detail=f"渠道 {channel} 暂无有效归属明细")
    payload = ledger.overview()
    if group:
        wanted = normalize_group(group)
        if wanted is None:
            raise _invalid_group(group)
        payload["groups"] = [row for row in payload["groups"] if row["group"] == wanted]
        payload["orders"] = [row for row in payload["orders"] if row["current_group"] == wanted]
        payload["claims"] = [row for row in payload["claims"] if row["group"] == wanted]
    return payload


@router.get("/group-detail")
def group_detail(group: str | None = Query(default=None)) -> dict[str, Any]:
    _check_group(group)
    rows = ledger.group_detail()
    if group:
        wanted = normalize_group(group)
        rows = [row for row in rows if row["group"] == wanted]
    return {"items": rows, "total": len(rows), "groups": list(GROUPS)}


@router.get("/orders")
def orders(group: str | None = Query(default=None)) -> dict[str, Any]:
    _check_group(group)
    rows = ledger.order_detail(group)
    return {"items": rows, "total": len(rows), "groups": list(GROUPS)}


@router.get("/claims")
def claims(group: str | None = Query(default=None)) -> dict[str, Any]:
    _check_group(group)
    rows = ledger.claims(group)
    return {"items": rows, "total": len(rows)}


@router.post("/claims")
def claim(payload: ClaimPayload) -> dict[str, Any]:
    if normalize_group(payload.group) is None:
        raise _invalid_group(payload.group)
    result = ledger.claim(
        payload.order_id, payload.group,
        claimed_at=payload.claimed_at, claim_id=payload.claim_id,
    )
    if not result.get("ok"):
        status = 404 if "不存在" in result.get("message", "") else 403
        raise HTTPException(status_code=status, detail=result)
    return result


@router.post("/claims/restore")
def restore_claim(payload: RestoreClaimPayload) -> dict[str, Any]:
    result = ledger.restore_claim(payload.claim_id, operated_at=payload.operated_at)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/corrections")
def corrections() -> dict[str, Any]:
    overview_payload = ledger.overview()
    return {
        "items": overview_payload["handover"]["corrections"],
        "total": len(ledger.corrections),
        "handover": overview_payload["handover"],
    }


@router.post("/corrections")
def correct(payload: CorrectionPayload) -> dict[str, Any]:
    result = ledger.correct_handover(
        payload.effective_at,
        correction_id=payload.correction_id,
        operated_at=payload.operated_at,
        reason=payload.reason or "",
        channel=payload.channel,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/pending/process")
def process_pending(payload: ProcessPendingPayload) -> dict[str, Any]:
    _check_group(payload.group)
    return ledger.process_pending(
        group=payload.group, order_id=payload.order_id, operated_at=payload.operated_at,
    )


@router.get("/pending")
def pending(group: str | None = Query(default=None)) -> dict[str, Any]:
    _check_group(group)
    summary = ledger.pending_summary()
    if group:
        wanted = normalize_group(group)
        summary = {
            "by_group": {wanted: summary["by_group"][wanted]},
            "groups": {wanted: summary["groups"][wanted]},
            "has_pending": summary["groups"][wanted]["has_pending"],
        }
    return summary


@router.get("/reconciliation")
def reconciliation(group: str | None = Query(default=None)) -> dict[str, Any]:
    _check_group(group)
    payload = ledger.reconciliation()
    if group:
        wanted = normalize_group(group)
        payload["groups"] = [row for row in payload["groups"] if row["group"] == wanted]
        payload["orders"] = [row for row in payload["orders"] if row["current_group"] == wanted]
    return payload


@router.get("/orders/{order_id}")
def order_detail(order_id: str) -> dict[str, Any]:
    result = ledger.current_owner(order_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/export")
def export(
    group: str = Query(...),
    kind: str | None = Query(default=None),
    columns: str | None = Query(default=None),
    format: str = Query(default="csv"),
) -> Response:
    """月末导出：按有效组、核对类型生成核对文件，按转化时间排序。

    不传 kind 时一次生成「已确认分摊 / 冲减 / 补记」三份 CSV 并打包，
    也可以 format=json 拿到同一批行，便于逐单对平。
    """
    wanted = normalize_group(group)
    if wanted is None:
        raise _invalid_group(group)
    selected_columns = _selected_columns(columns)

    if kind:
        canonical_kind = normalize_kind(kind)
        if canonical_kind is None:
            raise HTTPException(
                status_code=400,
                detail=f"核对类型 {kind} 不存在，可选：{'、'.join(KINDS)}",
            )
        rows = ledger.export_rows(wanted, canonical_kind, selected_columns)
        if format == "json":
            return Response(
                content=json.dumps(
                    {"group": wanted, "kind": canonical_kind, "columns": selected_columns,
                     "items": rows, "total": len(rows)},
                    ensure_ascii=False,
                ),
                media_type="application/json",
            )
        filename = _export_filename(wanted, canonical_kind)
        return Response(
            content=_csv_text(rows, selected_columns),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

    archive = io.BytesIO()
    bundle: dict[str, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for canonical_kind in KINDS:
            rows = ledger.export_rows(wanted, canonical_kind, selected_columns)
            bundle[canonical_kind] = rows
            package.writestr(_export_filename(wanted, canonical_kind),
                             _csv_text(rows, selected_columns))
    archive.seek(0)
    if format == "json":
        return Response(
            content=json.dumps(
                {"group": wanted, "columns": selected_columns,
                 "files": {item: _export_filename(wanted, item) for item in KINDS},
                 "items": bundle},
                ensure_ascii=False,
            ),
            media_type="application/json",
        )
    filename = f"{ledger.channel}-{wanted}-月末核对.zip"
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/reset")
def reset(payload: ResetPayload | None = None) -> dict[str, Any]:
    if payload and payload.channel and str(payload.channel).strip().upper() != ledger.channel:
        raise HTTPException(status_code=404, detail=f"渠道 {payload.channel} 暂无有效归属明细")
    return {"ok": True, "message": "投放复盘账本已恢复为初始示例", "overview": ledger.reset()}
