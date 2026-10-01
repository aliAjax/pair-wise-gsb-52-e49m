# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限、计划版本与冲突检查，以及复核批次的依据固定、月份结算和结论重算（`MonthlyRules`）。
- `src/repository.py`：SQLite建表、事务和查询（含服务台账 `service_records`、复核批次 `review_batches`、批次条目 `batch_entries`）。
- `src/service.py`：用例编排、权限检查、乐观并发、审计、批次提交合并、失效重算与旧数据回填。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面（标出仍在重算/重算失败的批次）。
- `tests/`：完整流程、规则计算、失败场景与复核批次测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 复核批次与服务台账

- `POST /api/service-records`：导入服务记录 `{"data":{"student_id","month":"YYYY-MM","minutes","provider","import_key"}}`。同一学生、月份、`import_key` 重复导入不重复计数，返回中 `duplicate` 标识重复导入。
- `GET /api/service-records?student_id=&month=`：服务台账查询。
- `POST /api/batches`：按学生+月份提交复核批次，提交时固定计划依据（计划版本、同意范围、服务分钟数）。
- `GET /api/batches?state=&stale=1` / `GET /api/batches/{id}`：批次列表与详情（含批次条目和台账明细）。
- `POST /api/batches/{id}/retry`：重算仍在重算的月份；重算失败保留上一版结论与错误信息，成功后清除。
- `POST /api/batches/{id}/settle`：管理员结算未结月份（已结算月份结论不再被改动）。
- `POST /api/admin/backfill-batches`：管理员为旧数据缺批次号的**已完成月份**回填批次，幂等；服务启动时以系统管理员身份自动执行一次。

语义约定：

- 监护人同意或支持计划修订后，**已结算（历史月份）结论保留**，仅未结算月份标记失效并按当前计划重算。
- 同一学生同月只保留一个有效批次：两人并发提交时后到提交并入既有批次（批次条目记录 `submit_merged`）。
- 批次号形如 `RB-{student_id}-{month}`；历史月份提交直接结算，当前/未来月份在重算成功后仍为 `recalculating`，可显式结算。
- `GET /api/stats` 在原有记录状态统计上增加 `batch_total`、`batch_settled`、`batch_recalculating`、`batch_recompute_errors`、`batch_stale`，用于标出仍在重算的结果。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
