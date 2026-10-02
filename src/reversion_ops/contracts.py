"""交易终止与权利回转的领域输入契约。

所有 ``from_dict`` 在输入不合法时抛出 :class:`ValidationError`，
由服务层转换为对外的 ``ValidationFailed``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from .clock import parse_utc

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

ASSET_KINDS = {"territory_right", "data", "sample", "material", "ip", "ongoing_obligation"}
GRANT_KINDS = {"sublicense", "co_development", "third_party_commitment"}
TERMINAL_ASSET_STATUSES = {"returned", "held_over", "excluded", "not_required"}


class ValidationError(ValueError):
    pass


def required_text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationError(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationError(f"{field} 格式不正确")
    return result


def sha256(value: object, field: str) -> str:
    result = required_text(value, field, 64).lower()
    if not SHA256.fullmatch(result):
        raise ValidationError(f"{field} 必须是 64 位十六进制 SHA-256")
    return result


def non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} 必须是非负整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def iso_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parse_utc(text, field)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return text


@dataclass(frozen=True, slots=True)
class AgreementRevision:
    agreement_id: str
    revision_seq: int
    title: str
    kind: str
    effective_date: str
    supersedes_revision: int | None
    content_sha256: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgreementRevision":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in {"original", "amendment"}:
            raise ValidationError("kind 必须是 original 或 amendment")
        revision_seq = non_negative_int(raw.get("revision_seq"), "revision_seq")
        supersedes = raw.get("supersedes_revision")
        if kind == "original":
            if revision_seq != 0:
                raise ValidationError("原协议 revision_seq 必须为 0")
            supersedes = None
        else:
            supersedes = non_negative_int(supersedes, "supersedes_revision")
            if supersedes >= revision_seq:
                raise ValidationError("修订必须引用更早的修订序号")
        return cls(
            agreement_id=identifier(raw.get("agreement_id"), "agreement_id"),
            revision_seq=revision_seq,
            title=required_text(raw.get("title"), "title"),
            kind=kind,
            effective_date=date_text(raw.get("effective_date"), "effective_date"),
            supersedes_revision=supersedes,
            content_sha256=sha256(raw.get("content_sha256"), "content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class Territory:
    territory_id: str
    region: str
    rights_scope: str
    affected: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Territory":
        affected = raw.get("affected", True)
        if not isinstance(affected, bool):
            raise ValidationError("affected 必须是布尔值")
        return cls(
            territory_id=identifier(raw.get("territory_id"), "territory_id"),
            region=required_text(raw.get("region"), "region", 128),
            rights_scope=required_text(raw.get("rights_scope"), "rights_scope", 256),
            affected=affected,
        )


@dataclass(frozen=True, slots=True)
class ThirdPartyGrant:
    grant_id: str
    territory_id: str | None
    counterparty: str
    grant_kind: str
    affected: bool
    detail: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ThirdPartyGrant":
        grant_kind = required_text(raw.get("grant_kind"), "grant_kind", 32)
        if grant_kind not in GRANT_KINDS:
            raise ValidationError("grant_kind 不受支持")
        affected = raw.get("affected", True)
        if not isinstance(affected, bool):
            raise ValidationError("affected 必须是布尔值")
        territory = raw.get("territory_id")
        return cls(
            grant_id=identifier(raw.get("grant_id"), "grant_id"),
            territory_id=None if territory in (None, "") else identifier(territory, "territory_id"),
            counterparty=required_text(raw.get("counterparty"), "counterparty", 128),
            grant_kind=grant_kind,
            affected=affected,
            detail=required_text(raw.get("detail", ""), "detail", 512) if raw.get("detail") else "",
        )


@dataclass(frozen=True, slots=True)
class TerminationNotice:
    termination_id: str
    deal_id: str
    notice_key: str
    reason: str
    dispute_window_days: int
    effective_date: str
    partial_scope: bool
    scope_note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TerminationNotice":
        partial_scope = raw.get("partial_scope", False)
        if not isinstance(partial_scope, bool):
            raise ValidationError("partial_scope 必须是布尔值")
        return cls(
            termination_id=identifier(raw.get("termination_id"), "termination_id"),
            deal_id=identifier(raw.get("deal_id"), "deal_id"),
            notice_key=identifier(raw.get("notice_key"), "notice_key"),
            reason=required_text(raw.get("reason"), "reason", 1000),
            dispute_window_days=non_negative_int(raw.get("dispute_window_days", 30), "dispute_window_days"),
            effective_date=date_text(raw.get("effective_date"), "effective_date"),
            partial_scope=partial_scope,
            scope_note=required_text(raw.get("scope_note", ""), "scope_note", 1000)
            if raw.get("scope_note") else "",
        )


@dataclass(frozen=True, slots=True)
class AssetItemInput:
    asset_id: str
    asset_kind: str
    title: str
    detail: Mapping[str, Any]
    source_revision_seq: int
    source_clause: str
    affected: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssetItemInput":
        asset_kind = required_text(raw.get("asset_kind"), "asset_kind", 32)
        if asset_kind not in ASSET_KINDS:
            raise ValidationError("asset_kind 不受支持")
        affected = raw.get("affected", True)
        if not isinstance(affected, bool):
            raise ValidationError("affected 必须是布尔值")
        detail = raw.get("detail", {})
        if not isinstance(detail, Mapping):
            raise ValidationError("detail 必须是对象")
        clause = raw.get("source_clause")
        return cls(
            asset_id=identifier(raw.get("asset_id"), "asset_id"),
            asset_kind=asset_kind,
            title=required_text(raw.get("title"), "title", 256),
            detail=dict(detail),
            source_revision_seq=non_negative_int(raw.get("source_revision_seq"), "source_revision_seq"),
            source_clause=required_text(clause, "source_clause", 128),
            affected=affected,
        )


@dataclass(frozen=True, slots=True)
class HandoverSubmission:
    batch_id: str
    asset_id: str
    manifest: Mapping[str, Any]
    content_sha256: str
    deadline_at: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HandoverSubmission":
        manifest = raw.get("manifest", {})
        if not isinstance(manifest, Mapping) or not manifest:
            raise ValidationError("manifest 必须是非空对象")
        deadline = raw.get("deadline_at")
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            asset_id=identifier(raw.get("asset_id"), "asset_id"),
            manifest=dict(manifest),
            content_sha256=sha256(raw.get("content_sha256"), "content_sha256"),
            deadline_at=None if deadline in (None, "") else iso_text(deadline, "deadline_at"),
        )
