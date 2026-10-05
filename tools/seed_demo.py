"""端到端演示种子：构建资格图 v1、排班、再发布 v2（换版+加前置），展示冻结与影响分析。

运行：python3 tools/seed_demo.py data/demo.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qual_graph.service import Service
from qual_graph.storage import Store


def build_v1(service: Service) -> None:
    service.get_or_create_draft()
    nodes = [
        ("HALL-MAIN", "hall", "主展厅"),
        ("LVL-BASIC", "audience_level", "基础受众"),
        ("LVL-ADV", "audience_level", "进阶受众"),
        ("TOP-ASTRO-V1", "topic", "天文主题（初版）"),
        ("TOP-DINO", "topic", "恐龙主题"),
        ("TOP-ROCKET", "topic", "火箭主题"),
    ]
    for nid, kind, name in nodes:
        service.upsert_node({"node_id": nid, "kind": kind, "name": name})
    # 主题前置：展厅 + 受众等级；高级主题还需要基础主题
    for target, required in [
        ("TOP-ASTRO-V1", "HALL-MAIN"),
        ("TOP-ASTRO-V1", "LVL-BASIC"),
        ("TOP-DINO", "HALL-MAIN"),
        ("TOP-DINO", "LVL-BASIC"),
        ("TOP-ROCKET", "TOP-ASTRO-V1"),
        ("TOP-ROCKET", "LVL-ADV"),
    ]:
        service.add_dependency({"target": target, "required": required})
    service.publish(note="v1 初始资格图")


def seed_people(service: Service) -> None:
    # 张老师：主展厅+基础受众+天文初版（均有效）
    service.add_evidence({"evidence_id": "EV-ZHANG-HALL", "person_id": "P-ZHANG",
                          "node_id": "HALL-MAIN", "evidence_type": "certificate",
                          "valid_from": "2025-01-01", "valid_until": "2027-01-01"})
    service.add_evidence({"evidence_id": "EV-ZHANG-LVL", "person_id": "P-ZHANG",
                          "node_id": "LVL-BASIC", "evidence_type": "assessment",
                          "valid_from": "2025-01-01", "valid_until": None})
    service.add_evidence({"evidence_id": "EV-ZHANG-ASTRO", "person_id": "P-ZHANG",
                          "node_id": "TOP-ASTRO-V1", "evidence_type": "certificate",
                          "valid_from": "2025-03-01", "valid_until": "2027-03-01"})
    service.add_evidence({"evidence_id": "EV-ZHANG-ROCKET", "person_id": "P-ZHANG",
                          "node_id": "TOP-ROCKET", "evidence_type": "certificate",
                          "valid_from": "2025-06-01", "valid_until": "2027-06-01"})
    # 李老师：有进阶受众，但天文证书 2026-09 已过期（演示过期证书不能满足高级主题）
    service.add_evidence({"evidence_id": "EV-LI-HALL", "person_id": "P-LI",
                          "node_id": "HALL-MAIN", "evidence_type": "certificate",
                          "valid_from": "2024-01-01", "valid_until": None})
    service.add_evidence({"evidence_id": "EV-LI-LVLA", "person_id": "P-LI",
                          "node_id": "LVL-ADV", "evidence_type": "assessment",
                          "valid_from": "2024-06-01", "valid_until": None})
    service.add_evidence({"evidence_id": "EV-LI-ASTRO", "person_id": "P-LI",
                          "node_id": "TOP-ASTRO-V1", "evidence_type": "certificate",
                          "valid_from": "2024-09-01", "valid_until": "2026-09-01"})


def publish_v2(service: Service) -> None:
    """v2：天文主题换版（新证书可顶替旧要求），火箭主题新增恐龙主题前置。"""
    service.get_or_create_draft()  # 基于 v1 复制
    service.upsert_node({"node_id": "TOP-ASTRO-V2", "kind": "topic", "name": "天文主题（第二版）"})
    service.add_substitution({
        "replaces": "TOP-ASTRO-V2", "original": "TOP-ASTRO-V1",
        "reason": "教材换版，新考核覆盖旧版内容", "supersedes": True,
    })
    service.add_dependency({"target": "TOP-ROCKET", "required": "TOP-DINO",
                            "note": "v2 起火箭主题需同时掌握恐龙主题"})
    service.publish(note="v2 天文换版，火箭新增前置")


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "demo.json"
    if db_path.exists():
        db_path.unlink()
    service = Service(Store(db_path))

    build_v1(service)
    seed_people(service)

    print("== v1 下张老师讲解火箭主题的资格检查（应不满足：缺 LVL-ADV） ==")
    print(json.dumps(service.check_qualification("P-ZHANG", ["TOP-ROCKET"], "2026-10-05"),
                     ensure_ascii=False, indent=2))

    print("== v1 下张老师讲解天文主题：满足路径 ==")
    ok = service.check_qualification("P-ZHANG", ["TOP-ASTRO-V1"], "2026-10-05")
    print(json.dumps(ok, ensure_ascii=False, indent=2))

    print("== 用临时豁免让张老师通过进阶受众，完成排班（已排定即冻结） ==")
    service.add_waiver({"waiver_id": "WV-ZHANG-ADV", "person_id": "P-ZHANG",
                        "node_id": "LVL-ADV", "reason": "月内突击补考安排中",
                        "valid_from": "2026-10-01", "valid_until": "2026-10-31"})
    schedule = service.create_schedule({
        "schedule_id": "SC-DEMO-1", "person_id": "P-ZHANG",
        "required_nodes": ["TOP-ROCKET"], "event_date": "2026-10-20",
    })
    service.transition_schedule("SC-DEMO-1", "待确认")
    service.transition_schedule("SC-DEMO-1", "已排定")
    print(f"排班冻结于图谱 v{schedule['created_graph_version']}，路径步数：{len(schedule['satisfied_path'])}")

    print("== 发布 v2（换版 + 新前置） ==")
    publish_v2(service)

    print("== v2 下李老师（旧版天文证书已过期）讲解火箭：缺口 ==")
    print(json.dumps(service.check_qualification("P-LI", ["TOP-ROCKET"], "2026-10-05"),
                     ensure_ascii=False, indent=2))

    print("== 撤销张老师的天文初版证书；历史排班保持不变 ==")
    service.revoke_evidence("EV-ZHANG-ASTRO", "证书复核未通过")
    frozen = service.get_schedule("SC-DEMO-1")
    frozen_ev = {s["evidence_id"] for s in frozen["satisfied_path"]}
    print(f"历史排班状态={frozen['state']}，冻结版本=v{frozen['created_graph_version']}，"
          f"路径证据仍为 {sorted(frozen_ev)}（含已撤销的 EV-ZHANG-ASTRO 快照）")

    print("== v1 -> v2 规则变化影响分析 ==")
    print(json.dumps(service.impact_analysis(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
