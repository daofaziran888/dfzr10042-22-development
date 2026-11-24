"""投放复盘接口契约测试：直接调用路由函数，覆盖导出与权限返回。"""
from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest
from fastapi import HTTPException

from app.domain.attribution_ledger import GROUP_A, GROUP_B, ledger
from app.routers import attribution


@pytest.fixture(autouse=True)
def fresh_ledger():
    ledger.reset()
    yield
    ledger.reset()


def parse_csv(text: str) -> list[list[str]]:
    # 去掉 BOM 再解析，顺便验证导出文件以 UTF-8 BOM 开头，Excel 不乱码。
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


class TestReviewApi:
    def test_overview_reports_reconciled_totals(self):
        payload = attribution.overview(channel="C1", group=None)
        assert payload["totals"]["本期合计"] == 200.0
        assert payload["totals"]["旧渠道触点合计"] == 200.0
        assert payload["totals"]["对平"] is True

    def test_group_filter_returns_only_that_group(self):
        payload = attribution.overview(channel="C1", group="乙组")
        assert [row["group"] for row in payload["groups"]] == [GROUP_B]
        assert all(row["current_group"] == GROUP_B for row in payload["orders"])

    def test_unknown_channel_is_404(self):
        with pytest.raises(HTTPException) as exc:
            attribution.overview(channel="C9", group=None)
        assert exc.value.status_code == 404

    def test_unknown_group_is_400(self):
        with pytest.raises(HTTPException) as exc:
            attribution.orders(group="丙组")
        assert exc.value.status_code == 400

    def test_correction_then_claim_returns_403_for_old_group(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        with pytest.raises(HTTPException) as exc:
            attribution.claim(attribution.ClaimPayload(
                order_id="ORD-1001", group=GROUP_A,
                claimed_at="2026-11-17T17:42:00",
            ))
        assert exc.value.status_code == 403
        assert exc.value.detail["current_group"] == GROUP_B

    def test_claim_after_correction_records_new_group(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        result = attribution.claim(attribution.ClaimPayload(
            order_id="ORD-1001", group=GROUP_B,
            claimed_at="2026-11-17T17:42:00",
        ))
        assert result["ok"] is True
        active = [row for row in ledger.claims() if row["order_id"] == "ORD-1001" and row["active"]]
        assert len(active) == 1 and active[0]["group"] == GROUP_B

    def test_replayed_correction_returns_replayed_flag(self):
        first = attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        second = attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["correction"]["entries"] == first["correction"]["entries"]

    def test_single_kind_json_export_after_correction(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        response = attribution.export(
            group=GROUP_A, kind="冲减", columns="原订单,组别,金额", format="json",
        )
        payload = json.loads(response.body)
        assert payload["items"] == [
            {"原订单": "ORD-1001", "组别": GROUP_A, "金额": -80.0}
        ]

    def test_bundle_export_contains_three_files(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        response = attribution.export(
            group=GROUP_B, kind=None, columns=None, format="csv",
        )
        with zipfile.ZipFile(io.BytesIO(response.body)) as package:
            names = package.namelist()
            assert set(names) == {
                "C1-乙组-已确认分摊.csv", "C1-乙组-冲减.csv", "C1-乙组-补记.csv",
            }
            supplement = package.read("C1-乙组-补记.csv").decode("utf-8-sig")
        rows = parse_csv(supplement)
        assert rows[0] == ["组别", "金额", "原订单"]
        assert rows[1] == [GROUP_B, "80.0", "ORD-1001"]

    def test_export_rejects_column_outside_three(self):
        with pytest.raises(HTTPException) as exc:
            attribution.export(
                group=GROUP_A, kind="已确认分摊", columns="转化时间", format="json",
            )
        assert exc.value.status_code == 400

    def test_export_orders_by_conversion_time(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        response = attribution.export(
            group=GROUP_B, kind="已确认分摊", columns=None, format="json",
        )
        items = json.loads(response.body)["items"]
        # 只有 ORD-1002 是乙组已确认，补记里的 ORD-1001 不在本文件
        assert [row["原订单"] for row in items] == ["ORD-1002"]

    def test_owner_lookup_shows_other_group_pending(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        payload = attribution.order_detail("ORD-1001")
        assert payload["current_group"] == GROUP_B
        assert payload["other_group"] == GROUP_A
        assert payload["other_pending_correction"] == {"amount": -80.0, "has_pending": True}

    def test_pending_process_endpoint_is_idempotent(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        first = attribution.process_pending(attribution.ProcessPendingPayload(group=GROUP_B))
        second = attribution.process_pending(attribution.ProcessPendingPayload(group=GROUP_B))
        assert first["count"] == 1
        assert second["count"] == 0
        assert ledger.pending_summary()["groups"][GROUP_B]["has_pending"] is False


class TestModuleRows:
    def test_weight_module_rows_follow_live_ledger(self):
        attribution.correct(attribution.CorrectionPayload(
            effective_at="2026-11-17T12:00:00", correction_id="COR-FIN-01",
        ))
        rows = ledger.module_rows()
        assert [row["组别"] for row in rows] == [GROUP_A, GROUP_B]
        assert rows[1]["归因权重"] == 200.0
        assert rows[0]["确认状态"] == "待处理"
