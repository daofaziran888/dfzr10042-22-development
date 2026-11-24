"""归因权重明细：只读模块服务，数据来自 C1 有效归属账本。"""
from __future__ import annotations

from typing import Any

from app.domain.attribution_ledger import ledger
from app.services.base import BaseModuleService


class AttributionWeightService(BaseModuleService):
    MODULE = "attribution_weight"
    ENTITY = "归因权重"
    OPERATION_TERM = "核对"
    LIST_FIELDS = ["明细编号", "渠道", "组别", "生效时间", "归因权重", "订单数", "原订单", "确认状态"]
    REQUIRED_FIELDS = []
    STATUS_ORDER = ["已确认", "待处理"]

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = ledger.module_rows()
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in str(row.get("明细编号", "")).casefold()
                or keyword.casefold() in str(row.get("组别", "")).casefold()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        for row in ledger.module_rows():
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        return None, ["归因权重明细由有效归属账本维护，不支持手工登记"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return None, "归因权重明细为只读视图，请在投放复盘页执行更正、领用与核对"

    def domain_snapshot(self) -> dict[str, Any]:
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": "归因权重组别明细",
            "count": len(ledger.module_rows()),
            "by_status": {
                status: sum(1 for row in ledger.module_rows() if row.get("status") == status)
                for status in self.STATUS_ORDER
            },
            "abnormal": 0,
            "channel": ledger.channel,
        }
