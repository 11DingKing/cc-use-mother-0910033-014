"""应用服务层：编排图谱发布、证据/豁免、排班冻结与影响分析。"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from .errors import ConflictError, NotFoundError, PublishBlockedError, ValidationError
from .evaluator import Evaluator
from .graph import GraphDraft, PublishedGraph, diff_graphs
from .models import Evidence, QualificationNode, DependencyEdge, SubstitutionLink
from .schedule import FROZEN_FROM, TRANSITIONS, PathStep, Schedule
from .storage import Store
from .timeutil import iso, today, to_date


class Service:
    """对外统一服务入口。所有写操作记录审计事件。"""

    def __init__(self, store: str | Path | Store):
        self.store = store if isinstance(store, Store) else Store(store)

    # =====================================================================
    # 图谱草稿与发布
    # =====================================================================

    def get_or_create_draft(self) -> GraphDraft:
        draft = self.store.load_draft()
        if draft is None:
            current = self.store.load_published()
            draft = GraphDraft.from_published(current) if current else GraphDraft()
            self.store.save_draft(draft)
        return draft

    def _draft(self) -> GraphDraft:
        draft = self.store.load_draft()
        if draft is None:
            raise NotFoundError("当前没有草稿，请先创建（POST /graph/draft）")
        return draft

    def _save_draft(self, draft: GraphDraft) -> GraphDraft:
        self.store.save_draft(draft)
        return draft

    def upsert_node(self, data: dict, actor: str = "场馆管理员") -> dict:
        draft = self._draft()
        try:
            node = QualificationNode.from_dict(data)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        try:
            draft.upsert_node(node)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        self._save_draft(draft)
        self.store.audit("node_upsert", actor, {"node": node.to_dict()})
        self.store.save()
        return node.to_dict()

    def remove_node(self, node_id: str, actor: str = "场馆管理员") -> None:
        draft = self._draft()
        try:
            draft.remove_node(node_id)
        except KeyError as exc:
            raise NotFoundError(f"节点不存在：{node_id}") from exc
        self._save_draft(draft)
        self.store.audit("node_remove", actor, {"node_id": node_id})
        self.store.save()

    def add_dependency(self, data: dict, actor: str = "场馆管理员") -> dict:
        draft = self._draft()
        try:
            edge = DependencyEdge.from_dict(data)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        draft.add_dependency(edge)
        self._save_draft(draft)
        self.store.audit("dependency_add", actor, {"edge": edge.to_dict()})
        self.store.save()
        return edge.to_dict()

    def remove_dependency(self, target: str, required: str, actor: str = "场馆管理员") -> None:
        draft = self._draft()
        try:
            draft.remove_dependency(target, required)
        except KeyError as exc:
            raise NotFoundError(f"依赖边不存在：{target} -> {required}") from exc
        self._save_draft(draft)
        self.store.audit("dependency_remove", actor, {"target": target, "required": required})
        self.store.save()

    def add_substitution(self, data: dict, actor: str = "场馆管理员") -> dict:
        draft = self._draft()
        try:
            link = SubstitutionLink.from_dict(data)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        draft.add_substitution(link)
        self._save_draft(draft)
        self.store.audit("substitution_add", actor, {"edge": link.to_dict()})
        self.store.save()
        return link.to_dict()

    def remove_substitution(self, replaces: str, original: str, actor: str = "场馆管理员") -> None:
        draft = self._draft()
        try:
            draft.remove_substitution(replaces, original)
        except KeyError as exc:
            raise NotFoundError(f"替代边不存在：{replaces} ~ {original}") from exc
        self._save_draft(draft)
        self.store.audit("substitution_remove", actor, {"replaces": replaces, "original": original})
        self.store.save()

    def validate_draft(self) -> dict:
        draft = self._draft()
        issues = draft.validate()
        return {
            "blocked": any(i["level"] == "error" for i in issues),
            "issue_count": len(issues),
            "issues": issues,
        }

    def publish(self, note: str = "", actor: str = "场馆管理员", publish_date: date | None = None) -> dict:
        """发布草稿为新版本。存在 error 级问题时阻断发布。"""
        draft = self._draft()
        issues = draft.validate()
        errors = [i for i in issues if i["level"] == "error"]
        if errors:
            raise PublishBlockedError(issues)
        current = self.store.load_published()
        version = (current.version + 1) if current else 1
        graph = PublishedGraph(
            version=version,
            published_at=publish_date or today(),
            nodes=dict(draft.nodes),
            deps=list(draft.deps),
            subs=list(draft.subs),
            note=note,
        )
        self.store.save_published(graph)
        self.store.clear_draft()
        self.store.audit(
            "graph_publish",
            actor,
            {"version": version, "warnings": [i for i in issues if i["level"] == "warning"], "note": note},
        )
        self.store.save()
        return {"version": version, "graph": graph.to_dict(), "warnings": [i for i in issues if i["level"] == "warning"]}

    def list_versions(self) -> list[dict]:
        result = []
        for version in self.store.list_versions():
            graph = self.store.load_published(version)
            result.append(
                {
                    "version": version,
                    "published_at": iso(graph.published_at),
                    "note": graph.note,
                    "node_count": len(graph.nodes),
                    "dependency_count": len(graph.deps),
                    "substitution_count": len(graph.subs),
                }
            )
        return result

    def get_graph(self, version: int | None = None) -> PublishedGraph:
        graph = self.store.load_published(version)
        if graph is None:
            raise NotFoundError("尚未发布任何图谱版本" if version is None else f"图谱版本不存在：v{version}")
        return graph

    # =====================================================================
    # 证据
    # =====================================================================

    def add_evidence(self, data: dict, actor: str = "场馆管理员") -> dict:
        graph = self._require_graph()
        try:
            evidence = Evidence.from_dict(
                {**data, "created_graph_version": graph.version}
                if "created_graph_version" not in data
                else data
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if evidence.node_id not in graph.nodes:
            raise ValidationError(
                f"证据引用的节点在当前图谱 v{graph.version} 中不存在：{evidence.node_id}"
            )
        self.store.upsert_evidence(evidence)
        self.store.audit("evidence_add", actor, {"evidence": evidence.to_dict()})
        self.store.save()
        return evidence.to_dict()

    def revoke_evidence(self, evidence_id: str, reason: str, actor: str = "场馆管理员") -> dict:
        evidence = self.store.get_evidence(evidence_id)
        if evidence is None:
            raise NotFoundError(f"证据不存在：{evidence_id}")
        if evidence.status == "revoked":
            raise ConflictError(f"证据已撤销：{evidence_id}")
        revoked = Evidence(
            evidence_id=evidence.evidence_id,
            person_id=evidence.person_id,
            node_id=evidence.node_id,
            evidence_type=evidence.evidence_type,
            valid_from=evidence.valid_from,
            valid_until=evidence.valid_until,
            status="revoked",
            revoked_reason=reason,
            created_graph_version=evidence.created_graph_version,
            note=evidence.note,
        )
        self.store.upsert_evidence(revoked)
        self.store.audit(
            "evidence_revoke", actor, {"evidence_id": evidence_id, "reason": reason}
        )
        self.store.save()
        return revoked.to_dict()

    def list_evidences(self, person_id: str | None = None) -> list[dict]:
        return [e.to_dict() for e in self.store.list_evidences(person_id)]

    # =====================================================================
    # 临时豁免
    # =====================================================================

    def _require_graph(self) -> PublishedGraph:
        return self.get_graph()

    def add_waiver(self, data: dict, actor: str = "活动统筹员") -> dict:
        graph = self._require_graph()
        waiver_id = str(data.get("waiver_id", "")).strip() or f"WV-{uuid.uuid4().hex[:10]}"
        node_id = str(data.get("node_id", "")).strip()
        person_id = str(data.get("person_id", "")).strip()
        if not person_id or not node_id:
            raise ValidationError("豁免必须给出 person_id 与 node_id")
        if node_id not in graph.nodes:
            raise ValidationError(f"豁免引用的节点不存在：{node_id}")
        valid_from = to_date(data.get("valid_from"))
        until = data.get("valid_until")
        valid_until = to_date(until) if until else None
        if valid_until and valid_until < valid_from:
            raise ValidationError("豁免失效日早于生效日")
        waiver = {
            "waiver_id": waiver_id,
            "person_id": person_id,
            "node_id": node_id,
            "reason": str(data.get("reason", "")),
            "valid_from": iso(valid_from),
            "valid_until": iso(valid_until) if valid_until else None,
            "status": "active",
            "revoked_reason": "",
            "created_graph_version": graph.version,
        }
        try:
            self.store.add_waiver(waiver)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        self.store.audit("waiver_add", actor, {"waiver": waiver})
        self.store.save()
        return waiver

    def revoke_waiver(self, waiver_id: str, reason: str, actor: str = "活动统筹员") -> dict:
        waiver = self.store.revoke_waiver(waiver_id, reason)
        if waiver is None:
            raise NotFoundError(f"豁免单不存在：{waiver_id}")
        self.store.audit("waiver_revoke", actor, {"waiver_id": waiver_id, "reason": reason})
        self.store.save()
        return waiver

    def list_waivers(self) -> list[dict]:
        return self.store.list_waivers()

    def _active_waivers_as_evidence(self, person_id: str, on_date: date) -> list[Evidence]:
        """把有效豁免适配为 Evaluator 可消费的 Evidence（type=waiver）。"""
        result = []
        for w in self.store.active_waivers(person_id, on_date):
            result.append(
                Evidence(
                    evidence_id=w["waiver_id"],
                    person_id=w["person_id"],
                    node_id=w["node_id"],
                    evidence_type="waiver",
                    valid_from=to_date(w["valid_from"]),
                    valid_until=to_date(w["valid_until"]) if w.get("valid_until") else None,
                    status="active",
                    created_graph_version=w.get("created_graph_version", 0),
                    note=w.get("reason", ""),
                )
            )
        return result

    # =====================================================================
    # 资格评估与缺口
    # =====================================================================

    def check_qualification(
        self,
        person_id: str,
        targets: list[str],
        on_date: str | date | None = None,
        version: int | None = None,
    ) -> dict:
        graph = self.get_graph(version)
        on_d = to_date(on_date) if on_date else today()
        evaluator = Evaluator(graph)
        waivers = self._active_waivers_as_evidence(person_id, on_d)
        return evaluator.evaluate(
            person_id, self.store.list_evidences(person_id), targets, on_d, waivers=waivers
        )

    def query_gaps(
        self,
        targets: list[str],
        on_date: str | date | None = None,
        person_ids: list[str] | None = None,
        version: int | None = None,
    ) -> dict:
        graph = self.get_graph(version)
        on_d = to_date(on_date) if on_date else today()
        all_waivers = []
        if person_ids:
            for p in person_ids:
                all_waivers.extend(self._active_waivers_as_evidence(p, on_d))
        else:
            # 覆盖所有有豁免的人
            for w in self.store.list_waivers():
                if w["status"] == "active":
                    all_waivers.extend(self._active_waivers_as_evidence(w["person_id"], on_d))
        evaluator = Evaluator(graph)
        rows = evaluator.gaps_for_people(
            self.store.list_evidences(), targets, on_d, person_ids, waivers=all_waivers
        )
        ready = [r["person_id"] for r in rows if r["satisfied"]]
        not_ready = [
            {"person_id": r["person_id"], "missing": r["missing"], "evidence_used": r["evidence_used"]}
            for r in rows
            if not r["satisfied"]
        ]
        return {
            "graph_version": graph.version,
            "on_date": iso(on_d),
            "targets": targets,
            "ready_people": ready,
            "not_ready": not_ready,
        }

    # =====================================================================
    # 排班：生成路径并冻结
    # =====================================================================

    def create_schedule(self, data: dict, actor: str = "活动统筹员") -> dict:
        graph = self._require_graph()
        schedule_id = str(data.get("schedule_id", "")).strip() or f"SC-{uuid.uuid4().hex[:10]}"
        person_id = str(data.get("person_id", "")).strip()
        targets = list(data.get("required_nodes", []))
        if not person_id or not targets:
            raise ValidationError("排班必须给出 person_id 与 required_nodes")
        event_date = to_date(data.get("event_date"))
        if self.store.get_schedule(schedule_id) is not None:
            raise ConflictError(f"排班单已存在：{schedule_id}")

        evaluator = Evaluator(graph)
        waivers = self._active_waivers_as_evidence(person_id, event_date)
        result = evaluator.evaluate(
            person_id, self.store.list_evidences(person_id), targets, event_date, waivers=waivers
        )
        if not result["satisfied"]:
            missing_desc = ", ".join(m["node_id"] for m in result["missing"])
            raise ConflictError(f"资格不满足，无法排班（{person_id} @ {iso(event_date)}），缺口：{missing_desc}")

        evidence_by_id = {e.evidence_id: e for e in self.store.list_evidences(person_id)}
        waiver_ids = {w.evidence_id for w in waivers}
        frozen_evidence = []
        for ev_id in result["evidence_used"]:
            if ev_id in evidence_by_id:
                frozen_evidence.append(evidence_by_id[ev_id].to_dict())
            else:
                # 来自豁免
                w = self.store.get_waiver(ev_id)
                frozen_evidence.append({"waiver": w})
        steps = [
            PathStep(
                requirement_id=s["requirement_id"],
                requirement_name=s["requirement_name"],
                satisfied_by_node=s["satisfied_by_node"],
                satisfied_by_name=s["satisfied_by_name"],
                evidence_id=s["evidence_id"],
                valid_from=s["valid_from"],
                valid_until=s["valid_until"],
                substitution_chain=s["substitution_chain"],
            )
            for s in result["steps"]
        ]
        schedule = Schedule(
            schedule_id=schedule_id,
            person_id=person_id,
            required_nodes=targets,
            event_date=event_date,
            state="筹备",
            created_graph_version=graph.version,
            frozen_graph_snapshot=graph.snapshot(),
            frozen_evidence_snapshots=frozen_evidence,
            satisfied_path=steps,
            created_at=today(),
        )
        self.store.save_schedule(schedule)
        self.store.audit(
            "schedule_create",
            actor,
            {"schedule_id": schedule_id, "graph_version": graph.version, "person_id": person_id},
        )
        self.store.save()
        return schedule.to_dict()

    def transition_schedule(self, schedule_id: str, new_state: str, actor: str = "活动统筹员") -> dict:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise NotFoundError(f"排班单不存在：{schedule_id}")
        if new_state not in TRANSITIONS:
            raise ValidationError(f"未知状态：{new_state}")
        if new_state not in TRANSITIONS[schedule.state]:
            raise ConflictError(f"非法状态迁移：{schedule.state} -> {new_state}")
        old_state = schedule.state
        schedule.state = new_state
        self.store.save_schedule(schedule)
        self.store.audit(
            "schedule_transition",
            actor,
            {"schedule_id": schedule_id, "from": old_state, "to": new_state, "frozen": new_state in FROZEN_FROM},
        )
        self.store.save()
        return schedule.to_dict()

    def list_schedules(self, person_id: str | None = None) -> list[dict]:
        return [s.to_dict() for s in self.store.list_schedules(person_id)]

    def get_schedule(self, schedule_id: str) -> dict:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise NotFoundError(f"排班单不存在：{schedule_id}")
        return schedule.to_dict()

    # =====================================================================
    # 规则变化影响分析
    # =====================================================================

    def rule_changes(self, from_version: int | None = None, to_version: int | None = None) -> dict:
        versions = self.store.list_versions()
        if not versions:
            raise NotFoundError("尚未发布任何图谱版本")
        to_v = to_version or versions[-1]
        from_v = from_version if from_version is not None else (to_v - 1 if to_v > 1 else None)
        old = self.store.load_published(from_v) if from_v is not None else None
        if from_v is not None and old is None:
            raise NotFoundError(f"图谱版本不存在：v{from_v}")
        new = self.store.load_published(to_v)
        if new is None:
            raise NotFoundError(f"图谱版本不存在：v{to_v}")
        return diff_graphs(old, new)

    def impact_analysis(self, from_version: int | None = None, to_version: int | None = None) -> dict:
        """分析规则变化对人员证据和历史排班的影响（只读，不改写历史）。"""
        changes = self.rule_changes(from_version, to_version)
        new = self.get_graph(changes["to_version"])

        # 1) 证据指向被删除节点
        orphan_evidence = []
        for ev in self.store.list_evidences():
            if ev.node_id not in new.nodes:
                orphan_evidence.append(
                    {"evidence_id": ev.evidence_id, "person_id": ev.person_id, "node_id": ev.node_id}
                )

        # 2) 受影响人员：在新版本下重新评估其原排班要求（若要求节点仍存在）
        affected_people: dict[str, dict] = {}
        for schedule in self.store.list_schedules():
            targets = [t for t in schedule.required_nodes if t in new.nodes]
            removed_targets = [t for t in schedule.required_nodes if t not in new.nodes]
            entry = affected_people.setdefault(
                schedule.person_id,
                {"person_id": schedule.person_id, "schedules": [], "new_gaps": set(), "removed_requirements": set()},
            )
            entry["schedules"].append(schedule.schedule_id)
            entry["removed_requirements"].update(removed_targets)
            evaluator = Evaluator(new)
            waivers = self._active_waivers_as_evidence(schedule.person_id, schedule.event_date)
            result = evaluator.evaluate(
                schedule.person_id,
                self.store.list_evidences(schedule.person_id),
                targets,
                schedule.event_date,
                waivers=waivers,
            )
            entry["new_gaps"].update(m["node_id"] for m in result["missing"])

        people_report = []
        for entry in sorted(affected_people.values(), key=lambda x: x["person_id"]):
            people_report.append(
                {
                    "person_id": entry["person_id"],
                    "schedule_ids": entry["schedules"],
                    "new_gaps": sorted(entry["new_gaps"]),
                    "removed_requirements": sorted(entry["removed_requirements"]),
                }
            )

        # 3) 新增依赖的传导影响：哪些上级节点会把更多人卡在新前置上
        tightened_nodes = sorted({target for target, _ in changes["dependencies_added"]})

        # 4) 受影响的已冻结排班（仅报告，不重算其路径）
        frozen_schedules = [
            s.schedule_id
            for s in self.store.list_schedules()
            if s.state in FROZEN_FROM
        ]

        return {
            "changes": changes,
            "orphan_evidence": orphan_evidence,
            "affected_people": people_report,
            "tightened_nodes": tightened_nodes,
            "frozen_schedules_unchanged": frozen_schedules,
        }

    # =====================================================================
    # 审计
    # =====================================================================

    def audit_log(self, limit: int | None = None) -> list[dict]:
        events = self.store.audit_log
        return events[-limit:] if limit else events
