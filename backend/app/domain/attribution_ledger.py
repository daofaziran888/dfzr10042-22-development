"""归因权重有效归属账本。

围绕一个推广渠道（默认 C1）维护三样东西：

* **归因权重明细**：每个转化订单对应一笔权重，按转化的有效时间归属到某个运营组；
* **领用记录**：复盘人员只能领取「当前有效归属组」的订单权重，领用动作与更正并发时串行执行；
* **核对分录**：已确认分摊、冲减、补记三类分录。更正导致归属变化时，旧组的分摊被冲减、
  新组补记，同一笔权重在任何时点只在一个有效组内成立，且逐单金额恒等于原始归因权重。

账本只依赖标准库，并且所有写操作都包在一把可重入锁里，便于在内存服务中模拟并发串行化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any, Iterable

CHANNEL = "C1"
GROUP_A = "甲组"
GROUP_B = "乙组"
GROUPS = (GROUP_A, GROUP_B)

PERIOD_START = "2026-11-16T00:00:00"
PERIOD_END = "2026-11-22T23:59:59"
ORIGINAL_HANDOVER_AT = "2026-11-18T00:00:00"  # 原口径：周三起移交乙组
CORRECTED_HANDOVER_AT = "2026-11-17T12:00:00"  # 财务更正：周二中午起移交乙组

KIND_CONFIRMED = "已确认分摊"
KIND_REVERSAL = "冲减"
KIND_SUPPLEMENT = "补记"
KINDS = (KIND_CONFIRMED, KIND_REVERSAL, KIND_SUPPLEMENT)

STATUS_CONFIRMED = "已确认"
STATUS_PENDING = "待处理"
STATUS_PROCESSED = "已处理"
STATUS_REVERSED = "已冲减"
STATUS_RESTORED = "已还原"

DEFAULT_OPERATED_AT = "2026-11-24T09:30:00"


def parse_time(value: Any) -> datetime:
    """把 ISO 字符串/ datetime 解析成可比较的 naive datetime。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("时间不能为空")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def format_time(value: Any) -> str:
    return parse_time(value).isoformat(timespec="seconds")


def money(value: Any) -> float:
    return round(float(value) + 0.0, 2)


def normalize_group(value: Any) -> str | None:
    text = str(value or "").strip()
    if text in GROUPS:
        return text
    key = text.casefold().replace("_", "").replace("-", "")
    aliases = {
        "a": GROUP_A,
        "groupa": GROUP_A,
        "甲": GROUP_A,
        "甲组": GROUP_A,
        "b": GROUP_B,
        "groupb": GROUP_B,
        "乙": GROUP_B,
        "乙组": GROUP_B,
    }
    return aliases.get(key)


def normalize_kind(value: Any) -> str | None:
    text = str(value or "").strip()
    if text in KINDS:
        return text
    key = text.casefold().replace("_", "").replace("-", "").replace(" ", "")
    aliases = {
        "已确认分摊": KIND_CONFIRMED,
        "确认分摊": KIND_CONFIRMED,
        "分摊": KIND_CONFIRMED,
        "confirmed": KIND_CONFIRMED,
        "confirmedallocation": KIND_CONFIRMED,
        "allocation": KIND_CONFIRMED,
        "冲减": KIND_REVERSAL,
        "reversal": KIND_REVERSAL,
        "deduction": KIND_REVERSAL,
        "补记": KIND_SUPPLEMENT,
        "supplement": KIND_SUPPLEMENT,
        "supplementary": KIND_SUPPLEMENT,
    }
    return aliases.get(key)


@dataclass
class Order:
    order_id: str
    channel: str
    conversion_time: str
    amount: float

    @property
    def conversion_dt(self) -> datetime:
        return parse_time(self.conversion_time)


@dataclass
class Touchpoint:
    touchpoint_id: str
    order_id: str
    channel: str
    conversion_time: str
    weight: float


@dataclass
class AllocationEntry:
    entry_id: str
    order_id: str
    group: str
    kind: str
    amount: float
    status: str
    active: bool
    correction_id: str | None = None
    related_entry_id: str | None = None
    created_at: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "order_id": self.order_id,
            "group": self.group,
            "组别": self.group,
            "kind": self.kind,
            "类型": self.kind,
            "amount": money(self.amount),
            "金额": money(self.amount),
            "status": self.status,
            "active": self.active,
            "correction_id": self.correction_id,
            "related_entry_id": self.related_entry_id,
            "created_at": self.created_at,
            "note": self.note,
        }


