"""交易终止与权利回转的纯领域规则。

本模块不依赖数据库与时间源，所有判定都是可单测的纯函数：
- 案件状态机（通知 → 争议期 → 范围定稿 → 交接 → 关闭）；
- 回转范围项的类型与处置方式合法性；
- 争议窗口计算；
- 关闭决定与恢复自研 / 重新授权放行的前置动作清单；
- 通知去重指纹。

时间文本统一使用带时区的 ISO-8601 字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class TerminationError(ValueError):
    """终止流程领域错误的基类。"""

    code = "termination_error"


class InvalidCaseState(TerminationError):
    """案件当前状态不允许该动作（含关闭后试图重开）。"""

    code = "invalid_case_state"


class ScopeReferenceError(TerminationError):
    """回转范围没有正确引用原协议或已登记修订。"""

    code = "scope_reference_error"


class PrerequisiteError(TerminationError):
    """关闭或放行的前置动作尚未完成。"""

    code = "prerequisite_failed"


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 案件状态：notified（已通知，争议期内）→ disputed（争议挂起）→
# scope_finalized（回转范围定稿）→ handover_in_progress（交接执行中）→
# closed（最终关闭，终态）。
CASE_STATES = (
    "notified",
    "disputed",
    "scope_finalized",
    "handover_in_progress",
    "closed",
)

TRIGGER_TYPES = ("strategic_adjustment", "clinical_outcome", "contractual", "mutual")

# 范围项类型：地区权利、数据、样本、实物资产、再许可、共同开发/第三方承诺、遗留义务
ITEM_TYPES = ("right", "data", "sample", "asset", "sublicense", "collaboration", "obligation")

# 处置方式：revert 回转原企业；retain 对方保留；suspend 暂停；ongoing 持续有效
DISPOSITIONS = ("revert", "retain", "suspend", "ongoing")

# 各类型允许的处置方式。共同开发与再许可可以只暂停；义务默认持续。
ALLOWED_DISPOSITIONS: dict[str, frozenset[str]] = {
    "right": frozenset({"revert", "retain"}),
    "data": frozenset({"revert", "retain"}),
    "sample": frozenset({"revert", "retain"}),
    "asset": frozenset({"revert", "retain"}),
    "sublicense": frozenset({"revert", "retain", "suspend"}),
    "collaboration": frozenset({"suspend", "ongoing", "revert"}),
    "obligation": frozenset({"ongoing"}),
}

# 定稿时按处置方式落地的项目状态
FINALIZED_ITEM_STATUS = {
    "revert": "confirmed",       # 待交接
    "retain": "retained",        # 对方保留，无需交接
    "suspend": "confirmed",      # 待暂停动作
    "ongoing": "ongoing",        # 持续义务，关闭时允许保留
}

# 案件状态机：允许的目标状态
CASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "notified": frozenset({"disputed", "scope_finalized", "closed"}),
    "disputed": frozenset({"notified"}),
    "scope_finalized": frozenset({"handover_in_progress", "closed"}),
    "handover_in_progress": frozenset({"closed"}),
    "closed": frozenset(),  # 终态：任何材料都不能重开
}

OPEN_STATES = frozenset({"notified", "disputed", "scope_finalized", "handover_in_progress"})

CLEARANCE_KINDS = ("resume_self_research", "relicense")

# 再许可第三方处置动作
SUBLICENSE_ACTIONS = ("terminated", "novated", "retained", "suspended")
# 共同开发 / 第三方承诺的最终出路
COLLABORATION_OUTCOMES = ("released", "continuing")


# ---------------------------------------------------------------------------
# 时间辅助
# ---------------------------------------------------------------------------


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_days(value: str, days: int) -> str:
    if days < 0:
        raise TerminationError("争议期天数不能为负")
    return (parse_time(value) + timedelta(days=days)).isoformat()


def is_at_or_after(at_text: str, cutoff_text: str) -> bool:
    return parse_time(at_text) >= parse_time(cutoff_text)


# ----------------------------------------------------------------------------
# 通知去重
# ---------------------------------------------------------------------------


def notice_fingerprint(agreement_id: str, notice_key: str) -> str:
    """同一协议下同一外部通知编号只允许产生一次回转。"""

    return sha256(f"{agreement_id}|{notice_key}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 范围项输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeItemInput:
    item_type: str
    region: str
    subject: str
    disposition: str
    source_amendment_seq: int  # 0 表示原协议，>0 表示历次修订序号
    clause: str
    required: bool = True
    due_at: str | None = None
    detail: dict = field(default_factory=dict)

    def validate(self) -> None:
        if self.item_type not in ALLOWED_DISPOSITIONS:
            raise TerminationError(f"未知范围项类型: {self.item_type}")
        if not self.region.strip() or not self.subject.strip() or not self.clause.strip():
            raise TerminationError("地区、标的与条款引用为必填项")
        if self.disposition not in ALLOWED_DISPOSITIONS[self.item_type]:
            raise TerminationError(
                f"{self.item_type} 类型不支持处置方式 {self.disposition}"
            )
        if self.source_amendment_seq < 0:
            raise ScopeReferenceError("修订序号必须 >= 0（0 代表原协议）")
        if self.due_at:
            parse_time(self.due_at)


def ensure_transition(current: str, target: str) -> None:
    if target not in CASE_TRANSITIONS.get(current, frozenset()):
        raise InvalidCaseState(f"案件状态 {current} 不允许流转到 {target}")


def assert_open(status: str, action: str) -> None:
    if status == "closed":
        # 迟到材料走专门的留痕通道，其他一切动作都被拒绝
        raise InvalidCaseState(f"案件已关闭，{action} 不会重开已关闭决定")
    if status not in OPEN_STATES:
        raise InvalidCaseState(f"案件状态 {status} 不允许 {action}")


# ---------------------------------------------------------------------------
# 前置动作评估
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSnapshot:
    """评估前置条件所需的案件最小投影（由服务层从行数据组装）。"""

    status: str
    dispute_window_ends_at: str
    has_open_dispute: bool
    items: tuple[dict, ...]
    now: str


def dispute_window_ready(snapshot: CaseSnapshot) -> bool:
    """争议期已届满，且不存在未解决争议。"""

    return (
        not snapshot.has_open_dispute
        and is_at_or_after(snapshot.now, snapshot.dispute_window_ends_at)
    )


def close_blockers(snapshot: CaseSnapshot) -> list[str]:
    """关闭前必须清零的阻断项。遗留义务（ongoing）不阻断关闭。"""

    blockers: list[str] = []
    if snapshot.status == "closed":
        blockers.append("case_already_closed")
        return blockers
    if snapshot.has_open_dispute:
        blockers.append("open_dispute")
    if not is_at_or_after(snapshot.now, snapshot.dispute_window_ends_at):
        blockers.append("dispute_window_open")
    if snapshot.status not in {"scope_finalized", "handover_in_progress"}:
        blockers.append("scope_not_finalized")

    for item in snapshot.items:
        status = item["status"]
        disposition = item["disposition"]
        item_type = item["item_type"]
        label = f"{item_type}:{item['region']}:{item['subject']}"
        if status == "in_handover":
            blockers.append(f"handover_unconfirmed:{label}")
        elif disposition == "revert" and status != "completed":
            blockers.append(f"reversion_incomplete:{label}")
        elif disposition == "suspend" and status == "confirmed":
            # 已进入定稿但暂停动作尚未执行
            blockers.append(f"suspension_pending:{label}")
        elif item_type == "sublicense" and status not in {"completed", "suspended", "retained"}:
            blockers.append(f"sublicense_unresolved:{label}")
        elif item_type == "collaboration" and disposition == "suspend" and status == "suspended":
            # 暂停只是临时措施，关闭前必须拿到解除凭证或明确转为持续
            blockers.append(f"collaboration_suspension_unresolved:{label}")
    return blockers


def clearance_blockers(snapshot: CaseSnapshot, kind: str) -> list[str]:
    """恢复自研 / 重新授权前必须确认的全部前置动作。"""

    if kind not in CLEARANCE_KINDS:
        raise TerminationError(f"未知放行类型: {kind}")
    blockers: list[str] = []
    if snapshot.status != "closed":
        blockers.append("case_not_closed")
    # 关闭检查已覆盖交接完整性，这里再兜底一次，防止绕过关闭直接调用
    for item in snapshot.items:
        label = f"{item['item_type']}:{item['region']}:{item['subject']}"
        if item["disposition"] == "revert" and item["status"] != "completed":
            blockers.append(f"reversion_incomplete:{label}")
        if item["status"] == "in_handover":
            blockers.append(f"handover_unconfirmed:{label}")
        if item["item_type"] == "collaboration" and item["status"] == "suspended":
            blockers.append(f"affected_collaboration_still_suspended:{label}")
        # 仅暂停而未终止的再许可，对同一地区重新授权构成权利负担
        if kind == "relicense" and item["item_type"] == "sublicense" and item["status"] == "suspended":
            blockers.append(f"suspended_sublicense_encumbers_region:{label}")
    return blockers
