"""JSON 文件存储：图谱版本、草稿、证据、豁免、排班与审计事件。"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .graph import GraphDraft, PublishedGraph
from .models import DependencyEdge, Evidence, QualificationNode, SubstitutionLink
from .schedule import Schedule
from .timeutil import iso, to_date


class Store:
    """单文件原子写入的 JSON 存储。

    数据组织::

        published: [{version, published_at, note, nodes, dependencies, substitutions}]
        draft:     {based_on_version, nodes, dependencies, substitutions} | null
        evidences: [...]
        waivers:   [{waiver_id, person_id, node_id, reason, valid_from, valid_until,
                     status, created_graph_version}]
        schedules: [Schedule.to_dict()]
        audit:     [{seq, at, action, actor, detail}]
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists() and self.path.stat().st_size > 0:
            return json.loads(self.path.read_text(encoding="utf-8"))
        return self.empty_state()

    @staticmethod
    def empty_state() -> dict:
        return {
            "published": [],
            "draft": None,
            "evidences": [],
            "waivers": [],
            "schedules": [],
            "audit": [],
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- 审计 --------------------------------------------------------------

    def audit(self, action: str, actor: str, detail: dict) -> None:
        seq = len(self._data["audit"]) + 1
        self._data["audit"].append(
            {"seq": seq, "at": iso(date.today()), "action": action, "actor": actor, "detail": detail}
        )

    @property
    def audit_log(self) -> list[dict]:
        return list(self._data["audit"])

    # -- 图谱版本 ----------------------------------------------------------

    def list_versions(self) -> list[int]:
        return [v["version"] for v in self._data["published"]]

    def latest_version(self) -> int | None:
        versions = self.list_versions()
        return versions[-1] if versions else None

    def save_published(self, graph: PublishedGraph) -> None:
        self._data["published"] = [
            v for v in self._data["published"] if v["version"] != graph.version
        ]
        self._data["published"].append(graph.snapshot() | {"note": graph.note})
        self._data["published"].sort(key=lambda v: v["version"])

    def load_published(self, version: int | None = None) -> PublishedGraph | None:
        if not self._data["published"]:
            return None
        if version is None:
            payload = self._data["published"][-1]
        else:
            payload = next((v for v in self._data["published"] if v["version"] == version), None)
            if payload is None:
                return None
        return PublishedGraph.from_dict(payload)

    # -- 草稿 --------------------------------------------------------------

    def save_draft(self, draft: GraphDraft) -> None:
        self._data["draft"] = {
            "based_on_version": draft.based_on_version,
            "nodes": [n.to_dict() for n in draft.nodes.values()],
            "dependencies": [e.to_dict() for e in draft.deps],
            "substitutions": [s.to_dict() for s in draft.subs],
        }

    def load_draft(self) -> GraphDraft | None:
        payload = self._data["draft"]
        if payload is None:
            return None
        draft = GraphDraft(based_on_version=payload.get("based_on_version", 0))
        for node in payload["nodes"]:
            draft.upsert_node(QualificationNode.from_dict(node))
        for edge in payload["dependencies"]:
            draft.add_dependency(DependencyEdge.from_dict(edge))
        for sub in payload["substitutions"]:
            draft.add_substitution(SubstitutionLink.from_dict(sub))
        return draft

    def clear_draft(self) -> None:
        self._data["draft"] = None

    # -- 证据 --------------------------------------------------------------

    def list_evidences(self, person_id: str | None = None) -> list[Evidence]:
        items = [Evidence.from_dict(e) for e in self._data["evidences"]]
        if person_id:
            items = [e for e in items if e.person_id == person_id]
        return items

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        for raw in self._data["evidences"]:
            if raw["evidence_id"] == evidence_id:
                return Evidence.from_dict(raw)
        return None

    def upsert_evidence(self, evidence: Evidence) -> None:
        self._data["evidences"] = [
            e for e in self._data["evidences"] if e["evidence_id"] != evidence.evidence_id
        ]
        self._data["evidences"].append(evidence.to_dict())

    # -- 豁免 --------------------------------------------------------------

    def list_waivers(self) -> list[dict]:
        return [dict(w) for w in self._data["waivers"]]

    def get_waiver(self, waiver_id: str) -> dict | None:
        return next((dict(w) for w in self._data["waivers"] if w["waiver_id"] == waiver_id), None)

    def add_waiver(self, waiver: dict) -> None:
        if self.get_waiver(waiver["waiver_id"]) is not None:
            raise ValueError(f"豁免单已存在：{waiver['waiver_id']}")
        self._data["waivers"].append(waiver)

    def revoke_waiver(self, waiver_id: str, reason: str) -> dict | None:
        waiver = self.get_waiver(waiver_id)
        if waiver is None:
            return None
        waiver["status"] = "revoked"
        waiver["revoked_reason"] = reason
        for i, raw in enumerate(self._data["waivers"]):
            if raw["waiver_id"] == waiver_id:
                self._data["waivers"][i] = waiver
        return waiver

    def active_waivers(self, person_id: str, on_date) -> list[dict]:
        on_date = to_date(on_date)
        result = []
        for w in self._data["waivers"]:
            if w.get("status") != "active" or w["person_id"] != person_id:
                continue
            if to_date(w["valid_from"]) > on_date:
                continue
            until = w.get("valid_until")
            if until and to_date(until) < on_date:
                continue
            result.append(dict(w))
        return result

    # -- 排班 --------------------------------------------------------------

    def list_schedules(self, person_id: str | None = None) -> list[Schedule]:
        items = [Schedule.from_dict(s) for s in self._data["schedules"]]
        if person_id:
            items = [s for s in items if s.person_id == person_id]
        return items

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        for raw in self._data["schedules"]:
            if raw["schedule_id"] == schedule_id:
                return Schedule.from_dict(raw)
        return None

    def save_schedule(self, schedule: Schedule) -> None:
        self._data["schedules"] = [
            s for s in self._data["schedules"] if s["schedule_id"] != schedule.schedule_id
        ]
        self._data["schedules"].append(schedule.to_dict())
