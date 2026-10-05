"""满足路径评估：在指定日期依据已发布图谱与证据台账生成可解释路径。"""
from __future__ import annotations

from collections import deque
from datetime import date

from .graph import PublishedGraph
from .models import Evidence
from .timeutil import iso


class Evaluator:
    """在某个已发布图谱版本上做资格判定。

    判定规则：
    - 目标节点的全部传递前置都必须满足；
    - 任一前置可由本节点证据或替代链上游节点的证据满足；
    - 证据必须 active 且在判定日处于有效期内（过期证书不能继续满足高级主题）；
    - waiver（临时豁免）只豁免对应节点本身，不豁免其前置；
    - 选择路径时取“替代链最短、节点 id、证据 id”最小者，保证结果可复现。
    """

    def __init__(self, graph: PublishedGraph):
        self.graph = graph
        self._satisfy_adj = self._build_satisfy_adjacency()

    def _build_satisfy_adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {n: set() for n in self.graph.nodes}
        for link in self.graph.subs:
            # 新替旧：replaces -> original；双向互认再加反向
            adj[link.replaces].add(link.original)
            if link.bidirectional:
                adj[link.original].add(link.replaces)
        return adj

    def required_closure(self, targets: list[str]) -> list[str]:
        """目标节点的全部传递前置（含目标自身），按依赖顺序排列。"""
        seen: set[str] = set()
        order: list[str] = []

        def visit(node: str) -> None:
            if node in seen:
                return
            seen.add(node)
            for req in self.graph.requirements_of(node):
                visit(req)
            order.append(node)

        for t in targets:
            visit(t)
        return order

    def _substitution_chain(self, evidence_node: str, required_node: str) -> list[str]:
        """证据节点到需求节点的最短替代链（BFS）。"""
        if evidence_node == required_node:
            return [evidence_node]
        prev: dict[str, str] = {evidence_node: evidence_node}
        queue: deque[str] = deque([evidence_node])
        while queue:
            cur = queue.popleft()
            for nxt in sorted(self._satisfy_adj.get(cur, ())):
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == required_node:
                    chain = [nxt]
                    while chain[-1] != evidence_node:
                        chain.append(prev[chain[-1]])
                    chain.reverse()
                    return chain
                queue.append(nxt)
        return []  # 不可达（理论上不会发生）

    def valid_evidence_index(
        self, evidences: list[Evidence], on_date: date
    ) -> dict[str, list[Evidence]]:
        """按节点归集判定日有效证据（active 且在有效期内）。"""
        index: dict[str, list[Evidence]] = {}
        for ev in evidences:
            if ev.status != "active":
                continue
            if ev.node_id not in self.graph.nodes:
                continue
            if ev.valid_from > on_date:
                continue
            if ev.valid_until is not None and ev.valid_until < on_date:
                continue
            index.setdefault(ev.node_id, []).append(ev)
        return index

    def evaluate(
        self,
        person_id: str,
        evidences: list[Evidence],
        targets: list[str],
        on_date: date,
        waivers: list[Evidence] | None = None,
    ) -> dict:
        """返回可解释的满足结果。

        ``waivers`` 为临时豁免（以 Evidence 表示、evidence_type='waiver'），
        只直接满足其登记节点，不沿替代链扩散，也不免除前置节点。
        """
        for t in targets:
            if t not in self.graph.nodes:
                raise KeyError(t)

        mine = [e for e in evidences if e.person_id == person_id]
        evidence_index = self.valid_evidence_index(mine, on_date)
        waiver_index = self.valid_evidence_index(
            [w for w in (waivers or []) if w.person_id == person_id], on_date
        )

        required = self.required_closure(targets)

        steps = []
        missing: list[dict] = []
        used_evidence: set[str] = set()

        for req in required:
            candidates = []
            for holder in sorted(self.graph.satisfiers_of(req)):
                chain = self._substitution_chain(holder, req)
                if not chain:
                    continue
                for ev in evidence_index.get(holder, ()):
                    candidates.append((len(chain), holder, ev.evidence_id, chain, ev))
            # 豁免只在登记节点本身直接生效
            for ev in waiver_index.get(req, ()):
                candidates.append((1, req, ev.evidence_id, [req], ev))
            candidates.sort(key=lambda c: (c[0], c[1], c[2]))
            if candidates:
                _, holder, _, chain, ev = candidates[0]
                used_evidence.add(ev.evidence_id)
                steps.append(
                    {
                        "requirement_id": req,
                        "requirement_name": self.graph.nodes[req].name,
                        "satisfied_by_node": holder,
                        "satisfied_by_name": self.graph.nodes[holder].name,
                        "evidence_id": ev.evidence_id,
                        "evidence_type": ev.evidence_type,
                        "valid_from": iso(ev.valid_from),
                        "valid_until": iso(ev.valid_until) if ev.valid_until else None,
                        "substitution_chain": chain,
                        "direct": holder == req,
                    }
                )
            else:
                missing.append(
                    {
                        "node_id": req,
                        "node_name": self.graph.nodes[req].name,
                        "satisfiers": sorted(self.graph.satisfiers_of(req)),
                    }
                )

        # 仅保留与目标真正相关的缺口（required 已按闭包过滤）
        return {
            "person_id": person_id,
            "graph_version": self.graph.version,
            "on_date": iso(on_date),
            "targets": list(targets),
            "satisfied": not missing,
            "steps": steps,
            "missing": missing,
            "evidence_used": sorted(used_evidence),
        }

    def gaps_for_people(
        self,
        evidences: list[Evidence],
        targets: list[str],
        on_date: date,
        person_ids: list[str] | None = None,
        waivers: list[Evidence] | None = None,
    ) -> list[dict]:
        """批量查询多人的资格缺口（排班前的候选筛选）。"""
        if person_ids is None:
            people = {e.person_id for e in evidences if e.status == "active"}
            if waivers:
                people.update(w.person_id for w in waivers if w.status == "active")
            person_ids = sorted(people)
        results = []
        for person_id in person_ids:
            result = self.evaluate(person_id, evidences, targets, on_date, waivers=waivers)
            results.append(
                {
                    "person_id": person_id,
                    "satisfied": result["satisfied"],
                    "missing": result["missing"],
                    "evidence_used": result["evidence_used"],
                }
            )
        return results
