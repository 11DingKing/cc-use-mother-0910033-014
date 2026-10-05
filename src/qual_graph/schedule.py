"""排班聚合：排班时冻结所用规则与证据，后续撤销/换版/豁免不改写历史。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .timeutil import iso, to_date

# 与领域契约 states 对齐
SCHEDULE_STATES = ("筹备", "待确认", "已排定", "执行中", "已结算")
# 允许的状态迁移
TRANSITIONS: dict[str, set[str]] = {
    "筹备": {"待确认", "已排定"},
    "待确认": {"筹备", "已排定"},
    "已排定": {"执行中", "筹备"},
    "执行中": {"已结算"},
    "已结算": set(),
}
# 一旦越过这些状态，排班冻结，规则变化不再影响它
FROZEN_FROM = ("已排定", "执行中", "已结算")


@dataclass(frozen=True)
class PathStep:
    """满足路径中的一步解释。"""

    requirement_id: str
    requirement_name: str
    satisfied_by_node: str
    satisfied_by_name: str
    evidence_id: str
    valid_from: str
    valid_until: str | None
    substitution_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "requirement_name": self.requirement_name,
            "satisfied_by_node": self.satisfied_by_node,
            "satisfied_by_name": self.satisfied_by_name,
            "evidence_id": self.evidence_id,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "substitution_chain": list(self.substitution_chain),
        }


@dataclass
class Schedule:
    schedule_id: str
    person_id: str
    required_nodes: list[str]
    event_date: date
    state: str = "筹备"
    created_graph_version: int = 0
    frozen_graph_snapshot: dict | None = None
    frozen_evidence_snapshots: list[dict] = field(default_factory=list)
    satisfied_path: list[PathStep] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    created_at: date | None = None

    def to_dict(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "person_id": self.person_id,
            "required_nodes": list(self.required_nodes),
            "event_date": iso(self.event_date),
            "state": self.state,
            "created_graph_version": self.created_graph_version,
            "frozen_graph_snapshot": self.frozen_graph_snapshot,
            "frozen_evidence_snapshots": list(self.frozen_evidence_snapshots),
            "satisfied_path": [s.to_dict() for s in self.satisfied_path],
            "missing_nodes": list(self.missing_nodes),
            "created_at": iso(self.created_at) if self.created_at else None,
            "frozen": self.state in FROZEN_FROM,
        }

    @staticmethod
    def from_dict(data: dict) -> "Schedule":
        return Schedule(
            schedule_id=data["schedule_id"],
            person_id=data["person_id"],
            required_nodes=list(data.get("required_nodes", [])),
            event_date=to_date(data["event_date"]),
            state=data.get("state", "筹备"),
            created_graph_version=int(data.get("created_graph_version", 0)),
            frozen_graph_snapshot=data.get("frozen_graph_snapshot"),
            frozen_evidence_snapshots=[dict(e) for e in data.get("frozen_evidence_snapshots", [])],
            satisfied_path=[
                PathStep(
                    requirement_id=s["requirement_id"],
                    requirement_name=s["requirement_name"],
                    satisfied_by_node=s["satisfied_by_node"],
                    satisfied_by_name=s["satisfied_by_name"],
                    evidence_id=s["evidence_id"],
                    valid_from=s["valid_from"],
                    valid_until=s.get("valid_until"),
                    substitution_chain=list(s.get("substitution_chain", [])),
                )
                for s in data.get("satisfied_path", [])
            ],
            missing_nodes=list(data.get("missing_nodes", [])),
            created_at=to_date(data["created_at"]) if data.get("created_at") else None,
        )
