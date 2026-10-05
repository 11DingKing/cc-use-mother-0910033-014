"""领域模型与错误类型。

资格图语义
----------
资格 ``Qualification`` 表示一种可被讲解员持有的能力（主题 / 展厅 / 受众等级）。
一个资格对直接持有的要求由若干 ``RequirementGroup`` 组成：

* 组与组之间是 **AND**：全部组满足，直接持有才满足；
* 组内候选是 **OR**：任一候选满足，该组即满足；
* 组内候选可以是另一个资格（前置依赖），也可以是证据类型（需持有效证据）。

替代关系 ``supersedes`` 表示：持有旧资格即视为持有新资格（旧 → 新）。
替代链在满足判定时沿出边展开，并在解释中显式记录路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

NodeKind = Literal["topic", "hall", "audience_level"]
IssueCode = Literal[
    "cycle",
    "dangling_reference",
    "self_reference",
    "duplicate_candidate",
    "empty_group",
    "duplicate_id",
    "invalid_evidence_type",
]


class GraphError(ValueError):
    """业务规则错误。"""


@dataclass(frozen=True)
class RequirementGroup:
    """一个 OR 候选组；``candidates`` 元素为资格 ID 或证据类型代码。"""

    candidates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": list(self.candidates)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RequirementGroup:
        raw = value.get("candidates", [])
        if not isinstance(raw, list) or not raw:
            raise GraphError("需求组必须含至少一个候选")
        return cls(candidates=tuple(str(c) for c in raw))


@dataclass(frozen=True)
class Qualification:
    qid: str
    name: str
    kind: NodeKind
    requirement_groups: tuple[RequirementGroup, ...] = ()
    supersedes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "name": self.name,
            "kind": self.kind,
            "requirement_groups": [g.to_dict() for g in self.requirement_groups],
            "supersedes": list(self.supersedes),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Qualification:
        kind = value.get("kind")
        if kind not in ("topic", "hall", "audience_level"):
            raise GraphError(f"资格 {value.get('qid')} 类型非法：{kind}")
        groups = tuple(RequirementGroup.from_dict(g) for g in value.get("requirement_groups", []))
        return cls(
            qid=str(value["qid"]),
            name=str(value.get("name", value["qid"])),
            kind=kind,
            requirement_groups=groups,
            supersedes=tuple(str(s) for s in value.get("supersedes", [])),
        )


@dataclass(frozen=True)
class Evidence:
    """讲解员持有的证据（证书等），可撤销、有有效期。"""

    evidence_id: int | None
    guide_id: str
    evidence_type: str
    title: str
    issued_on: date
    valid_until: date | None
    revoked: bool = False
    revoked_on: date | None = None
    revoke_reason: str | None = None

    def active_on(self, day: date) -> bool:
        """证据在 ``day`` 当天是否可用于满足要求。"""
        if self.revoked and (self.revoked_on is None or self.revoked_on <= day):
            return False
        if day < self.issued_on:
            return False
        if self.valid_until is not None and day > self.valid_until:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "guide_id": self.guide_id,
            "evidence_type": self.evidence_type,
            "title": self.title,
            "issued_on": self.issued_on.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "revoked": self.revoked,
            "revoked_on": self.revoked_on.isoformat() if self.revoked_on else None,
            "revoke_reason": self.revoke_reason,
        }


@dataclass(frozen=True)
class Exemption:
    """临时豁免：在 [start, end] 窗口内免证据/前置持有某资格。"""

    exemption_id: int | None
    guide_id: str
    qid: str
    start_on: date
    end_on: date
    reason: str
    revoked: bool = False

    def active_on(self, day: date) -> bool:
        return not self.revoked and self.start_on <= day <= self.end_on

    def to_dict(self) -> dict[str, Any]:
        return {
            "exemption_id": self.exemption_id,
            "guide_id": self.guide_id,
            "qid": self.qid,
            "start_on": self.start_on.isoformat(),
            "end_on": self.end_on.isoformat(),
            "reason": self.reason,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class ValidationIssue:
    code: IssueCode
    message: str
    qid: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.qid is not None:
            out["qid"] = self.qid
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class SatisfactionExplanation:
    """一次资格满足判定的可解释结果（解释树）。"""

    qid: str
    satisfied: bool
    day: date
    basis: str  # exemption | evidence | direct | prerequisite | supersession
    detail: str
    children: tuple[SatisfactionExplanation, ...] = ()
    path: tuple[str, ...] = ()  # 替代链路径（起点 → 当前）

    def to_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "satisfied": self.satisfied,
            "day": self.day.isoformat(),
            "basis": self.basis,
            "detail": self.detail,
            "path": list(self.path),
            "children": [c.to_dict() for c in self.children],
        }

    def leaves(self) -> list[SatisfactionExplanation]:
        """展开为平面叶子列表（真正提供满足的底层依据）。"""
        if not self.children:
            return [self]
        result: list[SatisfactionExplanation] = []
        for child in self.children:
            result.extend(child.leaves())
        return result
