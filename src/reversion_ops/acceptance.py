"""交易终止与权利回转的离线命令行验收。

在内存 SQLite 上完整走一遍：原协议+修订登记、部分终止通知（重复通知幂等）、
争议期内只暂停受影响合作、资产清单引用原协议及修订、数据/样本交付、
再许可处置、前置动作门控关闭、迟到材料不重开、恢复自研/重新授权放行，
最后输出当前可支配权利、遗留义务、未完成交接和时间线完整性。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import ReversionService
from .storage import inspect_schema


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
    service = ReversionService(connection, clock)

    for user_id, name, role in (
        ("legal-1", "法务负责人", "legal"),
        ("rd-1", "研发负责人", "rd"),
        ("bd-1", "商务拓展", "bd"),
        ("receiver-1", "接收方代表", "receiver"),
        ("auditor-1", "审计人员", "auditor"),
    ):
        service.create_user(user_id, name, role)

    # 原协议及第一次修订；交易先引用原协议，再采纳修订
    service.register_agreement_revision("legal-1", {
        "agreement_id": "agr-x", "revision_seq": 0, "title": "化合物 A 区域授权协议",
        "kind": "original", "effective_date": "2023-05-01", "content_sha256": "a" * 64,
    })
    service.create_deal("legal-1", "deal-a", "化合物 A 对外授权", "agr-x")
    service.register_agreement_revision("legal-1", {
        "agreement_id": "agr-x", "revision_seq": 1, "title": "第一次修订（扩展适应症与数据归属）",
        "kind": "amendment", "effective_date": "2025-02-01",
        "supersedes_revision": 0, "content_sha256": "b" * 64,
    })
    service.adopt_agreement_revision("legal-1", "deal-a", 1)

    # 中国大陆权利本次终止；境外权利不受影响、继续执行
    service.add_territory("rd-1", "deal-a", {
        "territory_id": "t-cn", "region": "中国大陆", "rights_scope": "独家开发与商业化", "affected": True})
    service.add_territory("rd-1", "deal-a", {
        "territory_id": "t-row", "region": "境外其余地区", "rights_scope": "独家开发与商业化", "affected": False})
    # 受影响的再许可、不受影响的共同开发、受影响的第三方供应承诺
    service.add_third_party_grant("bd-1", "deal-a", {
        "grant_id": "g-sub", "territory_id": "t-cn", "counterparty": "境内分许可方",
        "grant_kind": "sublicense", "affected": True})
    service.add_third_party_grant("bd-1", "deal-a", {
        "grant_id": "g-co", "territory_id": "t-row", "counterparty": "境外共同开发方",
        "grant_kind": "co_development", "affected": False, "detail": "境外共同开发不受本次终止影响"})
    service.add_third_party_grant("bd-1", "deal-a", {
        "grant_id": "g-tp", "counterparty": "原料药供应方",
        "grant_kind": "third_party_commitment", "affected": True})

    notice = {
        "termination_id": "term-1", "deal_id": "deal-a", "notice_key": "notice-2026-001",
        "reason": "合作方战略调整，退回中国大陆地区权利", "dispute_window_days": 30,
        "effective_date": "2026-10-02", "partial_scope": True,
        "scope_note": "仅终止中国大陆地区；境外共同开发继续执行",
    }
    first = service.notify_termination("legal-1", notice)
    repeated = service.notify_termination("legal-1", notice)  # 重复通知不产生第二次回转

    # 争议期内提出争议：只暂停受影响部分
    service.raise_dispute("legal-1", "term-1", "对方对数据归属范围有异议")
    rights_during_dispute = service.deal_rights("auditor-1", "deal-a")
    co_dev_kept = next(g for g in rights_during_dispute["grants"] if g["grant_id"] == "g-co")
    sublicense_suspended = next(g for g in rights_during_dispute["grants"] if g["grant_id"] == "g-sub")

    # 解决争议并越过争议期
    service.resolve_dispute("legal-1", "term-1", "双方确认数据按修订版第 4.2 条归属")
    clock.advance(days=31)

    # 资产清单：范围分别引用原协议与第一次修订
    service.add_asset_item("rd-1", "term-1", {
        "asset_id": "a-rights", "asset_kind": "territory_right", "title": "中国大陆独家权利",
        "source_revision_seq": 0, "source_clause": "第 2.1 条 授权范围", "affected": True})
    service.add_asset_item("rd-1", "term-1", {
        "asset_id": "a-data", "asset_kind": "data", "title": "中国大陆临床数据集",
        "detail": {"patients": 320, "format": "CDISC SDTM"},
        "source_revision_seq": 1, "source_clause": "第 4.2 条 数据归属与返还", "affected": True})
    service.add_asset_item("rd-1", "term-1", {
        "asset_id": "a-sample", "asset_kind": "sample", "title": "生物样本库剩余样本",
        "source_revision_seq": 1, "source_clause": "第 4.3 条 样本处置", "affected": True})
    for asset_id in ("a-rights", "a-data", "a-sample"):
        service.confirm_asset("rd-1", asset_id)
    # 样本按约定托管延续，成为遗留义务
    service.dispose_asset("rd-1", "a-sample", "held_over", "样本检测周期未结束，按修订版托管至 2027 年")

    service.begin_wind_down("legal-1", "term-1")

    # 数据交付并被接收
    service.submit_handover("rd-1", "term-1", {
        "batch_id": "b-data-1", "asset_id": "a-data",
        "manifest": {"files": ["sdtm.zip", "audit_trail.csv"], "bytes": 8421000},
        "content_sha256": "c" * 64})
    service.receive_handover("receiver-1", "term-1", "b-data-1", True, "数据集与清单一致")

    # 受影响再许可终止；第三方供应承诺延续；不受影响共同开发不得被处置
    service.dispose_grant("bd-1", "g-sub", "terminated", "随区域权利回转终止再许可")
    service.dispose_grant("bd-1", "g-tp", "surviving", "原料药供货义务延续至在研批次结束")
    try:
        service.dispose_grant("bd-1", "g-co", "terminated", "尝试终止不受影响共同开发")
        co_disposal = "wrongly_allowed"
    except InvalidState:
        co_disposal = "rejected"

    # 关闭前置动作：未满足时关闭被阻断
    service.add_prerequisite("rd-1", "term-1", "p-close", "首付款后财务结算确认", "close")
    service.add_prerequisite("rd-1", "term-1", "p-resume", "回收数据内部核对", "resume_dev")
    service.add_prerequisite("bd-1", "term-1", "p-relicense", "重新授权策略审批", "relicense")
    try:
        service.close_termination("legal-1", "term-1")
        first_attempt = "wrongly_closed"
    except InvalidState as exc:
        first_attempt = f"blocked:{str(exc)[:20]}"
    service.resolve_prerequisite("rd-1", "p-close", True, "财务已结清")
    closed = service.close_termination("legal-1", "term-1", "closed_with_obligations")

    # 关闭后迟到材料只归档，不重开决定
    late = service.register_late_material(
        "receiver-1", "term-1", "补充移交的原始电子签名文件", "d" * 64, asset_id="a-data")

    # 恢复自研 / 重新授权必须在各自前置动作完成后放行
    try:
        service.grant_resumption("rd-1", "deal-a", "resume_dev", "t-cn")
        resume_first = "wrongly_granted"
    except InvalidState:
        resume_first = "blocked"
    service.resolve_prerequisite("rd-1", "p-resume", True, "数据核对完成")
    resume = service.grant_resumption("rd-1", "deal-a", "resume_dev", "t-cn")
    service.resolve_prerequisite("bd-1", "p-relicense", True, "重新授权策略获批")
    relicense = service.grant_resumption("bd-1", "deal-a", "relicense", "t-cn")

    rights = service.deal_rights("auditor-1", "deal-a")
    overview = service.termination_overview("auditor-1", "term-1")
    chain = service.verify_timeline("auditor-1", "term-1")
    schema = inspect_schema(connection)

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "duplicate_notice_collapsed": repeated["duplicate"] is True
        and repeated["termination_id"] == first["termination_id"],
        "co_development_kept_during_dispute": co_dev_kept["state"] == "active",
        "sublicense_suspended_during_dispute": sublicense_suspended["state"] == "suspended",
        "unaffected_co_disposal": co_disposal,
        "first_close_attempt": first_attempt,
        "closed_state": closed["state"],
        "deal_state_after_partial_close": closed["deal_state"],
        "late_material_reopened": late["reopened"],
        "resume_first_attempt": resume_first,
        "resume_clearance": resume["clearance_kind"],
        "relicense_clearance": relicense["clearance_kind"],
        "current_disposable_regions": [r["region"] for r in rights["current_disposable_rights"]],
        "licensed_territories_remaining": [
            t["territory_id"] for t in rights["territories"] if t["state"] == "licensed"],
        "residual_obligation_kinds": sorted({
            k for item in rights["residual_obligations"]
            for k in (("asset_kind",) if "asset_kind" in item else ("grant_kind",))}),
        "incomplete_handover": rights["incomplete_handover"],
        "timeline_events": chain["events"],
        "timeline_valid": chain["valid"],
        "close_decision": overview["close_decision"]["close_decision_id"],
        "agreement_revisions_cited": sorted(
            {a["source_revision_seq"] for a in overview["assets"]}),
        "schema": schema,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行交易终止与权利回转离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
