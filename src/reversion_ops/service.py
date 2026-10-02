"""交易终止与权利回转的领域用例。

流程主线（状态只能沿主线推进，时间线事件只追加）：

    终止通知 noticed
        └─ 争议期内提出争议 → dispute_pending ──解决──┐
        │                                            │
        └──────── 争议期届满且无未决争议 ────────────┴→ winding_down
                        资产清单确认、数据/样本交付、再许可处置
                        └─ 全部前置动作满足 → ready → closed（关闭决定不可重开）

恢复自研 / 重新授权必须在关闭且对应前置动作全部满足后放行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .contracts import (
    AgreementRevision,
    AssetItemInput,
    HandoverSubmission,
    TerminationNotice,
    Territory,
    ThirdPartyGrant,
    ValidationError,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction

ROLE_PERMISSIONS = {
    "legal": {
        "agreement.write", "deal.write", "termination.notify", "dispute.write",
        "winddown.run", "close.decide", "report.read", "audit.read", "timeline.read",
    },
    "rd": {
        "territory.write", "asset.write", "handover.write", "prerequisite.write",
        "clearance.resume_dev", "report.read", "timeline.read",
    },
    "bd": {
        "grant.write", "prerequisite.write", "clearance.relicense", "report.read", "timeline.read",
    },
    "receiver": {
        "handover.receive", "late_material.register", "report.read", "timeline.read",
    },
    "auditor": {"report.read", "audit.read", "timeline.read"},
}

AFFECTED_GRANT_DISPOSABLE = {"terminated", "surviving"}
CLOSE_PREREQUISITES = ("close", "resume_dev", "relicense")
CLEARANCE_PERMISSION = {"resume_dev": "clearance.resume_dev", "relicense": "clearance.relicense"}


class ReversionService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ---- 身份与审计 ------------------------------------------------------

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reversion_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _timeline(
        self,
        termination_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """向该终止案例的不可覆盖时间线追加一个哈希链事件。"""
        previous = self.connection.execute(
            "SELECT event_hash FROM termination_timeline_events WHERE termination_id=? "
            "ORDER BY event_id DESC LIMIT 1",
            (termination_id,),
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        occurred_at = self._now()
        body = {
            "termination_id": termination_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO termination_timeline_events(termination_id,event_type,actor_id,payload_json,"
            "occurred_at,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?)",
            (
                termination_id, event_type, actor_id, canonical_json(payload),
                occurred_at, previous_hash, event_hash,
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reversion_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ---- 原协议及历次修订 ------------------------------------------------

    def register_agreement_revision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "agreement.write")
        try:
            revision = AgreementRevision.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            if revision.kind == "amendment":
                parent = self.connection.execute(
                    "SELECT revision_seq FROM agreements WHERE agreement_id=? AND revision_seq=?",
                    (revision.agreement_id, revision.supersedes_revision),
                ).fetchone()
                if parent is None:
                    raise ValidationFailed("修订引用的前序版本不存在")
            try:
                self.connection.execute(
                    "INSERT INTO agreements(agreement_id,revision_seq,title,kind,effective_date,"
                    "supersedes_revision,content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        revision.agreement_id, revision.revision_seq, revision.title, revision.kind,
                        revision.effective_date, revision.supersedes_revision, revision.content_sha256,
                        actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("协议版本或内容摘要已经登记") from exc
        return {
            "agreement_id": revision.agreement_id,
            "revision_seq": revision.revision_seq,
            "kind": revision.kind,
        }

    def _agreement_revisions(self, agreement_id: str) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=? ORDER BY revision_seq", (agreement_id,)
        ).fetchall()
        if not rows:
            raise NotFound("原协议不存在")
        return rows

    def create_deal(self, actor_id: str, deal_id: str, name: str, agreement_id: str) -> dict[str, Any]:
        self._require(actor_id, "deal.write")
        revisions = self._agreement_revisions(agreement_id)
        if revisions[0]["kind"] != "original" or revisions[0]["revision_seq"] != 0:
            raise ValidationFailed("必须先登记原协议（revision_seq=0）")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO deals(deal_id,name,agreement_id,current_agreement_revision,state,"
                    "created_by,created_at) VALUES(?,?,?,0,'active',?,?)",
                    (deal_id, name, agreement_id, actor_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("交易编号已经存在") from exc
        return {"deal_id": deal_id, "agreement_id": agreement_id, "state": "active"}

    def adopt_agreement_revision(self, actor_id: str, deal_id: str, revision_seq: int) -> dict[str, Any]:
        """把一次已登记修订纳入交易的回转依据（历次修订链）。"""
        self._require(actor_id, "deal.write")
        with transaction(self.connection, immediate=True):
            deal = self._deal_row(deal_id)
            if revision_seq <= deal["current_agreement_revision"]:
                raise ValidationFailed("只能采纳更新的协议修订")
            target = self.connection.execute(
                "SELECT revision_seq FROM agreements WHERE agreement_id=? AND revision_seq=? AND kind='amendment'",
                (deal["agreement_id"], revision_seq),
            ).fetchone()
            if target is None:
                raise NotFound("该协议修订不存在")
            self.connection.execute(
                "UPDATE deals SET current_agreement_revision=? WHERE deal_id=?",
                (revision_seq, deal_id),
            )
        return {"deal_id": deal_id, "current_agreement_revision": revision_seq}

    def add_territory(self, actor_id: str, deal_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "territory.write")
        self._deal_row(deal_id)
        try:
            territory = Territory.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO deal_territories(territory_id,deal_id,region,rights_scope,affected,state) "
                    "VALUES(?,?,?,?,?,'licensed')",
                    (territory.territory_id, deal_id, territory.region, territory.rights_scope,
                     1 if territory.affected else 0),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("地区编号或地区名称冲突") from exc
        return {"territory_id": territory.territory_id, "affected": territory.affected}

    def add_third_party_grant(self, actor_id: str, deal_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "grant.write")
        self._deal_row(deal_id)
        try:
            grant = ThirdPartyGrant.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            if grant.territory_id is not None and not self.connection.execute(
                "SELECT 1 FROM deal_territories WHERE territory_id=? AND deal_id=?",
                (grant.territory_id, deal_id),
            ).fetchone():
                raise NotFound("引用的地区不存在")
            try:
                self.connection.execute(
                    "INSERT INTO third_party_grants(grant_id,deal_id,territory_id,counterparty,grant_kind,"
                    "affected,state,detail,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                    (grant.grant_id, deal_id, grant.territory_id, grant.counterparty, grant.grant_kind,
                     1 if grant.affected else 0, grant.detail, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("再许可/第三方承诺编号冲突") from exc
        return {"grant_id": grant.grant_id, "affected": grant.affected, "state": "active"}

    def _deal_row(self, deal_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
        if row is None:
            raise NotFound("交易不存在")
        return row

    # ---- 1) 终止通知（幂等：重复通知不产生第二次回转） --------------------

    def notify_termination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "termination.notify")
        try:
            notice = TerminationNotice.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        request_digest = content_digest(raw)
        duplicate = self.connection.execute(
            "SELECT request_sha256,response_json FROM reversion_idempotency "
            "WHERE scope='termination_notice' AND idempotency_key=?",
            (notice.notice_key,),
        ).fetchone()
        if duplicate is not None:
            if duplicate["request_sha256"] != request_digest:
                raise Conflict("同一通知键对应了不同的通知内容")
            return {**json.loads(duplicate["response_json"]), "duplicate": True}
        deal = self._deal_row(notice.deal_id)
        if deal["state"] in {"noticed", "dispute_pending", "winding_down", "closed"}:
            raise InvalidState("该交易已有进行中或已关闭的终止案例")
        now = self.clock.now()
        dispute_ends_at = utc_text(now + timedelta(days=notice.dispute_window_days))
        response = {
            "termination_id": notice.termination_id,
            "deal_id": notice.deal_id,
            "state": "noticed",
            "duplicate": False,
        }
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO terminations(termination_id,deal_id,state,notice_key,reason,"
                    "dispute_window_days,effective_date,dispute_ends_at,partial_scope,scope_note,"
                    "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        notice.termination_id, notice.deal_id, "noticed", notice.notice_key, notice.reason,
                        notice.dispute_window_days, notice.effective_date, dispute_ends_at,
                        1 if notice.partial_scope else 0, notice.scope_note,
                        actor_id, utc_text(now), utc_text(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("终止案例或通知键冲突") from exc
            self.connection.execute(
                "UPDATE deals SET state='noticed' WHERE deal_id=?", (notice.deal_id,)
            )
            self.connection.execute(
                "INSERT INTO reversion_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('termination_notice',?,?,?,?)",
                (notice.notice_key, request_digest,
                 canonical_json({k: v for k, v in response.items() if k != "duplicate"}), utc_text(now)),
            )
            self._timeline(notice.termination_id, "termination.noticed", actor_id, {
                "deal_id": notice.deal_id,
                "notice_key": notice.notice_key,
                "reason": notice.reason,
                "partial_scope": notice.partial_scope,
                "dispute_window_days": notice.dispute_window_days,
                "dispute_ends_at": dispute_ends_at,
                "agreement_basis_revision": deal["current_agreement_revision"],
            })
        return response

    def _termination_row(self, termination_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM terminations WHERE termination_id=?", (termination_id,)
        ).fetchone()
        if row is None:
            raise NotFound("终止案例不存在")
        return row

    # ---- 2) 争议期：只暂停受影响部分 ------------------------------------

    def raise_dispute(self, actor_id: str, termination_id: str, summary: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        if not summary.strip():
            raise ValidationFailed("争议说明不能为空")
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] != "noticed":
                raise InvalidState("只有通知后的争议期内可以提出争议")
            open_dispute = self.connection.execute(
                "SELECT 1 FROM disputes WHERE termination_id=? AND state='open'", (termination_id,)
            ).fetchone()
            if open_dispute:
                raise Conflict("已有未解决的争议")
            dispute_id = "disp-" + hashlib.sha256(
                f"{termination_id}|{self._now()}|{summary}".encode()
            ).hexdigest()[:16]
            self.connection.execute(
                "INSERT INTO disputes(dispute_id,termination_id,raised_by,summary,state,created_at) "
                "VALUES(?,?,?,?, 'open',?)",
                (dispute_id, termination_id, actor_id, summary, self._now()),
            )
            # 共同开发 / 再许可 / 第三方承诺：只暂停被本次终止波及的部分
            suspended = self.connection.execute(
                "UPDATE third_party_grants SET state='suspended' "
                "WHERE deal_id=? AND affected=1 AND state='active'",
                (termination["deal_id"],),
            ).rowcount
            self.connection.execute(
                "UPDATE terminations SET state='dispute_pending',updated_at=? WHERE termination_id=?",
                (self._now(), termination_id),
            )
            self._timeline(termination_id, "dispute.raised", actor_id,
                           {"dispute_id": dispute_id, "suspended_grants": suspended})
        return {"termination_id": termination_id, "state": "dispute_pending"}

    def resolve_dispute(self, actor_id: str, termination_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            dispute = self.connection.execute(
                "SELECT * FROM disputes WHERE termination_id=? AND state='open' ORDER BY created_at DESC LIMIT 1",
                (termination_id,),
            ).fetchone()
            if dispute is None:
                raise InvalidState("没有未解决的争议")
            self.connection.execute(
                "UPDATE disputes SET state='resolved',resolution_note=?,resolved_at=? WHERE dispute_id=?",
                (note, self._now(), dispute["dispute_id"]),
            )
            self.connection.execute(
                "UPDATE terminations SET state='noticed',dispute_resolved_at=?,updated_at=? WHERE termination_id=?",
                (self._now(), self._now(), termination_id),
            )
            self._timeline(termination_id, "dispute.resolved", actor_id, {"note": note})
        return {"termination_id": termination_id, "state": "noticed"}

    # ---- 3) 资产清单（范围引用原协议及历次修订） -------------------------

    def add_asset_item(self, actor_id: str, termination_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "asset.write")
        try:
            item = AssetItemInput.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] == "closed":
                raise InvalidState("终止已关闭，不能再增列资产；迟到材料请登记为关闭后材料")
            deal = self._deal_row(termination["deal_id"])
            cited = self.connection.execute(
                "SELECT kind FROM agreements WHERE agreement_id=? AND revision_seq=?",
                (deal["agreement_id"], item.source_revision_seq),
            ).fetchone()
            if cited is None:
                raise ValidationFailed("回转范围必须引用已登记的原协议或历次修订版本")
            if item.source_revision_seq > deal["current_agreement_revision"]:
                raise ValidationFailed("引用的修订尚未被交易采纳")
            try:
                self.connection.execute(
                    "INSERT INTO asset_items(asset_id,termination_id,asset_kind,title,detail_json,"
                    "source_agreement_id,source_revision_seq,source_clause,affected,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.asset_id, termination_id, item.asset_kind, item.title,
                        canonical_json(item.detail), deal["agreement_id"], item.source_revision_seq,
                        item.source_clause, 1 if item.affected else 0, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("资产清单项冲突") from exc
            self._timeline(termination_id, "asset.listed", actor_id, {
                "asset_id": item.asset_id,
                "asset_kind": item.asset_kind,
                "source": f"{deal['agreement_id']}@r{item.source_revision_seq}#{item.source_clause}",
                "affected": item.affected,
            })
        return {"asset_id": item.asset_id, "status": "pending"}

    def confirm_asset(self, actor_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "asset.write")
        with transaction(self.connection, immediate=True):
            row = self._asset_row(asset_id)
            if row["status"] != "pending":
                raise InvalidState("资产清单项已确认或已处置")
            self.connection.execute(
                "UPDATE asset_items SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE asset_id=?",
                (actor_id, self._now(), asset_id),
            )
            self._timeline(row["termination_id"], "asset.confirmed", actor_id, {"asset_id": asset_id})
        return {"asset_id": asset_id, "status": "confirmed"}

    def dispose_asset(self, actor_id: str, asset_id: str, status: str, note: str) -> dict[str, Any]:
        """对无法/无需回转的资产给出最终处置（held_over/excluded/not_required）。"""
        self._require(actor_id, "asset.write")
        if status not in {"held_over", "excluded", "not_required"}:
            raise ValidationFailed("不支持的资产处置状态")
        if not note.strip():
            raise ValidationFailed("处置说明不能为空")
        with transaction(self.connection, immediate=True):
            row = self._asset_row(asset_id)
            if row["status"] in {"returned", "held_over", "excluded", "not_required"}:
                raise InvalidState("资产已有终局处置")
            self.connection.execute(
                "UPDATE asset_items SET status=? WHERE asset_id=?", (status, asset_id)
            )
            self._timeline(row["termination_id"], "asset.disposed", actor_id,
                           {"asset_id": asset_id, "status": status, "note": note})
        return {"asset_id": asset_id, "status": status}

    def _asset_row(self, asset_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM asset_items WHERE asset_id=?", (asset_id,)).fetchone()
        if row is None:
            raise NotFound("资产清单项不存在")
        return row

    # ---- 进入收尾（争议期届满 + 清单确认） -------------------------------

    def begin_wind_down(self, actor_id: str, termination_id: str) -> dict[str, Any]:
        self._require(actor_id, "winddown.run")
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] not in {"noticed", "winding_down"}:
                raise InvalidState("争议未解决，不能进入回转收尾")
            open_dispute = self.connection.execute(
                "SELECT 1 FROM disputes WHERE termination_id=? AND state='open'", (termination_id,)
            ).fetchone()
            if open_dispute:
                raise InvalidState("争议期内存在未解决争议，受影响部分继续暂停")
            if self.clock.now() < parse_utc(termination["dispute_ends_at"]):
                raise InvalidState("争议期尚未届满，不能进入回转收尾")
            unconfirmed = self.connection.execute(
                "SELECT count(*) FROM asset_items WHERE termination_id=? AND affected=1 AND status='pending'",
                (termination_id,),
            ).fetchone()[0]
            affected_count = self.connection.execute(
                "SELECT count(*) FROM asset_items WHERE termination_id=? AND affected=1",
                (termination_id,),
            ).fetchone()[0]
            if affected_count == 0:
                raise InvalidState("受影响资产清单为空，不能冻结回转范围")
            if unconfirmed:
                raise InvalidState("仍有受影响资产未经确认")
            if termination["state"] != "winding_down":
                self.connection.execute(
                    "UPDATE terminations SET state='winding_down',updated_at=? WHERE termination_id=?",
                    (self._now(), termination_id),
                )
                deal = self._deal_row(termination["deal_id"])
                revisions = [
                    dict(r) for r in self.connection.execute(
                        "SELECT revision_seq,kind,effective_date FROM agreements "
                        "WHERE agreement_id=? ORDER BY revision_seq", (deal["agreement_id"],)
                    ).fetchall()
                ]
                self._timeline(termination_id, "winddown.began", actor_id, {
                    "scope_basis": {
                        "agreement_id": deal["agreement_id"],
                        "current_revision": deal["current_agreement_revision"],
                        "revisions": revisions,
                    },
                    "partial_scope": bool(termination["partial_scope"]),
                })
        return {"termination_id": termination_id, "state": "winding_down"}

    # ---- 4) 数据 / 样本交付 ---------------------------------------------

    def submit_handover(self, actor_id: str, termination_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "handover.write")
        try:
            submission = HandoverSubmission.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] == "dispute_pending":
                raise InvalidState("争议未解决，受影响的数据/样本交付暂停")
            if termination["state"] == "closed":
                raise InvalidState("终止已关闭，交付通道不再接收新材料")
            asset = self.connection.execute(
                "SELECT * FROM asset_items WHERE asset_id=? AND termination_id=?",
                (submission.asset_id, termination_id),
            ).fetchone()
            if asset is None:
                raise NotFound("交付对应的资产清单项不存在")
            if not asset["affected"]:
                raise InvalidState("不在回转范围内的资产无需交付")
            digest = submission.content_sha256
            try:
                self.connection.execute(
                    "INSERT INTO handover_batches(batch_id,termination_id,asset_id,manifest_json,"
                    "content_sha256,submitted_by,submitted_at,deadline_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        submission.batch_id, termination_id, submission.asset_id,
                        canonical_json(submission.manifest), digest,
                        actor_id, self._now(), submission.deadline_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("交付批次编号冲突") from exc
            self._timeline(termination_id, "handover.submitted", actor_id,
                           {"batch_id": submission.batch_id, "asset_id": submission.asset_id})
        return {"batch_id": submission.batch_id, "state": "submitted"}

    def receive_handover(
        self, actor_id: str, termination_id: str, batch_id: str, accepted: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "handover.receive")
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] == "closed":
                raise InvalidState("终止已关闭，不能再补办接收")
            batch = self.connection.execute(
                "SELECT * FROM handover_batches WHERE batch_id=? AND termination_id=?",
                (batch_id, termination_id),
            ).fetchone()
            if batch is None:
                raise NotFound("交付批次不存在")
            if batch["state"] != "submitted":
                raise InvalidState("交付批次已处理")
            new_state = "received" if accepted else "rejected"
            self.connection.execute(
                "UPDATE handover_batches SET state=?,received_by=?,received_at=?,receive_note=? WHERE batch_id=?",
                (new_state, actor_id, self._now(), note, batch_id),
            )
            asset_status = None
            if accepted:
                self.connection.execute(
                    "UPDATE asset_items SET status='returned' WHERE asset_id=? AND status IN ('confirmed','pending')",
                    (batch["asset_id"],),
                )
                asset_status = "returned"
            self._timeline(termination_id, "handover.received", actor_id, {
                "batch_id": batch_id, "asset_id": batch["asset_id"],
                "accepted": accepted, "asset_status": asset_status,
            })
        return {"batch_id": batch_id, "state": new_state, "asset_status": asset_status}

    # ---- 再许可 / 共同开发处置 -------------------------------------------

    def dispose_grant(self, actor_id: str, grant_id: str, state: str, note: str) -> dict[str, Any]:
        """终止后对受影响的再许可/共同开发/第三方承诺给出处置。"""
        self._require(actor_id, "grant.write")
        if state not in {"terminated", "surviving"}:
            raise ValidationFailed("再许可处置只能是 terminated 或 surviving")
        if not note.strip():
            raise ValidationFailed("处置说明不能为空")
        with transaction(self.connection, immediate=True):
            grant = self.connection.execute(
                "SELECT * FROM third_party_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if grant is None:
                raise NotFound("再许可/第三方承诺不存在")
            if not grant["affected"]:
                raise InvalidState("不在受影响范围内的合作事项保持执行，不能处置")
            if grant["state"] in {"terminated", "surviving"}:
                raise InvalidState("该合作事项已有终局处置")
            self.connection.execute(
                "UPDATE third_party_grants SET state=? WHERE grant_id=?", (state, grant_id)
            )
            self._timeline_for_deal(grant["deal_id"], "grant.disposed", actor_id, {
                "grant_id": grant_id, "state": state, "note": note,
            })
        return {"grant_id": grant_id, "state": state}

    def _timeline_for_deal(
        self, deal_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        termination = self.connection.execute(
            "SELECT termination_id FROM terminations WHERE deal_id=? ORDER BY created_at DESC LIMIT 1",
            (deal_id,),
        ).fetchone()
        if termination is not None:
            self._timeline(termination["termination_id"], event_type, actor_id, payload)

    # ---- 5) 关闭前置动作 -------------------------------------------------

    def add_prerequisite(
        self, actor_id: str, termination_id: str, prerequisite_id: str, title: str, required_for: str
    ) -> dict[str, Any]:
        self._require(actor_id, "prerequisite.write")
        if required_for not in CLOSE_PREREQUISITES:
            raise ValidationFailed("required_for 必须是 close、resume_dev 或 relicense")
        if not title.strip():
            raise ValidationFailed("前置动作标题不能为空")
        with transaction(self.connection, immediate=True):
            self._termination_row(termination_id)
            try:
                self.connection.execute(
                    "INSERT INTO prerequisites(prerequisite_id,termination_id,title,required_for,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (prerequisite_id, termination_id, title, required_for, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("前置动作编号冲突") from exc
        return {"prerequisite_id": prerequisite_id, "status": "open", "required_for": required_for}

    def resolve_prerequisite(
        self, actor_id: str, prerequisite_id: str, satisfied: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "prerequisite.write")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM prerequisites WHERE prerequisite_id=?", (prerequisite_id,)
            ).fetchone()
            if row is None:
                raise NotFound("前置动作不存在")
            if row["status"] != "open":
                raise InvalidState("前置动作已处理")
            new_status = "satisfied" if satisfied else "waived"
            self.connection.execute(
                "UPDATE prerequisites SET status=?,satisfied_by=?,satisfied_at=?,detail=? WHERE prerequisite_id=?",
                (new_status, actor_id, self._now(), note, prerequisite_id),
            )
            self._timeline(row["termination_id"], "prerequisite.resolved", actor_id,
                           {"prerequisite_id": prerequisite_id, "status": new_status})
        return {"prerequisite_id": prerequisite_id, "status": new_status}

    # ---- 关闭就绪检查 ----------------------------------------------------

    def _readiness(self, termination: sqlite3.Row) -> dict[str, Any]:
        termination_id = termination["termination_id"]
        pending_assets = [
            dict(r) for r in self.connection.execute(
                "SELECT asset_id,asset_kind,title,status FROM asset_items "
                "WHERE termination_id=? AND affected=1 AND status IN ('pending','confirmed') "
                "AND asset_kind <> 'territory_right'",
                (termination_id,),
            ).fetchall()
        ]
        open_batches = [
            dict(r) for r in self.connection.execute(
                "SELECT batch_id,asset_id,state FROM handover_batches "
                "WHERE termination_id=? AND state='submitted'",
                (termination_id,),
            ).fetchall()
        ]
        # 被拒收的批次只有在其资产仍无任何成功接收批次时才计入未完成
        open_batches += [
            dict(r) for r in self.connection.execute(
                "SELECT b.batch_id,b.asset_id,b.state FROM handover_batches b "
                "WHERE b.termination_id=? AND b.state='rejected' AND NOT EXISTS ("
                "SELECT 1 FROM handover_batches r WHERE r.asset_id=b.asset_id AND r.state='received')",
                (termination_id,),
            ).fetchall()
        ]
        open_prerequisites = [
            dict(r) for r in self.connection.execute(
                "SELECT prerequisite_id,title,required_for FROM prerequisites "
                "WHERE termination_id=? AND required_for='close' AND status='open'",
                (termination_id,),
            ).fetchall()
        ]
        unresolved_grants = [
            dict(r) for r in self.connection.execute(
                "SELECT g.grant_id,g.grant_kind,g.state FROM third_party_grants g "
                "JOIN terminations t ON t.deal_id=g.deal_id "
                "WHERE t.termination_id=? AND g.affected=1 AND g.state IN ('active','suspended')",
                (termination_id,),
            ).fetchall()
        ]
        blockers = {
            "assets_not_terminal": pending_assets,
            "handover_open": open_batches,
            "close_prerequisites_open": open_prerequisites,
            "grants_unresolved": unresolved_grants,
        }
        ready = not any(blockers.values())
        return {"ready": ready, "blockers": blockers}

    def close_readiness(self, actor_id: str, termination_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        termination = self._termination_row(termination_id)
        return {"termination_id": termination_id, "state": termination["state"], **self._readiness(termination)}

    # ---- 6) 最终关闭（决定不可重开） -------------------------------------

    def close_termination(
        self, actor_id: str, termination_id: str, decision: str = "closed"
    ) -> dict[str, Any]:
        self._require(actor_id, "close.decide")
        if decision not in {"closed", "closed_with_obligations"}:
            raise ValidationFailed("关闭决定不合法")
        # 就绪门禁在独立事务中落库：即使因阻断而返回错误，preclose_blocked
        # 状态和“关闭受阻”时间线事件也必须保留下来。
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] == "closed":
                raise InvalidState("终止已关闭，关闭决定不可重开")
            if termination["state"] not in {"winding_down", "preclose_blocked"}:
                raise InvalidState("只能在回转收尾阶段关闭")
            readiness = self._readiness(termination)
            blocked = not readiness["ready"]
            if blocked and termination["state"] != "preclose_blocked":
                self.connection.execute(
                    "UPDATE terminations SET state='preclose_blocked',updated_at=? WHERE termination_id=?",
                    (self._now(), termination_id),
                )
            if blocked:
                self._timeline(termination_id, "close.blocked", actor_id, readiness["blockers"])
        if blocked:
            raise InvalidState("仍有未完成交接或前置动作，不能关闭")
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            deal = self._deal_row(termination["deal_id"])
            partial = bool(termination["partial_scope"])
            # 地区权利随关闭决定回转；全部终止时不受逐项 affected 限制
            scope_predicate = "affected=1" if partial else "1=1"
            self.connection.execute(
                "UPDATE asset_items SET status='reverted' "
                f"WHERE termination_id=? AND {scope_predicate} AND asset_kind='territory_right' "
                "AND status IN ('confirmed','pending')",
                (termination_id,),
            )
            rights_snapshot = self._rights_snapshot(deal["deal_id"])
            surviving = [
                dict(r) for r in self.connection.execute(
                    "SELECT asset_id,asset_kind,title,status FROM asset_items "
                    f"WHERE termination_id=? AND {scope_predicate} AND status IN ('held_over','excluded')",
                    (termination_id,),
                ).fetchall()
            ]
            surviving += [
                {"grant_id": r["grant_id"], "grant_kind": r["grant_kind"], "state": r["state"]}
                for r in self.connection.execute(
                    "SELECT grant_id,grant_kind,state FROM third_party_grants "
                    f"WHERE deal_id=? AND {scope_predicate} AND state='surviving'", (deal["deal_id"],)
                ).fetchall()
            ]
            pending_handover = readiness["blockers"]["handover_open"]
            close_decision_id = "close-" + hashlib.sha256(
                f"{termination_id}|{self._now()}".encode()
            ).hexdigest()[:16]
            self.connection.execute(
                "INSERT INTO close_decisions(close_decision_id,termination_id,decision,"
                "rights_snapshot_json,surviving_obligations_json,pending_handover_json,decided_by,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    close_decision_id, termination_id, decision,
                    canonical_json(rights_snapshot), canonical_json(surviving),
                    canonical_json(pending_handover), actor_id, self._now(),
                ),
            )
            # 部分终止只回转受影响地区；全部终止回转所有地区。不受影响部分继续执行
            self.connection.execute(
                f"UPDATE deal_territories SET state='reverted' WHERE deal_id=? AND {scope_predicate}",
                (deal["deal_id"],),
            )
            self.connection.execute(
                "UPDATE third_party_grants SET state='terminated' "
                f"WHERE deal_id=? AND {scope_predicate} AND state='suspended'",
                (deal["deal_id"],),
            )
            remaining = self.connection.execute(
                "SELECT count(*) FROM deal_territories WHERE deal_id=? AND state='licensed'",
                (deal["deal_id"],),
            ).fetchone()[0]
            deal_state = "active" if remaining else "closed"
            self.connection.execute(
                "UPDATE deals SET state=? WHERE deal_id=?", (deal_state, deal["deal_id"])
            )
            self.connection.execute(
                "UPDATE terminations SET state='closed',closed_at=?,close_decision_id=?,updated_at=? "
                "WHERE termination_id=?",
                (self._now(), close_decision_id, self._now(), termination_id),
            )
            self._timeline(termination_id, "termination.closed", actor_id, {
                "close_decision_id": close_decision_id,
                "decision": decision,
                "deal_state": deal_state,
                "surviving_obligations": surviving,
            })
        return {"termination_id": termination_id, "state": "closed", "close_decision_id": close_decision_id,
                "deal_state": deal_state}

    # ---- 迟到材料：不重开已关闭决定 --------------------------------------

    def register_late_material(
        self, actor_id: str, termination_id: str, title: str, content_sha256: str,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "late_material.register")
        if not title.strip():
            raise ValidationFailed("材料标题不能为空")
        if len(content_sha256) != 64:
            raise ValidationFailed("内容摘要必须是 64 位 SHA-256")
        with transaction(self.connection, immediate=True):
            termination = self._termination_row(termination_id)
            if termination["state"] != "closed":
                raise InvalidState("只有关闭后送达的材料才走迟到登记")
            if asset_id is not None and not self.connection.execute(
                "SELECT 1 FROM asset_items WHERE asset_id=? AND termination_id=?",
                (asset_id, termination_id),
            ).fetchone():
                raise NotFound("引用的资产清单项不存在")
            late_id = "late-" + hashlib.sha256(
                f"{termination_id}|{title}|{self._now()}".encode()
            ).hexdigest()[:16]
            self.connection.execute(
                "INSERT INTO late_materials(late_material_id,termination_id,asset_id,title,"
                "content_sha256,received_at,registered_by,disposition) VALUES(?,?,?,?,?,?,?,'archived')",
                (late_id, termination_id, asset_id, title, content_sha256.lower(),
                 self._now(), actor_id),
            )
            self._timeline(termination_id, "late_material.registered", actor_id,
                           {"late_material_id": late_id, "title": title, "asset_id": asset_id})
        return {"late_material_id": late_id, "disposition": "archived", "reopened": False}

    # ---- 恢复自研 / 重新授权放行 -----------------------------------------

    def grant_resumption(
        self,
        actor_id: str,
        deal_id: str,
        clearance_kind: str,
        territory_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        permission = CLEARANCE_PERMISSION.get(clearance_kind)
        if permission is None:
            raise ValidationFailed("放行类型必须是 resume_dev 或 relicense")
        self._require(actor_id, permission)
        with transaction(self.connection, immediate=True):
            deal = self._deal_row(deal_id)
            if territory_id is not None:
                territory = self.connection.execute(
                    "SELECT * FROM deal_territories WHERE territory_id=? AND deal_id=?",
                    (territory_id, deal_id),
                ).fetchone()
                if territory is None:
                    raise NotFound("地区不存在")
                if territory["state"] != "reverted":
                    raise InvalidState("该地区权利尚未回转，不能放行")
            required = [
                dict(r) for r in self.connection.execute(
                    "SELECT prerequisite_id,title FROM prerequisites p JOIN terminations t "
                    "ON t.termination_id=p.termination_id WHERE t.deal_id=? "
                    "AND p.required_for IN ('close',?) AND p.status='open'",
                    (deal_id, clearance_kind),
                ).fetchall()
            ]
            if required:
                raise InvalidState("全部前置动作完成前不能恢复自研/重新授权")
            closed = self.connection.execute(
                "SELECT termination_id FROM terminations WHERE deal_id=? AND state='closed' "
                "ORDER BY closed_at DESC LIMIT 1", (deal_id,)
            ).fetchone()
            if closed is None:
                raise InvalidState("终止尚未最终关闭")
            basis = self._prerequisite_digest(closed["termination_id"])
            try:
                cursor = self.connection.execute(
                    "INSERT INTO resumption_clearances(clearance_id,deal_id,territory_id,clearance_kind,"
                    "prerequisite_digest,granted_by,granted_at,note) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "clr-" + hashlib.sha256(
                            f"{deal_id}|{territory_id or ''}|{clearance_kind}".encode()
                        ).hexdigest()[:16],
                        deal_id, territory_id, clearance_kind, basis, actor_id, self._now(), note,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self.connection.execute(
                    "SELECT clearance_id,prerequisite_digest FROM resumption_clearances "
                    "WHERE deal_id=? AND clearance_kind=? AND IFNULL(territory_id,'')=IFNULL(?, '')",
                    (deal_id, clearance_kind, territory_id),
                ).fetchone()
                if existing is not None and existing["prerequisite_digest"] == basis:
                    return {"clearance_id": existing["clearance_id"], "duplicate": True}
                raise Conflict("放行登记冲突")
            self._timeline(closed["termination_id"], "resumption.granted", actor_id, {
                "clearance_kind": clearance_kind, "territory_id": territory_id,
            })
        return {"deal_id": deal_id, "clearance_kind": clearance_kind,
                "territory_id": territory_id, "prerequisite_digest": basis, "duplicate": False}

    def _prerequisite_digest(self, termination_id: str) -> str:
        rows = [
            dict(r) for r in self.connection.execute(
                "SELECT prerequisite_id,required_for,title,status,satisfied_at FROM prerequisites "
                "WHERE termination_id=? ORDER BY prerequisite_id", (termination_id,)
            ).fetchall()
        ]
        return content_digest(rows)

    # ---- 查询：可支配权利 / 遗留义务 / 未完成交接 ------------------------

    def _rights_snapshot(self, deal_id: str) -> dict[str, Any]:
        territories = [
            dict(r) for r in self.connection.execute(
                "SELECT territory_id,region,rights_scope,affected,state FROM deal_territories "
                "WHERE deal_id=? ORDER BY territory_id", (deal_id,)
            ).fetchall()
        ]
        grants = [
            dict(r) for r in self.connection.execute(
                "SELECT grant_id,territory_id,counterparty,grant_kind,affected,state "
                "FROM third_party_grants WHERE deal_id=? ORDER BY grant_id", (deal_id,)
            ).fetchall()
        ]
        disposable = [
            {"territory_id": t["territory_id"], "region": t["region"], "rights_scope": t["rights_scope"]}
            for t in territories if t["state"] == "reverted"
        ]
        return {"territories": territories, "grants": grants, "disposable_rights": disposable}

    def deal_rights(self, actor_id: str, deal_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        deal = self._deal_row(deal_id)
        snapshot = self._rights_snapshot(deal_id)
        termination_rows = self.connection.execute(
            "SELECT termination_id,state,close_decision_id FROM terminations WHERE deal_id=? ORDER BY created_at",
            (deal_id,),
        ).fetchall()
        surviving_obligations: list[dict[str, Any]] = []
        incomplete_handover: list[dict[str, Any]] = []
        for t in termination_rows:
            surviving_obligations += [
                dict(r) for r in self.connection.execute(
                    "SELECT asset_id,asset_kind,title,status FROM asset_items "
                    "WHERE termination_id=? AND affected=1 AND status IN ('held_over','excluded')",
                    (t["termination_id"],),
                ).fetchall()
            ]
            incomplete_handover += [
                dict(r) for r in self.connection.execute(
                    "SELECT b.batch_id,b.asset_id,b.state,a.title FROM handover_batches b "
                    "JOIN asset_items a ON a.asset_id=b.asset_id "
                    "WHERE b.termination_id=? AND ("
                    "b.state='submitted' OR (b.state='rejected' AND NOT EXISTS ("
                    "SELECT 1 FROM handover_batches r WHERE r.asset_id=b.asset_id AND r.state='received')))",
                    (t["termination_id"],),
                ).fetchall()
            ]
            incomplete_handover += [
                {"prerequisite_id": r["prerequisite_id"], "title": r["title"], "required_for": r["required_for"]}
                for r in self.connection.execute(
                    "SELECT prerequisite_id,title,required_for FROM prerequisites "
                    "WHERE termination_id=? AND status='open'", (t["termination_id"],)
                ).fetchall()
            ]
        # 存续第三方承诺按交易归集一次（不随终止案例数重复）
        surviving_obligations += [
            {"grant_id": r["grant_id"], "grant_kind": r["grant_kind"], "counterparty": r["counterparty"],
             "state": r["state"]}
            for r in self.connection.execute(
                "SELECT DISTINCT grant_id,grant_kind,counterparty,state FROM third_party_grants "
                "WHERE deal_id=? AND affected=1 AND state='surviving' ORDER BY grant_id", (deal_id,)
            ).fetchall()
        ]
        clearances = [
            dict(r) for r in self.connection.execute(
                "SELECT clearance_id,territory_id,clearance_kind,granted_at FROM resumption_clearances "
                "WHERE deal_id=? ORDER BY granted_at", (deal_id,)
            ).fetchall()
        ]
        return {
            "deal_id": deal_id,
            "deal_state": deal["state"],
            "current_disposable_rights": snapshot["disposable_rights"],
            "territories": snapshot["territories"],
            "grants": snapshot["grants"],
            "residual_obligations": surviving_obligations,
            "incomplete_handover": incomplete_handover,
            "clearances": clearances,
        }

    def termination_overview(self, actor_id: str, termination_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        termination = self._termination_row(termination_id)
        deal = self._deal_row(termination["deal_id"])
        assets = [
            dict(r) for r in self.connection.execute(
                "SELECT asset_id,asset_kind,title,status,affected,source_agreement_id,"
                "source_revision_seq,source_clause FROM asset_items WHERE termination_id=? ORDER BY asset_id",
                (termination_id,),
            ).fetchall()
        ]
        batches = [
            dict(r) for r in self.connection.execute(
                "SELECT batch_id,asset_id,state,submitted_at,received_at FROM handover_batches "
                "WHERE termination_id=? ORDER BY batch_id", (termination_id,)
            ).fetchall()
        ]
        prerequisites = [
            dict(r) for r in self.connection.execute(
                "SELECT prerequisite_id,title,required_for,status FROM prerequisites "
                "WHERE termination_id=? ORDER BY required_for,prerequisite_id", (termination_id,)
            ).fetchall()
        ]
        close = self.connection.execute(
            "SELECT * FROM close_decisions WHERE termination_id=?", (termination_id,)
        ).fetchone()
        timeline = self.timeline_events(termination_id)
        revisions = [
            dict(r) for r in self.connection.execute(
                "SELECT revision_seq,kind,effective_date,content_sha256 FROM agreements "
                "WHERE agreement_id=? ORDER BY revision_seq", (deal["agreement_id"],)
            ).fetchall()
        ]
        return {
            "termination": dict(termination),
            "deal": {"deal_id": deal["deal_id"], "name": deal["name"], "state": deal["state"]},
            "agreement_basis": {
                "agreement_id": deal["agreement_id"],
                "current_revision": deal["current_agreement_revision"],
                "revisions": revisions,
            },
            "assets": assets,
            "handover_batches": batches,
            "prerequisites": prerequisites,
            "readiness": self._readiness(termination),
            "close_decision": None if close is None else {
                "close_decision_id": close["close_decision_id"],
                "decision": close["decision"],
                "rights_snapshot": json.loads(close["rights_snapshot_json"]),
                "surviving_obligations": json.loads(close["surviving_obligations_json"]),
                "decided_at": close["decided_at"],
            },
            "timeline": timeline,
        }

    def timeline_events(self, termination_id: str) -> list[dict[str, Any]]:
        return [
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "actor_id": r["actor_id"],
                "payload": json.loads(r["payload_json"]),
                "occurred_at": r["occurred_at"],
                "previous_hash": r["previous_hash"],
                "event_hash": r["event_hash"],
            }
            for r in self.connection.execute(
                "SELECT * FROM termination_timeline_events WHERE termination_id=? ORDER BY event_id",
                (termination_id,),
            ).fetchall()
        ]

    def verify_timeline(self, actor_id: str, termination_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        if termination_id is not None:
            chains = [(termination_id, self.connection.execute(
                "SELECT * FROM termination_timeline_events WHERE termination_id=? ORDER BY event_id",
                (termination_id,),
            ).fetchall())]
        else:
            ids = [r["termination_id"] for r in self.connection.execute(
                "SELECT DISTINCT termination_id FROM termination_timeline_events ORDER BY termination_id"
            ).fetchall()]
            chains = [
                (tid, self.connection.execute(
                    "SELECT * FROM termination_timeline_events WHERE termination_id=? ORDER BY event_id",
                    (tid,),
                ).fetchall())
                for tid in ids
            ]
        valid = True
        count = 0
        heads: list[str] = []
        for _tid, rows in chains:
            previous = "0" * 64
            for row in rows:
                body = {
                    "termination_id": row["termination_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "occurred_at": row["occurred_at"],
                    "previous_hash": row["previous_hash"],
                }
                calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
                if row["previous_hash"] != previous or row["event_hash"] != calculated:
                    valid = False
                    break
                previous = row["event_hash"]
                count += 1
            heads.append(previous)
            if not valid:
                break
        return {"valid": valid, "events": count, "chains": len(chains),
                "head_hash": heads[0] if len(heads) == 1 else None}
