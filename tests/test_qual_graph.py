"""资格图后端的综合回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qual_graph.api import build_server
from qual_graph.errors import ConflictError, PublishBlockedError, ValidationError
from qual_graph.service import Service
from qual_graph.storage import Store


def make_service() -> Service:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    Path(tmp.name).unlink(missing_ok=True)
    return Service(Store(tmp.name))


def build_base_graph(service: Service) -> None:
    service.get_or_create_draft()
    for nid, kind, name in [
        ("H", "hall", "主展厅"),
        ("B", "audience_level", "基础"),
        ("A", "audience_level", "进阶"),
        ("T1", "topic", "主题一"),
        ("T2", "topic", "主题二"),
    ]:
        service.upsert_node({"node_id": nid, "kind": kind, "name": name})
    for target, required in [("T1", "H"), ("T1", "B"), ("T2", "T1"), ("T2", "A")]:
        service.add_dependency({"target": target, "required": required})
    service.publish(note="base")


def give(service, eid, person, node, **kw) -> None:
    payload = {"evidence_id": eid, "person_id": person, "node_id": node,
               "evidence_type": "certificate", "valid_from": "2025-01-01",
               "valid_until": None}
    payload.update(kw)
    service.add_evidence(payload)


class GraphValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_dependency_cycle_blocks_publish(self) -> None:
        build_base_graph(self.svc)
        self.svc.get_or_create_draft()
        self.svc.add_dependency({"target": "H", "required": "T1"})  # T1->H->T1 成环
        report = self.svc.validate_draft()
        self.assertTrue(report["blocked"])
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("dependency_cycle", codes)
        with self.assertRaises(PublishBlockedError):
            self.svc.publish()

    def test_self_dependency_rejected(self) -> None:
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "X", "kind": "topic", "name": "X"})
        with self.assertRaises(ValidationError):
            self.svc.add_dependency({"target": "X", "required": "X"})

    def test_dependency_substitution_contradiction(self) -> None:
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "T1", "kind": "topic", "name": "主题一"})
        self.svc.upsert_node({"node_id": "T2", "kind": "topic", "name": "主题二"})
        self.svc.add_dependency({"target": "T1", "required": "T2"})
        # T1 与 T2 互为替代却又有前置依赖 => 矛盾
        self.svc.add_substitution({"replaces": "T2", "original": "T1", "bidirectional": True})
        report = self.svc.validate_draft()
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("dependency_substitution_conflict", codes)
        self.assertTrue(report["blocked"])

    def test_dangling_reference_blocks(self) -> None:
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "T1", "kind": "topic", "name": "主题一"})
        self.svc.add_dependency({"target": "T1", "required": "GHOST"})
        report = self.svc.validate_draft()
        self.assertTrue(any(i["code"] == "dangling_dependency" for i in report["issues"]))

    def test_supersede_kind_mismatch_blocks(self) -> None:
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "T1", "kind": "topic", "name": "主题一"})
        self.svc.upsert_node({"node_id": "H2", "kind": "hall", "name": "新展厅"})
        self.svc.add_substitution({"replaces": "H2", "original": "T1", "supersedes": True})
        report = self.svc.validate_draft()
        self.assertTrue(any(i["code"] == "supersede_kind_mismatch" for i in report["issues"]))

    def test_directed_substitution_cycle_blocks(self) -> None:
        self.svc.get_or_create_draft()
        for nid in ("T1", "T2", "T3"):
            self.svc.upsert_node({"node_id": nid, "kind": "topic", "name": nid})
        self.svc.add_substitution({"replaces": "T2", "original": "T1"})
        self.svc.add_substitution({"replaces": "T3", "original": "T2"})
        self.svc.add_substitution({"replaces": "T1", "original": "T3"})
        report = self.svc.validate_draft()
        self.assertTrue(any(i["code"] == "substitution_cycle" for i in report["issues"]))

    def test_clean_graph_publishes(self) -> None:
        build_base_graph(self.svc)
        self.assertEqual(self.svc.list_versions()[0]["version"], 1)


class EvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        build_base_graph(self.svc)
        give(self.svc, "E1", "P1", "H", valid_from="2025-01-01", valid_until="2027-01-01")
        give(self.svc, "E2", "P1", "B")
        give(self.svc, "E3", "P1", "T1", valid_until="2027-01-01")

    def test_transitive_requirements_path(self) -> None:
        result = self.svc.check_qualification("P1", ["T1"], "2026-10-05")
        self.assertTrue(result["satisfied"])
        ids = {s["requirement_id"] for s in result["steps"]}
        self.assertEqual(ids, {"H", "B", "T1"})

    def test_missing_dependency_reported(self) -> None:
        # P1 持有 T2 自身资格，但缺进阶 A 前置，不能讲 T2
        give(self.svc, "E3B", "P1", "T2")
        result = self.svc.check_qualification("P1", ["T2"], "2026-10-05")
        self.assertFalse(result["satisfied"])
        self.assertEqual([m["node_id"] for m in result["missing"]], ["A"])

    def test_expired_certificate_cannot_satisfy_advanced_topic(self) -> None:
        give(self.svc, "E4", "P2", "H")
        give(self.svc, "E5", "P2", "B")
        give(self.svc, "E6", "P2", "T1", valid_from="2024-01-01", valid_until="2026-09-30")
        result = self.svc.check_qualification("P2", ["T1"], "2026-10-05")
        self.assertFalse(result["satisfied"])
        self.assertEqual([m["node_id"] for m in result["missing"]], ["T1"])
        # 有效期内的当天仍有效
        result_edge = self.svc.check_qualification("P2", ["T1"], "2026-09-30")
        self.assertTrue(result_edge["satisfied"])

    def test_revoked_evidence_fails(self) -> None:
        self.svc.revoke_evidence("E1", "复核未过")
        result = self.svc.check_qualification("P1", ["T1"], "2026-10-05")
        self.assertFalse(result["satisfied"])
        self.assertIn("H", [m["node_id"] for m in result["missing"]])

    def test_future_evidence_not_yet_valid(self) -> None:
        give(self.svc, "E9", "P3", "H", valid_from="2026-12-01", valid_until=None)
        result = self.svc.check_qualification("P3", ["H"], "2026-10-05")
        self.assertFalse(result["satisfied"])

    def test_supersede_v2_certificate_satisfies_v1_requirement(self) -> None:
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "T1V2", "kind": "topic", "name": "主题一第二版"})
        self.svc.add_substitution({"replaces": "T1V2", "original": "T1", "supersedes": True})
        self.svc.publish(note="v2")
        give(self.svc, "V2C", "P4", "T1V2")
        give(self.svc, "V2H", "P4", "H")
        give(self.svc, "V2B", "P4", "B")
        result = self.svc.check_qualification("P4", ["T1"], "2026-10-05")
        self.assertTrue(result["satisfied"])
        step = next(s for s in result["steps"] if s["requirement_id"] == "T1")
        self.assertEqual(step["satisfied_by_node"], "T1V2")
        self.assertEqual(step["substitution_chain"], ["T1V2", "T1"])
        self.assertFalse(step["direct"])

    def test_bidirectional_substitution(self) -> None:
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "B2", "kind": "audience_level", "name": "基础互认版"})
        self.svc.add_substitution({"replaces": "B2", "original": "B", "bidirectional": True})
        self.svc.publish(note="equiv")
        give(self.svc, "B2C", "P5", "B2")
        give(self.svc, "B2H", "P5", "H")
        give(self.svc, "B2T", "P5", "T1")
        result = self.svc.check_qualification("P5", ["T1"], "2026-10-05")
        self.assertTrue(result["satisfied"])

    def test_waiver_satisfies_node_but_not_prerequisites(self) -> None:
        # 豁免 T1 本身不能免除 H、B 前置
        give(self.svc, "WH", "P6", "H")
        # 只给豁免不给 B
        self.svc.add_waiver({"person_id": "P6", "node_id": "T1",
                             "valid_from": "2026-10-01", "valid_until": "2026-10-31"})
        result = self.svc.check_qualification("P6", ["T1"], "2026-10-05")
        self.assertFalse(result["satisfied"])
        self.assertEqual([m["node_id"] for m in result["missing"]], ["B"])
        # 补上 B 后，豁免直接满足 T1
        give(self.svc, "WB", "P6", "B")
        result2 = self.svc.check_qualification("P6", ["T1"], "2026-10-05")
        self.assertTrue(result2["satisfied"])
        t1_step = next(s for s in result2["steps"] if s["requirement_id"] == "T1")
        self.assertEqual(t1_step["evidence_type"], "waiver")

    def test_expired_waiver_inactive(self) -> None:
        self.svc.add_waiver({"waiver_id": "W-OLD", "person_id": "P7", "node_id": "H",
                             "valid_from": "2026-01-01", "valid_until": "2026-02-01"})
        result = self.svc.check_qualification("P7", ["H"], "2026-10-05")
        self.assertFalse(result["satisfied"])


class ScheduleFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        build_base_graph(self.svc)
        for nid, node in [("EH", "H"), ("EB", "B"), ("EA", "A"), ("ET1", "T1")]:
            give(self.svc, nid, "P1", node)

    def test_cannot_schedule_unqualified(self) -> None:
        give(self.svc, "OH", "P2", "H")
        with self.assertRaises(ConflictError):
            self.svc.create_schedule({"schedule_id": "S-BAD", "person_id": "P2",
                                      "required_nodes": ["T1"], "event_date": "2026-10-20"})

    def test_freeze_survives_revocation_version_and_waiver_change(self) -> None:
        self.svc.add_waiver({"waiver_id": "W1", "person_id": "P1", "node_id": "T2",
                             "reason": "特批", "valid_from": "2026-10-01",
                             "valid_until": "2026-10-31"})
        sid = "S-1"
        self.svc.create_schedule({"schedule_id": sid, "person_id": "P1",
                                  "required_nodes": ["T2"], "event_date": "2026-10-20"})
        self.svc.transition_schedule(sid, "待确认")
        self.svc.transition_schedule(sid, "已排定")
        before = self.svc.get_schedule(sid)
        self.assertEqual(before["state"], "已排定")
        self.assertTrue(before["frozen"])
        self.assertEqual(before["created_graph_version"], 1)
        self.assertIsNotNone(before["frozen_graph_snapshot"])
        self.assertTrue(before["frozen_evidence_snapshots"])

        # 1) 撤销证据
        self.svc.revoke_evidence("ET1", "事后撤销")
        # 2) 撤销豁免
        self.svc.revoke_waiver("W1", "特批收回")
        # 3) 发布新版规则：T2 增加新前置，旧节点 T1 被换版
        self.svc.get_or_create_draft()
        self.svc.upsert_node({"node_id": "T1V2", "kind": "topic", "name": "主题一第二版"})
        self.svc.add_substitution({"replaces": "T1V2", "original": "T1", "supersedes": True})
        self.svc.upsert_node({"node_id": "X", "kind": "topic", "name": "新增主题"})
        self.svc.add_dependency({"target": "T2", "required": "X"})
        self.svc.publish(note="v2")

        after = self.svc.get_schedule(sid)
        self.assertEqual(after["state"], "已排定")
        self.assertEqual(after["created_graph_version"], 1)
        self.assertEqual(after["frozen_graph_snapshot"]["version"], 1)
        used = {s["evidence_id"] for s in after["satisfied_path"]}
        self.assertIn("ET1", used)
        self.assertIn("W1", used)

    def test_illegal_transition_rejected(self) -> None:
        self.svc.create_schedule({"schedule_id": "S-2", "person_id": "P1",
                                  "required_nodes": ["T1"], "event_date": "2026-10-20"})
        # 筹备 不能直接到 执行中
        with self.assertRaises(ConflictError):
            self.svc.transition_schedule("S-2", "执行中")
        # 已结算 是终态
        self.svc.transition_schedule("S-2", "待确认")
        self.svc.transition_schedule("S-2", "已排定")
        self.svc.transition_schedule("S-2", "执行中")
        self.svc.transition_schedule("S-2", "已结算")
        with self.assertRaises(ConflictError):
            self.svc.transition_schedule("S-2", "已排定")


class ImpactAnalysisTest(unittest.TestCase):
    def test_changes_and_impact_report(self) -> None:
        svc = make_service()
        build_base_graph(svc)
        for nid, node in [("EH", "H"), ("EB", "B"), ("EA", "A"), ("ET1", "T1"), ("ET2", "T2")]:
            give(svc, nid, "P1", node)
        svc.create_schedule({"schedule_id": "S-9", "person_id": "P1",
                             "required_nodes": ["T2"], "event_date": "2026-10-20"})
        svc.transition_schedule("S-9", "待确认")
        svc.transition_schedule("S-9", "已排定")

        svc.get_or_create_draft()
        svc.upsert_node({"node_id": "X", "kind": "topic", "name": "新增主题"})
        svc.add_dependency({"target": "T2", "required": "X"})
        svc.remove_node("B")  # 连带删除 B 相关依赖
        svc.publish(note="v2")

        changes = svc.rule_changes()
        self.assertEqual(changes["to_version"], 2)
        self.assertIn("X", changes["nodes_added"])
        self.assertIn("B", changes["nodes_removed"])
        self.assertIn(("T2", "X"), changes["dependencies_added"])

        impact = svc.impact_analysis()
        self.assertEqual(impact["tightened_nodes"], ["T2"])
        person = next(p for p in impact["affected_people"] if p["person_id"] == "P1")
        self.assertIn("X", person["new_gaps"])
        self.assertIn("S-9", impact["frozen_schedules_unchanged"])
        orphan = {o["node_id"] for o in impact["orphan_evidence"]}
        self.assertIn("B", orphan)


class PersistenceTest(unittest.TestCase):
    def test_reload_from_disk(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        path = tmp.name
        svc = Service(Store(path))
        build_base_graph(svc)
        give(svc, "E1", "P1", "H")

        reloaded = Service(Store(path))
        graph = reloaded.get_graph()
        self.assertEqual(graph.version, 1)
        self.assertIn("H", graph.nodes)
        self.assertEqual(reloaded.list_evidences()[0]["evidence_id"], "E1")


class ApiSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        self.db = tmp.name
        self.server = build_server(self.db, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method: str, path: str, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.call("POST", "/graph/draft")
        self.assertEqual(self.call("PUT", "/graph/draft/nodes",
                                   {"node_id": "H", "kind": "hall", "name": "主展厅"})[0], 200)
        self.assertEqual(self.call("PUT", "/graph/draft/nodes",
                                   {"node_id": "T1", "kind": "topic", "name": "主题一"})[0], 200)
        self.assertEqual(self.call("POST", "/graph/draft/dependencies",
                                   {"target": "T1", "required": "H"})[0], 201)
        status, body = self.call("POST", "/graph/publish", {"note": "v1"})
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 1)

        # 循环依赖阻断发布
        self.call("POST", "/graph/draft")
        self.call("POST", "/graph/draft/dependencies", {"target": "H", "required": "T1"})
        status, body = self.call("GET", "/graph/draft/validate")
        self.assertTrue(body["blocked"])
        status, body = self.call("POST", "/graph/publish")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "publish_blocked")
        # 清理后版本仍为 1
        self.assertEqual(self.call("GET", "/graph/versions")[1]["versions"][0]["version"], 1)

        # 证据与资格检查
        self.call("POST", "/evidences", {"evidence_id": "E1", "person_id": "P1",
                                         "node_id": "H", "valid_from": "2025-01-01"})
        status, body = self.call("POST", "/qualification/check",
                                 {"person_id": "P1", "targets": ["T1"], "on_date": "2026-10-05"})
        self.assertEqual(status, 200)
        self.assertFalse(body["satisfied"])
        self.assertEqual([m["node_id"] for m in body["missing"]], ["T1"])

        # 未知接口 404
        self.assertEqual(self.call("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
