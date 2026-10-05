# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`GET /api/claims/pending`、`POST /api/claims` 和 `POST /api/directory`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 辖区分账

所有 `/api` 请求都必须携带 `X-Region` 辖区身份头，缺失时返回 401 `region_required`。

- 建单时把调用方辖区写入记录，之后读取（列表、详情、审计、态势汇总）和修改（来源、动作）都落在同一辖区。
- 越权访问其他辖区的记录一律返回与"记录不存在"相同的 404 `item_not_found`，且在挡回前不写任何审计事件，不留痕迹。
- 已有数据缺辖区时按辖区目录（`region_directory`）回填：创建者映射优先，其次运营方组织一致映射；回填由 `initialize()` 自动执行，也可由监管员通过 `POST /api/directory` 登记映射后触发，回填结果写入 `region_backfilled` 审计事件。
- 认不出归属的记录保持挂起（`region` 为空、`suspended=true`），对值班室不可见也不可操作，仅监管员可通过 `GET /api/claims/pending` 查看。
- 监管员通过 `POST /api/claims`（`{"item_ids": [...]}`）把挂起记录认领进自己的辖区，认领写入 `region_claimed` 审计事件。认领以 `UPDATE ... WHERE region IS NULL` 在单事务内原子完成：两名监管员同时认领同一批记录时只有先到者成立，后到者收到 409 `already_claimed`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、辖区隔离、越权挡回、归属回填、挂起认领和并发认领。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
