"""HTTP API：基于标准库，零三方依赖。

启动：``python -m qual_graph.api``（端口 8080，可用环境变量 PORT / QUAL_GRAPH_DB 覆盖）。
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .service import Service
from .storage import Store


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ApiHandler(BaseHTTPRequestHandler):
    service: Service = None  # type: ignore[assignment]
    lock: threading.Lock = None  # type: ignore[assignment]

    server_version = "QualGraph/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    # -- 工具 --------------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return payload

    def _query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _dispatch(self, method: str):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            with self.lock:
                return self._route(method, path)
        except DomainError as exc:
            from .errors import PublishBlockedError

            if isinstance(exc, PublishBlockedError):
                _json_response(self, exc.status, {"error": exc.code, "message": str(exc), "issues": exc.issues})
            else:
                _json_response(self, exc.status, {"error": exc.code, "message": str(exc)})

    def _route(self, method: str, path: str):
        s = self.service
        body = self._read_json() if method in ("POST", "PUT", "PATCH") else {}
        query = self._query()

        if method == "GET" and path == "/health":
            return _json_response(self, 200, {"status": "ok"})

        # ---- 图谱草稿 ----------------------------------------------------
        if path == "/graph/draft":
            if method == "POST":
                return _json_response(self, 201, {"draft_based_on": s.get_or_create_draft().based_on_version})
            if method == "GET":
                draft = s.get_or_create_draft()
                return _json_response(self, 200, {
                    "based_on_version": draft.based_on_version,
                    "nodes": [n.to_dict() for n in draft.nodes.values()],
                    "dependencies": [e.to_dict() for e in draft.deps],
                    "substitutions": [l.to_dict() for l in draft.subs],
                })
        if method == "PUT" and path == "/graph/draft/nodes":
            return _json_response(self, 200, s.upsert_node(body, actor=body.pop("_actor", "场馆管理员")))
        m = re.fullmatch(r"/graph/draft/nodes/([^/]+)", path)
        if m and method == "DELETE":
            s.remove_node(m.group(1), actor=query.get("actor", "场馆管理员"))
            return _json_response(self, 200, {"deleted": m.group(1)})
        if method == "POST" and path == "/graph/draft/dependencies":
            return _json_response(self, 201, s.add_dependency(body, actor=body.pop("_actor", "场馆管理员")))
        if method == "DELETE" and path == "/graph/draft/dependencies":
            s.remove_dependency(query["target"], query["required"])
            return _json_response(self, 200, {"deleted": [query["target"], query["required"]]})
        if method == "POST" and path == "/graph/draft/substitutions":
            return _json_response(self, 201, s.add_substitution(body, actor=body.pop("_actor", "场馆管理员")))
        if method == "DELETE" and path == "/graph/draft/substitutions":
            s.remove_substitution(query["replaces"], query["original"])
            return _json_response(self, 200, {"deleted": [query["replaces"], query["original"]]})
        if method == "GET" and path == "/graph/draft/validate":
            return _json_response(self, 200, s.validate_draft())

        # ---- 发布与版本 --------------------------------------------------
        if method == "POST" and path == "/graph/publish":
            return _json_response(self, 201, s.publish(note=body.get("note", ""), actor=body.get("_actor", "场馆管理员")))
        if method == "GET" and path == "/graph/versions":
            return _json_response(self, 200, {"versions": s.list_versions()})
        if method == "GET" and path == "/graph":
            version = int(query["version"]) if query.get("version") else None
            return _json_response(self, 200, s.get_graph(version).to_dict())

        # ---- 证据 --------------------------------------------------------
        if method == "POST" and path == "/evidences":
            return _json_response(self, 201, s.add_evidence(body, actor=body.pop("_actor", "场馆管理员")))
        if method == "GET" and path == "/evidences":
            return _json_response(self, 200, {"evidences": s.list_evidences(query.get("person_id"))})
        m = re.fullmatch(r"/evidences/([^/]+)/revoke", path)
        if m and method == "POST":
            return _json_response(self, 200, s.revoke_evidence(
                m.group(1), body.get("reason", ""), actor=body.get("_actor", "场馆管理员")))

        # ---- 豁免 --------------------------------------------------------
        if method == "POST" and path == "/waivers":
            return _json_response(self, 201, s.add_waiver(body, actor=body.pop("_actor", "活动统筹员")))
        if method == "GET" and path == "/waivers":
            return _json_response(self, 200, {"waivers": s.list_waivers()})
        m = re.fullmatch(r"/waivers/([^/]+)/revoke", path)
        if m and method == "POST":
            return _json_response(self, 200, s.revoke_waiver(
                m.group(1), body.get("reason", ""), actor=body.get("_actor", "活动统筹员")))

        # ---- 资格判定与缺口 ---------------------------------------------
        if method == "POST" and path == "/qualification/check":
            return _json_response(self, 200, s.check_qualification(
                body["person_id"], body["targets"],
                on_date=body.get("on_date"), version=body.get("version")))
        if method == "POST" and path == "/qualification/gaps":
            return _json_response(self, 200, s.query_gaps(
                body["targets"], on_date=body.get("on_date"),
                person_ids=body.get("person_ids"), version=body.get("version")))

        # ---- 排班 --------------------------------------------------------
        if method == "POST" and path == "/schedules":
            return _json_response(self, 201, s.create_schedule(body, actor=body.pop("_actor", "活动统筹员")))
        if method == "GET" and path == "/schedules":
            return _json_response(self, 200, {"schedules": s.list_schedules(query.get("person_id"))})
        m = re.fullmatch(r"/schedules/([^/]+)", path)
        if m and method == "GET":
            return _json_response(self, 200, s.get_schedule(m.group(1)))
        m = re.fullmatch(r"/schedules/([^/]+)/transition", path)
        if m and method == "POST":
            return _json_response(self, 200, s.transition_schedule(
                m.group(1), body["state"], actor=body.get("_actor", "活动统筹员")))

        # ---- 规则变化与影响 ---------------------------------------------
        if method == "GET" and path == "/rules/changes":
            frm = int(query["from"]) if query.get("from") else None
            to = int(query["to"]) if query.get("to") else None
            return _json_response(self, 200, s.rule_changes(frm, to))
        if method == "GET" and path == "/rules/impact":
            frm = int(query["from"]) if query.get("from") else None
            to = int(query["to"]) if query.get("to") else None
            return _json_response(self, 200, s.impact_analysis(frm, to))

        # ---- 审计 --------------------------------------------------------
        if method == "GET" and path == "/audit":
            limit = int(query["limit"]) if query.get("limit") else None
            return _json_response(self, 200, {"events": s.audit_log(limit)})

        _json_response(self, 404, {"error": "not_found", "message": f"没有这个接口：{method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    service = Service(Store(db_path))
    handler = ApiHandler
    handler.service = service
    handler.lock = threading.Lock()
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    db_path = os.environ.get("QUAL_GRAPH_DB", "data/qual_graph.json")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = build_server(db_path, host, port)
    print(f"讲解主题资格图服务已启动：http://{host}:{port}  数据库：{db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
