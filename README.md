# 开发社区中秋活动承载与安全放行中枢

本项目在节日公共服务通用后台基础层（组织、服务站点、操作者、角色权限、请求幂等、SQLite 事务与哈希串联审计）之上，提供中秋晚会等**多环节同晚活动**的承载核算与安全放行能力：策划人提交带版本的活动单元和依赖关系，系统结合站点开放窗口、岗位资格、物资/消防检查与人流上限生成可执行方案；安全员只能签署自己负责的检查项，所有必需签署齐备且资源版本未变化时，方案才能在单个事务内一次性发布。

## 核心能力

- **带版本的编排**：方案以修订本流转 `drafting → published → superseded`；活动单元每次内容变化版本递增，内容相同的单元沿用旧版本；相同内容重提（含数组顺序变化）稳定返回，不产生新修订。
- **闸门评估**：`GET /plans/gate` 与 `GET /release-board` 展示每个单元能否放行以及具体阻挡条件——超人流上限、时间不在开放窗口、岗位缺资格、场地关闭/受限、跨方案容量重叠、缺少检查项或签署等。
- **容量账本**：场地人流上限、室内外标记、设施状态独立维护并带版本；发布时把单元写入容量承诺，按时间重叠扫描峰值；取消/结束的单元自动退出占位。
- **职责分离的签署**：检查项按场地（如消防）和单元（如物资用电）登记到具体安全员；安全员只能签自己负责的项；签名与作用域资源摘要绑定，资源版本变化后旧签变为 `stale` 必须重签，未受影响范围的签署在新修订中自动沿用（`carried`）。
- **一次性发布**：发布前在事务内重算全部闸门；任一条件不满足则整单回滚，不留容量承诺和发布事件。
- **降雨与设施影响分析**：降雨预警下，未开始的室外单元只生成迁移到雨天备选场地的建议（含可行性预览与调整前后责任链）；正在进行/暂停的单元进入 `manual_handling` 人工处置，**绝不静默换场**；已结束/已取消单元不受影响。
- **显式生命周期**：`scheduled → in_progress ⇄ paused → ended`、未开始可 `cancelled`、人工处置可 `resume/end`；越序操作被拒绝并给出顺序提示；同一 `request_id` 重复操作返回稳定结果（`reapplied`）。

## 目录

- `src/festival_foundation/`：基础层与 `orchestration.py`（编排、容量账本、签署、影响分析）、HTTP 路由和离线验收；
- `tests/`：基础规则与中枢规则、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m festival_foundation.acceptance
```

验收会在临时 SQLite 数据库中登记组织、人员、站点资源与窗口，提交“诗会、花灯展示、民俗体验”三个同晚单元，验证签署未齐不能发布、降雨时进行中诗会进入人工处置、其余单元迁移到礼堂并重新签署后再发布，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 接口

写入接口继续通过 `X-Actor-Id` 标识操作者，所有写操作要求 `request_id` 以保证幂等。

基础层：`POST /organizations`、`POST /actors`、`POST /sites`、`POST /domain-records`、`GET /domain-records`、`GET /audit-events`、`GET /health`。

中枢接口：

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /site-resources` | admin/operator | 场地人流上限、室内外、设施状态（版本化） |
| `POST /site-windows` | admin/operator | 场地开放窗口（版本化） |
| `POST /certifications` | admin | 授予岗位资格 |
| `POST /safety-checks` | admin/operator | 登记消防/物资检查项与负责安全员 |
| `POST /plans` | admin/operator | 提交/更新方案与单元、依赖、岗位、雨天备选 |
| `POST /plans/discard` | admin/operator | 放弃待发布修订 |
| `GET  /plans/gate?plan_id=` | 全部 | 方案闸门：阻挡条件、必需签署、资源摘要 |
| `POST /plans/sign` | reviewer（仅负责人） | 签署自己负责的检查项 |
| `POST /plans/publish` | admin/operator | 条件齐备时一次性发布 |
| `GET  /release-board?site_id=` | 全部 | 跨方案放行看板 |
| `GET  /capacity-ledger?site_id=` | 全部 | 场地容量配置与承诺 |
| `POST /incidents/rain` | admin/operator | 发布降雨预警并执行影响分流 |
| `POST /incidents/facility` | admin/operator | 报告设施条件变化（更新资源版本） |
| `GET  /impact-analysis?plan_id=` | 全部 | 迁移建议、可行性、调整前后责任链、人工处置单元 |
| `POST /relocations/accept` | admin/operator | 接受建议生成新草稿（仅未开始单元） |
| `POST /units/start` `/pause` `/resume` `/end` `/cancel` | admin/operator | 单元现场状态顺序操作 |
| `POST /units/manual-resolve` | admin/operator/reviewer | 人工处置决定：`resume` 或 `end` |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

服务重启后 SQLite 中的业务状态、容量承诺与审计链继续保留。
