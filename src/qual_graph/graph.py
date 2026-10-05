"""资格图谱：草稿编辑、发布前校验（循环与矛盾检测）、已发布版本只读视图。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from .models import DependencyEdge, QualificationNode, SubstitutionLink
from .timeutil import iso


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _tarjan_scc(adj: dict[str, set[str]]) -> list[list[str]]:
    """返回所有强连通分量（节点 id 排序，分量按最小节点排序，保证输出稳定）。"""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    def strong_connect(v: str) -> None:
        nonlocal index
        indices[v] = lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in indices:
                strong_connect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            result.append(sorted(component))

    for node in sorted(adj):
        if node not in indices:
            strong_connect(node)
    return sorted(result, key=lambda comp: comp[0])


def validate_graph(
    nodes: dict[str, QualificationNode],
    deps: list[DependencyEdge],
    subs: list[SubstitutionLink],
) -> list[dict]:
    """发布前静态校验，返回问题清单。``level == 'error'`` 的问题阻断发布。"""
    issues: list[dict] = []

    def error(code: str, message: str, **refs) -> None:
        issues.append({"level": "error", "code": code, "message": message, "refs": refs})

    def warn(code: str, message: str, **refs) -> None:
        issues.append({"level": "warning", "code": code, "message": message, "refs": refs})

    known = set(nodes)

    # 1) 悬空引用
    for edge in deps:
        if edge.target not in known:
            error("dangling_dependency", f"依赖目标节点不存在：{edge.target}", edge=edge.to_dict())
        if edge.required not in known:
            error("dangling_dependency", f"被依赖节点不存在：{edge.required}", edge=edge.to_dict())
    for link in subs:
        if link.replaces not in known:
            error("dangling_substitution", f"替代节点不存在：{link.replaces}", edge=link.to_dict())
        if link.original not in known:
            error("dangling_substitution", f"被替代节点不存在：{link.original}", edge=link.to_dict())

    # 2) 重复边
    seen_dep: set[tuple[str, str]] = set()
    for edge in deps:
        key = (edge.target, edge.required)
        if key in seen_dep:
            warn("duplicate_dependency", f"重复的依赖边：{edge.target} -> {edge.required}", edge=edge.to_dict())
        seen_dep.add(key)
    seen_sub: set[tuple[str, bool]] = set()
    for link in subs:
        key = (link.replaces, link.original, link.bidirectional)
        if key in seen_sub:
            warn("duplicate_substitution", f"重复的替代边：{link.replaces} ~ {link.original}", edge=link.to_dict())
        seen_sub.add(key)

    # 3) 替代等价类（无向连通分量）
    parent = {n: n for n in known}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for link in subs:
        if link.replaces in known and link.original in known:
            union(link.replaces, link.original)

    classes: dict[str, list[str]] = {}
    for n in known:
        classes.setdefault(find(n), []).append(n)
    node_class = {n: root for root, members in classes.items() for n in members}

    # 4) 矛盾：同一等价类内存在依赖边（互相替代的节点不应互为前置）
    for edge in deps:
        if edge.target in known and edge.required in known:
            if node_class[edge.target] == node_class[edge.required]:
                error(
                    "dependency_substitution_conflict",
                    f"依赖与替代矛盾：{edge.target} 与 {edge.required} 互为替代却又存在前置依赖",
                    edge=edge.to_dict(),
                )

    # 5) 矛盾：换版替代跨类型（主题 v2 不应替代展厅节点）
    for link in subs:
        if link.supersedes and link.replaces in known and link.original in known:
            if nodes[link.replaces].kind != nodes[link.original].kind:
                error(
                    "supersede_kind_mismatch",
                    f"换版替代跨节点类型：{link.replaces}({nodes[link.replaces].kind}) "
                    f"替代 {link.original}({nodes[link.original].kind})",
                    edge=link.to_dict(),
                )

    # 6) 依赖图按等价类收缩后做循环检测
    dep_adj: dict[str, set[str]] = {n: set() for n in known}
    for edge in deps:
        if edge.target in known and edge.required in known:
            dep_adj[edge.target].add(edge.required)
    class_adj: dict[str, set[str]] = {root: set() for root in classes}
    for src, targets in dep_adj.items():
        for dst in targets:
            cs, cd = node_class[src], node_class[dst]
            if cs != cd:
                class_adj[cs].add(cd)
    for component in _tarjan_scc(class_adj):
        if len(component) > 1:
            members = sorted(n for root in component for n in classes[root])
            error(
                "dependency_cycle",
                "资格依赖存在循环：" + " -> ".join(members),
                nodes=members,
            )

    # 7) 单向替代边成环（换版链首尾相接，方向矛盾）；双向互认不参与
    unidir_adj: dict[str, set[str]] = {n: set() for n in known}
    for link in subs:
        if not link.bidirectional and link.replaces in known and link.original in known:
            unidir_adj[link.replaces].add(link.original)
    for component in _tarjan_scc(unidir_adj):
        if len(component) > 1:
            error(
                "substitution_cycle",
                "单向替代关系形成循环（换版链首尾相接）：" + " -> ".join(component),
                nodes=component,
            )

    # 8) 预警：一个旧节点被多个换版新版本同时替代（可能是拆分，需人工确认）
    superseded_by: dict[str, list[str]] = {}
    for link in subs:
        if link.supersedes and link.original in known:
            superseded_by.setdefault(link.original, []).append(link.replaces)
    for original, replacers in superseded_by.items():
        if len(set(replacers)) > 1:
            warn(
                "supersede_split",
                f"节点 {original} 被多个新版本同时替代：{', '.join(sorted(set(replacers)))}",
                original=original,
                replacers=sorted(set(replacers)),
            )

    return issues


# ---------------------------------------------------------------------------
# 草稿与已发布版本
# ---------------------------------------------------------------------------

class GraphDraft:
    """可编辑的图谱草稿。发布成功后冻结为只读 PublishedGraph。"""

    def __init__(self, based_on_version: int = 0):
        self.based_on_version = based_on_version
        self.nodes: dict[str, QualificationNode] = {}
        self.deps: list[DependencyEdge] = []
        self.subs: list[SubstitutionLink] = []

    def upsert_node(self, node: QualificationNode) -> None:
        if node.node_id in self.nodes and self.nodes[node.node_id].kind != node.kind:
            raise ValueError(f"节点类型不可变更：{node.node_id}")
        self.nodes[node.node_id] = node

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            raise KeyError(node_id)
        del self.nodes[node_id]
        self.deps = [e for e in self.deps if node_id not in (e.target, e.required)]
        self.subs = [l for l in self.subs if node_id not in (l.replaces, l.original)]

    def add_dependency(self, edge: DependencyEdge) -> None:
        self.deps.append(edge)

    def remove_dependency(self, target: str, required: str) -> None:
        before = len(self.deps)
        self.deps = [e for e in self.deps if not (e.target == target and e.required == required)]
        if len(self.deps) == before:
            raise KeyError(f"{target} -> {required}")

    def add_substitution(self, link: SubstitutionLink) -> None:
        self.subs.append(link)

    def remove_substitution(self, replaces: str, original: str) -> None:
        before = len(self.subs)
        self.subs = [l for l in self.subs if not (l.replaces == replaces and l.original == original)]
        if len(self.subs) == before:
            raise KeyError(f"{replaces} ~ {original}")

    def validate(self) -> list[dict]:
        return validate_graph(self.nodes, self.deps, self.subs)

    @staticmethod
    def from_published(graph: "PublishedGraph") -> "GraphDraft":
        draft = GraphDraft(based_on_version=graph.version)
        draft.nodes = dict(graph.nodes)
        draft.deps = list(graph.deps)
        draft.subs = list(graph.subs)
        return draft


@dataclass
class PublishedGraph:
    """已发布图谱版本，构造后不可变。"""

    version: int
    published_at: date
    nodes: dict[str, QualificationNode]
    deps: list[DependencyEdge]
    subs: list[SubstitutionLink]
    note: str = ""
    _dep_adj: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _dependents_closure: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    _satisfiers: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    _equivalence: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for edge in self.deps:
            adj[edge.target].append(edge.required)
        self._dep_adj = {k: tuple(sorted(v)) for k, v in adj.items()}  # type: ignore[assignment]
        self._dependents_closure = self._build_dependents_closure()
        self._satisfiers, self._equivalence = self._build_satisfiers()

    # -- 索引构建 ----------------------------------------------------------

    def _build_dependents_closure(self) -> dict[str, frozenset[str]]:
        reverse: dict[str, set[str]] = {n: set() for n in self.nodes}
        for edge in self.deps:
            reverse[edge.required].add(edge.target)

        closure: dict[str, frozenset[str]] = {}
        for node in self.nodes:
            seen: set[str] = set()
            stack = list(reverse[node])
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(reverse[cur])
            closure[node] = frozenset(seen)
        return closure

    def _build_satisfiers(self) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
        # “可满足”有向图：X 能顶替 Y 时 X -> Y
        satisfies: dict[str, set[str]] = {n: {n} for n in self.nodes}
        undirected: dict[str, set[str]] = {n: set() for n in self.nodes}
        for link in self.subs:
            satisfies[link.replaces].add(link.original)
            undirected[link.replaces].add(link.original)
            undirected[link.original].add(link.replaces)
            if link.bidirectional:
                satisfies[link.original].add(link.replaces)

        # 传递闭包：新版本证据可沿换版链满足旧版本要求
        closure: dict[str, set[str]] = {}
        for start in self.nodes:
            seen = {start}
            stack = [start]
            while stack:
                cur = stack.pop()
                for nxt in satisfies[cur]:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            closure[start] = seen
        satisfiers = {target: frozenset(src for src, reach in closure.items() if target in reach)
                      for target in self.nodes}

        equivalence: dict[str, frozenset[str]] = {}
        for start in self.nodes:
            seen = {start}
            stack = [start]
            while stack:
                cur = stack.pop()
                for nxt in undirected[cur]:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            equivalence[start] = frozenset(seen)
        return satisfiers, equivalence

    # -- 查询 --------------------------------------------------------------

    def requirements_of(self, node_id: str) -> tuple[str, ...]:
        """直接前置依赖。"""
        return self._dep_adj[node_id]

    def satisfiers_of(self, node_id: str) -> frozenset[str]:
        """持有其中任一节点的有效证据即可满足 ``node_id``（含自身与替代链）。"""
        return self._satisfiers[node_id]

    def equivalence_of(self, node_id: str) -> frozenset[str]:
        """通过替代关系连通的等价类。"""
        return self._equivalence[node_id]

    def dependents_of(self, node_id: str) -> frozenset[str]:
        """传递依赖该节点的所有上级节点（影响分析用）。"""
        return self._dependents_closure[node_id]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "published_at": iso(self.published_at),
            "note": self.note,
            "node_count": len(self.nodes),
            "dependency_count": len(self.deps),
            "substitution_count": len(self.subs),
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda x: x.node_id)],
            "dependencies": [e.to_dict() for e in sorted(self.deps, key=lambda x: (x.target, x.required))],
            "substitutions": [
                l.to_dict()
                for l in sorted(self.subs, key=lambda x: (x.replaces, x.original))
            ],
        }

    @staticmethod
    def from_dict(data: dict) -> "PublishedGraph":
        nodes = [QualificationNode.from_dict(n) for n in data["nodes"]]
        return PublishedGraph(
            version=int(data["version"]),
            published_at=date.fromisoformat(data["published_at"]),
            nodes={n.node_id: n for n in nodes},
            deps=[DependencyEdge.from_dict(e) for e in data["dependencies"]],
            subs=[SubstitutionLink.from_dict(s) for s in data["substitutions"]],
            note=data.get("note", ""),
        )

    def snapshot(self) -> dict:
        """供历史排班冻结使用的完整自描述快照。"""
        return {
            "version": self.version,
            "published_at": iso(self.published_at),
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda x: x.node_id)],
            "dependencies": [e.to_dict() for e in sorted(self.deps, key=lambda x: (x.target, x.required))],
            "substitutions": [
                l.to_dict()
                for l in sorted(self.subs, key=lambda x: (x.replaces, x.original))
            ],
        }


def diff_graphs(old: PublishedGraph | None, new: PublishedGraph) -> dict:
    """两个已发布版本之间的规则变化明细。"""
    old_nodes = set(old.nodes) if old else set()
    new_nodes = set(new.nodes)
    old_deps = {(e.target, e.required) for e in old.deps} if old else set()
    new_deps = {(e.target, e.required) for e in new.deps}
    old_subs = {(s.replaces, s.original, s.bidirectional, s.supersedes) for s in old.subs} if old else set()
    new_subs = {(s.replaces, s.original, s.bidirectional, s.supersedes) for s in new.subs}
    kind_changed = sorted(
        n for n in old_nodes & new_nodes if old.nodes[n].kind != new.nodes[n].kind
    )
    renamed = sorted(
        n for n in old_nodes & new_nodes if old.nodes[n].name != new.nodes[n].name
    )
    return {
        "from_version": old.version if old else None,
        "to_version": new.version,
        "nodes_added": sorted(new_nodes - old_nodes),
        "nodes_removed": sorted(old_nodes - new_nodes),
        "nodes_kind_changed": kind_changed,
        "nodes_renamed": renamed,
        "dependencies_added": sorted(new_deps - old_deps),
        "dependencies_removed": sorted(old_deps - new_deps),
        "substitutions_added": sorted(new_subs - old_subs),
        "substitutions_removed": sorted(old_subs - new_subs),
    }
