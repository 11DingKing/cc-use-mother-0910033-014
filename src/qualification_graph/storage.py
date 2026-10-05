"""SQLite 存储层。

设计要点
========
* 图谱按版本整版本冻结：``graph_nodes`` / ``evidence_types`` 带版本号，
  发布后永不更新（新版本 = 克隆 + 修改 + 再发布）。
* 证据与豁免是双时间记录：撤销只置标记与日期，不删除行，历史可追溯。
* 排班保存解释快照（使用的图谱版本、规则哈希、解释树、底层依据 ID），
  证据撤销、图谱换版、豁免变化都不会回写历史排班。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from .models import Evidence, Exemption, Qualification

SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_versions (
    version        INTEGER PRIMARY KEY,
    status         TEXT NOT NULL CHECK (status IN ('draft', 'published')),
    created_from   INTEGER,
    published_on   TEXT,
    rule_hash      TEXT,
    notes          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    version INTEGER NOT NULL,
    qid     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    name    TEXT NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (version, qid)
);

CREATE TABLE IF NOT EXISTS evidence_types (
    version INTEGER NOT NULL,
    code    TEXT NOT NULL,
    label   TEXT NOT NULL,
    PRIMARY KEY (version, code)
);

CREATE TABLE IF NOT EXISTS evidences (
    evidence_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    guide_id      TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    title         TEXT NOT NULL,
    issued_on     TEXT NOT NULL,
    valid_until   TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    revoked_on    TEXT,
    revoke_reason TEXT
);

CREATE TABLE IF NOT EXISTS exemptions (
    exemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guide_id     TEXT NOT NULL,
    qid          TEXT NOT NULL,
    start_on     TEXT NOT NULL,
    end_on       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    guide_id         TEXT NOT NULL,
    qid              TEXT NOT NULL,
    scheduled_on     TEXT NOT NULL,
    assigned_on      TEXT NOT NULL,
    graph_version    INTEGER NOT NULL,
    rule_hash        TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    leaves_json      TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_evidences_guide ON evidences(guide_id);
CREATE INDEX IF NOT EXISTS idx_exemptions_guide ON exemptions(guide_id);
CREATE INDEX IF NOT EXISTS idx_schedules_guide ON schedules(guide_id);
"""


def _d(value: date) -> str:
    return value.isoformat()


