# 讲解主题资格图

场馆将讲解能力细分为**主题、展厅、受众等级**三类资格节点，节点之间存在前置依赖与替代关系。本项目在领域契约（`domain/contract.json`）之外，提供零三方依赖（仅 Python 3.11+ 标准库）的后端服务，维护资格节点、依赖、替代、证据和有效期，在发布图谱前检测循环与矛盾，排班时生成可解释的满足路径，并保证证据撤销、替代换版和临时豁免**不改写历史排班**。

## 核心不变量（对应契约四约束）

1. **资格依赖图**：三类节点 + 前置依赖边 + 替代边，全部版本化管理，草稿通过校验才能发布。
2. **循环矛盾检测**：发布前 Tarjan 强连通检测找依赖环（按替代等价类收缩后判定）、单向替代换版环；并检测悬空引用、依赖⨯替代矛盾、换版跨类型矛盾；`warning` 不阻断、`error` 阻断发布。
3. **可解释满足路径**：每个要求节点给出由谁的哪条证据、经怎样的替代链满足；过期/未生效/撤销证据一律无效；豁免只免本节点、不免前置；路径选择确定性（链最短、id 最小）。
4. **历史排班冻结**：排班进入「已排定」时冻结**当时的图谱快照与证据快照**；之后撤销证据、发布换版图谱、撤销豁免均只影响未来判定，冻结路径原样可查；状态机对齐契约五状态（筹备→待确认→已排定→执行中→已结算）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验（既有）。
- `src/qual_graph/`：后端包。
  - `models.py`：节点（topic/hall/audience_level）、依赖边、替代边（含 `supersedes` 换版标记）、证据。
  - `graph.py`：草稿、发布前校验（循环/矛盾）、已发布版本只读视图与版本 diff。
  - `evaluator.py`：有效期判定、替代链 BFS、可解释路径、批量缺口查询。
  - `schedule.py`：排班聚合、冻结快照、状态机。
  - `storage.py`：单文件原子写入的 JSON 存储 + 审计事件流。
  - `service.py`：编排所有用例的服务层。
  - `api.py`：标准库 HTTP API（`http.server`）。
- `tools/check_contract.py`：契约摘要检查（既有）。
- `tools/seed_demo.py`：端到端演示（v1 发布 → 排班冻结 → v2 换版+加前置 → 撤销证据 → 影响分析）。
- `tests/`：契约测试 + 后端 23 项回归测试（含真实 HTTP 冒烟）。

## 验证

```bash
# 全部测试（原有契约测试 1 项 + 后端回归 22 项，共 23 项）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 端到端演示
python3 tools/seed_demo.py /tmp/demo.json

# 启动 HTTP 服务
PYTHONPATH=src QUAL_GRAPH_DB=/tmp/qg.json PORT=8080 python3 -m qual_graph.api
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/graph/draft` | 创建/获取草稿（自动从最新已发布版本复制） |
| PUT | `/graph/draft/nodes` | 新建/更新节点 |
| POST/DELETE | `/graph/draft/dependencies` | 维护前置依赖 |
| POST/DELETE | `/graph/draft/substitutions` | 维护替代关系（`supersedes:true` 表示换版） |
| GET | `/graph/draft/validate` | 发布前检测（循环、矛盾、悬空、跨类型） |
| POST | `/graph/publish` | 发布新版本（有 error 级问题返回 422 并附问题清单） |
| GET | `/graph/versions` · `/graph?version=` | 版本列表与只读图谱 |
| POST/GET | `/evidences` | 登记证据（证书/考核，带 `valid_from/valid_until`） |
| POST | `/evidences/{id}/revoke` | 撤销证据（历史排班不动） |
| POST/GET | `/waivers` · POST `/waivers/{id}/revoke` | 临时豁免及其撤销 |
| POST | `/qualification/check` | 单人可解释满足路径 |
| POST | `/qualification/gaps` | 多人资格缺口（可排谁、缺什么） |
| POST/GET | `/schedules` · GET `/schedules/{id}` | 排班（创建即按当日规则评估并附路径） |
| POST | `/schedules/{id}/transition` | 状态迁移（进入「已排定」起冻结） |
| GET | `/rules/changes` | 两版图谱差异（节点/依赖/替代的增删改） |
| GET | `/rules/impact?from=&to=` | 规则变化影响：孤儿证据、人员新缺口、收紧节点、未改写的冻结排班 |
| GET | `/audit` | 全部操作的审计事件流 |

## 关键设计取舍

- **换版不就地改旧节点**：新版主题（如 `TOP-ASTRO-V2`）以带 `supersedes` 的单向替代边指向旧版；持有新版证书沿替代链满足旧要求，旧证书不能反向满足新要求。
- **过期证书不能满足高级主题**：判定严格按日历日（含当日有效），高级主题的传递闭包里任一前置证据失效即整体不满足。
- **豁免不扩散**：豁免是某节点自身的直接满足，既不沿替代链顶替，也不免除其前置，且自身有有效期与撤销。
- **冻结是快照而非标记**：排班冻结完整图谱（节点/依赖/替代）与所用证据内容，影响分析只报告“若按新规会怎样”，从不回写历史。
- **可复现**：Tarjan、路径选择、diff 输出全部确定性排序，无随机、无隐式时间依赖（日期显式传入，默认上海时区当日）。
