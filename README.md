# 开发社区中秋活动承载与安全放行中枢基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

在基础层之上，本项目实现了社区中秋活动承载与安全放行中枢：

- **活动编排**：策划人提交带版本的活动单元（诗会、花灯、民俗体验等）与依赖关系（无环校验、同站点约束），系统结合站点开放窗口、岗位资格、物资检查项与人流上限生成可执行方案，方案锁定单元版本与资源版本快照；
- **容量账本**：按站点维护人流上限，方案占用经 `reserved → committed → released` 流转，同时段重叠人数超限即阻挡；
- **安全签署**：安全员（reviewer）只能签署自己负责的检查项，必需签署齐备且单元与资源版本均未变化时，方案才可在单个事务内一次性发布；
- **影响分析**：降雨预警或设施条件变化只对未开始单元给出迁移建议（引用登记的雨天备选场地），正在进行的单元一律进入人工处置，绝不静默换场；
- **生命周期**：开始、暂停、恢复、结束、取消遵守明确状态顺序，重复操作返回稳定结果，结束与取消自动释放容量；
- **责任链**：接口展示方案生成与发布人、逐项签署人、每项资源在方案生成时（调整前）与当前（调整后）的维护人和版本差异。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `festival.py`：中秋活动编排、容量账本、签署放行与影响分析领域服务；
  - `festival_models.py`：编排领域的数据对象；
  - `festival_acceptance.py`：编排放行链路的离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

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
PYTHONPATH=src python3 -m festival_foundation.festival_acceptance
```

第一条命令验收基础登记链；第二条命令在临时 SQLite 数据库中完成建档、资源维护、单元提交、方案生成、安全签署、一次性发布、生命周期流转与降雨影响分析，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

### 中秋编排接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /festival/units` | 提交或修订带版本的活动单元（含依赖关系） |
| `GET /festival/units?site_id=` / `GET /festival/unit?unit_id=` | 查询单元及其状态 |
| `POST /festival/resources` | 维护站点窗口、岗位资格、物资检查、人流上限、雨天备选（内容变化才递增版本） |
| `GET /festival/resources?site_id=` | 查询资源版本与维护人 |
| `POST /festival/plans` | 生成可执行方案并锁定资源版本快照 |
| `GET /festival/plans?site_id=` / `GET /festival/plan?plan_id=` | 查询方案、排定与签署状态 |
| `POST /festival/plans/sign-off` | 安全员签署自己负责的检查项 |
| `POST /festival/plans/release` | 签署齐备且版本未变时一次性发布 |
| `POST /festival/units/transition` | 单元生命周期：`start`/`pause`/`resume`/`finish`/`cancel` |
| `POST /festival/impacts` | 上报降雨预警或设施变化并生成处置评估 |
| `GET /festival/impacts?site_id=` / `GET /festival/impact?impact_id=` | 查询影响事件与逐单元处置结论 |
| `GET /festival/clearance?site_id=` | 展示哪些活动可以放行、被什么条件阻挡 |
| `GET /festival/capacity?site_id=` | 展示人流上限与账本占用峰值 |
| `GET /festival/responsibility?plan_id=` | 展示调整前后的责任链与资源版本差异 |
