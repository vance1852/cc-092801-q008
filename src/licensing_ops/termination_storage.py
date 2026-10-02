"""交易终止与权利回转的 SQLite 模式与辅助函数。

独立于既有生物安全表，全部表名以 term_ 前缀，避免与旧业务耦合。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS term_agreements (
    agreement_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    counterparty TEXT NOT NULL,
    original_signed_at TEXT NOT NULL,
    current_amendment_seq INTEGER NOT NULL DEFAULT 0 CHECK (current_amendment_seq >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS term_agreement_amendments (
    agreement_id TEXT NOT NULL REFERENCES term_agreements(agreement_id),
    amendment_seq INTEGER NOT NULL CHECK (amendment_seq > 0),
    title TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agreement_id, amendment_seq)
);

CREATE TABLE IF NOT EXISTS term_cases (
    case_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES term_agreements(agreement_id),
    title TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('strategic_adjustment','clinical_outcome','contractual','mutual')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('notified','disputed','scope_finalized','handover_in_progress','closed')),
    notice_key TEXT NOT NULL,
    notice_fingerprint TEXT NOT NULL UNIQUE,
    notice_received_at TEXT NOT NULL,
    effective_termination_at TEXT,
    dispute_window_days INTEGER NOT NULL CHECK (dispute_window_days >= 0),
    dispute_window_ends_at TEXT NOT NULL,
    finalized_at TEXT,
    closed_at TEXT,
    close_decision TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 重复通知不得产生第二次回转：fingerprint 已唯一，案件与协议也只允许一个未关闭案件
CREATE UNIQUE INDEX IF NOT EXISTS term_one_open_case_per_agreement
ON term_cases(agreement_id)
WHERE status <> 'closed';

CREATE TABLE IF NOT EXISTS term_scope_items (
    item_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES term_cases(case_id),
    item_type TEXT NOT NULL CHECK (item_type IN ('right','data','sample','asset','sublicense','collaboration','obligation')),
    region TEXT NOT NULL,
    subject TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('revert','retain','suspend','ongoing')),
    source_amendment_seq INTEGER NOT NULL CHECK (source_amendment_seq >= 0),
    clause_ref TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    due_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('draft','confirmed','in_handover','completed','suspended','retained','ongoing')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, item_type, region, subject)
);

CREATE TABLE IF NOT EXISTS term_timeline_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT REFERENCES term_cases(case_id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS term_disputes (
    dispute_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES term_cases(case_id),
    raised_by TEXT NOT NULL,
    topic TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','resolved')),
    resolution_note TEXT,
    raised_at TEXT NOT NULL,
    resolved_at TEXT
);

-- 迟到材料：关闭后到达的材料只登记，不重开决定
CREATE TABLE IF NOT EXISTS term_late_materials (
    material_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES term_cases(case_id),
    item_id TEXT REFERENCES term_scope_items(item_id),
    material_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    note TEXT NOT NULL,
    received_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

-- 交接凭证与动作记录（数据交付、样本转移、再许可处置、共同开发暂停/解除）
CREATE TABLE IF NOT EXISTS term_handovers (
    handover_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES term_scope_items(item_id),
    case_id TEXT NOT NULL REFERENCES term_cases(case_id),
    action TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    note TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(item_id, action, evidence_ref)
);

CREATE TABLE IF NOT EXISTS term_clearances (
    clearance_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES term_cases(case_id),
    kind TEXT NOT NULL CHECK (kind IN ('resume_self_research','relicense')),
    region TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('granted','rejected')),
    prerequisite_snapshot_json TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS term_timeline_case_idx ON term_timeline_events(case_id, event_id);
CREATE INDEX IF NOT EXISTS term_scope_case_idx ON term_scope_items(case_id);
CREATE INDEX IF NOT EXISTS term_disputes_case_idx ON term_disputes(case_id);
CREATE INDEX IF NOT EXISTS term_late_case_idx ON term_late_materials(case_id);
"""

REQUIRED_TABLES = frozenset({
    "term_agreements", "term_agreement_amendments", "term_cases", "term_scope_items",
    "term_timeline_events", "term_disputes", "term_late_materials",
    "term_handovers", "term_clearances",
})


def utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA_SQL)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA_SQL)
    db.commit()


def inspect_schema(db: sqlite3.Connection) -> dict[str, object]:
    names = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'term\_%' ESCAPE '\\'"
        ).fetchall()
    }
    return {"missing_tables": sorted(REQUIRED_TABLES - names)}


def event(db: sqlite3.Connection, case_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO term_timeline_events(case_id,event_type,actor,payload_json,created_at)"
        " VALUES(?,?,?,?,?)",
        (case_id, event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), utcnow_text()),
    )


def rows(db: sqlite3.Connection, query: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in db.execute(query, args).fetchall()]
