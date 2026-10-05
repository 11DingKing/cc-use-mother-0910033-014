# 讲解主题资格图

场馆将讲解能力细分为**主题、展厅、受众等级**三类资格节点。节点之间存在前置依赖与
新旧版替代关系，人工维护容易形成循环，或让已撤销/过期证书继续满足高级主题。本项目用
Python（标准库 + SQLite，零第三方依赖）建设资格图后端，维护资格节点、依赖、替代、
证据、有效期与临时豁免，并在发布图谱前检测循环与矛盾，排班时生成可解释的满足路径。

## 核心约束

1. **资格依赖图**：资格需求建模为 AND-of-OR 需求组（组间 AND、组内候选 OR），
   候选可以是前置资格或证据类型；`supersedes` 表示新旧版替代（旧 → 新），
   支持多级替代链。
2. **循环矛盾检测**：在“依赖 + 替代”合并图上做 DFS 找环，并检查悬空引用、
   自引用、空需求组、跨组重复候选。发布时强制校验，有阻断问题即拒绝发布。
3. **可解释满足路径**：按 `临时豁免 → 直接需求 → 替代链` 优先级判定，
   返回完整解释树（basis 为 `exemption / evidence / direct / prerequisite /
   supersession`），叶子即真正提供满足的底层依据（具体证据/豁免编号）。
4. **历史排班冻结**：排班落库时快照图谱版本号与规则哈希（全量规则 SHA-256）、
   完整解释树与底层依据；证据撤销、替代关系换版、豁免变化**一律不回写**历史排班。
   另有只读“现时复评”接口对照新规则下的状态。
5. **双时间证据**：证据记录签发日、有效期截止日、撤销标记与撤销日/原因，按指定
   日期判定有效性；临时豁免有生效窗口、可撤销。
6. **版本化图谱**：`draft → published` 整版本冻结，发布后不可改；新草稿从已发布
   版本克隆，替代关系换版 = 发新版本。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/qualification_graph/`：资格图后端
  - `models.py`：领域模型（资格/需求组/证据/豁免/解释树）。
  - `validation.py`：纯函数式发布前校验与找环。
  - `storage.py`：SQLite 存储（版本化图谱、双时间证据、冻结排班）。
  - `service.py`：核心服务（草稿发布、满足引擎、缺口分析、排班冻结、规则影响）。
  - `api.py`：HTTP API（`http.server`，含 `--port/--db` 启动入口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约测试 + 后端单元/集成测试（含真实 HTTP 端到端流程）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`PYTHONPATH=src python3 -m qualification_graph.api --port 8080 --db data.db`
（也可用环境变量 `QUALGRAPH_PORT` / `QUALGRAPH_DB`，默认 `127.0.0.1:8080`）

## HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/graphs/drafts` | 基于最新已发布版本创建草稿 |
| GET | `/graphs/versions` | 版本列表（含规则哈希、发布日） |
| GET | `/graphs/{v}` | 读取某版本完整图谱 |
| PUT | `/graphs/{v}/nodes` | 草稿新增/更新资格节点 |
| DELETE | `/graphs/{v}/nodes/{qid}` | 草稿删除节点 |
| PUT | `/graphs/{v}/evidence-types` | 登记证据类型代码 |
| GET | `/graphs/{v}/validate` | 发布前校验（循环/矛盾） |
| POST | `/graphs/{v}/publish` | 发布（有阻断问题拒绝） |
| POST/GET | `/evidences`、`/evidences/{id}/revoke` | 发证/查询/撤销（不删行） |
| POST/GET | `/exemptions`、`/exemptions/{id}/revoke` | 临时豁免窗口/撤销 |
| GET | `/satisfaction?guide_id=&qid=&day=&version=` | 可解释满足路径 |
| GET | `/gaps?...` | 资格缺口（未持有/过期/撤销/未生效分类） |
| POST/GET | `/schedules`、`/schedules/{id}` | 排班（不满足则拒绝）与冻结快照 |
| GET | `/schedules/{id}/reevaluate?day=` | 现时复评（只读，不改写历史） |
| GET | `/impact?old=&new=&day=` | 规则变化影响：节点 diff、讲解员状态翻转、受影响历史排班 |

## 典型流程

```bash
B=http://127.0.0.1:8080
# 1. 建草稿、登记证据类型、配置节点后发布
curl -s -X POST $B/graphs/drafts -d '{}'
curl -s -X PUT  $B/graphs/1/evidence-types -d '{"code":"cert_basic","label":"基础讲解证"}'
curl -s -X PUT  $B/graphs/1/nodes -d '{"node":{"qid":"hall_a","kind":"hall",
  "requirement_groups":[{"candidates":["cert_basic"]}]}}'
curl -s $B/graphs/1/validate
curl -s -X POST $B/graphs/1/publish -d '{"day":"2026-01-10"}'
# 2. 发证 → 查可解释满足路径 → 排班（冻结规则版本与解释树）
# 3. 证据撤销 / 换版（新草稿改 supersedes 后再发布）：历史排班不变，
#    /schedules/{id}/reevaluate 与 /impact 给出对照与影响清单
```
