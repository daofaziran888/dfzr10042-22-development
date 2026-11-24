"""归因权重有效归属 / 移交更正 / 领用并发 / 月末核对测试。"""
from __future__ import annotations

import threading

import pytest

from app.domain.attribution_ledger import (
    GROUP_A,
    GROUP_B,
    KIND_CONFIRMED,
    KIND_REVERSAL,
    KIND_SUPPLEMENT,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_RESTORED,
    AttributionLedger,
    normalize_columns,
    normalize_group,
    normalize_kind,
)


@pytest.fixture
def book() -> AttributionLedger:
    return AttributionLedger()


class TestGroupDetail:
    def test_original_handover_splits_period_by_effective_time(self, book):
        rows = {row["group"]: row for row in book.group_detail()}
        assert rows[GROUP_A]["weight"] == 80.0
        assert rows[GROUP_A]["original_orders"] == ["ORD-1001"]
        assert rows[GROUP_B]["weight"] == 120.0
        assert rows[GROUP_B]["original_orders"] == ["ORD-1002"]
        assert rows[GROUP_A]["effective_to"] == "2026-11-18T00:00:00"
        assert rows[GROUP_B]["effective_from"] == "2026-11-18T00:00:00"

    def test_period_total_matches_channel_touchpoints(self, book):
        totals = book.overview()["totals"]
        assert totals["本期合计"] == 200.0
        assert totals["旧渠道触点合计"] == 200.0
        assert totals["对平"] is True

    def test_original_channel_and_conversion_still_traceable(self, book):
        orders = book.order_detail()
        assert [row["order_id"] for row in orders] == ["ORD-1001", "ORD-1002"]
        assert all(row["channel"] == "C1" for row in orders)
        assert orders[0]["conversion_time"] == "2026-11-17T14:30:00"
        assert orders[0]["current_group"] == GROUP_A
        assert orders[1]["current_group"] == GROUP_B


