# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限和计划版本和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

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
- `GET /api/stats`：状态统计（含`batches`与`recalculating_batches`）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.start_month`可选（默认当前月）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 服务记录与月度复核批次

- `POST /api/records/{id}/services`：导入服务记录 `{"data":{"month":"2026-08","minutes":250,"provider":"SP-3","import_key":"唯一键"}}`。同一记录下`import_key`重复导入返回`duplicate:true`且不重复计数。
- `GET /api/records/{id}/services?month=YYYY-MM`：服务记录列表。
- `POST /api/batches`：提交复核批次 `{"record_id":1,"month":"2026-08"}`。提交时固定依据（计划版本、同意、当月服务记录）；同一学生同月只有一个有效批次，两人同时提交时后者并入，返回`merged:true`并聚合提交人。
- `GET /api/batches`：批次列表，支持`student_id`、`status`、`month`、`recalculating=1`、`limit`。
- `GET /api/batches/{id}`：批次详情（`recalculating`标记是否仍在重算）。
- `GET /api/batches/{id}/versions`：历版依据与结论（重算成功会追加版本，失败时保留上一版）。
- `POST /api/batches/{id}/actions/settle`：管理员结算月份（仅`submitted`可结算）。
- `POST /api/batches/{id}/actions/recalculate`：重试重算该月。
- `GET /api/batches/backfill`：回填状态。
- `POST /api/batches/backfill`：管理员对旧数据缺批次号的完成月份按计划回填（已结算，幂等可重复执行）。

### 结算与重算语义

- 监护人同意（`consent`）或支持计划修订（`amend`）后：**已结算月份结论保留不变，未结算月份自动失效并重算**。
- 批次状态：`submitted`（未结算）→`settled`（已结算）；依据改动后未结算批次进入`recalculating`，重算成功回到`submitted`，失败则保留上一版结论并记录`error`，可继续重试。
- 页面与`/api/stats`会标出仍在重算的批次数量；`recalculating=1`可只看这些月份。
- `service_minutes`表示每月计划分钟数；创建时累计`delivered_minutes`可超过单月额度以覆盖多个完成月份（供回填使用）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及批次去重合并、并发提交、已结算保留/未结算重算、重算失败保留重试、服务记录重复导入和旧数据回填。
