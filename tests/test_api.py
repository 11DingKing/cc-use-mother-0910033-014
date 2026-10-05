"""HTTP API 端到端测试（标准库 http.client，无需外部依赖）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_graph.api import create_app


class ApiClient:
    def __init__(self, server) -> None:
        self.host, self.port = server.server_address[:2]

    def request(self, method: str, path: str, payload: dict | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else None
        conn.close()
        return resp.status, data


class ApiFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.service = create_app(":memory:")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.service.storage.close()

    def test_full_lifecycle(self) -> None:
        api = self.api

        # 1. 建草稿、登记证据类型、配置节点
        status, data = api.request("POST", "/graphs/drafts", {})
        self.assertEqual(status, 201)
        v = data["version"]
        self.assertEqual(status, 201)
        self.assertEqual(api.request("PUT", f"/graphs/{v}/evidence-types",
                                     {"code": "cert_basic", "label": "基础讲解证"})[0], 200)
        self.assertEqual(api.request("PUT", f"/graphs/{v}/nodes",
                                     {"node": {"qid": "hall_a", "kind": "hall", "name": "A展厅",
                                               "requirement_groups": [{"candidates": ["cert_basic"]}]}})[0], 200)
        self.assertEqual(api.request("PUT", f"/graphs/{v}/nodes",
                                     {"node": {"qid": "topic_v1", "kind": "topic",
                                               "requirement_groups": [{"candidates": ["cert_basic"]}]}})[0], 200)
        self.assertEqual(api.request("PUT", f"/graphs/{v}/nodes",
                                     {"node": {"qid": "topic_v2", "kind": "topic",
                                               "requirement_groups": [{"candidates": ["cert_basic"]}],
                                               "supersedes": ["topic_v1"]}})[0], 200)

        # 2. 校验通过并发布
        status, data = api.request("GET", f"/graphs/{v}/validate")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, published = api.request("POST", f"/graphs/{v}/publish", {"day": "2026-01-10"})
        self.assertEqual(status, 200)
        self.assertEqual(published["node_count"], 3)

        # 3. 发布后不可改
        status, err = api.request("PUT", f"/graphs/{v}/nodes",
                                  {"node": {"qid": "hall_a", "kind": "hall"}})
        self.assertEqual(status, 400)
        self.assertIn("已发布", err["error"])

        # 4. 发证 → 满足判定走替代/直接路径
        status, data = api.request("POST", "/evidences", {
            "guide_id": "g1", "evidence_type": "cert_basic", "title": "基础证",
            "issued_on": "2025-06-01", "valid_until": "2026-12-31",
        })
        self.assertEqual(status, 201)
        eid = data["evidence_id"]

        status, sat = api.request("GET", "/satisfaction?guide_id=g1&qid=topic_v2&day=2026-05-01")
        self.assertEqual(status, 200)
        self.assertTrue(sat["satisfied"])
        self.assertIn(sat["basis"], ("direct", "supersession"))

        # 5. 排班并冻结
        status, schedule = api.request("POST", "/schedules", {
            "guide_id": "g1", "qid": "topic_v2", "scheduled_on": "2026-05-20",
        })
        self.assertEqual(status, 201)
        sid = schedule["schedule_id"]
        self.assertEqual(schedule["graph_version"], v)

        # 6. 撤销证据：排班冻结不变，当前判定转缺口
        self.assertEqual(api.request("POST", f"/evidences/{eid}/revoke",
                                     {"day": "2026-03-01", "reason": "过期未复审"})[0], 200)
        status, frozen = api.request("GET", f"/schedules/{sid}")
        self.assertTrue(frozen["frozen_explanation"]["satisfied"])
        status, gaps = api.request("GET", "/gaps?guide_id=g1&qid=topic_v2&day=2026-05-20")
        self.assertFalse(gaps["satisfied"])
        self.assertEqual(gaps["missing_evidence_types"][0]["status"], "revoked")

        status, review = api.request("GET", f"/schedules/{sid}/reevaluate")
        self.assertEqual(status, 200)
        self.assertFalse(review["satisfied_now_on_current_rules"])
        self.assertTrue(review["frozen_explanation_unchanged"]["satisfied"])

        # 7. 换版（取消替代）并查看影响
        # g2 持未撤销的基础证：旧规则经替代满足，新规则丢失资格 → 应出现 lost 翻转
        self.assertEqual(api.request("POST", "/evidences", {
            "guide_id": "g2", "evidence_type": "cert_basic", "title": "基础证",
            "issued_on": "2025-01-01",
        })[0], 201)
        status, data = api.request("POST", "/graphs/drafts", {})
        v2 = data["version"]
        self.assertEqual(api.request("PUT", f"/graphs/{v2}/evidence-types",
                                     {"code": "cert_adv", "label": "高级证"})[0], 200)
        self.assertEqual(api.request("PUT", f"/graphs/{v2}/nodes",
                                     {"node": {"qid": "topic_v2", "kind": "topic",
                                               "requirement_groups": [{"candidates": ["cert_adv"]}]}})[0], 200)
        self.assertEqual(api.request("POST", f"/graphs/{v2}/publish", {"day": "2026-04-01"})[0], 200)

        status, impact = api.request("GET", f"/impact?old={v}&new={v2}&day=2026-05-20")
        self.assertEqual(status, 200)
        self.assertIn("cert_adv", impact["evidence_types_added"])
        self.assertTrue(any(
            f["guide_id"] == "g2" and f["qid"] == "topic_v2" and f["change"] == "lost"
            for f in impact["guide_flips"]
        ))
        hit = next(s for s in impact["schedules_using_old_version"] if s["schedule_id"] == sid)
        self.assertFalse(hit["history_rewritten"])

    def test_cycle_blocks_publish_over_api(self) -> None:
        api = self.api
        _, data = api.request("POST", "/graphs/drafts", {})
        v = data["version"]
        api.request("PUT", f"/graphs/{v}/evidence-types", {"code": "c", "label": "证"})
        api.request("PUT", f"/graphs/{v}/nodes",
                    {"node": {"qid": "a", "kind": "topic",
                              "requirement_groups": [{"candidates": ["b"]}]}})
        api.request("PUT", f"/graphs/{v}/nodes",
                    {"node": {"qid": "b", "kind": "topic",
                              "requirement_groups": [{"candidates": ["a"]}]}})
        status, data = api.request("GET", f"/graphs/{v}/validate")
        self.assertFalse(data["ok"])
        self.assertTrue(any(i["code"] == "cycle" for i in data["issues"]))
        status, err = api.request("POST", f"/graphs/{v}/publish", {})
        self.assertEqual(status, 400)
        self.assertIn("循环", err["error"])

    def test_exemption_flow_and_bad_requests(self) -> None:
        api = self.api
        _, data = api.request("POST", "/graphs/drafts", {})
        v = data["version"]
        api.request("PUT", f"/graphs/{v}/evidence-types", {"code": "c", "label": "证"})
        api.request("PUT", f"/graphs/{v}/nodes",
                    {"node": {"qid": "a", "kind": "topic",
                              "requirement_groups": [{"candidates": ["c"]}]}})
        api.request("POST", f"/graphs/{v}/publish", {})

        status, data = api.request("POST", "/exemptions", {
            "guide_id": "g9", "qid": "a", "start_on": "2026-06-01",
            "end_on": "2026-06-10", "reason": "临时顶岗",
        })
        self.assertEqual(status, 201)
        xid = data["exemption_id"]
        status, sat = api.request("GET", "/satisfaction?guide_id=g9&qid=a&day=2026-06-05")
        self.assertTrue(sat["satisfied"])
        self.assertEqual(sat["basis"], "exemption")
        # 窗口外
        status, sat = api.request("GET", "/satisfaction?guide_id=g9&qid=a&day=2026-06-11")
        self.assertFalse(sat["satisfied"])
        # 撤销豁免
        self.assertEqual(api.request("POST", f"/exemptions/{xid}/revoke", {})[0], 200)
        status, sat = api.request("GET", "/satisfaction?guide_id=g9&qid=a&day=2026-06-05")
        self.assertFalse(sat["satisfied"])

        # 错误处理：缺字段、坏日期、未知路径
        status, err = api.request("POST", "/evidences", {"guide_id": "x"})
        self.assertEqual(status, 400)
        status, err = api.request("GET", "/satisfaction?guide_id=x&qid=a&day=not-a-date")
        self.assertEqual(status, 400)
        self.assertEqual(api.request("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
