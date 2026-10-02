"""交易终止与权利回转的事务化应用服务。

关键不变量：
- 通知指纹去重 + 协议级未关闭案件唯一索引，重复通知不会产生第二次回转；
- 时间线事件只追加（term_timeline_events），状态字段可更新但历史不覆盖；
- 回转范围项必须引用原协议（seq=0）或已登记的历次修订；
- 共同开发 / 第三方承诺可只暂停受影响部分，其余合作继续；
- 关闭是终态：迟到材料只登记留痕，绝不重开已关闭决定；
- 恢复自研 / 重新授权必须取得放行，放行前核验全部前置动作。
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from .auth import Auth
from .termination import (
    COLLABORATION_OUTCOMES,
    CASE_TRANSITIONS,
    CLEARANCE_KINDS,
    FINALIZED_ITEM_STATUS,
    SUBLICENSE_ACTIONS,
    InvalidCaseState,
    PrerequisiteError,
    ScopeItemInput,
    ScopeReferenceError,
    TerminationError,
    add_days,
    assert_open,
    clearance_blockers,
    close_blockers,
    dispute_window_ready,
    ensure_transition,
    notice_fingerprint,
    parse_time,
)
from .termination_storage import (
    event,
    initialize,
    rows,
    transaction,
    utcnow_text,
)


class TerminationService:
    def __init__(self, database: str = ":memory:", clock=None) -> None:
        if isinstance(database, sqlite3.Connection):
            self.db = database
        else:
            self.db = sqlite3.connect(database, timeout=10, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
        self.auth = Auth(self.db)
        initialize(self.db)
        self.clock = clock or utcnow_text

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock()

    def _actor(self, token: str, permission: str):
        return self.auth.require(token, permission)

    def _get_case(self, case_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM term_cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        return row

    def _get_item(self, case_id: str, item_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM term_scope_items WHERE case_id=? AND item_id=?", (case_id, item_id)
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return row

    def _items(self, case_id: str) -> list[dict]:
        return rows(
            self.db,
            "SELECT * FROM term_scope_items WHERE case_id=? ORDER BY item_id",
            (case_id,),
        )

    def _snapshot(self, case: sqlite3.Row) -> "CaseSnapshot":
        from .termination import CaseSnapshot

        return CaseSnapshot(
            status=case["status"],
            dispute_window_ends_at=case["dispute_window_ends_at"],
            has_open_dispute=bool(
                self.db.execute(
                    "SELECT 1 FROM term_disputes WHERE case_id=? AND status='open' LIMIT 1",
                    (case["case_id"],),
                ).fetchone()
            ),
            items=tuple(self._items(case["case_id"])),
            now=self._now(),
        )

    def _touch(self, case_id: str) -> None:
        self.db.execute(
            "UPDATE term_cases SET updated_at=? WHERE case_id=?", (self._now(), case_id)
        )

    # ------------------------------------------------------------------
    # 原协议与历次修订（回转范围的引用基础）
    # ------------------------------------------------------------------

    def register_agreement(self, token, agreement_id, title, counterparty, signed_at):
        actor = self._actor(token, "agreement.register")
        parse_time(signed_at)
        if not title.strip() or not counterparty.strip():
            raise TerminationError("协议名称与对方为必填项")
        with transaction(self.db):
            if self.db.execute(
                "SELECT 1 FROM term_agreements WHERE agreement_id=?", (agreement_id,)
            ).fetchone():
                raise TerminationError("协议已登记")
            self.db.execute(
                "INSERT INTO term_agreements VALUES(?,?,?,?,?,?)",
                (agreement_id, title, counterparty, signed_at, 0, self._now()),
            )
            event(self.db, None, "agreement.registered", actor.user_id,
                  {"agreement_id": agreement_id, "title": title, "counterparty": counterparty})
        return self.agreement(token, agreement_id)

    def add_amendment(self, token, agreement_id, amendment_seq, title, signed_at, summary):
        actor = self._actor(token, "agreement.register")
        parse_time(signed_at)
        if amendment_seq <= 0 or not title.strip():
            raise TerminationError("修订序号必须为正整数且标题必填")
        with transaction(self.db):
            row = self.db.execute(
                "SELECT current_amendment_seq FROM term_agreements WHERE agreement_id=?",
                (agreement_id,),
            ).fetchone()
            if row is None:
                raise KeyError(agreement_id)
            if amendment_seq != row["current_amendment_seq"] + 1:
                raise ScopeReferenceError(
                    f"修订序号必须连续：期望 {row['current_amendment_seq'] + 1}，收到 {amendment_seq}"
                )
            self.db.execute(
                "INSERT INTO term_agreement_amendments VALUES(?,?,?,?,?,?)",
                (agreement_id, amendment_seq, title, signed_at, summary, self._now()),
            )
            self.db.execute(
                "UPDATE term_agreements SET current_amendment_seq=? WHERE agreement_id=?",
                (amendment_seq, agreement_id),
            )
            event(self.db, None, "agreement.amendment_added", actor.user_id,
                  {"agreement_id": agreement_id, "amendment_seq": amendment_seq, "title": title})
        return self.agreement(token, agreement_id)

    def agreement(self, token, agreement_id):
        self._actor(token, "read")
        row = self.db.execute(
            "SELECT * FROM term_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise KeyError(agreement_id)
        result = dict(row)
        result["amendments"] = rows(
            self.db,
            "SELECT agreement_id,amendment_seq,title,signed_at,summary "
            "FROM term_agreement_amendments WHERE agreement_id=? ORDER BY amendment_seq",
            (agreement_id,),
        )
        return result

    def _validate_scope_reference(self, agreement_id: str, source_amendment_seq: int) -> None:
        row = self.db.execute(
            "SELECT current_amendment_seq FROM term_agreements WHERE agreement_id=?",
            (agreement_id,),
        ).fetchone()
        if row is None:
            raise KeyError(agreement_id)
        if source_amendment_seq > row["current_amendment_seq"]:
            raise ScopeReferenceError(
                f"范围引用了不存在的修订序号 {source_amendment_seq}；"
                f"协议 {agreement_id} 当前最新修订为 {row['current_amendment_seq']}（0=原协议）"
            )

    # ------------------------------------------------------------------
    # 终止通知（重复通知不产生第二次回转）
    # ------------------------------------------------------------------

    def open_termination_case(
        self, token, agreement_id, title, trigger_type, reason, notice_key,
        notice_received_at, dispute_window_days, effective_termination_at=None,
    ):
        actor = self._actor(token, "termination.write")
        parse_time(notice_received_at)
        if trigger_type not in ("strategic_adjustment", "clinical_outcome", "contractual", "mutual"):
            raise TerminationError("未知终止触发类型")
        if not reason.strip() or not notice_key.strip():
            raise TerminationError("终止原因与通知编号为必填项")
        window_ends = add_days(notice_received_at, dispute_window_days)
        fingerprint = notice_fingerprint(agreement_id, notice_key)
        case_id = "term-" + uuid.uuid4().hex[:16]
        with transaction(self.db):
            agreement = self.db.execute(
                "SELECT 1 FROM term_agreements WHERE agreement_id=?", (agreement_id,)
            ).fetchone()
            if agreement is None:
                raise KeyError(agreement_id)
            # 同一通知编号：幂等返回既有案件
            existing = self.db.execute(
                "SELECT case_id FROM term_cases WHERE notice_fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                event(self.db, existing["case_id"], "notice.repeated", actor.user_id,
                      {"notice_key": notice_key, "received_at": notice_received_at})
                self._touch(existing["case_id"])
                return {"case": self.case(token, existing["case_id"]), "duplicate": True}
            # 协议已有未关闭案件：重复通知只留痕，不产生第二次回转
            open_case = self.db.execute(
                "SELECT case_id FROM term_cases WHERE agreement_id=? AND status<>'closed'",
                (agreement_id,),
            ).fetchone()
            if open_case is not None:
                event(self.db, open_case["case_id"], "notice.redundant", actor.user_id,
                      {"notice_key": notice_key, "received_at": notice_received_at})
                self._touch(open_case["case_id"])
                return {"case": self.case(token, open_case["case_id"]), "duplicate": True}
            # 协议案件已关闭：终止与回转已终结，任何后续通知都不能产生第二次回转
            closed_case = self.db.execute(
                "SELECT case_id FROM term_cases WHERE agreement_id=? ORDER BY created_at DESC LIMIT 1",
                (agreement_id,),
            ).fetchone()
            if closed_case is not None:
                event(self.db, closed_case["case_id"], "notice.after_close_ignored", actor.user_id,
                      {"notice_key": notice_key, "received_at": notice_received_at})
                self._touch(closed_case["case_id"])
                return {"case": self.case(token, closed_case["case_id"]), "duplicate": True}
            self.db.execute(
                "INSERT INTO term_cases(case_id,agreement_id,title,trigger_type,reason,status,"
                "notice_key,notice_fingerprint,notice_received_at,effective_termination_at,"
                "dispute_window_days,dispute_window_ends_at,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, agreement_id, title, trigger_type, reason, "notified", notice_key,
                 fingerprint, notice_received_at, effective_termination_at, dispute_window_days,
                 window_ends, actor.user_id, self._now(), self._now()),
            )
            event(self.db, case_id, "case.notified", actor.user_id,
                  {"agreement_id": agreement_id, "trigger_type": trigger_type, "reason": reason,
                   "notice_key": notice_key, "notice_received_at": notice_received_at,
                   "dispute_window_days": dispute_window_days,
                   "dispute_window_ends_at": window_ends})
        return {"case": self.case(token, case_id), "duplicate": False}

    # ------------------------------------------------------------------
    # 争议期
    # ------------------------------------------------------------------

    def raise_dispute(self, token, case_id, topic, detail):
        actor = self._actor(token, "dispute.write")
        if not topic.strip() or not detail.strip():
            raise TerminationError("争议主题与说明为必填项")
        with transaction(self.db):
            case = self._get_case(case_id)
            if case["status"] != "notified":
                raise InvalidCaseState("仅争议期内（未进入争议挂起/已定稿）可以提出争议")
            if parse_time(self._now()) >= parse_time(case["dispute_window_ends_at"]):
                raise InvalidCaseState("争议期已届满，不再受理新争议")
            ensure_transition(case["status"], "disputed")
            dispute_id = "disp-" + uuid.uuid4().hex[:16]
            self.db.execute(
                "INSERT INTO term_disputes VALUES(?,?,?,?,?,?,?,?,?)",
                (dispute_id, case_id, actor.user_id, topic, detail, "open", None, self._now(), None),
            )
            self.db.execute(
                "UPDATE term_cases SET status='disputed', updated_at=? WHERE case_id=?",
                (self._now(), case_id),
            )
            event(self.db, case_id, "dispute.raised", actor.user_id,
                  {"dispute_id": dispute_id, "topic": topic})
            return self._dispute_dict(dispute_id)

    def resolve_dispute(self, token, dispute_id, resolution_note):
        actor = self._actor(token, "dispute.write")
        if not resolution_note.strip():
            raise TerminationError("争议解决说明为必填项")
        with transaction(self.db):
            row = self.db.execute(
                "SELECT * FROM term_disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise KeyError(dispute_id)
            if row["status"] != "open":
                raise InvalidCaseState("争议已解决")
            case = self._get_case(row["case_id"])
            ensure_transition(case["status"], "notified")
            self.db.execute(
                "UPDATE term_disputes SET status='resolved',resolution_note=?,resolved_at=? "
                "WHERE dispute_id=?",
                (resolution_note, self._now(), dispute_id),
            )
            self.db.execute(
                "UPDATE term_cases SET status='notified', updated_at=? WHERE case_id=?",
                (self._now(), case["case_id"]),
            )
            event(self.db, case["case_id"], "dispute.resolved", actor.user_id,
                  {"dispute_id": dispute_id})
            return self._dispute_dict(dispute_id)

    def _dispute_dict(self, dispute_id: str) -> dict:
        return dict(self.db.execute(
            "SELECT * FROM term_disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone())

    # ------------------------------------------------------------------
    # 回转资产 / 范围清单（必须引用原协议或历次修订）
    # ------------------------------------------------------------------

    def add_scope_item(self, token, case_id, item: ScopeItemInput):
        actor = self._actor(token, "scope.write")
        item.validate()
        with transaction(self.db):
            case = self._get_case(case_id)
            if case["status"] not in ("notified", "disputed"):
                raise InvalidCaseState("回转范围定稿后不得新增范围项")
            self._validate_scope_reference(case["agreement_id"], item.source_amendment_seq)
            item_id = "item-" + uuid.uuid4().hex[:16]
            self.db.execute(
                "INSERT INTO term_scope_items(item_id,case_id,item_type,region,subject,"
                "disposition,source_amendment_seq,clause_ref,detail_json,required,due_at,"
                "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, case_id, item.item_type, item.region, item.subject, item.disposition,
                 item.source_amendment_seq, item.clause,
                 json.dumps(item.detail, ensure_ascii=False, sort_keys=True),
                 1 if item.required else 0, item.due_at, "draft", self._now(), self._now()),
            )
            event(self.db, case_id, "scope.item_added", actor.user_id,
                  {"item_id": item_id, "item_type": item.item_type, "region": item.region,
                   "subject": item.subject, "disposition": item.disposition,
                   "source_amendment_seq": item.source_amendment_seq, "clause_ref": item.clause})
            return self._item_dict(item_id)

    def finalize_scope(self, token, case_id):
        """争议期届满且无未决争议后定稿回转范围。"""

        actor = self._actor(token, "scope.write")
        with transaction(self.db):
            case = self._get_case(case_id)
            snapshot = self._snapshot(case)
            if case["status"] == "disputed" or not dispute_window_ready(snapshot):
                raise InvalidCaseState("争议期未届满或存在未决争议，不能定稿回转范围")
            ensure_transition(case["status"], "scope_finalized")
            items = self._items(case_id)
            if not items:
                raise TerminationError("回转范围为空：至少登记一项地区权利、数据、样本或义务")
            finalized = []
            for item in items:
                new_status = FINALIZED_ITEM_STATUS[item["disposition"]]
                self.db.execute(
                    "UPDATE term_scope_items SET status=?, updated_at=? WHERE item_id=?",
                    (new_status, self._now(), item["item_id"]),
                )
                finalized.append({"item_id": item["item_id"], "status": new_status})
            self.db.execute(
                "UPDATE term_cases SET status='scope_finalized', finalized_at=?, updated_at=? "
                "WHERE case_id=?",
                (self._now(), self._now(), case_id),
            )
            event(self.db, case_id, "scope.finalized", actor.user_id,
                  {"agreement_id": case["agreement_id"], "items": finalized,
                   "reference_basis": "原协议 seq=0 及 term_agreement_amendments 已登记修订"})
        return self.case(token, case_id)

    def _item_dict(self, item_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM term_scope_items WHERE item_id=?", (item_id,)
        ).fetchone()
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result

    # ------------------------------------------------------------------
    # 数据交付 / 样本与权利回转 / 再许可处置 / 共同开发部分暂停
    # ------------------------------------------------------------------

    def start_handover(self, token, case_id, item_id, note=""):
        """开始一项回转交接（地区权利、数据、样本、实物资产或再许可终止/更替）。"""

        actor = self._actor(token, "handover.write")
        with transaction(self.db):
            case = self._get_case(case_id)
            assert_open(case["status"], "开始回转交接")
            item = self._get_item(case_id, item_id)
            if item["status"] != "confirmed":
                raise InvalidCaseState("仅已定稿且待执行的范围项可以开始交接")
            if item["disposition"] == "suspend":
                raise InvalidCaseState("暂停类范围项必须使用部分暂停通道，不能按回转交接处理")
            if case["status"] == "scope_finalized":
                ensure_transition("scope_finalized", "handover_in_progress")
                self.db.execute(
                    "UPDATE term_cases SET status='handover_in_progress', updated_at=? WHERE case_id=?",
                    (self._now(), case_id),
                )
                event(self.db, case_id, "case.handover_started", actor.user_id, {})
            self.db.execute(
                "UPDATE term_scope_items SET status='in_handover', updated_at=? WHERE item_id=?",
                (self._now(), item_id),
            )
            event(self.db, case_id, "handover.started", actor.user_id,
                  {"item_id": item_id, "note": note})
            return self._item_dict(item_id)

    def complete_handover(self, token, case_id, item_id, evidence_ref, note=""):
        """登记交付凭证并确认回转完成。凭证引用必填，便于审计核对。"""

        actor = self._actor(token, "handover.write")
        if not evidence_ref.strip():
            raise TerminationError("交接凭证引用为必填项")
        with transaction(self.db):
            case = self._get_case(case_id)
            assert_open(case["status"], "确认回转交接")
            item = self._get_item(case_id, item_id)
            if item["status"] != "in_handover":
                raise InvalidCaseState("范围项不在交接中，不能确认完成")
            handover_id = self._record_handover(
                case, item, action="completed", evidence_ref=evidence_ref, note=note, actor=actor.user_id
            )
            self.db.execute(
                "UPDATE term_scope_items SET status='completed', updated_at=? WHERE item_id=?",
                (self._now(), item_id),
            )
            event(self.db, case_id, "handover.completed", actor.user_id,
                  {"item_id": item_id, "handover_id": handover_id, "evidence_ref": evidence_ref})
            return self._item_dict(item_id)

    def suspend_affected_part(self, token, case_id, item_id, scope_note, evidence_ref=""):
        """只暂停共同开发 / 再许可中受影响的部分，未受影响合作继续执行。"""

        actor = self._actor(token, "handover.write")
        if not scope_note.strip():
            raise TerminationError("必须说明暂停的受影响范围")
        with transaction(self.db):
            case = self._get_case(case_id)
            assert_open(case["status"], "暂停受影响部分")
            item = self._get_item(case_id, item_id)
            if item["disposition"] != "suspend" or item["status"] != "confirmed":
                raise InvalidCaseState("仅处置方式为“暂停”且已定稿的范围项可以执行暂停")
            handover_id = self._record_handover(
                case, item, action="suspended", evidence_ref=evidence_ref or f"pause:{item_id}",
                note=scope_note, actor=actor.user_id,
            )
            self.db.execute(
                "UPDATE term_scope_items SET status='suspended', updated_at=? WHERE item_id=?",
                (self._now(), item_id),
            )
            event(self.db, case_id, "collaboration.part_suspended", actor.user_id,
                  {"item_id": item_id, "handover_id": handover_id, "affected_scope": scope_note})
            return self._item_dict(item_id)

    def resolve_collaboration(self, token, case_id, item_id, outcome, evidence_ref, note=""):
        """共同开发 / 第三方承诺暂停后的最终出路：解除（released）或明确继续（continuing）。"""

        actor = self._actor(token, "handover.write")
        if outcome not in COLLABORATION_OUTCOMES:
            raise TerminationError("共同开发出路必须是 released 或 continuing")
        if not evidence_ref.strip():
            raise TerminationError("解除或继续的凭证引用为必填项")
        with transaction(self.db):
            case = self._get_case(case_id)
            assert_open(case["status"], "登记共同开发最终出路")
            item = self._get_item(case_id, item_id)
            if item["item_type"] != "collaboration":
                raise TerminationError("仅共同开发 / 第三方承诺项可以登记最终出路")
            if item["status"] != "suspended":
                raise InvalidCaseState("仅已暂停的共同开发项可以登记最终出路")
            new_status = "completed" if outcome == "released" else "ongoing"
            self._record_handover(
                case, item, action=f"collaboration_{outcome}", evidence_ref=evidence_ref,
                note=note, actor=actor.user_id,
            )
            self.db.execute(
                "UPDATE term_scope_items SET status=?, updated_at=? WHERE item_id=?",
                (new_status, self._now(), item_id),
            )
            event(self.db, case_id, "collaboration.resolved", actor.user_id,
                  {"item_id": item_id, "outcome": outcome, "new_status": new_status})
            return self._item_dict(item_id)

    def resolve_sublicense(self, token, case_id, item_id, action, evidence_ref, note=""):
        """再许可影响的最终处置：终止 / 更替回本企业 / 由被许可方保留 / 维持暂停。"""

        actor = self._actor(token, "handover.write")
        if action not in SUBLICENSE_ACTIONS:
            raise TerminationError("再许可处置必须是 terminated/novated/retained/suspended")
        if not evidence_ref.strip():
            raise TerminationError("再许可处置凭证引用为必填项")
        with transaction(self.db):
            case = self._get_case(case_id)
            item = self._get_item(case_id, item_id)
            if item["item_type"] != "sublicense":
                raise TerminationError("仅再许可项可以登记再许可处置")
            # 允许在案件关闭后对“维持暂停”的再许可执行正式终止/更替：
            # 这是已决定处置的后续落地，不重开案件、不改变案件状态
            if case["status"] != "closed":
                assert_open(case["status"], "登记再许可处置")
            elif item["status"] != "suspended" or action not in ("terminated", "novated", "retained"):
                raise InvalidCaseState("已关闭案件仅允许对维持暂停的再许可登记最终处置")
            if item["status"] not in ("suspended", "in_handover", "confirmed"):
                raise InvalidCaseState("当前再许可项状态不允许登记处置")
            self._record_handover(
                case, item, action=f"sublicense_{action}", evidence_ref=evidence_ref,
                note=note, actor=actor.user_id,
            )
            new_status = {"terminated": "completed", "novated": "completed",
                          "retained": "retained", "suspended": "suspended"}[action]
            self.db.execute(
                "UPDATE term_scope_items SET status=?, updated_at=? WHERE item_id=?",
                (new_status, self._now(), item_id),
            )
            event(self.db, case_id, "sublicense.resolved", actor.user_id,
                  {"item_id": item_id, "action": action, "new_status": new_status})
            return self._item_dict(item_id)

    def _record_handover(self, case, item, *, action, evidence_ref, note, actor) -> str:
        handover_id = "hov-" + uuid.uuid4().hex[:16]
        try:
            self.db.execute(
                "INSERT INTO term_handovers VALUES(?,?,?,?,?,?,?,?)",
                (handover_id, item["item_id"], case["case_id"], action, evidence_ref, note,
                 actor, self._now()),
            )
        except sqlite3.IntegrityError:
            # 同一范围项、同一动作、同一凭证：幂等
            row = self.db.execute(
                "SELECT handover_id FROM term_handovers WHERE item_id=? AND action=? AND evidence_ref=?",
                (item["item_id"], action, evidence_ref),
            ).fetchone()
            return row["handover_id"]
        return handover_id

    # ------------------------------------------------------------------
    # 最终关闭（终态，不可重开）
    # ------------------------------------------------------------------

    def close_case(self, token, case_id, close_decision):
        actor = self._actor(token, "close.write")
        if not close_decision.strip():
            raise TerminationError("关闭决定说明为必填项")
        with transaction(self.db):
            case = self._get_case(case_id)
            if case["status"] == "closed":
                raise InvalidCaseState("案件已关闭，关闭决定为终态，不可重复关闭")
            blockers = close_blockers(self._snapshot(case))
            if blockers:
                raise PrerequisiteError(
                    "前置动作未完成，不能关闭：" + ", ".join(sorted(blockers))
                )
            ensure_transition(case["status"], "closed")
            self.db.execute(
                "UPDATE term_cases SET status='closed', closed_at=?, close_decision=?, "
                "updated_at=? WHERE case_id=?",
                (self._now(), close_decision, self._now(), case_id),
            )
            event(self.db, case_id, "case.closed", actor.user_id,
                  {"close_decision": close_decision, "blockers_checked": []})
        return self.case(token, case_id)

    # ------------------------------------------------------------------
    # 迟到材料：只登记留痕，绝不重开已关闭决定
    # ------------------------------------------------------------------

    def record_late_material(
        self, token, case_id, material_type, source_ref, note, received_at=None, item_id=None
    ):
        actor = self._actor(token, "late_material.write")
        if not material_type.strip() or not source_ref.strip():
            raise TerminationError("材料类型与来源引用为必填项")
        received_at = received_at or self._now()
        parse_time(received_at)
        with transaction(self.db):
            case = self._get_case(case_id)
            if item_id is not None:
                self._get_item(case_id, item_id)
            material_id = "mat-" + uuid.uuid4().hex[:16]
            self.db.execute(
                "INSERT INTO term_late_materials VALUES(?,?,?,?,?,?,?,?,?)",
                (material_id, case_id, item_id, material_type, source_ref, note,
                 received_at, actor.user_id, self._now()),
            )
            reopened = False
            event(self.db, case_id, "late_material.recorded", actor.user_id,
                  {"material_id": material_id, "material_type": material_type,
                   "source_ref": source_ref, "case_status_at_receipt": case["status"],
                   "case_reopened": reopened})
            self._touch(case_id)
        return {
            "material_id": material_id, "case_id": case_id,
            "case_status": case["status"], "case_reopened": reopened,
        }

    # ------------------------------------------------------------------
    # 恢复自研 / 重新授权放行
    # ------------------------------------------------------------------

    def request_clearance(self, token, case_id, kind, region=None):
        actor = self._actor(token, "clearance.grant")
        if kind not in CLEARANCE_KINDS:
            raise TerminationError("放行类型必须是 resume_self_research 或 relicense")
        with transaction(self.db):
            case = self._get_case(case_id)
            blockers = clearance_blockers(self._snapshot(case), kind)
            decision = "granted" if not blockers else "rejected"
            clearance_id = "clr-" + uuid.uuid4().hex[:16]
            self.db.execute(
                "INSERT INTO term_clearances VALUES(?,?,?,?,?,?,?,?)",
                (clearance_id, case_id, kind, region, decision,
                 json.dumps(blockers, ensure_ascii=False, sort_keys=True),
                 actor.user_id, self._now()),
            )
            event(self.db, case_id, "clearance.decided", actor.user_id,
                  {"clearance_id": clearance_id, "kind": kind, "region": region,
                   "decision": decision, "blockers": blockers})
        # 拒绝决定同样落库留痕，事务提交后再抛出
        if blockers:
            raise PrerequisiteError(
                f"{kind} 放行被拒绝，前置动作未完成：" + ", ".join(sorted(blockers))
            )
        return {
            "clearance_id": clearance_id, "case_id": case_id, "kind": kind,
            "region": region, "decision": decision, "blockers": blockers,
        }

    # ------------------------------------------------------------------
    # 查询：时间线、案件、权利回转状态
    # ------------------------------------------------------------------

    def timeline(self, token, case_id):
        self._actor(token, "read")
        self._get_case(case_id)
        events = rows(
            self.db,
            "SELECT event_id,event_type,actor,payload_json,created_at "
            "FROM term_timeline_events WHERE case_id=? ORDER BY event_id",
            (case_id,),
        )
        for e in events:
            e["payload"] = json.loads(e.pop("payload_json"))
        return {"case_id": case_id, "events": events, "append_only": True}

    def case(self, token, case_id):
        self._actor(token, "read")
        case = self._get_case(case_id)
        result = dict(case)
        result["items"] = [self._item_dict(i["item_id"]) for i in self._items(case_id)]
        result["disputes"] = rows(
            self.db, "SELECT * FROM term_disputes WHERE case_id=? ORDER BY raised_at", (case_id,)
        )
        result["late_materials"] = rows(
            self.db, "SELECT * FROM term_late_materials WHERE case_id=? ORDER BY recorded_at",
            (case_id,),
        )
        result["handovers"] = rows(
            self.db, "SELECT * FROM term_handovers WHERE case_id=? ORDER BY recorded_at",
            (case_id,),
        )
        return result

    def rights_position(self, token, case_id):
        """清楚展示：当前可支配权利、遗留义务、未完成交接与暂停事项。"""

        self._actor(token, "read")
        case = self._get_case(case_id)
        items = [self._item_dict(i["item_id"]) for i in self._items(case_id)]
        closed = case["status"] == "closed"

        available_rights = []
        pending_reversions = []
        legacy_obligations = []
        unfinished_handovers = []
        suspended_matters = []
        retained_matters = []
        for item in items:
            summary = {
                "item_id": item["item_id"], "item_type": item["item_type"],
                "region": item["region"], "subject": item["subject"],
                "disposition": item["disposition"], "status": item["status"],
                "source_amendment_seq": item["source_amendment_seq"],
                "clause_ref": item["clause_ref"],
            }
            if item["item_type"] == "right":
                if item["status"] == "completed":
                    entry = dict(summary)
                    entry["disposable"] = closed
                    entry["note"] = "权利已回转，案件关闭后可自由支配" if closed else "权利已回转，关闭后方可对外处置"
                    (available_rights if closed else pending_reversions).append(entry)
                else:
                    pending_reversions.append(summary)
            elif item["item_type"] == "obligation" or item["status"] == "ongoing":
                legacy_obligations.append(summary)
            elif item["status"] == "retained":
                retained_matters.append(summary)
            elif item["status"] == "suspended":
                suspended_matters.append(summary)
            elif item["status"] in ("confirmed", "in_handover"):
                unfinished_handovers.append(summary)
            elif item["status"] == "completed" and item["item_type"] != "right":
                # 已完成的数据/样本/再许可回转，仍展示在可支配资料中
                available_rights.append(summary) if item["item_type"] in ("data", "sample", "asset") else None

        clearances = rows(
            self.db,
            "SELECT clearance_id,kind,region,decision,decided_at "
            "FROM term_clearances WHERE case_id=? ORDER BY decided_at",
            (case_id,),
        )
        return {
            "case_id": case_id,
            "agreement_id": case["agreement_id"],
            "case_status": case["status"],
            "closed": closed,
            "dispute_window_ends_at": case["dispute_window_ends_at"],
            "current_disposable_rights": available_rights,
            "pending_reversions": pending_reversions,
            "legacy_obligations": legacy_obligations,
            "unfinished_handovers": unfinished_handovers,
            "suspended_matters": suspended_matters,
            "retained_by_counterparty": retained_matters,
            "late_materials": rows(
                self.db,
                "SELECT material_id,material_type,source_ref,received_at,item_id "
                "FROM term_late_materials WHERE case_id=? ORDER BY received_at",
                (case_id,),
            ),
            "clearances": clearances,
        }
