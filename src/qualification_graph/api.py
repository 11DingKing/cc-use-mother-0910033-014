"""HTTP API（标准库 http.server，零第三方依赖）。

路由概览
========
图谱版本
* ``GET  /health``
* ``POST /graphs/drafts``                     基于最新已发布版本创建草稿
* ``GET  /graphs/versions``                   版本列表
* ``GET  /graphs/{version}``                  图谱内容
* ``PUT  /graphs/{version}/nodes``            草稿中新增/更新资格节点
* ``DELETE /graphs/{version}/nodes/{qid}``    草稿删除节点
* ``PUT  /graphs/{version}/evidence-types``   草稿登记证据类型
* ``GET  /graphs/{version}/validate``         发布前校验（循环/矛盾）
* ``POST /graphs/{version}/publish``          发布（有阻断问题则拒绝）

证据与豁免
* ``POST /evidences`` / ``GET /evidences?guide_id=``
* ``POST /evidences/{id}/revoke``
* ``POST /exemptions`` / ``GET /exemptions?guide_id=``
* ``POST /exemptions/{id}/revoke``

满足判定 / 缺口 / 排班
* ``GET  /satisfaction?guide_id=&qid=&day=&version=``
* ``GET  /gaps?guide_id=&qid=&day=&version=``
* ``POST /schedules`` / ``GET /schedules?guide_id=`` / ``GET /schedules/{id}``
* ``GET /schedules/{id}/reevaluate?day=``     只读复评，不改写历史

规则影响
* ``GET /impact?old=&new=&day=``
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .models import GraphError, Qualification, RequirementGroup
from .service import QualificationService
from .storage import Storage


def _parse_day(value: str | None, field: str = "day") -> date:
    if not value:
        raise GraphError(f"缺少日期参数 {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GraphError(f"日期格式非法（需 YYYY-MM-DD）：{value}") from exc


class ApiHandler(BaseHTTPRequestHandler):
    service: QualificationService  # 由工厂注入

    def log_message(self, fmt: str, *args: object) -> None:  # 安静日志
        return

    # ---- 基础工具 ---------------------------------------------------------

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise GraphError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise GraphError("请求体必须是 JSON 对象")
        return value

    def _handle(self, fn) -> None:
        try:
            fn()
        except GraphError as exc:
            self._send(400, {"error": str(exc)})
        except KeyError as exc:
            self._send(400, {"error": f"缺少字段：{exc.args[0]}"})
        except (TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc)})

    # ---- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle(self._route_get)

    def do_POST(self) -> None:  # noqa: N802
        self._handle(self._route_post)

    def do_PUT(self) -> None:  # noqa: N802
        self._handle(self._route_put)

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle(self._route_delete)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        q = lambda k, default=None: query.get(k, [default])[0]

        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/graphs/versions":
            return self._send(200, {"versions": self.service.list_versions()})

        m = re.fullmatch(r"/graphs/(\d+)", path)
        if m:
            return self._send(200, self.service.get_graph(int(m.group(1))))
        m = re.fullmatch(r"/graphs/(\d+)/validate", path)
        if m:
            issues = self.service.validate_draft(int(m.group(1)))
            return self._send(200, {"version": int(m.group(1)), "ok": not issues, "issues": issues})

        if path == "/satisfaction":
            day = _parse_day(q("day"))
            result = self.service.explain(q("guide_id"), q("qid"), day, _opt_int(q("version")))
            return self._send(200, result.to_dict())
        if path == "/gaps":
            day = _parse_day(q("day"))
            return self._send(
                200,
                self.service.qualification_gaps(q("guide_id"), q("qid"), day, _opt_int(q("version"))),
            )
        if path == "/evidences":
            return self._send(
                200,
                [e.to_dict() for e in self.service.storage.list_evidences(q("guide_id"))],
            )
        if path == "/exemptions":
            return self._send(
                200,
                [e.to_dict() for e in self.service.storage.list_exemptions(q("guide_id"))],
            )
        if path == "/schedules":
            return self._send(200, self.service.list_schedules(q("guide_id")))
        m = re.fullmatch(r"/schedules/(\d+)", path)
        if m:
            return self._send(200, self.service.get_schedule(int(m.group(1))))
        m = re.fullmatch(r"/schedules/(\d+)/reevaluate", path)
        if m:
            return self._send(
                200,
                self.service.reevaluate_schedule(int(m.group(1)), _parse_day(q("day"), "day") if q("day") else None),
            )
        if path == "/impact":
            old = int(q("old"))
            return self._send(
                200,
                self.service.rule_change_impact(
                    old, _opt_int(q("new")), _parse_day(q("day")) if q("day") else None
                ),
            )
        self._send(404, {"error": f"未知路径：{self.path}"})

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/graphs/drafts":
            version = self.service.create_draft(_opt_int(body.get("created_from")))
            return self._send(201, {"version": version, "status": "draft"})
        m = re.fullmatch(r"/graphs/(\d+)/publish", path)
        if m:
            result = self.service.publish(
                int(m.group(1)),
                _parse_day(body["day"]) if body.get("day") else None,
                str(body.get("notes", "")),
            )
            return self._send(200, result)

        if path == "/evidences":
            evidence_id = self.service.grant_evidence(
                body["guide_id"],
                body["evidence_type"],
                body.get("title", body["evidence_type"]),
                _parse_day(body.get("issued_on") or date.today().isoformat(), "issued_on"),
                _parse_day(body["valid_until"], "valid_until") if body.get("valid_until") else None,
            )
            return self._send(201, {"evidence_id": evidence_id})
        m = re.fullmatch(r"/evidences/(\d+)/revoke", path)
        if m:
            self.service.revoke_evidence(
                int(m.group(1)),
                _parse_day(body["day"]) if body.get("day") else None,
                str(body.get("reason", "")),
            )
            return self._send(200, {"evidence_id": int(m.group(1)), "revoked": True})

        if path == "/exemptions":
            exemption_id = self.service.add_exemption(
                body["guide_id"],
                body["qid"],
                _parse_day(body["start_on"], "start_on"),
                _parse_day(body["end_on"], "end_on"),
                str(body.get("reason", "")),
            )
            return self._send(201, {"exemption_id": exemption_id})
        m = re.fullmatch(r"/exemptions/(\d+)/revoke", path)
        if m:
            self.service.revoke_exemption(int(m.group(1)))
            return self._send(200, {"exemption_id": int(m.group(1)), "revoked": True})

        if path == "/schedules":
            result = self.service.create_schedule(
                body["guide_id"],
                body["qid"],
                _parse_day(body["scheduled_on"], "scheduled_on"),
                _opt_int(body.get("version")),
                str(body.get("note", "")),
                _parse_day(body["assigned_on"], "assigned_on") if body.get("assigned_on") else None,
            )
            return self._send(201, result)

        self._send(404, {"error": f"未知路径：{self.path}"})

    def _route_put(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()

        m = re.fullmatch(r"/graphs/(\d+)/nodes", path)
        if m:
            payload = body.get("node", body)
            groups = tuple(
                RequirementGroup(tuple(g["candidates"]))
                for g in payload.get("requirement_groups", [])
            )
            node = Qualification(
                qid=payload["qid"],
                name=payload.get("name", payload["qid"]),
                kind=payload["kind"],
                requirement_groups=groups,
                supersedes=tuple(payload.get("supersedes", [])),
            )
            self.service.upsert_node(int(m.group(1)), node)
            return self._send(200, {"version": int(m.group(1)), "qid": node.qid, "saved": True})
        m = re.fullmatch(r"/graphs/(\d+)/evidence-types", path)
        if m:
            self.service.register_evidence_type(int(m.group(1)), body["code"], body.get("label", body["code"]))
            return self._send(200, {"version": int(m.group(1)), "code": body["code"], "saved": True})

        self._send(404, {"error": f"未知路径：{self.path}"})

    def _route_delete(self) -> None:
        path = urlparse(self.path).path
        m = re.fullmatch(r"/graphs/(\d+)/nodes/([^/]+)", path)
        if m:
            self.service.delete_node(int(m.group(1)), m.group(2))
            return self._send(200, {"version": int(m.group(1)), "qid": m.group(2), "deleted": True})
        self._send(404, {"error": f"未知路径：{self.path}"})


def _opt_int(value) -> int | None:
    return int(value) if value not in (None, "") else None


def create_app(db_path: str = ":memory:") -> tuple[HTTPServer, QualificationService]:
    storage = Storage(db_path)
    service = QualificationService(storage)

    class _BoundHandler(ApiHandler):
        pass

    _BoundHandler.service = service
    server = HTTPServer(("127.0.0.1", 0), _BoundHandler)
    return server, service


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="讲解主题资格图 HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("QUALGRAPH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QUALGRAPH_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("QUALGRAPH_DB", "qualification_graph.db"))
    args = parser.parse_args()

    storage = Storage(args.db)
    service = QualificationService(storage)

    class _BoundHandler(ApiHandler):
        pass

    _BoundHandler.service = service
    server = HTTPServer((args.host, args.port), _BoundHandler)
    print(f"资格图服务运行于 http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        storage.close()


if __name__ == "__main__":
    main()