class TestCorrection:
    def test_corrected_handover_moves_tuesday_order_to_b(self, book):
        result = book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        assert result["ok"] is True and result["replayed"] is False
        assert book.current_owner("ORD-1001")["current_group"] == GROUP_B
        rows = {row["group"]: row for row in book.group_detail()}
        assert rows[GROUP_A]["reversal_amount"] == -80.0
        assert rows[GROUP_A]["ledger_net"] == 0.0
        assert rows[GROUP_B]["supplement_amount"] == 80.0
        assert rows[GROUP_B]["weight"] == 200.0
        assert rows[GROUP_B]["ledger_net"] == 200.0

    def test_confirmed_claim_is_restored_not_deleted(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        claims = [row for row in book.claims() if row["order_id"] == "ORD-1001"]
        restored = [row for row in claims if row["status"] == STATUS_RESTORED]
        assert len(restored) == 1
        assert restored[0]["group"] == GROUP_A
        assert restored[0]["active"] is False
        assert restored[0]["restored_at"]
        assert restored[0]["correction_id"] == "COR-FIN-01"
        # 还原仍可追溯原始领取时间
        assert restored[0]["claimed_at"] == "2026-11-17T15:10:00"

    def test_each_order_has_only_one_active_group_after_correction(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        for order in book.order_detail():
            active_entries = [entry for entry in order["entries"] if entry["active"]]
            groups = {entry["group"] for entry in active_entries}
            assert groups == {order["current_group"]}

    def test_replaying_correction_creates_no_duplicate_compensation(self, book):
        first = book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        replay = book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert len([e for e in book.entries if e.kind == KIND_REVERSAL]) == 1
        assert len([e for e in book.entries if e.kind == KIND_SUPPLEMENT]) == 1
        assert book.reconciliation()["duplicate_compensation_count"] == 0

    def test_same_effective_time_without_id_is_idempotent(self, book):
        book.correct_handover("2026-11-17T12:00:00")
        replay = book.correct_handover("2026-11-17T12:00:00")
        assert replay["replayed"] is True
        assert len(book.corrections) == 1

    def test_each_order_still_sums_to_original_weight(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        recon = book.reconciliation()
        assert recon["balanced"] is True
        for order in recon["orders"]:
            assert order["ledger_total"] == order["weight"]
            assert sum(order["shares"].values()) == order["weight"]

    def test_compensation_difference_linked_but_counted_once(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        reversal = next(e for e in book.entries if e.kind == KIND_REVERSAL)
        supplement = next(e for e in book.entries if e.kind == KIND_SUPPLEMENT)
        assert reversal.correction_id == supplement.correction_id == "COR-FIN-01"
        assert reversal.related_entry_id == supplement.related_entry_id
        assert reversal.amount == -80.0 and supplement.amount == 80.0
        pending = book.pending_summary()["groups"]
        assert pending[GROUP_A]["amount"] == -80.0
        assert pending[GROUP_B]["amount"] == 80.0

    def test_other_group_keeps_pending_amount(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        owner = book.current_owner("ORD-1001")
        assert owner["current_group"] == GROUP_B
        assert owner["other_group"] == GROUP_A
        assert owner["other_pending_correction"]["amount"] == -80.0
        assert owner["other_pending_correction"]["has_pending"] is True
        book.process_pending(GROUP_B, "ORD-1001")
        owner = book.current_owner("ORD-1001")
        assert owner["other_pending_correction"]["has_pending"] is True
        book.process_pending(GROUP_A, "ORD-1001")
        assert book.current_owner("ORD-1001")["other_pending_correction"]["has_pending"] is False


class TestClaimPermissions:
    def test_only_current_owner_group_can_claim(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        denied = book.claim("ORD-1001", GROUP_A)
        assert denied["ok"] is False
        assert denied["current_group"] == GROUP_B
        allowed = book.claim("ORD-1001", GROUP_B, claimed_at="2026-11-17T13:20:00")
        assert allowed["ok"] is True
        assert allowed["claim"]["group"] == GROUP_B

    def test_duplicate_claim_returns_same_record(self, book):
        first = book.claim("ORD-1001", GROUP_A, claimed_at="2026-11-17T09:20:00")
        second = book.claim("ORD-1001", GROUP_A, claimed_at="2026-11-17T17:42:00")
        assert first["ok"] and second["ok"]
        assert first["claim"]["claim_id"] == second["claim"]["claim_id"]
        active = [c for c in book.claims() if c["order_id"] == "ORD-1001" and c["active"]]
        assert len(active) == 1

    def test_restored_claim_allows_new_group_but_never_two_active(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        book.claim("ORD-1001", GROUP_B, claimed_at="2026-11-17T13:20:00")
        active = [c for c in book.claims() if c["order_id"] == "ORD-1001" and c["active"]]
        assert len(active) == 1
        assert active[0]["group"] == GROUP_B
        assert book.claim("ORD-1001", GROUP_A)["ok"] is False

    def test_restore_claim_is_replay_safe(self, book):
        claim_id = book.claims("甲组")[0]["claim_id"]
        first = book.restore_claim(claim_id, "2026-11-24T10:00:00")
        second = book.restore_claim(claim_id, "2026-11-24T10:00:00")
        assert first["ok"] and second["ok"]
        assert first["claim"]["status"] == second["claim"]["status"] == STATUS_RESTORED

    def test_concurrent_correction_and_claim_serialize(self, book):
        """更正与领取并发时，同一笔权重只能进入一个有效组。"""
        results: list[dict] = []
        barrier = threading.Barrier(2)

        def correct():
            barrier.wait()
            results.append(book.correct_handover(
                "2026-11-17T12:00:00", correction_id="COR-FIN-01",
                operated_at="2026-11-24T10:00:00",
            ))

        def claim():
            barrier.wait()
            results.append(book.claim("ORD-1001", GROUP_A, claimed_at="2026-11-17T17:42:00"))

        threads = [threading.Thread(target=correct), threading.Thread(target=claim)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        active = [c for c in book.claims() if c["order_id"] == "ORD-1001" and c["active"]]
        # 更正先落库时领用被拒绝，领用先落库时更正会还原它；两种顺序都最多一个有效组。
        assert len(active) <= 1
        owner = book.current_owner("ORD-1001")["current_group"]
        assert all(claim["group"] == owner for claim in active)
        for order in book.order_detail():
            active_groups = {entry["group"] for entry in order["entries"] if entry["active"]}
            assert active_groups == {order["current_group"]}


class TestReconciliation:
    def test_open_period_reconciles_both_groups(self, book):
        recon = book.reconciliation()
        assert recon["balanced"] is True
        assert recon["duplicate_compensation_count"] == 0
        assert {row["current_group"] for row in recon["orders"]} == {GROUP_A, GROUP_B}

    def test_after_correction_group_nets_match_weights(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        for row in book.group_detail():
            assert row["ledger_net"] == row["weight"]
        assert book.reconciliation()["balanced"] is True

    def test_processing_pending_is_idempotent(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        first = book.process_pending(GROUP_A, "ORD-1001")
        second = book.process_pending(GROUP_A, "ORD-1001")
        assert first["count"] == 1
        assert second["count"] == 0
        assert all(entry.status != STATUS_PENDING
                   for entry in book.entries if entry.group == GROUP_A)


class TestMonthlyExport:
    def test_export_default_columns_and_conversion_order(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        rows = book.export_rows(GROUP_B, KIND_SUPPLEMENT, columns=None)
        assert rows == [{"组别": GROUP_B, "金额": 80.0, "原订单": "ORD-1001"}]

    def test_export_supports_three_optional_columns(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        rows = book.export_rows(GROUP_B, KIND_CONFIRMED, columns=["原订单", "组别", "金额"])
        assert rows == [{"原订单": "ORD-1002", "组别": GROUP_B, "金额": 120.0}]

    def test_export_rows_sort_by_conversion_time(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        book.orders["ORD-0999"] = type(book.orders["ORD-1001"])(
            "ORD-0999", "C1", "2026-11-16T08:00:00", 10.0)
        book._add_entry("ORD-0999", GROUP_B, KIND_SUPPLEMENT, 10.0,
                        STATUS_CONFIRMED, "2026-11-24T10:00:00")
        rows = book.export_rows(GROUP_B, KIND_SUPPLEMENT)
        assert [row["原订单"] for row in rows] == ["ORD-0999", "ORD-1001"]

    def test_export_rejects_unsupported_column(self, book):
        with pytest.raises(ValueError):
            book.export_rows(GROUP_A, KIND_CONFIRMED, ["渠道"])
        with pytest.raises(ValueError):
            book.export_rows(GROUP_A, KIND_CONFIRMED, ["转化时间"])

    def test_export_bundle_splits_three_kinds(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        bundle = book.export_bundle(GROUP_A)
        assert set(bundle) == {KIND_CONFIRMED, KIND_REVERSAL, KIND_SUPPLEMENT}
        assert bundle[KIND_REVERSAL] == [
            {"组别": GROUP_A, "金额": -80.0, "原订单": "ORD-1001"}
        ]
        assert bundle[KIND_SUPPLEMENT] == []

    def test_replay_after_export_does_not_add_rows(self, book):
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        before = book.export_bundle(GROUP_A)
        book.correct_handover("2026-11-17T12:00:00", correction_id="COR-FIN-01")
        after = book.export_bundle(GROUP_A)
        assert before == after


class TestNormalization:
    def test_group_aliases(self):
        assert normalize_group("A") == GROUP_A
        assert normalize_group("乙") == GROUP_B
        assert normalize_group("group-b") == GROUP_B
        assert normalize_group("丙组") is None

    def test_kind_aliases(self):
        assert normalize_kind("confirmed") == KIND_CONFIRMED
        assert normalize_kind("reversal") == KIND_REVERSAL
        assert normalize_kind("补记") == KIND_SUPPLEMENT

    def test_columns_accept_chinese_and_english(self):
        assert normalize_columns("金额,原订单") == ["金额", "原订单"]
        assert normalize_columns("group,amount,order") == ["组别", "金额", "原订单"]
        assert normalize_columns(None) == ["组别", "金额", "原订单"]
