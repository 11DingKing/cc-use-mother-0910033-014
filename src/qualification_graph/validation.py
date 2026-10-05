"""资格图发布前校验：循环、悬空引用、自引用、重复候选等矛盾。

校验为纯函数，输入节点字典与已登记的证据类型代码集合，
输出 ``ValidationIssue`` 列表，便于对草稿反复检查。
"""
from __future__ import annotations

from collections import defaultdict

from .models import Qualification, ValidationIssue


def build_edges(nodes: dict[str, Qualification]) -> dict[str, list[str]]:
    """合并“前置依赖 + 替代”的有向边（qid → 其指向的资格）。"""
    adjacency: dict[str, list[str]] = defaultdict(list)
    for qid, node in nodes.items():
        seen: set[str] = set()
        for group in node.requirement_groups:
            for cand in group.candidates:
                if cand in nodes and cand not in seen:
                    adjacency[qid].append(cand)
                    seen.add(cand)
        for old in node.supersedes:
            if old in nodes and old not in seen:
                adjacency[qid].append(old)
                seen.add(old)
    return dict(adjacency)


def find_cycles(nodes: dict[str, Qualification]) -> list[list[str]]:
    """在“依赖 + 替代”合并图上找出所有简单环（去重后返回）。

    依赖与替代都隐含“需要前者才有后者”，任一方向成环都会造成语义矛盾
    （例如 A 替代 B，而 B 又通过前置链要求 A）。
    """
    adjacency = build_edges(nodes)
    cycles: list[tuple[str, ...]] = []
    signatures: set[tuple[str, ...]] = set()

    def normalize(cycle: list[str]) -> tuple[str, ...]:
        # 以最小元素为起点旋转，消除同一环的不同起点重复。
        pivot = min(range(len(cycle)), key=lambda i: cycle[i])
        return tuple(cycle[pivot:] + cycle[:pivot])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {qid: WHITE for qid in nodes}
    stack: list[str] = []

    def dfs(qid: str) -> None:
        color[qid] = GRAY
        stack.append(qid)
        for nxt in adjacency.get(qid, []):
            if color[nxt] == GRAY:
                sig = normalize(stack[stack.index(nxt):])
                if sig not in signatures:
                    signatures.add(sig)
                    cycles.append(sig)
            elif color[nxt] == WHITE:
                dfs(nxt)
        stack.pop()
        color[qid] = BLACK

    for qid in sorted(nodes):
        if color[qid] == WHITE:
            dfs(qid)
    return [list(c) for c in cycles]


def validate_graph(
    nodes: dict[str, Qualification], evidence_types: set[str] | frozenset[str]
) -> list[ValidationIssue]:
    """对草稿执行全部发布前静态校验。

    候选引用必须是“已知资格 ID”或“已登记证据类型代码”之一，否则报悬空引用。
    """
    issues: list[ValidationIssue] = []

    for qid in sorted(nodes):
        node = nodes[qid]

        if qid in node.supersedes:
            issues.append(
                ValidationIssue("self_reference", f"资格 {qid} 不能替代自身", qid)
            )

        seen_candidates: set[str] = set()
        for group in node.requirement_groups:
            if not group.candidates:
                issues.append(ValidationIssue("empty_group", f"资格 {qid} 存在空需求组", qid))
            for cand in group.candidates:
                if cand == qid:
                    issues.append(
                        ValidationIssue(
                            "self_reference", f"资格 {qid} 不能以前置自身为要求", qid
                        )
                    )
                if cand not in nodes and cand not in evidence_types:
                    issues.append(
                        ValidationIssue(
                            "dangling_reference",
                            f"资格 {qid} 引用了不存在的资格或证据类型：{cand}",
                            qid,
                            {"reference": cand},
                        )
                    )
                if cand in seen_candidates:
                    issues.append(
                        ValidationIssue(
                            "duplicate_candidate",
                            f"资格 {qid} 的需求中候选 {cand} 在多个组重复出现",
                            qid,
                            {"reference": cand},
                        )
                    )
                seen_candidates.add(cand)

        for old in node.supersedes:
            if old not in nodes:
                issues.append(
                    ValidationIssue(
                        "dangling_reference",
                        f"资格 {qid} 的替代目标不存在：{old}",
                        qid,
                        {"reference": old},
                    )
                )

    for cycle in find_cycles(nodes):
        issues.append(
            ValidationIssue(
                "cycle",
                "资格依赖/替代存在循环：" + " → ".join([*cycle, cycle[0]]),
                cycle[0],
                {"cycle": cycle},
            )
        )
    return issues
