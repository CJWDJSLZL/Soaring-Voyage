# 翱翔启航（Soaring Voyage）

面向小学数学作业的 AI 批改平台。Phase 1 后端采用 FastAPI、asyncpg、PostgreSQL 16、Redis 与 Qdrant；数据库通过 PostgreSQL RLS 强制学校租户隔离。

## 本地启动

要求：Docker Engine 24+、Docker Compose v2、GNU Make；后端本地开发需要 Python 3.11+。

```bash
cp .env.example .env
# 替换所有 CHANGE_ME；密码如含 @、:、/ 等字符，DATABASE_URL 中需 URL 编码
chmod 600 .env
make up
make ps
curl http://127.0.0.1:8000/health
```

首次启动时 app 使用独立的 `MIGRATION_DATABASE_URL`（数据库管理角色）在 PostgreSQL advisory lock 下顺序执行迁移；运行时 `DATABASE_URL` 使用 init 脚本创建的 `soaring_voyage_app` 非 superuser 角色。各端口仅绑定宿主机回环地址。

> **MVP 适配器边界：** 当前可执行 API 的业务 Repository 和 SSE 票据仍是进程内开发适配器，演示账户及共享密码 `Test@1234` 也只在 `APP_ENV=test/development` 可用。`/health` 因此明确返回 `degraded`，不会声称 PostgreSQL、Redis 或 Qdrant 已接入。选择其他 `APP_ENV` 会 fail-fast（同时要求至少 32 字符的非示例 `SECRET_KEY`），不得将当前镜像作为生产数据面部署。生产前必须实现 asyncpg Repository、Redis 单次票据/事件适配器及真实依赖探测；多进程/多副本下内存票据不共享且重启即丢失。

## 开发与质量门禁

```bash
make install       # 创建 .venv 并安装后端及开发依赖
make lint          # ruff + format check + strict mypy
make test          # 全部 pytest
make test-db       # asyncpg / tenant RLS 契约测试
make harness       # 固定 180 条 MockLLM 用例，准确率必须 >= 94%
make migrate       # 手动应用待执行迁移（需要 DATABASE_URL）
```

GitHub Actions 同样执行 Ruff、Mypy、PostgreSQL 迁移、Pytest 和 Harness 94% 门禁，并上传覆盖率与 Harness 报告。

## 数据库与租户隔离

- 初始迁移创建 tenants、users、classes、class_students、problems、assignments、assignment_classes、assignment_problems、submissions、submission_answers、grading_results、human_review_queue、student_error_history、harness_runs、jobs、audit_logs。
- 所有租户业务表携带 `tenant_id`；复合外键阻止跨租户关联。
- `app.db.session.tenant_context()` 绑定可信认证身份；`tenant_conn()` 开启事务，并通过参数化 `set_config(..., true)` 设置 tenant/user/role。上下文在事务结束时自动清除。
- 租户表启用 `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`。公共题库记录（`problems.tenant_id IS NULL`）只读共享。
- `audit_logs` 只允许追加，只有 sysadmin 上下文可读取；触发器额外拒绝 UPDATE/DELETE。
- 答错的最终批改结果由触发器写入 `student_error_history`，同时保存题目快照。

业务 Repository 查询仍须显式带 `tenant_id`；RLS 是第二道防线，不能替代应用层过滤。

## 生产 Nginx

`nginx/nginx.conf` 提供：

- HTTP 到 HTTPS 重定向、TLS 1.2/1.3 与安全响应头；
- `/api/` 反向代理；
- `/api/v1/submissions/{id}/events` SSE 长连接（关闭 buffering/cache、1 小时 timeout、禁记含一次性票据的访问日志）；
- `/health` 无访问日志代理。

将证书分别放到 `/etc/nginx/ssl/mathgrader.crt` 和 `/etc/nginx/ssl/mathgrader.key`，上线前执行 `nginx -t`。自签证书仅用于受控内网；有内部 CA 时应使用 CA 签发证书。

## 运维最小清单

```bash
make ps
make logs
# 数据库备份（示例）
docker compose --env-file .env exec -T postgres \
  pg_dump -U soaring_voyage -Fc soaring_voyage > backup/db_$(date +%Y%m%d_%H%M%S).dump
# 完整性检查
pg_restore --list backup/db_*.dump >/dev/null
```

- 每日备份 PostgreSQL，异地/离线保存，保留至少 30 天；每周实际恢复到临时数据库验证。
- 每周运行 Harness，低于 94% 禁止发布。
- 监控 `/health`、HITL 队列、磁盘、PostgreSQL 连接数、Redis 内存与批改 P95。
- 升级前备份并审阅迁移 SQL；部署后执行健康检查和冒烟测试。
- `.env` 必须权限 600，绝不可提交密钥。审计日志按合规要求至少保留一年。

完整设计和 SOP 见 `docs/database-design.md` 与 `docs/deployment-operations.md`。
