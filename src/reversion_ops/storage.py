"""交易终止与权利回转服务的 SQLite 模式和事务辅助。

时间线由 ``termination_timeline_events`` 承载，每条事件通过
``previous_hash``/``event_hash`` 串联成哈希链，任何事件都只能追加，
不能修改或删除——这是“不可覆盖的时间线”的落地点。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 业务人员（法务、研发、商务、审计、交接接收方）
CREATE TABLE IF NOT EXISTS reversion_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('legal','rd','bd','auditor','receiver')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 原协议及其历次修订；回转范围必须引用其中具体条款
CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT NOT NULL,
    revision_seq INTEGER NOT NULL CHECK (revision_seq >= 0),
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('original','amendment')),
    effective_date TEXT NOT NULL,
    supersedes_revision INTEGER,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (agreement_id, revision_seq)
);

-- 一次对外授权交易及其当前整体状态
CREATE TABLE IF NOT EXISTS deals (
    deal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agreement_id TEXT NOT NULL,
    -- 最新已登记修订序号；回转范围以此为准引用原协议及历次修订
    current_agreement_revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','noticed','dispute_pending','winding_down','closed')),
    created_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    created_at TEXT NOT NULL
);

-- 交易下的地区/权利项；affected 标记本次终止是否触及
CREATE TABLE IF NOT EXISTS deal_territories (
    territory_id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL REFERENCES deals(deal_id),
    region TEXT NOT NULL,
    rights_scope TEXT NOT NULL,
    affected INTEGER NOT NULL DEFAULT 1 CHECK (affected IN (0,1)),
    state TEXT NOT NULL DEFAULT 'licensed'
        CHECK (state IN ('licensed','suspended','reverted')),
    UNIQUE (deal_id, region)
);

-- 再许可 / 共同开发 / 第三方承诺；终止时只暂停受影响部分
CREATE TABLE IF NOT EXISTS third_party_grants (
    grant_id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL REFERENCES deals(deal_id),
    territory_id TEXT REFERENCES deal_territories(territory_id),
    counterparty TEXT NOT NULL,
    grant_kind TEXT NOT NULL CHECK (grant_kind IN ('sublicense','co_development','third_party_commitment')),
    affected INTEGER NOT NULL DEFAULT 1 CHECK (affected IN (0,1)),
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','suspended','terminated','surviving')),
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 终止案例（一份已发出的终止通知对应一个案例）
CREATE TABLE IF NOT EXISTS terminations (
    termination_id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL REFERENCES deals(deal_id),
    state TEXT NOT NULL
        CHECK (state IN ('noticed','dispute_pending','winding_down','inventory_ready',
                         'handover_in_progress','preclose_blocked','ready_to_close','closed')),
    notice_key TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    dispute_window_days INTEGER NOT NULL CHECK (dispute_window_days >= 0),
    effective_date TEXT NOT NULL,
    dispute_ends_at TEXT NOT NULL,
    dispute_resolved_at TEXT,
    partial_scope INTEGER NOT NULL DEFAULT 0 CHECK (partial_scope IN (0,1)),
    scope_note TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    close_decision_id TEXT,
    created_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 争议登记；争议期内提出后整体流程挂起
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    termination_id TEXT NOT NULL REFERENCES terminations(termination_id),
    raised_by TEXT NOT NULL,
    summary TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open','resolved')),
    resolution_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- 资产清单：地区权利 / 数据 / 样本 / 物料 / 知识产权 / 后续义务
CREATE TABLE IF NOT EXISTS asset_items (
    asset_id TEXT PRIMARY KEY,
    termination_id TEXT NOT NULL REFERENCES terminations(termination_id),
    asset_kind TEXT NOT NULL
        CHECK (asset_kind IN ('territory_right','data','sample','material','ip','ongoing_obligation')),
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    -- 回转范围引用的原协议条款（必须引用原协议及历次修订）
    source_agreement_id TEXT NOT NULL,
    source_revision_seq INTEGER NOT NULL,
    source_clause TEXT NOT NULL,
    affected INTEGER NOT NULL DEFAULT 1 CHECK (affected IN (0,1)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','returned','reverted','held_over','excluded','not_required')),
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (termination_id, asset_kind, title),
    FOREIGN KEY (source_agreement_id, source_revision_seq)
        REFERENCES agreements(agreement_id, revision_seq)
);

-- 数据/样本/物料的交接批次与交付凭证
CREATE TABLE IF NOT EXISTS handover_batches (
    batch_id TEXT PRIMARY KEY,
    termination_id TEXT NOT NULL REFERENCES terminations(termination_id),
    asset_id TEXT NOT NULL REFERENCES asset_items(asset_id),
    manifest_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK (state IN ('submitted','received','rejected')),
    submitted_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    submitted_at TEXT NOT NULL,
    received_by TEXT,
    received_at TEXT,
    receive_note TEXT NOT NULL DEFAULT '',
    deadline_at TEXT
);

-- 关闭前必须完成的前置动作（恢复自研 / 重新授权前也要全部满足）
CREATE TABLE IF NOT EXISTS prerequisites (
    prerequisite_id TEXT PRIMARY KEY,
    termination_id TEXT NOT NULL REFERENCES terminations(termination_id),
    title TEXT NOT NULL,
    required_for TEXT NOT NULL
        CHECK (required_for IN ('close','resume_dev','relicense')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','satisfied','waived')),
    detail TEXT NOT NULL DEFAULT '',
    satisfied_by TEXT,
    satisfied_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prereq_termination
ON prerequisites(termination_id, required_for, status);

-- 关闭决定一旦形成即不可重开；迟到材料只能挂为关闭后登记
CREATE TABLE IF NOT EXISTS close_decisions (
    close_decision_id TEXT PRIMARY KEY,
    termination_id TEXT NOT NULL UNIQUE REFERENCES terminations(termination_id),
    decision TEXT NOT NULL CHECK (decision IN ('closed','closed_with_obligations')),
    rights_snapshot_json TEXT NOT NULL,
    surviving_obligations_json TEXT NOT NULL,
    pending_handover_json TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    decided_at TEXT NOT NULL
);

-- 关闭后才送达的材料登记，不改变关闭决定
CREATE TABLE IF NOT EXISTS late_materials (
    late_material_id TEXT PRIMARY KEY,
    termination_id TEXT NOT NULL REFERENCES terminations(termination_id),
    asset_id TEXT REFERENCES asset_items(asset_id),
    title TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    received_at TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    disposition TEXT NOT NULL DEFAULT 'archived'
);

-- 自研恢复 / 重新授权的放行登记，必须在全部前置动作满足后
CREATE TABLE IF NOT EXISTS resumption_clearances (
    clearance_id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL REFERENCES deals(deal_id),
    territory_id TEXT REFERENCES deal_territories(territory_id),
    clearance_kind TEXT NOT NULL CHECK (clearance_kind IN ('resume_dev','relicense')),
    prerequisite_digest TEXT NOT NULL CHECK (length(prerequisite_digest) = 64),
    granted_by TEXT NOT NULL REFERENCES reversion_users(user_id),
    granted_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (deal_id, territory_id, clearance_kind)
);

-- 通知幂等：同一业务通知键 + 内容摘要只能产生一次回转
CREATE TABLE IF NOT EXISTS reversion_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);

-- 不可覆盖的时间线：仅追加的哈希链事件
CREATE TABLE IF NOT EXISTS termination_timeline_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    termination_id TEXT NOT NULL REFERENCES terminations(termination_id),
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_timeline_termination
ON termination_timeline_events(termination_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "reversion_users", "agreements", "deals", "deal_territories",
    "third_party_grants", "terminations", "disputes", "asset_items",
    "handover_batches", "prerequisites", "close_decisions", "late_materials",
    "resumption_clearances", "reversion_idempotency", "termination_timeline_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return {
        "tables": tables,
        "missing_tables": sorted(REQUIRED_TABLES - set(tables)),
        "schema_version": None if version_row is None else version_row["value"],
    }
