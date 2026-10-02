"""交易终止与权利回转的离线命令行验收。

在临时内存库中演示完整时间线：
原协议+修订登记 → 终止通知（战略调整）→ 重复通知幂等 → 争议提出/解决 →
回转范围（引用修订；共同开发只暂停受影响部分）→ 数据/样本/地区权利交付 →
再许可终止 → 共同开发暂停后解除 → 前置校验 → 关闭 →
迟到材料不重开 → 恢复自研/重新授权放行 → 权利状态查询。
"""

from __future__ import annotations

import json

from .termination import PrerequisiteError, ScopeItemInput
from .termination_service import TerminationService
from .termination_storage import inspect_schema


def _clock(start: str):
    from datetime import datetime, timedelta

    current = {"t": datetime.fromisoformat(start)}

    def now() -> str:
        return current["t"].isoformat()

    def advance(days: int = 0, **kwargs) -> None:
        current["t"] += timedelta(days=days, **kwargs)

    return now, advance


def run() -> dict:
    now, advance = _clock("2026-10-02T09:00:00+00:00")
    s = TerminationService(":memory:", clock=now)
    for uid, pwd, role in (
        ("legal", "legal-pass-2026", "legal"),
        ("rd", "rd-pass-2026", "rd"),
        ("bd", "bd-pass-2026", "bd"),
        ("auditor", "auditor-2026", "auditor"),
    ):
        s.auth.create_user(uid, pwd, role)
    legal = s.auth.login("legal", "legal-pass-2026")
    rd = s.auth.login("rd", "rd-pass-2026")

    # 1) 原协议 + 两次修订（亚太权利在修订2中被扩展）
    s.register_agreement(legal, "AGR-X1", "X1 单抗全球开发授权", "NorthWind Pharma",
                         "2023-05-01T00:00:00+00:00")
    s.add_amendment(legal, "AGR-X1", 1, "补充欧盟地区权利", "2024-03-10T00:00:00+00:00",
                    "EU 权利与数据交付细则")
    s.add_amendment(legal, "AGR-X1", 2, "亚太扩展与共同开发", "2025-06-18T00:00:00+00:00",
                    "APAC 权利；与 NorthWind 共同开发 II 期；存在第三方 CRO 承诺")

    # 2) 终止通知（战略调整）+ 争议窗口 30 天
    opened = s.open_termination_case(
        legal, "AGR-X1", "X1 授权终止与权利回转", "strategic_adjustment",
        "对方战略调整，终止除共同开发外的授权安排", "NW-TERM-2026-009",
        "2026-10-01T00:00:00+00:00", 30)
    assert opened["duplicate"] is False
    case_id = opened["case"]["case_id"]

    # 3) 重复通知（不同时间再发、同编号）不得产生第二次回转
    duplicate = s.open_termination_case(
        legal, "AGR-X1", "X1 授权终止与权利回转", "strategic_adjustment",
        "对方战略调整", "NW-TERM-2026-009", "2026-10-03T00:00:00+00:00", 30)
    assert duplicate["duplicate"] is True and duplicate["case"]["case_id"] == case_id

    # 4) 争议提出与解决
    dispute = s.raise_dispute(legal, case_id, "欧盟数据归属", "对方主张 EU 桥接试验数据共有")
    s.resolve_dispute(legal, dispute["dispute_id"], "依据修订1第4.2条，数据随权利回转")

    # 5) 回转范围：每项都引用原协议或修订条款
    right_cn = s.add_scope_item(legal, case_id, ScopeItemInput(
        "right", "CN", "X1 单抗开发与商业化权利", "revert", 2, "Am2 §1"))["item_id"]
    right_eu = s.add_scope_item(legal, case_id, ScopeItemInput(
        "right", "EU", "X1 单抗开发与商业化权利", "revert", 1, "Am1 §2"))["item_id"]
    data_id = s.add_scope_item(rd, case_id, ScopeItemInput(
        "data", "global", "全部临床前与 I/II 期临床数据包", "revert", 0,
        "Original §8.3", detail={"formats": ["SDTM", "raw"]}))["item_id"]
    sample_id = s.add_scope_item(rd, case_id, ScopeItemInput(
        "sample", "global", "主细胞库与临床样本", "revert", 0, "Original §9.1"))["item_id"]
    sub_id = s.add_scope_item(legal, case_id, ScopeItemInput(
        "sublicense", "EU", "对方授予 EuroLab 的再许可", "suspend", 1, "Am1 §5"))["item_id"]
    collab_id = s.add_scope_item(legal, case_id, ScopeItemInput(
        "collaboration", "global", "II 期共同开发 + CRO 第三方承诺", "suspend",
        2, "Am2 §7", detail={"affected_part": "X1 适应症 A 队列",
                              "continuing_part": "适应症 B 联合研究"}))["item_id"]
    obligation_id = s.add_scope_item(legal, case_id, ScopeItemInput(
        "obligation", "global", "已入组患者随访与安全性报告", "ongoing",
        0, "Original §12"))["item_id"]

    # 争议期内不能定稿
    try:
        s.finalize_scope(legal, case_id)
        raise AssertionError("争议期内不应允许定稿")
    except Exception as e:
        assert "争议期" in str(e)
    advance(days=31)

    finalized = s.finalize_scope(legal, case_id)
    assert finalized["status"] == "scope_finalized"

    # 6) 数据 / 样本 / 地区权利回转交付
    for item_id, evidence in (
        (right_eu, "EU-REVERT-DEED-01"),
        (right_cn, "CN-REVERT-DEED-01"),
        (data_id, "DATA-HANDOVER-MANIFEST-77"),
        (sample_id, "SAMPLE-CHAIN-OF-CUSTODY-12"),
    ):
        s.start_handover(rd, case_id, item_id)
        s.complete_handover(rd, case_id, item_id, evidence)

    # 7) 再许可先暂停，随后正式终止
    s.suspend_affected_part(legal, case_id, sub_id, "暂停 EuroLab 再许可项下新适应症开发")
    s.resolve_sublicense(legal, case_id, sub_id, "terminated", "SUBLICENSE-TERM-NOTICE-3")

    # 8) 共同开发：只暂停受影响部分，随后取得第三方解除函后解除
    s.suspend_affected_part(
        legal, case_id, collab_id,
        "仅暂停适应症 A 队列；适应症 B 联合研究按修订2继续")
    s.resolve_collaboration(legal, case_id, collab_id, "released",
                            "CRO-RELEASE-LETTER-9", "CRO 承诺已解除")

    # 9) 全部前置动作完成前不能关闭（此处应成功：义务 ongoing 不阻断）
    closed = s.close_case(legal, case_id, "权利回转完成，患者随访义务按原协议持续")
    assert closed["status"] == "closed"

    # 10) 关闭后迟到的数据更正件：只登记，不重开
    late = s.record_late_material(
        rd, case_id, "data_correction", "EMAIL-NW-2026-11-44",
        "对方关闭后补寄的实验室原始数据更正")
    assert late["case_status"] == "closed" and late["case_reopened"] is False
    assert s.case(legal, case_id)["status"] == "closed"

    # 关闭后任何状态变更动作都被拒绝
    try:
        s.start_handover(rd, case_id, data_id, "迟到材料触发")
        raise AssertionError("已关闭案件不应允许新交接")
    except Exception as e:
        assert "已关闭" in str(e)

    # 11) 恢复自研 / 重新授权放行
    resume = s.request_clearance(legal, case_id, "resume_self_research")
    assert resume["decision"] == "granted"
    relicense = s.request_clearance(legal, case_id, "relicense", region="CN")
    assert relicense["decision"] == "granted"

    # 12) 查询结果：可支配权利、遗留义务、时间线
    position = s.rights_position(legal, case_id)
    timeline = s.timeline(legal, case_id)
    assert position["closed"] is True
    assert len(position["current_disposable_rights"]) >= 4  # EU/CN 权利 + 数据 + 样本
    assert len(position["legacy_obligations"]) == 1
    assert position["legacy_obligations"][0]["subject"].startswith("已入组患者")
    assert position["unfinished_handovers"] == []
    assert len(position["late_materials"]) == 1
    event_types = [e["event_type"] for e in timeline["events"]]
    assert "notice.repeated" in event_types
    assert "late_material.recorded" in event_types
    assert "case.closed" in event_types

    # 13) 关闭后再来一份新通知，仍然不产生第二次回转
    after = s.open_termination_case(
        legal, "AGR-X1", "X1", "strategic_adjustment", "新情况", "NW-TERM-2026-102",
        "2026-11-20T00:00:00+00:00", 30)
    assert after["duplicate"] is True and after["case"]["case_id"] == case_id
    event_types = [e["event_type"] for e in s.timeline(legal, case_id)["events"]]
    assert "notice.after_close_ignored" in event_types

    return {
        "status": "ok",
        "case_id": case_id,
        "schema": inspect_schema(s.db),
        "case_status": "closed",
        "timeline_event_count": len(timeline["events"]),
        "disposable_rights": len(position["current_disposable_rights"]),
        "legacy_obligations": len(position["legacy_obligations"]),
        "late_materials_recorded": len(position["late_materials"]),
        "resume_self_research": resume["decision"],
        "relicense": relicense["decision"],
    }


def main():
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
