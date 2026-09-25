# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `GET /api/entities/<id>/effective-calibrations`：按时间顺序读取仪器历次生效校准的审批快照。

### 校准审批生效

计量管理员（`authorizer`）审批通过校准（`calibration` 的 `approve` 动作）时，校准状态、仪器生效校准与审计在同一个数据库事务内完成：

- 仪器写入 `effective_calibration_id`、`effective_calibration_no`（未提供 `calibration_no` 时用校准记录 id）和 `due_at`，仪器版本加一。
- 每次审批在 `calibration_snapshots` 中保留一份不可变快照，并在校准和仪器上各写一条审计。
- 仪器已隔离，或已存在更新的已生效校准（按 `performed_at` 比较）时拒绝审批，返回 `409 Conflict`，仪器、校准与审计均保持不变。

检测结果放行（`result` 的 `release` 动作）按仪器当前生效校准（已审批且 `due_at` 未到期）判断；放行后的结果记录返回所采用的 `calibration_id`、`calibration_no` 和 `due_at`。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
