"""资格图后端回归测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_graph.models import GraphError, Qualification, RequirementGroup
from qualification_graph.service import QualificationService
from qualification_graph.storage import Storage
from qualification_graph.validation import find_cycles, validate_graph


def q(qid: str, groups=(), supersedes=(), kind: str = "topic", name: str | None = None) -> Qualification:
    return Qualification(
        qid=qid,
        name=name or qid,
        kind=kind,  # type: ignore[arg-type]
        requirement_groups=tuple(RequirementGroup(tuple(g)) for g in groups),
        supersedes=tuple(supersedes),
    )


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = QualificationService(Storage(":memory:"))

    def build_museum_graph(self) -> int:
        """构造典型场馆资格图并发布。

        证据类型：cert_basic（基础讲解证）、cert_advanced（高级讲解证）
        hall_a:        需 cert_basic
        level_junior:  需 hall_a
        topic_dino_v1: 需 cert_basic（旧版主题，门槛低）
        topic_dino_v2: 需 cert_advanced，但替代 topic_dino_v1
                       —— 持旧版基础证的讲解员经替代链仍可讲新版
        """
        v = self.service.create_draft()
        self.service.register_evidence_type(v, "cert_basic", "基础讲解证")
        self.service.register_evidence_type(v, "cert_advanced", "高级讲解证")
        self.service.upsert_node(v, q("hall_a", [("cert_basic",)], kind="hall", name="A展厅"))
        self.service.upsert_node(
            v, q("level_junior", [("hall_a",)], kind="audience_level", name="初级受众")
        )
        self.service.upsert_node(v, q("topic_dino_v1", [("cert_basic",)], name="恐龙主题旧版"))
        self.service.upsert_node(
            v,
            q(
                "topic_dino_v2",
                [("cert_advanced",)],
                supersedes=("topic_dino_v1",),
                name="恐龙主题新版",
            ),
        )
        published = self.service.publish(v, date(2026, 1, 10), "首版")
        self.assertEqual(published["node_count"], 4)
        return v


class ValidationTest(ServiceTest):
    def test_dependency_cycle_detected(self) -> None:
        nodes = {
            "a": q("a", [("b",)]),
            "b": q("b", [("c",)]),
            "c": q("c", [("a",)]),
        }
        self.assertEqual(find_cycles(nodes), [["a", "b", "c"]])
        self.assertTrue(any(i.code == "cycle" for i in validate_graph(nodes, frozenset())))

    def test_mixed_supersession_cycle_detected(self) -> None:
        # a 替代 b，但 b 的前置链又要求 a —— 混合环
        nodes = {"a": q("a", supersedes=("b",)), "b": q("b", [("a",)])}
        self.assertTrue(any(i.code == "cycle" for i in validate_graph(nodes, frozenset())))

    def test_dangling_and_self_reference(self) -> None:
        nodes = {"a": q("a", [("ghost",)]), "b": q("b", supersedes=("b",))}
        codes = {i.code for i in validate_graph(nodes, frozenset())}
        self.assertIn("dangling_reference", codes)
        self.assertIn("self_reference", codes)
        # 登记证据类型后悬空消失，仅余自引用（同时构成自环）
        remaining = {i.code for i in validate_graph(nodes, frozenset({"ghost"}))}
        self.assertEqual(remaining, {"self_reference", "cycle"})

    def test_duplicate_candidate_and_empty_group(self) -> None:
        nodes = {
            "a": q("a", [("b",), ("b",)]),
            "b": q("b"),
            "c": Qualification(qid="c", name="c", kind="topic",
                               requirement_groups=(RequirementGroup(()),)),
        }
        codes = {i.code for i in validate_graph(nodes, frozenset())}
        self.assertIn("duplicate_candidate", codes)
        self.assertIn("empty_group", codes)

    def test_acyclic_graph_validates_clean(self) -> None:
        self.build_museum_graph()
        draft = self.service.create_draft()
        self.assertEqual(self.service.validate_draft(draft), [])


class PublishTest(ServiceTest):
    def test_publish_blocked_by_cycle_and_published_immutable(self) -> None:
        v = self.service.create_draft()
        self.service.register_evidence_type(v, "cert", "证")
        self.service.upsert_node(v, q("a", [("b",)]))
        self.service.upsert_node(v, q("b", [("a",)]))
        with self.assertRaises(GraphError):
            self.service.publish(v)
        self.service.upsert_node(v, q("b", [("cert",)]))
        self.service.publish(v, date(2026, 1, 1))
        with self.assertRaises(GraphError):
            self.service.upsert_node(v, q("a"))

    def test_new_draft_clones_published_version(self) -> None:
        v1 = self.build_museum_graph()
        v2 = self.service.create_draft()
        graph = self.service.get_graph(v2)
        self.assertEqual(set(graph["nodes"]), {"hall_a", "level_junior", "topic_dino_v1", "topic_dino_v2"})
        self.assertEqual(graph["evidence_types"], {"cert_basic": "基础讲解证", "cert_advanced": "高级讲解证"})
        self.assertEqual(self.service.storage.version_status(v1), "published")


class SatisfactionTest(ServiceTest):
    def test_evidence_basis_with_expiry(self) -> None:
        self.build_museum_graph()
        self.service.grant_evidence(
            "g1", "cert_basic", "基础证", date(2025, 1, 1), date(2026, 12, 31)
        )
        ok = self.service.explain("g1", "hall_a", date(2026, 6, 1))
        self.assertTrue(ok.satisfied)
        self.assertEqual(ok.basis, "direct")
        self.assertEqual(ok.leaves()[0].basis, "evidence")

        expired = self.service.explain("g1", "hall_a", date(2027, 1, 1))
        self.assertFalse(expired.satisfied)
        self.assertIn("过期", expired.leaves()[0].detail)

    def test_revoked_evidence_stops_satisfying_but_keeps_history(self) -> None:
        self.build_museum_graph()
        eid = self.service.grant_evidence("g1", "cert_basic", "基础证", date(2025, 1, 1))
        schedule = self.service.create_schedule("g1", "hall_a", date(2026, 3, 1))
        self.service.revoke_evidence(eid, date(2026, 2, 1), "考核不合格")

        frozen = self.service.get_schedule(schedule["schedule_id"])
        self.assertTrue(frozen["frozen_explanation"]["satisfied"])
        self.assertEqual(frozen["frozen_bases"][0]["basis"], "evidence")
        self.assertEqual(frozen["graph_version"], 1)

        now = self.service.explain("g1", "hall_a", date(2026, 3, 1))
        self.assertFalse(now.satisfied)
        self.assertIn("已撤销", now.leaves()[0].detail)

        review = self.service.reevaluate_schedule(schedule["schedule_id"])
        self.assertFalse(review["satisfied_now_on_current_rules"])
        self.assertTrue(review["frozen_explanation_unchanged"]["satisfied"])

    def test_supersession_path_and_legacy_certificate(self) -> None:
        self.build_museum_graph()
        self.service.grant_evidence("g2", "cert_basic", "基础证", date(2024, 1, 1))
        # 直接要求 cert_advanced 不满足，但替代链 topic_dino_v1 → topic_dino_v2 满足
        result = self.service.explain("g2", "topic_dino_v2", date(2026, 5, 1))
        self.assertTrue(result.satisfied)
        self.assertEqual(result.basis, "supersession")
        self.assertEqual(result.children[0].path, ("topic_dino_v1", "topic_dino_v2"))
        leaf = result.leaves()[0]
        self.assertEqual(leaf.basis, "evidence")
        self.assertEqual(leaf.qid, "cert_basic")

    def test_multilevel_supersession_chain(self) -> None:
        v = self.service.create_draft()
        self.service.register_evidence_type(v, "c", "证")
        self.service.upsert_node(v, q("v1", [("c",)]))
        self.service.upsert_node(v, q("v2", supersedes=("v1",)))
        self.service.upsert_node(v, q("v3", supersedes=("v2",)))
        self.service.publish(v)
        self.service.grant_evidence("g", "c", "证", date(2026, 1, 1))
        r = self.service.explain("g", "v3", date(2026, 6, 1))
        self.assertTrue(r.satisfied)
        self.assertEqual(r.children[0].path, ("v1", "v2", "v3"))

    def test_exemption_window_and_no_history_rewrite(self) -> None:
        self.build_museum_graph()
        xid = self.service.add_exemption(
            "g3", "hall_a", date(2026, 6, 1), date(2026, 6, 30), "新入职特批"
        )
        inside = self.service.explain("g3", "hall_a", date(2026, 6, 15))
        self.assertTrue(inside.satisfied)
        self.assertEqual(inside.basis, "exemption")
        self.assertFalse(self.service.explain("g3", "hall_a", date(2026, 7, 1)).satisfied)

        schedule = self.service.create_schedule("g3", "hall_a", date(2026, 6, 20))
        self.service.revoke_exemption(xid)
        frozen = self.service.get_schedule(schedule["schedule_id"])
        self.assertEqual(frozen["frozen_bases"][0]["basis"], "exemption")
        self.assertFalse(self.service.explain("g3", "hall_a", date(2026, 6, 20)).satisfied)

    def test_and_of_or_groups(self) -> None:
        v = self.service.create_draft()
        self.service.register_evidence_type(v, "c1", "证1")
        self.service.register_evidence_type(v, "c2", "证2")
        # 两个 AND 组，每组一个候选
        self.service.upsert_node(v, q("a", [("c1",), ("c2",)]))
        self.service.publish(v)
        self.service.grant_evidence("g", "c1", "证1", date(2026, 1, 1))
        self.assertFalse(self.service.explain("g", "a", date(2026, 6, 1)).satisfied)
        self.service.grant_evidence("g", "c2", "证2", date(2026, 1, 1))
        self.assertTrue(self.service.explain("g", "a", date(2026, 6, 1)).satisfied)

    def test_or_group_alternatives(self) -> None:
        v = self.service.create_draft()
        self.service.register_evidence_type(v, "c1", "证1")
        self.service.register_evidence_type(v, "c2", "证2")
        # 一个 OR 组：任一证据即可
        self.service.upsert_node(v, q("a", [("c1", "c2")]))
        self.service.publish(v)
        self.service.grant_evidence("g", "c2", "证2", date(2026, 1, 1))
        result = self.service.explain("g", "a", date(2026, 6, 1))
        self.assertTrue(result.satisfied)
        self.assertEqual(result.children[0].children[0].qid, "c2")

    def test_schedule_rejects_unqualified(self) -> None:
        self.build_museum_graph()
        with self.assertRaises(GraphError) as ctx:
            self.service.create_schedule("g9", "topic_dino_v2", date(2026, 6, 1))
        self.assertIn("不满足资格", str(ctx.exception))


class GapTest(ServiceTest):
    def test_gap_classification(self) -> None:
        self.build_museum_graph()
        eid = self.service.grant_evidence(
            "g1", "cert_basic", "基础证", date(2025, 1, 1), date(2025, 12, 31)
        )
        gaps = self.service.qualification_gaps("g1", "topic_dino_v2", date(2026, 6, 1))
        self.assertFalse(gaps["satisfied"])
        by_code = {g["code"]: g for g in gaps["missing_evidence_types"]}
        self.assertEqual(by_code["cert_basic"]["status"], "expired")
        self.assertEqual(by_code["cert_basic"]["valid_until"], "2025-12-31")
        self.assertEqual(by_code["cert_advanced"]["status"], "not_held")

        self.service.revoke_evidence(eid, date(2026, 1, 1), "撤销")
        gaps2 = self.service.qualification_gaps("g1", "topic_dino_v2", date(2026, 6, 1))
        by_code2 = {g["code"]: g for g in gaps2["missing_evidence_types"]}
        self.assertEqual(by_code2["cert_basic"]["status"], "revoked")

        gaps3 = self.service.qualification_gaps("g_empty", "hall_a", date(2026, 6, 1))
        self.assertEqual(gaps3["missing_evidence_types"][0]["status"], "not_held")


class RuleChangeImpactTest(ServiceTest):
    def test_supersession_version_change_impact_and_frozen_schedules(self) -> None:
        v1 = self.build_museum_graph()
        self.service.grant_evidence("g2", "cert_basic", "基础证", date(2024, 1, 1))
        # g2 靠“旧版资格 + 替代关系”排上 topic_dino_v2
        schedule = self.service.create_schedule("g2", "topic_dino_v2", date(2026, 5, 10))
        self.assertEqual(schedule["graph_version"], v1)

        # 换版：topic_dino_v2 取消对 v1 的替代，改为要求高级受众等级
        v2 = self.service.create_draft()
        self.service.upsert_node(
            v2, q("level_senior", [("cert_advanced",)], kind="audience_level", name="高级受众")
        )
        self.service.upsert_node(
            v2, q("topic_dino_v2", [("level_senior",)], name="恐龙主题新版")
        )
        self.service.publish(v2, date(2026, 4, 1), "替代关系换版：取消旧版替代")

        impact = self.service.rule_change_impact(v1, v2, date(2026, 5, 10))
        self.assertIn("level_senior", impact["nodes_added"])
        self.assertIn("topic_dino_v2", {c["qid"] for c in impact["nodes_changed"]})

        flip = next(
            f for f in impact["guide_flips"]
            if f["guide_id"] == "g2" and f["qid"] == "topic_dino_v2"
        )
        self.assertEqual(flip["change"], "lost")

        hit = next(s for s in impact["schedules_using_old_version"]
                   if s["schedule_id"] == schedule["schedule_id"])
        self.assertFalse(hit["satisfied_under_new_rules_on_scheduled_day"])
        self.assertFalse(hit["history_rewritten"])

        # 历史排班依旧冻结在 v1 且依旧满足
        frozen = self.service.get_schedule(schedule["schedule_id"])
        self.assertEqual(frozen["graph_version"], v1)
        self.assertTrue(frozen["frozen_explanation"]["satisfied"])
        self.assertEqual(frozen["frozen_explanation"]["basis"], "supersession")

        # 复评指出当前规则下已不满足，但不回写
        review = self.service.reevaluate_schedule(schedule["schedule_id"])
        self.assertEqual(review["frozen_version"], v1)
        self.assertEqual(review["current_version"], v2)
        self.assertFalse(review["satisfied_now_on_current_rules"])

    def test_removed_node_appears_in_diff(self) -> None:
        v1 = self.build_museum_graph()
        v2 = self.service.create_draft()
        self.service.delete_node(v2, "topic_dino_v1")
        self.service.upsert_node(
            v2, q("topic_dino_v2", [("cert_advanced",)], name="恐龙主题新版")
        )
        self.service.publish(v2)
        impact = self.service.rule_change_impact(v1, v2)
        self.assertIn("topic_dino_v1", impact["nodes_removed"])
        self.assertTrue(any(c["qid"] == "topic_dino_v2" for c in impact["nodes_changed"]))


if __name__ == "__main__":
    unittest.main()
