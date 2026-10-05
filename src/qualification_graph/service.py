"""核心领域服务：图谱版本、满足判定、缺口分析、排班冻结、规则影响。"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from .models import (
    Evidence,
    Exemption,
    GraphError,
    Qualification,
    RequirementGroup,
    SatisfactionExplanation,
)
from .storage import Storage
from .validation import validate_graph


def rule_fingerprint(nodes: dict[str, Qualification], evidence_types: dict[str, str]) -> str:
    payload = json.dumps(
        {
            "nodes": {qid: node.to_dict() for qid, node in sorted(nodes.items())},
            "evidence_types": dict(sorted(evidence_types.items())),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class QualificationService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    # ---- 图谱草稿与发布 ---------------------------------------------------

    def create_draft(self, created_from: int | None = None) -> int:
        """创建草稿；默认从最近已发布版本克隆（无则空草稿）。"""
        if created_from is None:
            created_from = self.storage.latest_published()
        elif self.storage.version_status(created_from) is None:
            raise GraphError(f"图谱版本 {created_from} 不存在")
        version = self.storage.max_version() + 1
        self.storage.create_version(version, created_from, status="draft")
        if created_from is not None:
            self.storage.clone_nodes(created_from, version)
        return version

    def _require_draft(self, version: int) -> None:
        status = self.storage.version_status(version)
        if status is None:
            raise GraphError(f"图谱版本 {version} 不存在")
        if status != "draft":
            raise GraphError(f"图谱版本 {version} 已发布，不可修改；请基于它创建新草稿")

    def upsert_node(self, version: int, node: Qualification) -> None:
        self._require_draft(version)
        self.storage.put_node(version, node)

    def delete_node(self, version: int, qid: str) -> None:
        self._require_draft(version)
        if not self.storage.remove_node(version, qid):
            raise GraphError(f"草稿 {version} 中不存在资格 {qid}")

    def register_evidence_type(self, version: int, code: str, label: str) -> None:
        self._require_draft(version)
        if not code:
            raise GraphError("证据类型代码不能为空")
        self.storage.set_evidence_type(version, code, label)

    def validate_draft(self, version: int) -> list[dict[str, Any]]:
        if self.storage.version_status(version) is None:
            raise GraphError(f"图谱版本 {version} 不存在")
        nodes = self.storage.load_nodes(version)
        evidence_types = set(self.storage.load_evidence_types(version))
        return [issue.to_dict() for issue in validate_graph(nodes, frozenset(evidence_types))]

    def publish(self, version: int, day: date | None = None, notes: str = "") -> dict[str, Any]:
        """发布前强制校验；存在任何问题都拒绝发布。"""
        self._require_draft(version)
        nodes = self.storage.load_nodes(version)
        evidence_types = self.storage.load_evidence_types(version)
        issues = validate_graph(nodes, frozenset(evidence_types))
        blocking = [i for i in issues if i.code in ("cycle", "dangling_reference", "self_reference")]
        if blocking:
            raise GraphError(
                "图谱存在循环或矛盾，拒绝发布："
                + "；".join(i.message for i in blocking)
            )
        if not nodes:
            raise GraphError("空图谱不能发布")
        day = day or date.today()
        fingerprint = rule_fingerprint(nodes, evidence_types)
        self.storage.mark_published(version, day, fingerprint, notes)
        return {
            "version": version,
            "published_on": day.isoformat(),
            "rule_hash": fingerprint,
            "node_count": len(nodes),
            "warnings": [i.to_dict() for i in issues],
        }

    def list_versions(self) -> list[dict[str, Any]]:
        rows = self.storage.list_versions()
        return [
            {
                "version": r["version"],
                "status": r["status"],
                "created_from": r["created_from"],
                "published_on": r["published_on"],
                "rule_hash": r["rule_hash"],
                "notes": r["notes"],
            }
            for r in rows
        ]

    def get_graph(self, version: int) -> dict[str, Any]:
        if self.storage.version_status(version) is None:
            raise GraphError(f"图谱版本 {version} 不存在")
        nodes = self.storage.load_nodes(version)
        evidence_types = self.storage.load_evidence_types(version)
        return {
            "version": version,
            "nodes": {qid: node.to_dict() for qid, node in sorted(nodes.items())},
            "evidence_types": evidence_types,
        }

    def _resolve_version(self, version: int | None) -> int:
        if version is not None:
            if self.storage.version_status(version) != "published":
                raise GraphError(f"图谱版本 {version} 不是已发布版本")
            return version
        latest = self.storage.latest_published()
        if latest is None:
            raise GraphError("尚无已发布的资格图")
        return latest

    # ---- 证据与豁免 -------------------------------------------------------

    def grant_evidence(
        self,
        guide_id: str,
        evidence_type: str,
        title: str,
        issued_on: date,
        valid_until: date | None = None,
    ) -> int:
        if valid_until is not None and valid_until < issued_on:
            raise GraphError("证据有效期截止日不能早于签发日")
        return self.storage.insert_evidence(
            Evidence(
                evidence_id=None,
                guide_id=guide_id,
                evidence_type=evidence_type,
                title=title,
                issued_on=issued_on,
                valid_until=valid_until,
            )
        )

    def revoke_evidence(self, evidence_id: int, day: date | None = None, reason: str = "") -> None:
        if not self.storage.revoke_evidence(evidence_id, day or date.today(), reason):
            evidence = self.storage.get_evidence(evidence_id)
            if evidence is None:
                raise GraphError(f"证据 {evidence_id} 不存在")
            raise GraphError(f"证据 {evidence_id} 已撤销，不能重复撤销")

    def add_exemption(
        self, guide_id: str, qid: str, start_on: date, end_on: date, reason: str
    ) -> int:
        if end_on < start_on:
            raise GraphError("豁免结束日不能早于开始日")
        if not reason:
            raise GraphError("临时豁免必须说明理由")
        return self.storage.insert_exemption(
            Exemption(
                exemption_id=None,
                guide_id=guide_id,
                qid=qid,
                start_on=start_on,
                end_on=end_on,
                reason=reason,
            )
        )

    def revoke_exemption(self, exemption_id: int) -> None:
        if not self.storage.revoke_exemption(exemption_id):
            if self.storage.get_exemption(exemption_id) is None:
                raise GraphError(f"豁免 {exemption_id} 不存在")
            raise GraphError(f"豁免 {exemption_id} 已撤销")

    # ---- 满足判定 ---------------------------------------------------------

    def explain(
        self,
        guide_id: str,
        qid: str,
        day: date,
        version: int | None = None,
    ) -> SatisfactionExplanation:
        """生成可解释的满足路径（基于指定/最新已发布图谱）。"""
        version = self._resolve_version(version)
        nodes = self.storage.load_nodes(version)
        if qid not in nodes:
            raise GraphError(f"资格 {qid} 不在图谱版本 {version} 中")
        evidences = self.storage.list_evidences(guide_id)
        exemptions = self.storage.list_exemptions(guide_id)
        return self._evaluate(qid, day, nodes, evidences, exemptions, frozenset(), (qid,))

    def _evaluate(
        self,
        qid: str,
        day: date,
        nodes: dict[str, Qualification],
        evidences: list[Evidence],
        exemptions: list[Exemption],
        visiting: frozenset[str],
        path: tuple[str, ...],
    ) -> SatisfactionExplanation:
        node = nodes.get(qid)
        if node is None:
            return SatisfactionExplanation(
                qid=qid, satisfied=False, day=day, basis="missing",
                detail=f"资格 {qid} 不在当前图谱中", path=path,
            )
        if qid in visiting:
            return SatisfactionExplanation(
                qid=qid, satisfied=False, day=day, basis="cycle",
                detail="检测到替代/前置循环，该路径不可用", path=path,
            )

        # 1. 临时豁免优先（窗口内、未撤销）。
        active_exemption = next(
            (e for e in exemptions if e.qid == qid and e.active_on(day)), None
        )
        if active_exemption is not None:
            return SatisfactionExplanation(
                qid=qid,
                satisfied=True,
                day=day,
                basis="exemption",
                detail=(
                    f"临时豁免 #{active_exemption.exemption_id} 生效中"
                    f"（{active_exemption.start_on} 至 {active_exemption.end_on}）：{active_exemption.reason}"
                ),
                path=path,
            )

        next_visiting = visiting | {qid}

        # 2. 直接要求：需求组 AND，组内候选 OR。
        group_results: list[SatisfactionExplanation] = []
        if node.requirement_groups:
            for index, group in enumerate(node.requirement_groups, start=1):
                candidate_results = [
                    self._evaluate_candidate(
                        cand, day, nodes, evidences, exemptions, next_visiting,
                        path + (cand,),
                    )
                    for cand in group.candidates
                ]
                winner = next((c for c in candidate_results if c.satisfied), None)
                if winner is not None:
                    group_results.append(
                        SatisfactionExplanation(
                            qid=qid,
                            satisfied=True,
                            day=day,
                            basis="prerequisite",
                            detail=f"需求组 {index}/{len(node.requirement_groups)} 由候选 {winner.qid} 满足",
                            children=(winner,),
                            path=path,
                        )
                    )
                else:
                    group_results.append(
                        SatisfactionExplanation(
                            qid=qid,
                            satisfied=False,
                            day=day,
                            basis="prerequisite",
                            detail=f"需求组 {index}/{len(node.requirement_groups)} 无候选满足",
                            children=tuple(candidate_results),
                            path=path,
                        )
                    )
        direct_satisfied = bool(group_results) and all(g.satisfied for g in group_results)

        # 3. 替代链：持有被替代的旧资格即视为持有本资格。
        supersession_results: list[SatisfactionExplanation] = []
        for old in node.supersedes:
            child = self._evaluate(
                old, day, nodes, evidences, exemptions, next_visiting, (old,)
            )
            if child.satisfied and child.basis == "supersession":
                # 多级替代：沿用内部胜出子链（oldest → … → old），再接当前节点。
                inner = next(c for c in child.children if c.satisfied)
                chain_path = inner.path + (qid,)
            elif child.satisfied:
                chain_path = (old, qid)
            else:
                chain_path = child.path + (qid,)
            annotated = SatisfactionExplanation(
                qid=child.qid,
                satisfied=child.satisfied,
                day=day,
                basis=child.basis,
                detail=child.detail,
                children=child.children,
                path=chain_path,
            )
            supersession_results.append(annotated)
        supersession_satisfied = any(c.satisfied for c in supersession_results)

        if direct_satisfied:
            detail = f"直接持有资格《{node.name}》：全部 {len(group_results)} 个需求组均满足"
            if supersession_satisfied:
                detail += "（同时存在可用替代链）"
            return SatisfactionExplanation(
                qid=qid, satisfied=True, day=day, basis="direct",
                detail=detail, children=tuple(group_results), path=path,
            )
        if supersession_satisfied:
            winners = tuple(c for c in supersession_results if c.satisfied)
            chain = "、".join(" → ".join(c.path) for c in winners)
            return SatisfactionExplanation(
                qid=qid, satisfied=True, day=day, basis="supersession",
                detail=f"由旧资格替代满足：{chain}",
                children=winners, path=path,
            )

        children = tuple(group_results) + tuple(
            c for c in supersession_results if not c.satisfied
        )
        if not children:
            reason = f"资格《{node.name}》无获取路径（未配置需求、替代或豁免）"
        elif group_results:
            reason = f"资格《{node.name}》的需求未全部满足，且无可用替代链"
        else:
            reason = f"资格《{node.name}》的替代资格均不满足"
        return SatisfactionExplanation(
            qid=qid, satisfied=False, day=day, basis="missing",
            detail=reason, children=children, path=path,
        )

    def _evaluate_candidate(
        self,
        candidate: str,
        day: date,
        nodes: dict[str,Qualification],
        evidences: list[Evidence],
        exemptions: list[Exemption],
        visiting: frozenset[str],
        path: tuple[str, ...],
    ) -> SatisfactionExplanation:
        if candidate in nodes:
            return self._evaluate(candidate, day, nodes, evidences, exemptions, visiting, path)
        return self._evaluate_evidence(candidate, day, evidences, path)

    def _evaluate_evidence(
        self,
        evidence_type: str,
        day: date,
        evidences: list[Evidence],
        path: tuple[str, ...],
    ) -> SatisfactionExplanation:
        mine = [e for e in evidences if e.evidence_type == evidence_type]
        active = next((e for e in mine if e.active_on(day)), None)
        if active is not None:
            until = f"，有效期至 {active.valid_until}" if active.valid_until else "（长期有效）"
            return SatisfactionExplanation(
                qid=evidence_type, satisfied=True, day=day, basis="evidence",
                detail=f"证据 #{active.evidence_id}《{active.title}》在 {day} 有效{until}",
                path=path,
            )
        if not mine:
            return SatisfactionExplanation(
                qid=evidence_type, satisfied=False, day=day, basis="evidence",
                detail=f"未持有证据类型 {evidence_type}", path=path,
            )
        revoked = [e for e in mine if e.revoked]
        if revoked:
            e = revoked[0]
            detail = f"证据 #{e.evidence_id}《{e.title}》已撤销（{e.revoked_on}）"
        else:
            expired = [e for e in mine if e.valid_until is not None and e.valid_until < day]
            future = [e for e in mine if e.issued_on > day]
            if expired:
                e = max(expired, key=lambda x: x.valid_until)
                detail = f"证据 #{e.evidence_id}《{e.title}》已于 {e.valid_until} 过期"
            elif future:
                e = min(future, key=lambda x: x.issued_on)
                detail = f"证据 #{e.evidence_id}《{e.title}》尚未生效（签发日 {e.issued_on}）"
            else:
                detail = f"证据类型 {evidence_type} 的持件在 {day} 不可用"
        return SatisfactionExplanation(
            qid=evidence_type, satisfied=False, day=day, basis="evidence",
            detail=detail, path=path,
        )

    # ---- 缺口分析 ---------------------------------------------------------

    def qualification_gaps(
        self,
        guide_id: str,
        qid: str,
        day: date,
        version: int | None = None,
    ) -> dict[str, Any]:
        version = self._resolve_version(version)
        explanation = self.explain(guide_id, qid, day, version)
        evidences = self.storage.list_evidences(guide_id)

        missing_evidence: dict[str, dict[str, Any]] = {}
        missing_qualifications: dict[str, str] = {}

        def walk(node: SatisfactionExplanation) -> None:
            if node.satisfied:
                return
            if node.basis == "evidence":
                code = node.qid
                if code not in missing_evidence:
                    missing_evidence[code] = self._classify_evidence_gap(code, day, evidences)
                return
            if not node.children and node.basis in ("missing", "cycle"):
                missing_qualifications.setdefault(node.qid, node.detail)
                return
            for child in node.children:
                walk(child)

        walk(explanation)
        return {
            "guide_id": guide_id,
            "qid": qid,
            "day": day.isoformat(),
            "graph_version": version,
            "satisfied": explanation.satisfied,
            "explanation": explanation.to_dict(),
            "missing_evidence_types": sorted(missing_evidence.values(), key=lambda x: x["code"]),
            "missing_qualifications": [
                {"qid": k, "reason": v} for k, v in sorted(missing_qualifications.items())
            ],
        }

    @staticmethod
    def _classify_evidence_gap(
        code: str, day: date, evidences: list[Evidence]
    ) -> dict[str, Any]:
        mine = [e for e in evidences if e.evidence_type == code]
        active = next((e for e in mine if e.active_on(day)), None)
        if active is not None:
            return {"code": code, "status": "active", "evidence_id": active.evidence_id}
        revoked = [e for e in mine if e.revoked]
        expired = [e for e in mine if e.valid_until is not None and day > e.valid_until and not e.revoked]
        future = [e for e in mine if e.issued_on > day]
        if revoked:
            return {
                "code": code,
                "status": "revoked",
                "evidence_ids": [e.evidence_id for e in revoked],
                "revoked_on": max(e.revoked_on for e in revoked if e.revoked_on).isoformat(),
            }
        if expired:
            nearest = max(expired, key=lambda e: e.valid_until)
            return {
                "code": code,
                "status": "expired",
                "evidence_id": nearest.evidence_id,
                "valid_until": nearest.valid_until.isoformat(),
            }
        if future:
            nearest = min(future, key=lambda e: e.issued_on)
            return {
                "code": code,
                "status": "not_yet_valid",
                "evidence_id": nearest.evidence_id,
                "issued_on": nearest.issued_on.isoformat(),
            }
        return {"code": code, "status": "not_held"}

    # ---- 排班与历史冻结 ----------------------------------------------------

    def create_schedule(
        self,
        guide_id: str,
        qid: str,
        scheduled_on: date,
        version: int | None = None,
        note: str = "",
        assigned_on: date | None = None,
    ) -> dict[str, Any]:
        version = self._resolve_version(version)
        explanation = self.explain(guide_id, qid, scheduled_on, version)
        if not explanation.satisfied:
            gaps = self.qualification_gaps(guide_id, qid, scheduled_on, version)
            raise GraphError(
                f"讲解员 {guide_id} 在 {scheduled_on} 不满足资格 {qid}，无法排班："
                + json.dumps(
                    {
                        "missing_evidence_types": gaps["missing_evidence_types"],
                        "missing_qualifications": gaps["missing_qualifications"],
                    },
                    ensure_ascii=False,
                )
            )
        assigned_on = assigned_on or date.today()
        nodes = self.storage.load_nodes(version)
        evidence_types = self.storage.load_evidence_types(version)
        fingerprint = rule_fingerprint(nodes, evidence_types)
        leaves = [leaf.to_dict() for leaf in explanation.leaves()]
        schedule_id = self.storage.insert_schedule(
            {
                "guide_id": guide_id,
                "qid": qid,
                "scheduled_on": scheduled_on,
                "assigned_on": assigned_on,
                "graph_version": version,
                "rule_hash": fingerprint,
                "explanation_json": json.dumps(explanation.to_dict(), ensure_ascii=False),
                "leaves_json": json.dumps(leaves, ensure_ascii=False),
                "note": note,
            }
        )
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: int) -> dict[str, Any]:
        row = self.storage.get_schedule(schedule_id)
        if row is None:
            raise GraphError(f"排班 {schedule_id} 不存在")
        return self._schedule_dict(row)

    def list_schedules(self, guide_id: str | None = None) -> list[dict[str, Any]]:
        return [self._schedule_dict(r) for r in self.storage.list_schedules(guide_id)]

    @staticmethod
    def _schedule_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "schedule_id": row["schedule_id"],
            "guide_id": row["guide_id"],
            "qid": row["qid"],
            "scheduled_on": row["scheduled_on"].isoformat(),
            "assigned_on": row["assigned_on"].isoformat(),
            "graph_version": row["graph_version"],
            "rule_hash": row["rule_hash"],
            "frozen_explanation": row["explanation"],
            "frozen_bases": row["leaves"],
            "note": row["note"],
        }

    def reevaluate_schedule(self, schedule_id: int, day: date | None = None) -> dict[str, Any]:
        """只读复评：用当前图谱与证据重算，绝不回写冻结的历史排班。"""
        frozen = self.storage.get_schedule(schedule_id)
        if frozen is None:
            raise GraphError(f"排班 {schedule_id} 不存在")
        day = day or frozen["scheduled_on"]
        latest = self.storage.latest_published()
        current = self.explain(frozen["guide_id"], frozen["qid"], day, latest)
        as_of_then = None
        if latest != frozen["graph_version"]:
            as_of_then = self.explain(
                frozen["guide_id"], frozen["qid"], frozen["scheduled_on"], latest
            ).to_dict()
        return {
            "schedule_id": schedule_id,
            "frozen_version": frozen["graph_version"],
            "current_version": latest,
            "reevaluate_day": day.isoformat(),
            "satisfied_now_on_current_rules": current.satisfied,
            "current_explanation": current.to_dict(),
            "current_rules_applied_on_scheduled_day": as_of_then,
            "frozen_explanation_unchanged": frozen["explanation"],
        }

    # ---- 规则变化影响 ------------------------------------------------------

    def rule_change_impact(
        self,
        old_version: int,
        new_version: int | None = None,
        day: date | None = None,
    ) -> dict[str, Any]:
        if self.storage.version_status(old_version) is None:
            raise GraphError(f"图谱版本 {old_version} 不存在")
        new_version = self._resolve_version(new_version)
        day = day or date.today()

        old_nodes = self.storage.load_nodes(old_version)
        new_nodes = self.storage.load_nodes(new_version)
        old_types = self.storage.load_evidence_types(old_version)
        new_types = self.storage.load_evidence_types(new_version)

        added = sorted(set(new_nodes) - set(old_nodes))
        removed = sorted(set(old_nodes) - set(new_nodes))
        changed: list[dict[str, str]] = []
        for qid in sorted(set(old_nodes) & set(new_nodes)):
            before = json.dumps(old_nodes[qid].to_dict(), ensure_ascii=False, sort_keys=True)
            after = json.dumps(new_nodes[qid].to_dict(), ensure_ascii=False, sort_keys=True)
            if before != after:
                changed.append(
                    {
                        "qid": qid,
                        "kind": new_nodes[qid].kind,
                        "before": old_nodes[qid].to_dict(),
                        "after": new_nodes[qid].to_dict(),
                    }
                )
        types_added = sorted(set(new_types) - set(old_types))
        types_removed = sorted(set(old_types) - set(new_types))

        # 受影响讲解员：所有出现过排班的 (讲解员, 资格) 对，外加变更节点涉及的讲解员。
        pairs = set(self.storage.distinct_scheduled_pairs())
        known_guides = {guide for guide, _ in pairs}
        for evidence in self.storage.list_evidences():
            known_guides.add(evidence.guide_id)
        for exemption in self.storage.list_exemptions():
            if exemption.qid in set(added + removed + [c["qid"] for c in changed]):
                known_guides.add(exemption.guide_id)
        for guide in known_guides:
            for qid in added + removed + [c["qid"] for c in changed]:
                pairs.add((guide, qid))

        flips: list[dict[str, Any]] = []
        for guide_id, qid in sorted(pairs):
            before_ok = qid in old_nodes and self.explain(guide_id, qid, day, old_version).satisfied
            after_ok = qid in new_nodes and self.explain(guide_id, qid, day, new_version).satisfied
            if before_ok != after_ok:
                flips.append(
                    {
                        "guide_id": guide_id,
                        "qid": qid,
                        "before_satisfied": before_ok,
                        "after_satisfied": after_ok,
                        "change": "lost" if before_ok else "gained",
                    }
                )

        affected_schedules: list[dict[str, Any]] = []
        for schedule in self.storage.list_schedules():
            if schedule["graph_version"] == new_version:
                continue
            current_at_day = self.explain(
                schedule["guide_id"], schedule["qid"], day, new_version
            )
            historical = self.explain(
                schedule["guide_id"],
                schedule["qid"],
                schedule["scheduled_on"],
                new_version,
            )
            affected_schedules.append(
                {
                    "schedule_id": schedule["schedule_id"],
                    "guide_id": schedule["guide_id"],
                    "qid": schedule["qid"],
                    "scheduled_on": schedule["scheduled_on"].isoformat(),
                    "frozen_version": schedule["graph_version"],
                    "satisfied_under_new_rules_on_scheduled_day": historical.satisfied,
                    "satisfied_under_new_rules_on_review_day": current_at_day.satisfied,
                    "history_rewritten": False,
                }
            )

        return {
            "old_version": old_version,
            "new_version": new_version,
            "review_day": day.isoformat(),
            "rule_hash_old": rule_fingerprint(old_nodes, old_types),
            "rule_hash_new": rule_fingerprint(new_nodes, new_types),
            "nodes_added": added,
            "nodes_removed": removed,
            "nodes_changed": changed,
            "evidence_types_added": types_added,
            "evidence_types_removed": types_removed,
            "guide_flips": flips,
            "schedules_using_old_version": affected_schedules,
        }
