"""领域模型：资格节点、依赖边、替代边、证据。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .timeutil import iso, to_date

NODE_KINDS = ("topic", "hall", "audience_level")


@dataclass(frozen=True)
class QualificationNode:
    """资格节点：主题 / 展厅 / 受众等级。"""

    node_id: str
    kind: str
    name: str
    description: str = ""

    @staticmethod
    def from_dict(data: dict) -> "QualificationNode":
        node_id = str(data.get("node_id", "")).strip()
        kind = str(data.get("kind", "")).strip()
        name = str(data.get("name", "")).strip()
        if not node_id:
            raise ValueError("node_id 不能为空")
        if kind not in NODE_KINDS:
            raise ValueError(f"节点类型必须是 {NODE_KINDS} 之一：{node_id}")
        if not name:
            raise ValueError(f"节点名称不能为空：{node_id}")
        return QualificationNode(
            node_id=node_id,
            kind=kind,
            name=name,
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
        }


@dataclass(frozen=True)
class DependencyEdge:
    """前置依赖：要取得 ``target`` 资格必须先满足 ``required``。"""

    target: str
    required: str
    note: str = ""

    @staticmethod
    def from_dict(data: dict) -> "DependencyEdge":
        target = str(data.get("target", "")).strip()
        required = str(data.get("required", "")).strip()
        if not target or not required:
            raise ValueError("依赖边必须同时给出 target 与 required")
        if target == required:
            raise ValueError(f"节点不能依赖自身：{target}")
        return DependencyEdge(target=target, required=required, note=str(data.get("note", "")))

    def to_dict(self) -> dict:
        return {"target": self.target, "required": self.required, "note": self.note}


@dataclass(frozen=True)
class SubstitutionLink:
    """替代关系：``replaces`` 可替代 ``original``。

    - 换版通过 ``supersedes`` 指向旧版本实现：新节点替代旧节点，
      旧关系在新版图谱发布时整体收档，不在旧对象上原地改写。
    - ``bidirectional`` 表示同级互认；换版替代恒为单向（新替旧）。
    """

    replaces: str
    original: str
    reason: str = ""
    bidirectional: bool = False
    supersedes: bool = False

    @staticmethod
    def from_dict(data: dict) -> "SubstitutionLink":
        replaces = str(data.get("replaces", "")).strip()
        original = str(data.get("original", "")).strip()
        if not replaces or not original:
            raise ValueError("替代边必须同时给出 replaces 与 original")
        if replaces == original:
            raise ValueError(f"节点不能替代自身：{replaces}")
        return SubstitutionLink(
            replaces=replaces,
            original=original,
            reason=str(data.get("reason", "")),
            bidirectional=bool(data.get("bidirectional", False)),
            supersedes=bool(data.get("supersedes", False)),
        )

    def to_dict(self) -> dict:
        return {
            "replaces": self.replaces,
            "original": self.original,
            "reason": self.reason,
            "bidirectional": self.bidirectional,
            "supersedes": self.supersedes,
        }


@dataclass(frozen=True)
class Evidence:
    """资格证据：证书 / 考核记录 / 豁免单，带有效期与撤销状态。"""

    evidence_id: str
    person_id: str
    node_id: str
    evidence_type: str
    valid_from: date
    valid_until: date | None
    status: str = "active"  # active / revoked
    revoked_reason: str = ""
    created_graph_version: int = 0
    note: str = ""

    @staticmethod
    def from_dict(data: dict) -> "Evidence":
        evidence_id = str(data.get("evidence_id", "")).strip()
        person_id = str(data.get("person_id", "")).strip()
        node_id = str(data.get("node_id", "")).strip()
        evidence_type = str(data.get("evidence_type", "")).strip() or "certificate"
        for key, value in (("evidence_id", evidence_id), ("person_id", person_id), ("node_id", node_id)):
            if not value:
                raise ValueError(f"证据 {key} 不能为空")
        valid_from = to_date(data.get("valid_from"))
        until = data.get("valid_until")
        valid_until = to_date(until) if until not in (None, "", "null") else None
        if valid_until is not None and valid_until < valid_from:
            raise ValueError(f"证据 {evidence_id} 的有效期截止早于生效日")
        status = str(data.get("status", "active"))
        if status not in ("active", "revoked"):
            raise ValueError(f"证据状态只能是 active/revoked：{evidence_id}")
        return Evidence(
            evidence_id=evidence_id,
            person_id=person_id,
            node_id=node_id,
            evidence_type=evidence_type,
            valid_from=valid_from,
            valid_until=valid_until,
            status=status,
            revoked_reason=str(data.get("revoked_reason", "")),
            created_graph_version=int(data.get("created_graph_version", 0)),
            note=str(data.get("note", "")),
        )

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "person_id": self.person_id,
            "node_id": self.node_id,
            "evidence_type": self.evidence_type,
            "valid_from": iso(self.valid_from),
            "valid_until": iso(self.valid_until) if self.valid_until else None,
            "status": self.status,
            "revoked_reason": self.revoked_reason,
            "created_graph_version": self.created_graph_version,
            "note": self.note,
        }