def _maybe_d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class Storage:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # check_same_thread=False：HTTP 服务器为单线程串行处理；
        # 同时允许测试在后台线程托管内存库。
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 图谱版本 -------------------------------------------------------

    def max_version(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM graph_versions").fetchone()
        return int(row["v"])

    def create_version(self, version: int, created_from: int | None, status: str = "draft") -> None:
        self.conn.execute(
            "INSERT INTO graph_versions(version, status, created_from) VALUES (?, ?, ?)",
            (version, status, created_from),
        )
        self.conn.commit()

    def version_status(self, version: int) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM graph_versions WHERE version = ?", (version,)
        ).fetchone()
        return row["status"] if row else None

    def list_versions(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT version, status, created_from, published_on, rule_hash, notes "
                "FROM graph_versions ORDER BY version"
            )
        )

    def get_version(self, version: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM graph_versions WHERE version = ?", (version,)
        ).fetchone()

    def latest_published(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM graph_versions WHERE status = 'published'"
        ).fetchone()
        return row["v"] if row and row["v"] is not None else None

    def mark_published(self, version: int, day: date, rule_hash: str, notes: str) -> None:
        self.conn.execute(
            "UPDATE graph_versions SET status='published', published_on=?, rule_hash=?, notes=? "
            "WHERE version=?",
            (_d(day), rule_hash, notes, version),
        )
        self.conn.commit()

    # ---- 节点与证据类型 ---------------------------------------------------

    def clone_nodes(self, src_version: int, dst_version: int) -> None:
        self.conn.execute(
            "INSERT INTO graph_nodes(version, qid, kind, name, data) "
            "SELECT ?, qid, kind, name, data FROM graph_nodes WHERE version=?",
            (dst_version, src_version),
        )
        self.conn.execute(
            "INSERT INTO evidence_types(version, code, label) "
            "SELECT ?, code, label FROM evidence_types WHERE version=?",
            (dst_version, src_version),
        )
        self.conn.commit()

    def put_node(self, version: int, node: Qualification) -> None:
        import json

        self.conn.execute(
            "INSERT INTO graph_nodes(version, qid, kind, name, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(version, qid) DO UPDATE SET kind=excluded.kind, name=excluded.name, "
            "data=excluded.data",
            (version, node.qid, node.kind, node.name, json.dumps(node.to_dict(), ensure_ascii=False)),
        )
        self.conn.commit()

    def remove_node(self, version: int, qid: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM graph_nodes WHERE version=? AND qid=?", (version, qid)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def load_nodes(self, version: int) -> dict[str, Qualification]:
        import json

        rows = self.conn.execute(
            "SELECT data FROM graph_nodes WHERE version=? ORDER BY qid", (version,)
        ).fetchall()
        return {
            q.qid: q
            for q in (Qualification.from_dict(json.loads(r["data"])) for r in rows)
        }

    def set_evidence_type(self, version: int, code: str, label: str) -> None:
        self.conn.execute(
            "INSERT INTO evidence_types(version, code, label) VALUES (?, ?, ?) "
            "ON CONFLICT(version, code) DO UPDATE SET label=excluded.label",
            (version, code, label),
        )
        self.conn.commit()

    def load_evidence_types(self, version: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT code, label FROM evidence_types WHERE version=? ORDER BY code", (version,)
        ).fetchall()
        return {r["code"]: r["label"] for r in rows}

    # ---- 证据 -------------------------------------------------------------

    def insert_evidence(self, evidence: Evidence) -> int:
        cur = self.conn.execute(
            "INSERT INTO evidences(guide_id, evidence_type, title, issued_on, valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                evidence.guide_id,
                evidence.evidence_type,
                evidence.title,
                _d(evidence.issued_on),
                _d(evidence.valid_until) if evidence.valid_until else None,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _row_to_evidence(self, row: sqlite3.Row) -> Evidence:
        return Evidence(
            evidence_id=row["evidence_id"],
            guide_id=row["guide_id"],
            evidence_type=row["evidence_type"],
            title=row["title"],
            issued_on=date.fromisoformat(row["issued_on"]),
            valid_until=_maybe_d(row["valid_until"]),
            revoked=bool(row["revoked"]),
            revoked_on=_maybe_d(row["revoked_on"]),
            revoke_reason=row["revoke_reason"],
        )

    def get_evidence(self, evidence_id: int) -> Evidence | None:
        row = self.conn.execute(
            "SELECT * FROM evidences WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        return self._row_to_evidence(row) if row else None

    def list_evidences(self, guide_id: str | None = None) -> list[Evidence]:
        if guide_id is None:
            rows = self.conn.execute("SELECT * FROM evidences ORDER BY evidence_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM evidences WHERE guide_id=? ORDER BY evidence_id", (guide_id,)
            ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    def revoke_evidence(self, evidence_id: int, day: date, reason: str) -> bool:
        cur = self.conn.execute(
            "UPDATE evidences SET revoked=1, revoked_on=?, revoke_reason=? "
            "WHERE evidence_id=? AND revoked=0",
            (_d(day), reason, evidence_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ---- 豁免 -------------------------------------------------------------

    def insert_exemption(self, exemption: Exemption) -> int:
        cur = self.conn.execute(
            "INSERT INTO exemptions(guide_id, qid, start_on, end_on, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                exemption.guide_id,
                exemption.qid,
                _d(exemption.start_on),
                _d(exemption.end_on),
                exemption.reason,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _row_to_exemption(self, row: sqlite3.Row) -> Exemption:
        return Exemption(
            exemption_id=row["exemption_id"],
            guide_id=row["guide_id"],
            qid=row["qid"],
            start_on=date.fromisoformat(row["start_on"]),
            end_on=date.fromisoformat(row["end_on"]),
            reason=row["reason"],
            revoked=bool(row["revoked"]),
        )

    def get_exemption(self, exemption_id: int) -> Exemption | None:
        row = self.conn.execute(
            "SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)
        ).fetchone()
        return self._row_to_exemption(row) if row else None

    def list_exemptions(self, guide_id: str | None = None) -> list[Exemption]:
        if guide_id is None:
            rows = self.conn.execute("SELECT * FROM exemptions ORDER BY exemption_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM exemptions WHERE guide_id=? ORDER BY exemption_id", (guide_id,)
            ).fetchall()
        return [self._row_to_exemption(r) for r in rows]

    def revoke_exemption(self, exemption_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE exemptions SET revoked=1 WHERE exemption_id=? AND revoked=0",
            (exemption_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ---- 排班 -------------------------------------------------------------

    def insert_schedule(self, row: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO schedules(guide_id, qid, scheduled_on, assigned_on, graph_version, "
            "rule_hash, explanation_json, leaves_json, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["guide_id"],
                row["qid"],
                _d(row["scheduled_on"]),
                _d(row["assigned_on"]),
                row["graph_version"],
                row["rule_hash"],
                row["explanation_json"],
                row["leaves_json"],
                row.get("note", ""),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _row_to_schedule(self, row: sqlite3.Row) -> dict[str, Any]:
        import json

        return {
            "schedule_id": row["schedule_id"],
            "guide_id": row["guide_id"],
            "qid": row["qid"],
            "scheduled_on": date.fromisoformat(row["scheduled_on"]),
            "assigned_on": date.fromisoformat(row["assigned_on"]),
            "graph_version": row["graph_version"],
            "rule_hash": row["rule_hash"],
            "explanation": json.loads(row["explanation_json"]),
            "leaves": json.loads(row["leaves_json"]),
            "note": row["note"],
        }

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)
        ).fetchone()
        return self._row_to_schedule(row) if row else None

    def list_schedules(self, guide_id: str | None = None) -> list[dict[str, Any]]:
        if guide_id is None:
            rows = self.conn.execute("SELECT * FROM schedules ORDER BY schedule_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM schedules WHERE guide_id=? ORDER BY schedule_id", (guide_id,)
            ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def distinct_scheduled_pairs(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT DISTINCT guide_id, qid FROM schedules ORDER BY guide_id, qid"
        ).fetchall()
        return [(r["guide_id"], r["qid"]) for r in rows]