@dataclass
class Claim:
    claim_id: str
    order_id: str
    group: str
    claimed_at: str
    amount: float
    status: str
    active: bool
    restored_at: str | None = None
    correction_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "order_id": self.order_id,
            "group": self.group,
            "组别": self.group,
            "claimed_at": self.claimed_at,
            "amount": money(self.amount),
            "金额": money(self.amount),
            "status": self.status,
            "active": self.active,
            "restored_at": self.restored_at,
            "correction_id": self.correction_id,
        }


@dataclass
class Correction:
    correction_id: str
    channel: str
    effective_at: str
    operated_at: str
    reason: str
    reverted_claims: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "correction_id": self.correction_id,
            "channel": self.channel,
            "effective_at": self.effective_at,
            "生效时间": self.effective_at,
            "operated_at": self.operated_at,
            "reason": self.reason,
            "reverted_claims": list(self.reverted_claims),
            "entries": list(self.entries),
        }


class AttributionLedger:
    """C1 渠道的归因权重有效归属账本。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self.reset()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def reset(self) -> dict[str, Any]:
        """恢复示例场景：周二订单 80 归甲组，周三订单 120 归乙组。"""
        with self._lock:
            self.channel = CHANNEL
            self.period_start = PERIOD_START
            self.period_end = PERIOD_END
            self.original_handover_at = ORIGINAL_HANDOVER_AT
            self.handover_at = ORIGINAL_HANDOVER_AT
            self.corrections: list[Correction] = []
            self.orders: dict[str, Order] = {
                "ORD-1001": Order("ORD-1001", CHANNEL, "2026-11-17T14:30:00", 80.0),
                "ORD-1002": Order("ORD-1002", CHANNEL, "2026-11-18T10:15:00", 120.0),
            }
            self.touchpoints: list[Touchpoint] = [
                Touchpoint("TP-0001", "ORD-1001", CHANNEL, "2026-11-17T14:30:00", 80.0),
                Touchpoint("TP-0002", "ORD-1002", CHANNEL, "2026-11-18T10:15:00", 120.0),
            ]
            self.entries: list[AllocationEntry] = []
            self.claim_records: list[Claim] = []
            self._claim_seq = 0
            self._entry_seq = 0
            self._correction_seq = 0
            # 初始两笔权重均已确认分摊，并分别被当时有效归属组领取。
            self._add_entry("ORD-1001", GROUP_A, KIND_CONFIRMED, 80.0,
                            STATUS_CONFIRMED, DEFAULT_OPERATED_AT, note="初始有效归属")
            self._add_entry("ORD-1002", GROUP_B, KIND_CONFIRMED, 120.0,
                            STATUS_CONFIRMED, DEFAULT_OPERATED_AT, note="初始有效归属")
            self._add_claim("ORD-1001", GROUP_A, "2026-11-17T15:10:00", STATUS_CONFIRMED)
            self._add_claim("ORD-1002", GROUP_B, "2026-11-18T11:00:00", STATUS_CONFIRMED)
            return self.overview()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _next_entry_id(self) -> str:
        self._entry_seq += 1
        return f"ENT-{self._entry_seq:04d}"

    def _next_claim_id(self) -> str:
        self._claim_seq += 1
        return f"CLM-{self._claim_seq:04d}"

    def _next_correction_id(self) -> str:
        self._correction_seq += 1
        return f"COR-{self._correction_seq:04d}"

    def _add_entry(self, order_id: str, group: str, kind: str, amount: float,
                   status: str, created_at: str, correction_id: str | None = None,
                   related_entry_id: str | None = None, note: str = "") -> AllocationEntry:
        entry = AllocationEntry(
            entry_id=self._next_entry_id(), order_id=order_id, group=group, kind=kind,
            amount=money(amount), status=status, active=kind != KIND_REVERSAL,
            correction_id=correction_id, related_entry_id=related_entry_id,
            created_at=format_time(created_at), note=note,
        )
        self.entries.append(entry)
        return entry

    def _add_claim(self, order_id: str, group: str, claimed_at: str, status: str) -> Claim:
        claim = Claim(
            claim_id=self._next_claim_id(), order_id=order_id, group=group,
            claimed_at=format_time(claimed_at), amount=self.orders[order_id].amount,
            status=status, active=status == STATUS_CONFIRMED,
        )
        self.claim_records.append(claim)
        return claim

    def _order_in_period(self, order: Order) -> bool:
        return parse_time(self.period_start) <= order.conversion_dt <= parse_time(self.period_end)

    def _period_orders(self) -> list[Order]:
        return sorted(
            (order for order in self.orders.values() if self._order_in_period(order)),
            key=lambda order: (order.conversion_dt, order.order_id),
        )

    def owner_for(self, order_id: str) -> str:
        """按订单转化时间落在哪个生效区间，给出当前有效归属组。"""
        order = self.orders[order_id]
        if order.channel != self.channel:
            return GROUP_A
        return GROUP_B if order.conversion_dt >= parse_time(self.handover_at) else GROUP_A

    def original_owner_for(self, order_id: str) -> str:
        order = self.orders[order_id]
        if order.channel != self.channel:
            return GROUP_A
        return GROUP_B if order.conversion_dt >= parse_time(self.original_handover_at) else GROUP_A

    def _active_allocation(self, order_id: str) -> AllocationEntry | None:
        for entry in self.entries:
            if entry.order_id == order_id and entry.active and entry.kind != KIND_REVERSAL:
                return entry
        return None

    def _active_claim(self, order_id: str) -> Claim | None:
        for claim in self.claim_records:
            if claim.order_id == order_id and claim.active:
                return claim
        return None

    def _claim_by_id(self, claim_id: str) -> Claim | None:
        for claim in self.claim_records:
            if claim.claim_id == claim_id:
                return claim
        return None

    # ------------------------------------------------------------------
    # 查询：组别明细 / 订单明细 / 领用明细
    # ------------------------------------------------------------------
    def group_detail(self) -> list[dict[str, Any]]:
        with self._lock:
            handover = parse_time(self.handover_at)
            period_start = parse_time(self.period_start)
            period_end = parse_time(self.period_end)
            rows: list[dict[str, Any]] = []
            for group in GROUPS:
                effective_from = period_start
                effective_to = handover if group == GROUP_A else period_end
                if group == GROUP_B:
                    effective_from = handover
                    effective_to = period_end
                orders = [order for order in self._period_orders()
                          if self.owner_for(order.order_id) == group]
                entries = [entry for entry in self.entries
                           if entry.group == group and self._order_in_period(self.orders[entry.order_id])]
                confirmed = sum(entry.amount for entry in entries if entry.kind == KIND_CONFIRMED)
                reversal = sum(entry.amount for entry in entries if entry.kind == KIND_REVERSAL)
                supplement = sum(entry.amount for entry in entries if entry.kind == KIND_SUPPLEMENT)
                pending = sum(entry.amount for entry in entries
                              if entry.kind in (KIND_REVERSAL, KIND_SUPPLEMENT)
                              and entry.status == STATUS_PENDING)
                weight = sum(order.amount for order in orders)
                rows.append({
                    "group": group,
                    "组别": group,
                    "effective_from": effective_from.isoformat(timespec="seconds"),
                    "effective_to": effective_to.isoformat(timespec="seconds"),
                    "生效时间": effective_from.isoformat(timespec="seconds"),
                    "order_count": len(orders),
                    "订单数": len(orders),
                    "weight": money(weight),
                    "归因权重": money(weight),
                    "confirmed_amount": money(confirmed),
                    "已确认分摊": money(confirmed),
                    "reversal_amount": money(reversal),
                    "冲减": money(reversal),
                    "supplement_amount": money(supplement),
                    "补记": money(supplement),
                    "ledger_net": money(confirmed + reversal + supplement),
                    "net": money(confirmed + reversal + supplement),
                    "pending_correction": money(pending),
                    "待处理更正金额": money(pending),
                    "original_orders": [order.order_id for order in orders],
                    "原订单": [order.order_id for order in orders],
                    "status": STATUS_PENDING if pending else STATUS_CONFIRMED,
                })
            return rows

    def order_detail(self, group: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            wanted = normalize_group(group) if group else None
            rows: list[dict[str, Any]] = []
            for order in self._period_orders():
                owner = self.owner_for(order.order_id)
                if wanted and owner != wanted:
                    continue
                claim = self._active_claim(order.order_id)
                allocation = self._active_allocation(order.order_id)
                history = [entry.as_dict() for entry in self.entries
                           if entry.order_id == order.order_id]
                rows.append({
                    "order_id": order.order_id,
                    "原订单": order.order_id,
                    "channel": order.channel,
                    "原渠道": order.channel,
                    "conversion_time": order.conversion_time,
                    "转化时间": order.conversion_time,
                    "amount": money(order.amount),
                    "归因权重": money(order.amount),
                    "current_group": owner,
                    "当前有效组": owner,
                    "original_group": self.original_owner_for(order.order_id),
                    "原归属组": self.original_owner_for(order.order_id),
                    "claimed": claim is not None,
                    "claim": claim.as_dict() if claim else None,
                    "active_entry": allocation.as_dict() if allocation else None,
                    "entries": history,
                })
            return rows

    def claims(self, group: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            wanted = normalize_group(group) if group else None
            rows = [claim.as_dict() for claim in self.claim_records
                    if not wanted or claim.group == wanted]
            return sorted(rows, key=lambda row: (row["claimed_at"], row["claim_id"]))

    # ------------------------------------------------------------------
    # 领用
    # ------------------------------------------------------------------
    def claim(self, order_id: str, group: str, claimed_at: Any = None,
              claim_id: str | None = None) -> dict[str, Any]:
        """领取订单分摊。归属校验与写记录在同一把锁内完成。

        权限规则：只能领取当前有效归属组的订单。重复领取同一订单返回已有记录；
        同一订单不会同时存在两个有效组的有效领用。
        """
        with self._lock:
            order = self.orders.get(str(order_id or ""))
            if order is None:
                return {"ok": False, "message": f"订单 {order_id} 不存在", "claim": None}
            canonical = normalize_group(group)
            if canonical is None:
                return {"ok": False, "message": f"组别 {group} 不存在", "claim": None}
            owner = self.owner_for(order.order_id)
            if canonical != owner:
                return {
                    "ok": False,
                    "message": (
                        f"领用权限随有效归属核定：{order.order_id} 当前归{owner}，"
                        f"{canonical}不可领用"
                    ),
                    "claim": None,
                    "current_group": owner,
                }
            existing = self._active_claim(order.order_id)
            if existing is not None:
                if existing.group == canonical:
                    return {
                        "ok": True,
                        "message": f"{order.order_id} 已由{canonical}确认领用，未重复记录",
                        "claim": existing.as_dict(),
                        "current_group": owner,
                    }
                return {
                    "ok": False,
                    "message": f"{order.order_id} 的有效归属已落在{existing.group}，同一笔权重不能进入两个组",
                    "claim": existing.as_dict(),
                    "current_group": owner,
                }
            stamp = claimed_at if claimed_at else DEFAULT_OPERATED_AT
            try:
                stamp_text = format_time(stamp)
            except (TypeError, ValueError):
                return {"ok": False, "message": f"领用时间 {claimed_at} 无法解析", "claim": None}
            claim = self._add_claim(order.order_id, canonical, stamp_text, STATUS_CONFIRMED)
            if claim_id:
                claim.claim_id = str(claim_id)
            return {
                "ok": True,
                "message": f"{order.order_id} 已由{canonical}领用并确认",
                "claim": claim.as_dict(),
                "current_group": owner,
            }

    def restore_claim(self, claim_id: str, operated_at: Any = None) -> dict[str, Any]:
        """还原一笔已确认领用，保留原始领取轨迹。"""
        with self._lock:
            claim = self._claim_by_id(str(claim_id or ""))
            if claim is None:
                return {"ok": False, "message": f"领用记录 {claim_id} 不存在", "claim": None}
            if not claim.active:
                return {"ok": True, "message": f"领用记录 {claim_id} 已是{claim.status}", "claim": claim.as_dict()}
            stamp = format_time(operated_at if operated_at else DEFAULT_OPERATED_AT)
            claim.active = False
            claim.status = STATUS_RESTORED
            claim.restored_at = stamp
            return {"ok": True, "message": f"领用记录 {claim_id} 已还原", "claim": claim.as_dict()}

    # ------------------------------------------------------------------
    # 更正
    # ------------------------------------------------------------------
    def correct_handover(self, effective_at: Any, correction_id: str | None = None,
                         operated_at: Any = None, reason: str = "",
                         channel: str | None = None) -> dict[str, Any]:
        """应用移交时点更正；同一时点重放不会重复生成冲减/补记。"""
        with self._lock:
            target_channel = str(channel or self.channel)
            if target_channel != self.channel:
                return {"ok": False, "message": f"渠道 {target_channel} 不在本账本范围", "replayed": False}
            try:
                new_effective = format_time(effective_at)
            except (TypeError, ValueError):
                return {"ok": False, "message": f"更正生效时间 {effective_at} 无法解析", "replayed": False}
            stamp = format_time(operated_at if operated_at else DEFAULT_OPERATED_AT)
            if correction_id:
                for correction in self.corrections:
                    if correction.correction_id == str(correction_id):
                        return {
                            "ok": True, "replayed": True,
                            "message": f"更正 {correction_id} 已应用，重放不再生成补偿分录",
                            "correction": correction.as_dict(),
                        }
            if new_effective == format_time(self.handover_at):
                return {
                    "ok": True, "replayed": True,
                    "message": "移交时点未变化，重放不生成重复补偿记录",
                    "correction": None,
                }

            correction = Correction(
                correction_id=str(correction_id) if correction_id else self._next_correction_id(),
                channel=self.channel, effective_at=new_effective, operated_at=stamp,
                reason=reason or "移交时点更正",
            )
            old_handover = self.handover_at
            self.handover_at = new_effective

            # 已确认领用如果因归属变化失效，先还原，保证同一订单只有一个有效组。
            for claim in self.claim_records:
                if claim.active and claim.group != self.owner_for(claim.order_id):
                    claim.active = False
                    claim.status = STATUS_RESTORED
                    claim.restored_at = stamp
                    claim.correction_id = correction.correction_id
                    correction.reverted_claims.append(claim.claim_id)

            # 归属发生变化的订单：旧组冲减、新组补记，逐单金额守恒。
            for order in self._period_orders():
                owner = self.owner_for(order.order_id)
                allocation = self._active_allocation(order.order_id)
                if allocation is None or allocation.group == owner:
                    continue
                old_group = allocation.group
                allocation.active = False
                allocation.status = STATUS_REVERSED
                reversal = self._add_entry(
                    order.order_id, old_group, KIND_REVERSAL, -order.amount,
                    STATUS_PENDING, stamp, correction_id=correction.correction_id,
                    related_entry_id=allocation.entry_id,
                    note=f"更正 {correction.correction_id}：{old_group}原分摊冲减",
                )
                supplement = self._add_entry(
                    order.order_id, owner, KIND_SUPPLEMENT, order.amount,
                    STATUS_PENDING, stamp, correction_id=correction.correction_id,
                    related_entry_id=allocation.entry_id,
                    note=f"更正 {correction.correction_id}：{owner}补记",
                )
                correction.entries.extend([reversal.entry_id, supplement.entry_id])

            self.corrections.append(correction)
            return {
                "ok": True,
                "replayed": False,
                "message": (
                    f"移交时点已由 {format_time(old_handover)} 更正为 {new_effective}，"
                    f"还原 {len(correction.reverted_claims)} 笔领用，生成 "
                    f"{len(correction.entries)} 笔核对分录"
                ),
                "correction": correction.as_dict(),
                "reverted_claims": list(correction.reverted_claims),
                "pending": self.pending_summary(),
            }

    def process_pending(self, group: str | None = None, order_id: str | None = None,
                        operated_at: Any = None) -> dict[str, Any]:
        """月末处理待更正金额；重复处理不会改变结果。"""
        with self._lock:
            wanted = normalize_group(group) if group else None
            stamp = format_time(operated_at if operated_at else DEFAULT_OPERATED_AT)
            processed = []
            for entry in self.entries:
                if entry.kind not in (KIND_REVERSAL, KIND_SUPPLEMENT):
                    continue
                if entry.status != STATUS_PENDING:
                    continue
                if wanted and entry.group != wanted:
                    continue
                if order_id and entry.order_id != str(order_id):
                    continue
                entry.status = STATUS_PROCESSED
                processed.append(entry.entry_id)
            return {
                "ok": True,
                "message": f"已处理 {len(processed)} 笔待更正分录",
                "processed": processed,
                "count": len(processed),
                "processed_at": stamp,
                "pending": self.pending_summary(),
            }

    # ------------------------------------------------------------------
    # 汇总 / 核对
    # ------------------------------------------------------------------
    def pending_summary(self) -> dict[str, Any]:
        has_pending = {group: False for group in GROUPS}
        amount = {group: 0.0 for group in GROUPS}
        for entry in self.entries:
            if entry.kind in (KIND_REVERSAL, KIND_SUPPLEMENT) and entry.status == STATUS_PENDING:
                amount[entry.group] = money(amount[entry.group] + entry.amount)
                has_pending[entry.group] = True
        return {
            "by_group": amount,
            "groups": {group: {"amount": amount[group], "has_pending": has_pending[group]}
                       for group in GROUPS},
            "has_pending": any(has_pending.values()),
        }

    def reconciliation(self) -> dict[str, Any]:
        with self._lock:
            group_rows = self.group_detail()
            order_rows: list[dict[str, Any]] = []
            duplicate_pairs: list[tuple[str, str, str, str]] = []
            seen_pairs: set[tuple[str, str, str, str]] = set()
            for order in self._period_orders():
                shares = {group: 0.0 for group in GROUPS}
                for entry in self.entries:
                    if entry.order_id != order.order_id:
                        continue
                    shares[entry.group] = money(shares[entry.group] + entry.amount)
                ledger_total = money(sum(shares.values()))
                if ledger_total != money(order.amount):
                    balanced = False
                else:
                    balanced = True
                order_rows.append({
                    "order_id": order.order_id,
                    "原订单": order.order_id,
                    "conversion_time": order.conversion_time,
                    "转化时间": order.conversion_time,
                    "weight": money(order.amount),
                    "归因权重": money(order.amount),
                    "current_group": self.owner_for(order.order_id),
                    "shares": shares,
                    "group_shares": shares,
                    "ledger_total": ledger_total,
                    "balanced": balanced,
                })
            for entry in self.entries:
                if entry.kind == KIND_CONFIRMED:
                    continue
                pair = (entry.order_id, entry.kind, entry.group, entry.correction_id or "")
                if pair in seen_pairs:
                    duplicate_pairs.append(pair)
                seen_pairs.add(pair)
            group_balanced = all(row["ledger_net"] == row["weight"] for row in group_rows)
            order_balanced = all(row["balanced"] for row in order_rows)
            return {
                "channel": self.channel,
                "period": {"start": self.period_start, "end": self.period_end},
                "groups": group_rows,
                "orders": order_rows,
                "duplicate_compensation_count": len(duplicate_pairs),
                "duplicate_compensations": [
                    {"order_id": item[0], "kind": item[1], "group": item[2],
                     "correction_id": item[3]} for item in duplicate_pairs
                ],
                "balanced": group_balanced and order_balanced and not duplicate_pairs,
            }

    def _group_has_period_entries(self, group: str) -> bool:
        return any(entry.group == group and self._order_in_period(self.orders[entry.order_id])
                   for entry in self.entries)

    def overview(self) -> dict[str, Any]:
        with self._lock:
            groups = self.group_detail()
            orders = self.order_detail()
            total_weight = money(sum(order["amount"] for order in orders))
            channel_total = money(sum(tp.weight for tp in self.touchpoints
                                      if tp.channel == self.channel
                                      and parse_time(self.period_start) <= parse_time(tp.conversion_time) <= parse_time(self.period_end)))
            pending = self.pending_summary()
            current_owner = {order["order_id"]: order["current_group"] for order in orders}
            return {
                "channel": self.channel,
                "渠道": self.channel,
                "period": {"start": self.period_start, "end": self.period_end, "start_date": self.period_start[:10], "end_date": self.period_end[:10]},
                "handover": {
                    "original_effective_at": self.original_handover_at,
                    "effective_at": self.handover_at,
                    "corrected": format_time(self.handover_at) != format_time(self.original_handover_at),
                    "corrections": [correction.as_dict() for correction in self.corrections],
                },
                "groups": groups,
                "orders": orders,
                "claims": self.claims(),
                "current_owner": current_owner,
                "totals": {
                    "attribution_weight": total_weight,
                    "本期合计": total_weight,
                    "channel_touchpoints": channel_total,
                    "旧渠道触点合计": channel_total,
                    "reconciled": total_weight == channel_total,
                    "对平": total_weight == channel_total,
                },
                "pending_corrections": pending,
                "reconciliation": {
                    "balanced": self.reconciliation()["balanced"],
                    "duplicate_compensation_count": self.reconciliation()["duplicate_compensation_count"],
                },
                "valid_groups": [
                    row["group"] for row in groups
                    if row["order_count"] > 0 or row["ledger_net"] != 0
                    or self._group_has_period_entries(row["group"])
                ],
            }

    def current_owner(self, order_id: str) -> dict[str, Any]:
        with self._lock:
            order = self.orders.get(str(order_id or ""))
            if order is None:
                return {"ok": False, "message": f"订单 {order_id} 不存在"}
            owner = self.owner_for(order.order_id)
            claim = self._active_claim(order.order_id)
            other = GROUP_A if owner == GROUP_B else GROUP_B
            pending = self.pending_summary()["groups"][other]
            return {
                "ok": True,
                "order_id": order.order_id,
                "current_group": owner,
                "当前有效组": owner,
                "original_group": self.original_owner_for(order.order_id),
                "amount": money(order.amount),
                "claim": claim.as_dict() if claim else None,
                "other_group": other,
                "other_pending_correction": pending,
            }

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_rows(self, group: str, kind: str,
                    columns: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """按转化时间排序输出指定组、指定类型的分录。"""
        with self._lock:
            canonical_group = normalize_group(group)
            if canonical_group is None:
                raise ValueError(f"组别 {group} 不存在")
            canonical_kind = normalize_kind(kind)
            if canonical_kind is None:
                raise ValueError(f"核对类型 {kind} 不存在")
            selected = normalize_columns(columns)
            entries = [entry for entry in self.entries
                       if entry.group == canonical_group and entry.kind == canonical_kind
                       and self._order_in_period(self.orders[entry.order_id])]
            entries.sort(key=lambda entry: (
                self.orders[entry.order_id].conversion_dt,
                self.orders[entry.order_id].order_id,
                entry.entry_id,
            ))
            rows: list[dict[str, Any]] = []
            for entry in entries:
                order = self.orders[entry.order_id]
                full = {
                    "组别": entry.group,
                    "金额": money(entry.amount),
                    "原订单": entry.order_id,
                }
                rows.append({column: full[column] for column in selected})
            return rows

    def export_bundle(self, group: str, columns: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        return {
            kind: self.export_rows(group, kind, columns)
            for kind in KINDS
        }

    # ------------------------------------------------------------------
    # 通用模块行（供 /api/attribution_weight 只读列表使用）
    # ------------------------------------------------------------------
    def module_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, group in enumerate(self.group_detail(), start=1):
            rows.append({
                "id": index,
                "明细编号": f"AW-{self.channel}-{index:04d}",
                "渠道": self.channel,
                "组别": group["group"],
                "生效时间": group["生效时间"],
                "归因权重": group["归因权重"],
                "订单数": group["订单数"],
                "原订单": "、".join(group["原订单"]) or "—",
                "确认状态": group["status"],
                "status": group["status"],
                "pending": group["pending_correction"] != 0,
                "abnormal": False,
            })
        return rows


def normalize_columns(columns: Iterable[str] | None) -> list[str]:
    allowed = {"组别", "金额", "原订单"}
    aliases = {
        "group": "组别",
        "组": "组别",
        "amount": "金额",
        "money": "金额",
        "order": "原订单",
        "order_id": "原订单",
        "orderid": "原订单",
        "原单": "原订单",
    }
    if columns is None:
        return ["组别", "金额", "原订单"]
    if isinstance(columns, str):
        raw = [part for part in columns.replace("、", ",").replace(" ", ",").split(",") if part]
    else:
        raw = list(columns)
    normalized: list[str] = []
    for column in raw:
        text = str(column).strip()
        text = aliases.get(text.casefold(), text)
        if text not in allowed:
            raise ValueError(f"导出列 {column} 不支持，可选：组别、金额、原订单")
        if text not in normalized:
            normalized.append(text)
    if not normalized:
        raise ValueError("至少选择一列导出字段")
    return normalized


ledger = AttributionLedger()

