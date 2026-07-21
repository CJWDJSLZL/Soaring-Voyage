# 部署与运维方案

**项目名称**：翱翔启航  
**文档版本**：v2.0  
**创建日期**：2026-07-19  
**最后更新**：2026-07-20  
**状态**：待确认

---

## 一、概述

### 1.1 部署目标

本文档描述单校私有化部署的完整操作手册，目标是：
- 从零开始 **2 小时内**完成首次部署并通过冒烟测试
- 日常运维操作有明确的 SOP（标准操作程序）
- 故障时能在 **30 分钟内**恢复服务
- 版本升级有完整的回滚方案

### 1.2 部署架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    学校内网服务器（Ubuntu 22.04）                  │
│                                                                  │
│  ┌──────────────┐    ┌────────────────────────────────────────┐  │
│  │  Nginx       │    │         Docker Compose                  │  │
│  │  :443 (TLS)  │───▶│  app(:8000)                            │  │
│  │  :80 (→443)  │    │  postgres(:5432)                       │  │
│  └──────────────┘    │  qdrant(:6333)                         │  │
│                      │  redis(:6379)                          │  │
│  /opt/math-grader/   └────────────────────────────────────────┘  │
│  ├── .env              ← 敏感配置（chmod 600）                    │
│  ├── docker-compose.yml                                          │
│  ├── scripts/          ← 运维脚本                                │
│  ├── backup/           ← 数据库备份（定时）                       │
│  └── logs/             ← 应用日志软链接                          │
│                                                                  │
│  /var/log/mathgrader/  ← 结构化日志目录                          │
└─────────────────────────────────────────────────────────────────┘
                    │ HTTPS（仅 LLM API 调用）
                    ▼
         DeepSeek API / Qianwen API（外网）
```

### 1.3 三套环境说明

| 环境 | 用途 | LLM 配置 | 数据库 | 关键差异 |
|------|------|---------|--------|---------|
| **开发环境** | 开发调试 | MockLLM（离线） | SQLite 或本地 PG | `DEBUG=true`；无 HTTPS |
| **测试环境** | 集成测试、Harness | 真实 LLM（低频调用） | 独立 PG 实例 | `DEBUG=false`；独立域名 |
| **生产环境** | 学校正式使用 | 真实 LLM（正常使用） | 独立 PG 实例 + 备份 | 完整安全配置 |

---

## 二、服务器准备

### 2.1 硬件要求

| 资源 | 最低要求 | 推荐配置 | 说明 |
|------|---------|---------|------|
| CPU | 8 核 | 16 核 | LLM 调用为 IO 密集，多核提升并发 |
| 内存 | 16 GB | 32 GB | PostgreSQL 4GB + Qdrant 2GB + App 4GB + OS 留量 |
| 存储 | 200 GB SSD | 500 GB SSD | 日志+备份+向量数据库持续增长 |
| 网络 | 100Mbps（外网可用） | 千兆内网 | 需访问 DeepSeek/Qianwen API |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS | 长期支持，2027年前有安全更新 |

### 2.2 依赖软件安装

```bash
# ── 第一步：更新系统 ───────────────────────────────────────────
sudo apt update && sudo apt upgrade -y

# ── 第二步：安装 Docker（官方脚本）────────────────────────────
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER     # 允许当前用户运行 Docker 无需 sudo
newgrp docker                      # 立即生效

# ── 第三步：安装 Docker Compose 插件 ──────────────────────────
sudo apt install docker-compose-plugin -y
docker compose version             # 验证：输出 Docker Compose version v2.x.x

# ── 第四步：安装 Nginx ─────────────────────────────────────────
sudo apt install nginx -y
nginx -v                           # 验证：输出 nginx/1.24.x

# ── 第五步：安装运维工具 ───────────────────────────────────────
sudo apt install -y htop iotop nethogs curl wget git vim jq logrotate

# ── 第六步：验证所有依赖 ───────────────────────────────────────
echo "Docker:  $(docker --version)"
echo "Compose: $(docker compose version)"
echo "Nginx:   $(nginx -v 2>&1)"
echo "JQ:      $(jq --version)"
```

### 2.3 系统安全基线

```bash
# 防火墙配置（仅开放必要端口）
sudo ufw enable
sudo ufw allow ssh      # 22（SSH 管理）
sudo ufw allow 80/tcp   # HTTP（重定向至 443）
sudo ufw allow 443/tcp  # HTTPS（应用访问）
sudo ufw status         # 验证规则

# 禁止 root 远程 SSH 登录
sudo sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl reload sshd

# 配置 fail2ban（SSH 暴力破解防护）
sudo apt install fail2ban -y
sudo systemctl enable fail2ban
```

---

## 三、首次部署步骤

### 3.1 获取代码与创建目录

```bash
# 克隆项目代码（或解压发布包）
sudo git clone https://git.school-internal.com/math-grader.git /opt/math-grader
cd /opt/math-grader

# 创建必要目录
mkdir -p /opt/math-grader/{backup,logs}
mkdir -p /var/log/mathgrader
chmod 750 /opt/math-grader/{backup,logs}
chmod 755 /var/log/mathgrader

# 创建日志软链接
ln -sf /var/log/mathgrader /opt/math-grader/logs/app

# 设置目录归属（运行 Docker 的非 root 用户）
sudo chown -R $USER:$USER /opt/math-grader
```

### 3.2 完整 docker-compose.yml

```yaml
# /opt/math-grader/docker-compose.yml
version: "3.9"

services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
      args:
        - APP_ENV=production
    image: mathgrader-app:latest
    container_name: mathgrader-app
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"    # 仅本机访问，通过 Nginx 反代
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
      - QDRANT_URL=http://qdrant:6333
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
      - QIANWEN_API_KEY=${QIANWEN_API_KEY}
      - SECRET_KEY=${SECRET_KEY}
      - DEBUG=false
      - LOG_LEVEL=INFO
      - API_PREFIX=/api/v1
      - CONFIDENCE_THRESHOLD=0.85
      - MAX_HINT_LEVEL=3
      - MAX_LLM_RETRIES=3
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      qdrant:
        condition: service_started
    volumes:
      - /var/log/mathgrader:/app/logs
      - ./prompts:/app/prompts:ro      # Prompt 模板只读挂载
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s               # 等待服务启动+Prompt 预热完成
    logging:
      driver: "local"
      options:
        max-size: "100m"
        max-file: "5"

  postgres:
    image: postgres:16-alpine
    container_name: mathgrader-postgres
    restart: unless-stopped
    environment:
      - POSTGRES_DB=mathgrader
      - POSTGRES_USER=mathgrader
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_INITDB_ARGS=--encoding=UTF8 --lc-collate=zh_CN.utf8 --lc-ctype=zh_CN.utf8
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init-db.sql:/docker-entrypoint-initdb.d/init.sql:ro
    ports:
      - "127.0.0.1:5432:5432"         # 仅本机访问
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mathgrader -d mathgrader"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 30s
    shm_size: 256mb                    # 提升 PostgreSQL 排序性能

  qdrant:
    image: qdrant/qdrant:v1.11.0
    container_name: mathgrader-qdrant
    restart: unless-stopped
    volumes:
      - qdrant_data:/qdrant/storage
      - ./config/qdrant.yaml:/qdrant/config/production.yaml:ro
    ports:
      - "127.0.0.1:6333:6333"         # REST API（仅本机）
      - "127.0.0.1:6334:6334"         # gRPC API（仅本机）

  redis:
    image: redis:7-alpine
    container_name: mathgrader-redis
    restart: unless-stopped
    command: >
      redis-server
      --requirepass ${REDIS_PASSWORD}
      --maxmemory 512mb
      --maxmemory-policy allkeys-lru
      --bind 127.0.0.1
      --protected-mode yes
      --rename-command FLUSHALL ""
      --rename-command FLUSHDB ""
      --rename-command CONFIG ""
    volumes:
      - redis_data:/data
    ports:
      - "127.0.0.1:6379:6379"         # 仅本机访问
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3

volumes:
  postgres_data:
    name: math-grader_postgres_data
  qdrant_data:
    name: math-grader_qdrant_data
  redis_data:
    name: math-grader_redis_data

networks:
  default:
    name: mathgrader-net
```

### 3.3 配置环境变量

```bash
# 复制示例配置
cp .env.example .env
chmod 600 .env      # 重要：限制文件权限，仅所有者可读

# 编辑配置文件
vim .env
```

**完整 `.env` 配置**：
```ini
# ── LLM API Keys ────────────────────────────────────────────────
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
QIANWEN_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── 数据库 ──────────────────────────────────────────────────────
DATABASE_URL=postgresql://mathgrader:${DB_PASSWORD}@postgres:5432/mathgrader
DB_PASSWORD=请修改为强密码_至少16位含字母数字特殊字符

# ── Redis ───────────────────────────────────────────────────────
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
REDIS_PASSWORD=请修改为强密码

# ── 安全 ────────────────────────────────────────────────────────
SECRET_KEY=使用命令生成：openssl rand -hex 32

# ── 应用配置 ────────────────────────────────────────────────────
DEBUG=false
LOG_LEVEL=INFO
API_PREFIX=/api/v1
ALLOWED_ORIGINS=https://school-grader.internal

# ── 批改参数 ────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD=0.85
MAX_HINT_LEVEL=3
MAX_LLM_RETRIES=3
LLM_TIMEOUT_SECONDS=30

# ── 连接池 ──────────────────────────────────────────────────────
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_RECYCLE=3600
```

```bash
# 生成随机密钥（必须执行！不要使用示例值）
echo "SECRET_KEY=$(openssl rand -hex 32)"
echo "DB_PASSWORD=$(openssl rand -base64 24 | tr -d '\/+=\n')"
echo "REDIS_PASSWORD=$(openssl rand -base64 16 | tr -d '\/+=\n')"
# 将以上输出复制到 .env 对应字段
```

### 3.4 Nginx 配置

```bash
# 生成 SSL 证书（内网自签名）
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/mathgrader.key \
  -out /etc/nginx/ssl/mathgrader.crt \
  -subj "/CN=school-grader.internal/O=XX小学/C=CN"

sudo chmod 600 /etc/nginx/ssl/mathgrader.key
sudo chmod 644 /etc/nginx/ssl/mathgrader.crt
```

**完整 Nginx 配置**（`/etc/nginx/sites-available/mathgrader`）：

```nginx
# HTTP → HTTPS 重定向
server {
    listen 80;
    server_name school-grader.internal _;
    return 301 https://$host$request_uri;
}

# 主配置（HTTPS）
server {
    listen 443 ssl http2;
    server_name school-grader.internal _;

    # SSL 证书
    ssl_certificate     /etc/nginx/ssl/mathgrader.crt;
    ssl_certificate_key /etc/nginx/ssl/mathgrader.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers on;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    # 安全响应头
    add_header X-Content-Type-Options    "nosniff" always;
    add_header X-Frame-Options           "DENY" always;
    add_header X-XSS-Protection          "1; mode=block" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header Content-Security-Policy   "default-src 'self'; style-src 'self' 'unsafe-inline';" always;

    # 请求体大小限制
    client_max_body_size 10m;
    client_body_timeout 60s;

    # 隐藏版本信息
    server_tokens off;

    # API 反向代理
    location /api/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;          # LLM 批改最长 30s，留余量
        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
        proxy_buffering off;             # 关闭缓冲，实时传输批改结果

        # 响应头安全（覆盖后端可能泄露的）
        proxy_hide_header X-Powered-By;
        proxy_hide_header Server;
    }

    # 健康检查（运维监控用，不记录访问日志）
    location /health {
        proxy_pass http://127.0.0.1:8000/health;
        access_log off;
    }

    # 前端静态资源
    location / {
        root /opt/math-grader/frontend/dist;
        try_files $uri $uri/ /index.html;
        expires 1h;
        add_header Cache-Control "public, max-age=3600";
    }

    # 访问日志
    access_log /var/log/mathgrader/access.log combined;
    error_log  /var/log/mathgrader/nginx-error.log warn;
}
```

```bash
# 启用配置并重启 Nginx
sudo ln -sf /etc/nginx/sites-available/mathgrader /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # 移除默认配置
sudo nginx -t                                  # 验证配置语法
sudo systemctl restart nginx
sudo systemctl enable nginx
```

### 3.5 启动服务

```bash
cd /opt/math-grader

# 构建并启动所有服务（第一次约 5-10 分钟，需下载 Docker 镜像）
docker compose up -d --build

# 查看启动状态（等待所有服务 healthy）
docker compose ps
```

**预期输出**：
```
NAME                    STATUS                   PORTS
mathgrader-postgres     Up (healthy)             127.0.0.1:5432->5432/tcp
mathgrader-redis        Up (healthy)             127.0.0.1:6379->6379/tcp
mathgrader-qdrant       Up                       127.0.0.1:6333->6333/tcp
mathgrader-app          Up (healthy)             127.0.0.1:8000->8000/tcp
```

若某服务不健康，查看日志排查：`docker compose logs --tail 50 postgres`

### 3.6 数据库初始化

```bash
# 执行数据库迁移（创建所有表、索引、触发器）
docker compose exec app alembic upgrade head

# 验证表创建
docker compose exec postgres psql -U mathgrader -d mathgrader -c "\dt" | grep -c "table"
# 期望输出：12（12张主要数据表）

# 验证索引创建
docker compose exec postgres psql -U mathgrader -d mathgrader -c "\di" | wc -l

# 创建初始租户和管理员账户
docker compose exec app python3 scripts/create_admin.py \
  --tenant-name "XX小学" \
  --tenant-code "BJ-XX-001" \
  --curriculum "人教版" \
  --admin-username "admin" \
  --admin-password "Admin@2026!" \
  --sysadmin-username "sysadmin" \
  --sysadmin-password "Sys@Admin2026!"

# 初始化 Qdrant 向量集合
docker compose exec app python3 scripts/init_qdrant.py

echo "数据库初始化完成"
```

### 3.7 冒烟测试

```bash
# 测试 1: 健康检查（所有服务应为 ok）
curl -sk https://localhost/health | jq .
# 期望：{"status":"ok","services":{"database":"ok","qdrant":"ok","redis":"ok","deepseek":"ok"}}

# 测试 2: 管理员登录
TOKEN=$(curl -sk -X POST https://localhost/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"Admin@2026!"}' | jq -r '.data.access_token')
echo "Token 获取成功：${TOKEN:0:20}..."

# 测试 3: 全校统计（验证数据库查询）
curl -sk https://localhost/api/v1/admin/stats/overview \
  -H "Authorization: Bearer $TOKEN" | jq .

# 测试 4: 运行 Harness（MockLLM 模式，验证 AI 管道）
docker compose exec app python3 scripts/run_harness_ci.py --mock --fail-below 0.94
# 期望：Harness PASSED，准确率 ≥ 94%

# 测试 5: 验证日志输出正常
docker compose logs app --tail 20 | grep -v "ERROR"
echo "冒烟测试完成！"
```

**全部通过后，首次部署完成（目标 2 小时内）。**

---

## 四、日常运维操作

### 4.1 常用命令速查

```bash
# ── 服务管理 ────────────────────────────────────────────────────
docker compose start                # 启动所有服务
docker compose stop                 # 停止所有服务（数据不丢失）
docker compose restart app          # 仅重启 App（不影响数据库/Redis）
docker compose logs -f app          # 实时查看 App 日志（Ctrl+C 退出）
docker compose logs -f --tail 100   # 查看所有服务最近 100 行日志
docker compose ps                   # 查看服务状态
docker stats --no-stream            # 查看资源占用（一次性）
docker stats                        # 实时资源占用（Ctrl+C 退出）

# ── 数据库操作 ──────────────────────────────────────────────────
docker compose exec postgres psql -U mathgrader -d mathgrader
# 进入 psql 交互终端
# 常用 psql 命令：\dt（列表）\q（退出）\d tablename（表结构）

docker compose exec app alembic upgrade head        # 执行新迁移
docker compose exec app alembic current             # 查看当前版本
docker compose exec app alembic history --verbose   # 迁移历史

# 查看数据库大小
docker compose exec postgres psql -U mathgrader -d mathgrader \
  -c "SELECT pg_size_pretty(pg_database_size('mathgrader'));"

# 查看慢查询（> 1秒）
docker compose exec postgres psql -U mathgrader -d mathgrader \
  -c "SELECT query, mean_exec_time, calls FROM pg_stat_statements WHERE mean_exec_time > 1000 ORDER BY mean_exec_time DESC LIMIT 10;"

# ── 手动备份 ────────────────────────────────────────────────────
bash /opt/math-grader/scripts/backup.sh
ls -lh /opt/math-grader/backup/        # 查看备份文件

# ── Prompt 预热（修改 Prompt 后重启前执行）──────────────────────
docker compose exec app python3 scripts/warmup_prompts.py

# ── 清理磁盘 ────────────────────────────────────────────────────
docker system prune -f              # 清理未使用的镜像/容器/网络（不删数据卷）
find /var/log/mathgrader -name "*.gz" -mtime +30 -delete  # 清理30天前日志
```

### 4.2 日志管理

**日志目录结构**：
```
/var/log/mathgrader/
├── app.log           ← 应用主日志（structlog JSON，每行一条事件）
├── access.log        ← Nginx 访问日志（combined 格式）
├── nginx-error.log   ← Nginx 错误日志
├── audit.log         ← 敏感操作审计日志（由 auditd 分离）
├── monitor.log       ← 监控脚本输出
└── backup.log        ← 备份脚本输出
```

**日志轮转配置**（`/etc/logrotate.d/mathgrader`）：
```
/var/log/mathgrader/*.log {
    daily                   # 每天轮转
    rotate 30               # 保留 30 份
    compress                # gzip 压缩
    delaycompress           # 延迟一天压缩（保留最新未压缩版本）
    missingok               # 文件不存在时不报错
    notifempty              # 空文件不轮转
    su root root            # 以 root 身份运行（解决权限问题）
    postrotate
        # 通知 Nginx 重载日志文件句柄
        nginx -s reopen 2>/dev/null || true
        # 通知 App 容器重载日志
        docker compose -f /opt/math-grader/docker-compose.yml \
            kill -s USR1 mathgrader-app 2>/dev/null || true
    endscript
}
```

**日志查询示例**：
```bash
# 查找某时间段的批改失败记录
jq 'select(.level=="error" and .event_type=="grading")' /var/log/mathgrader/app.log | head -20

# 查找某学生（UUID）的所有操作（注意：日志中只有UUID，无姓名）
jq 'select(.student_id=="550e8400-e29b-41d4-a716-446655440000")' /var/log/mathgrader/app.log

# 查找 LLM 调用延迟超过 5 秒的记录
jq 'select(.event=="llm_call" and .duration_ms > 5000)' /var/log/mathgrader/app.log

# 查找某 trace_id 的完整请求链路
jq 'select(.trace_id=="req-550e8400-e29b")' /var/log/mathgrader/app.log

# 统计今日 LLM 调用次数和成功率
jq 'select(.event=="llm_call")' /var/log/mathgrader/app.log | \
  jq -s 'group_by(.success) | map({success: .[0].success, count: length})'

# 查找降级批改（规则 fallback）记录
jq 'select(.grading_source=="rule_fallback")' /var/log/mathgrader/app.log | wc -l
```

---

## 五、监控与告警

### 5.1 监控指标体系

| 指标 | 类型 | 告警阈值 | 告警方式 |
|------|------|---------|---------|
| 服务健康检查 | 可用性 | /health 连续 2 次失败 | 监控脚本 + 日志 |
| 批改响应时间 P95 | 性能 | > 5 秒 | 日志分析 |
| LLM 调用失败率 | 可靠性 | > 10% / 5分钟窗口 | 日志分析 |
| 规则降级率 | 质量 | > 5% / 小时 | 日志分析 |
| HITL 队列积压 | 业务 | > 50 条待处理 | DB 查询 |
| 批改准确率（Harness） | 质量 | < 94%（每周检查并阻断发布） | Harness 报告 |
| CPU 使用率 | 资源 | > 85% 持续 5 分钟 | top/htop |
| 内存使用率 | 资源 | > 90% | free -h |
| 磁盘使用率 | 资源 | > 80% | df -h |
| PostgreSQL 连接数 | 资源 | > 80 个 | pg_stat_activity |
| Redis 内存使用 | 资源 | > 80% | redis-cli info memory |

### 5.2 监控脚本（轻量方案）

```bash
#!/bin/bash
# /opt/math-grader/scripts/monitor.sh
# 每分钟由 cron 执行

LOG="/var/log/mathgrader/monitor.log"
COMPOSE_FILE="/opt/math-grader/docker-compose.yml"
THRESHOLD_DISK=80
THRESHOLD_MEM=90
THRESHOLD_HITL=50

log_alert() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ALERT: $1" | tee -a $LOG
    # 可扩展：发送邮件/企微通知
    # send_alert "$1"
}

# 健康检查
check_health() {
    STATUS=$(curl -sk --max-time 5 https://localhost/health 2>/dev/null | jq -r '.status' 2>/dev/null)
    if [ "$STATUS" != "ok" ] && [ "$STATUS" != "degraded" ]; then
        log_alert "Health check failed! Status: '${STATUS}'. Run: docker compose logs app --tail 50"
    fi
}

# 磁盘检查
check_disk() {
    USAGE=$(df /opt/math-grader | awk 'NR==2 {print $5}' | tr -d '%')
    if [ "${USAGE:-0}" -gt "$THRESHOLD_DISK" ]; then
        AVAIL=$(df -h /opt/math-grader | awk 'NR==2 {print $4}')
        log_alert "Disk usage ${USAGE}% (available: ${AVAIL}). Clean: bash /opt/math-grader/scripts/cleanup.sh"
    fi
}

# 内存检查
check_memory() {
    MEM_PERCENT=$(free | awk 'NR==2 {printf "%.0f", $3/$2*100}')
    if [ "${MEM_PERCENT:-0}" -gt "$THRESHOLD_MEM" ]; then
        log_alert "Memory usage ${MEM_PERCENT}%. Top consumers: $(docker stats --no-stream --format '{{.Name}}: {{.MemPerc}}' | sort -t: -k2 -rn | head -3)"
    fi
}

# Docker 服务状态
check_containers() {
    UNHEALTHY=$(docker compose -f $COMPOSE_FILE ps --filter status=unhealthy --filter status=exited 2>/dev/null | grep -v "NAME" | wc -l)
    if [ "${UNHEALTHY:-0}" -gt 0 ]; then
        log_alert "Unhealthy/exited containers detected! Run: docker compose ps"
    fi
}

# HITL 队列积压
check_hitl_queue() {
    COUNT=$(docker compose -f $COMPOSE_FILE exec -T postgres \
        psql -U mathgrader -t -c "SELECT COUNT(*) FROM human_review_queue WHERE status='pending';" 2>/dev/null | tr -d ' \n')
    if [ "${COUNT:-0}" -gt "$THRESHOLD_HITL" ]; then
        log_alert "HITL queue backlog: ${COUNT} pending reviews. Teachers should review ASAP."
    fi
}

# LLM API 余额检查（简单方式：调用一次验证 key 有效性）
check_llm_availability() {
    HTTP_CODE=$(curl -sw "%{http_code}" -o /dev/null \
        -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
        "https://api.deepseek.com/v1/models" 2>/dev/null)
    if [ "$HTTP_CODE" != "200" ]; then
        log_alert "DeepSeek API check failed (HTTP $HTTP_CODE). Verify API key and balance."
    fi
}

# 执行所有检查
check_health
check_disk
check_memory
check_containers
check_hitl_queue
# check_llm_availability  # 每分钟检查会消耗 API 调用，可改为每小时一次
```

```bash
# 加入 crontab（每分钟监控）
crontab -e
# 添加以下行：
* * * * * /opt/math-grader/scripts/monitor.sh
0 * * * * /opt/math-grader/scripts/monitor.sh --check-llm  # 每小时检查 LLM
```

### 5.3 告警处理 SOP

#### SOP-01：服务不可用（health 检查失败）

```
触发：/health 连续 2 次失败（或 docker compose ps 显示 unhealthy）
─────────────────────────────────────────────────────────────────
1. 确认故障类型（30秒内）：
   docker compose ps                              # 哪个服务不健康？
   docker compose logs app --tail 50             # App 错误信息
   curl -sk https://localhost/health | jq .       # 哪个下游服务失败

2. 常见原因及处理（2-5分钟）：
   a. App 进程崩溃（OOM 或代码异常）：
      docker compose restart app
   b. PostgreSQL 连接失败：
      docker compose restart postgres
      sleep 10 && docker compose restart app
   c. 磁盘满（写日志失败）：
      bash /opt/math-grader/scripts/cleanup.sh
      docker compose restart app
   d. 内存不足（OOM）：
      docker stats                               # 确认哪个容器占用过多
      docker compose restart <container>

3. 验证恢复：
   curl -sk https://localhost/health | jq .status
   # 应输出 "ok"

4. 若 5 分钟内无法恢复：
   联系开发团队（企微群或电话），附上日志：
   docker compose logs --tail 100 > /tmp/emergency_logs.txt
```

#### SOP-02：LLM 服务不可用

```
触发：/health 显示 deepseek: unavailable 或 LLM 调用失败率 > 10%
─────────────────────────────────────────────────────────────────
1. 确认 DeepSeek 问题：
   curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
     https://api.deepseek.com/v1/models
   → HTTP 200：密钥有效，可能是网络问题或限速
   → HTTP 401：API Key 过期或无效，需充值/更新
   → 无响应：网络连通性问题，检查防火墙

2. 切换到千问备用（在 .env 中）：
   # 临时注释 DEEPSEEK_API_KEY，系统自动 fallback 到 Qianwen
   docker compose restart app

3. 若两者均不可用：
   系统自动降级为规则匹配批改（批改继续，准确率降低）
   HITL 队列会显著增加
   通知教师：部分批改结果需要人工审核

4. LLM 恢复后：
   docker compose restart app    # 重启服务，清除熔断器状态
```

#### SOP-03：磁盘使用率 > 80%

```
触发：df -h 显示 > 80%
─────────────────────────────────────────────────────────────────
1. 快速释放（1-2分钟）：
   # 清理 Docker 悬空镜像和缓存
   docker system prune -f

   # 清理超期日志
   find /var/log/mathgrader -name "*.gz" -mtime +30 -delete

   # 清理超期备份（保留最近 14 天）
   find /opt/math-grader/backup -name "*.dump" -mtime +14 -delete

2. 确认清理效果：
   df -h /opt/math-grader

3. 若仍 > 75%：
   # 检查最大文件
   du -sh /var/log/mathgrader/* | sort -rh | head -5
   du -sh /opt/math-grader/backup/* | sort -rh | head -5

4. 若无法通过清理解决，申请扩容磁盘（联系 IT）
```

---

## 六、备份与恢复

### 6.1 自动备份脚本

```bash
#!/bin/bash
# /opt/math-grader/scripts/backup.sh
set -euo pipefail

BACKUP_DIR="/opt/math-grader/backup"
DATE=$(date +%Y%m%d_%H%M%S)
LOG="/var/log/mathgrader/backup.log"
COMPOSE_FILE="/opt/math-grader/docker-compose.yml"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始备份..." >> $LOG

# PostgreSQL 全量备份
echo "  备份 PostgreSQL..." >> $LOG
docker compose -f $COMPOSE_FILE exec -T postgres \
    pg_dump -U mathgrader -Fc --no-password mathgrader \
    > $BACKUP_DIR/db_${DATE}.dump

# 验证备份文件完整性
if ! pg_restore --list $BACKUP_DIR/db_${DATE}.dump > /dev/null 2>&1; then
    echo "[ERROR] 备份文件验证失败！" >> $LOG
    rm -f $BACKUP_DIR/db_${DATE}.dump
    exit 1
fi

SIZE=$(du -sh $BACKUP_DIR/db_${DATE}.dump | cut -f1)
echo "  PostgreSQL 备份完成：db_${DATE}.dump (${SIZE})" >> $LOG

# Qdrant 向量数据备份（仅当 Qdrant 数据量较大时有价值）
echo "  备份 Qdrant..." >> $LOG
QDRANT_VOLUME=$(docker volume ls -q | grep qdrant_data)
if [ -n "$QDRANT_VOLUME" ]; then
    docker run --rm -v ${QDRANT_VOLUME}:/data -v $BACKUP_DIR:/backup alpine \
        tar -czf /backup/qdrant_${DATE}.tar.gz /data 2>/dev/null
    echo "  Qdrant 备份完成：qdrant_${DATE}.tar.gz" >> $LOG
fi

# 清理超期备份（保留 30 天）
find $BACKUP_DIR -name "db_*.dump"       -mtime +30 -delete
find $BACKUP_DIR -name "qdrant_*.tar.gz" -mtime +30 -delete

BACKUP_COUNT=$(ls $BACKUP_DIR/db_*.dump 2>/dev/null | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份完成。当前保留 ${BACKUP_COUNT} 份备份。" >> $LOG
```

```bash
# crontab：每天凌晨 2:00 自动备份
crontab -e
# 添加：
0 2 * * * /opt/math-grader/scripts/backup.sh >> /var/log/mathgrader/backup.log 2>&1
```

### 6.2 备份验证（每周）

```bash
#!/bin/bash
# /opt/math-grader/scripts/verify_backup.sh
# 每周日凌晨 3:00 运行

BACKUP_DIR="/opt/math-grader/backup"
LATEST=$(ls -t $BACKUP_DIR/db_*.dump 2>/dev/null | head -1)
LOG="/var/log/mathgrader/backup.log"

if [ -z "$LATEST" ]; then
    echo "[ERROR] 未找到备份文件！" >> $LOG
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 验证备份：$LATEST" >> $LOG

# 恢复到临时数据库
docker compose exec -T postgres psql -U mathgrader -c "DROP DATABASE IF EXISTS mathgrader_verify;" 2>/dev/null || true
docker compose exec -T postgres psql -U mathgrader -c "CREATE DATABASE mathgrader_verify;"
docker compose exec -T postgres pg_restore -U mathgrader -d mathgrader_verify --no-owner < $LATEST

# 验证关键表行数
USERS=$(docker compose exec -T postgres psql -U mathgrader -d mathgrader_verify -t -c "SELECT COUNT(*) FROM users;" | tr -d ' \n')
GRADING=$(docker compose exec -T postgres psql -U mathgrader -d mathgrader_verify -t -c "SELECT COUNT(*) FROM grading_results;" | tr -d ' \n')

echo "  验证结果：users=${USERS}, grading_results=${GRADING}" >> $LOG

# 清理临时库
docker compose exec -T postgres psql -U mathgrader -c "DROP DATABASE mathgrader_verify;"

if [ "${USERS:-0}" -gt 0 ] && [ "${GRADING:-0}" -ge 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份验证成功！" >> $LOG
else
    echo "[ERROR] 备份验证失败！users 表为空，请检查备份完整性。" >> $LOG
    exit 1
fi
```

### 6.3 灾难恢复流程

```bash
# ── 全量恢复（最坏情况：硬件故障后迁移到新服务器）────────────

# 步骤 1：在新服务器完成基础安装和 Docker 部署（参考第三章）

# 步骤 2：停止 App（防止新数据写入污染恢复）
docker compose stop app

# 步骤 3：恢复数据库
BACKUP_FILE=/opt/math-grader/backup/db_20260719_020000.dump

# 删除现有数据库（若存在）
docker compose exec postgres psql -U mathgrader -c "DROP DATABASE IF EXISTS mathgrader;"
docker compose exec postgres psql -U mathgrader -c "CREATE DATABASE mathgrader;"

# 恢复
docker compose exec -T postgres pg_restore \
    -U mathgrader -d mathgrader --no-owner --no-privileges < $BACKUP_FILE

echo "数据库恢复完成，验证表行数..."
docker compose exec postgres psql -U mathgrader -d mathgrader \
    -c "SELECT 'users' as t, COUNT(*) FROM users
        UNION ALL SELECT 'grading_results', COUNT(*) FROM grading_results
        UNION ALL SELECT 'submissions', COUNT(*) FROM submissions;"

# 步骤 4：重启 App
docker compose start app
sleep 15

# 步骤 5：执行冒烟测试
curl -sk https://localhost/health | jq .
bash /opt/math-grader/scripts/smoke_test.sh

echo "恢复完成！RPO = 24小时（丢失最后一次备份后的数据）"
```

---

## 七、版本升级流程

### 7.1 发布前检查清单

```
发布前必须全部完成（逐一勾选）：
──────────────────────────────────────────────────────────────
[ ] Harness MockLLM 通过（准确率 ≥ 94%）
[ ] Harness 真实 LLM 20% 抽样通过（准确率 ≥ 94%，发布前 24h 内执行）
[ ] 单元测试全部通过（pytest tests/unit/）
[ ] 集成测试全部通过（pytest tests/integration/）
[ ] API 测试全部通过（pytest tests/api/）
[ ] bandit 无 HIGH 级安全漏洞
[ ] pip audit 无已知 CVE
[ ] 有数据库迁移时：已 review migration SQL，确认无全表锁/数据丢失
[ ] .env 新增配置项已更新到 .env.example 和部署文档
[ ] 已执行手动备份（bash scripts/backup.sh）
[ ] CHANGELOG.md 已更新
```

### 7.2 升级步骤

```bash
# ── 升级前 ─────────────────────────────────────────────────────
# 手动备份（必做！）
bash /opt/math-grader/scripts/backup.sh
echo "当前版本：$(git rev-parse --short HEAD)"

# ── 升级 ────────────────────────────────────────────────────────
cd /opt/math-grader

# 拉取新代码
git fetch origin
git checkout <新版本 tag 或 commit hash>

# 检查是否有新的环境变量（对比 .env.example）
diff .env .env.example | grep "^>"    # 查看新增的配置项

# 若有新迁移，先执行（在重启 App 之前！）
docker compose exec app alembic upgrade head

# 重建并重启 App（仅重启 App，不重启数据库）
docker compose up -d --build --no-deps app

# 等待新版本启动完成
sleep 15
curl -sk https://localhost/health | jq .

# ── 升级后验证 ────────────────────────────────────────────────
bash /opt/math-grader/scripts/smoke_test.sh
docker compose logs app --tail 30 | grep -v "DEBUG"
echo "升级完成：$(git rev-parse --short HEAD)"
```

### 7.3 回滚流程

```bash
# 若升级后发现严重问题（P0/P1 缺陷），立即执行回滚

# 步骤 1：记录当前版本
CURRENT=$(git rev-parse --short HEAD)
echo "当前有问题的版本：$CURRENT"

# 步骤 2：回滚代码
git log --oneline -5    # 查看最近几次提交，确认回滚目标
git checkout <上一个正常版本 tag 或 hash>

# 步骤 3：回滚数据库迁移（若本次升级有新迁移）
docker compose exec app alembic downgrade -1

# 步骤 4：重启服务
docker compose up -d --build --no-deps app
sleep 15

# 步骤 5：验证
curl -sk https://localhost/health | jq .status   # 应输出 "ok"
bash /opt/math-grader/scripts/smoke_test.sh

echo "回滚完成，当前版本：$(git rev-parse --short HEAD)"
echo "请分析根因后再次尝试升级"
```

---

## 八、性能调优

### 8.1 连接池配置

```python
# config/settings.py 关键参数
db_min_size = 5                         # asyncpg 常驻最小连接数
db_max_size = 25                        # asyncpg 连接池最大连接数
db_command_timeout = 30                 # 单条 SQL 超时（秒）
db_max_inactive_connection_lifetime = 300  # 空闲连接回收周期（秒）

# 说明：
# 50 并发学生 × 5 个 LLM 节点 × 1个 DB 查询 = 250 个并发 DB 操作
# 但实际上很多是 asyncio，不是每个都占一个连接
# max_size=25 个连接；通过压测和 pg_stat_activity 再校准
```

### 8.2 LLM 并发控制

```python
import asyncio

# 全局信号量：限制同时进行的 LLM 调用数
# 防止超出 DeepSeek/Qianwen 的 QPS 限制（通常 20-100 QPS）
LLM_SEMAPHORE = asyncio.Semaphore(15)  # 同时最多 15 个 LLM 请求

# 使用方式
async def call_llm_with_limit(client, messages, **kwargs):
    async with LLM_SEMAPHORE:
        return await client.chat.completions.create(
            messages=messages, **kwargs
        )

# 50 学生同时提交：每人 4-5 个 LLM 调用（Parser+Evaluator+Classifier+Feedback）
# 峰值约 200-250 个 LLM 调用排队
# 信号量 15 个并发：每批 15 个，约 15-20 轮完成
# 总耗时估算：每次 LLM 调用约 0.5s + 网络延迟 ≈ 1s → 20 轮 × 1s = 20s
# 这是最坏情况（所有人完全同时提交），实际分散后 P95 满足 3s
```

### 8.3 Redis 缓存策略

```python
# 高频查询缓存清单

# 1. 班级作业统计（5分钟缓存，每次新提交后失效）
async def get_assignment_stats_cached(tenant_id, assignment_id):
    key = f"mg:stats:{tenant_id}:{assignment_id}"
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)
    stats = await compute_assignment_stats(tenant_id, assignment_id)
    await redis.setex(key, 300, json.dumps(stats))
    return stats

# 失效方法（新提交批改完成后调用）
async def invalidate_stats(tenant_id, assignment_id):
    await redis.delete(f"mg:stats:{tenant_id}:{assignment_id}")

# 2. 学生信息缓存（10分钟，高频读取）
async def get_user_cached(tenant_id, user_id):
    key = f"mg:user:{tenant_id}:{user_id}"
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)
    user = await db_get_user(tenant_id, user_id)
    await redis.setex(key, 600, json.dumps(user.dict()))
    return user

# 3. Prompt 静态前缀（常驻，不过期）
PROMPT_PREFIX_CACHE = {}  # 进程内字典，不使用 Redis（已在内存）
```

---

## 九、容量规划

### 9.1 数据增长估算

| 数据类型 | 每学期增长 | 每年增长 | 说明 |
|---------|----------|---------|------|
| grading_results | ~50,000 行 | ~100,000 行 | 500学生 × 20作业 × 5题 |
| student_error_history | ~25,000 行 | ~50,000 行 | 约50%错误率 |
| submission_answers | ~50,000 行 | ~100,000 行 | 同上 |
| audit_logs | ~10,000 行 | ~20,000 行 | 敏感操作不频繁 |
| **数据库总大小** | ~500 MB | ~1 GB | 含 JSONB agent_trace |
| 应用日志 | ~2 GB | ~4 GB | 结构化 JSON 日志，压缩后 ~500MB |
| 备份文件 | ~500MB × 30份 | — | 全量备份压缩后 |

### 9.2 磁盘预算

```
200 GB SSD 预算分配：
├── OS + Docker 镜像          ~20 GB
├── PostgreSQL 数据（2年）    ~10 GB（含索引）
├── Qdrant 向量数据            ~5 GB（1万道题的向量）
├── Redis 数据（内存持久化）   ~1 GB
├── 备份文件（滚动30天）       ~15 GB
├── 应用日志（滚动30天）       ~10 GB
├── 预留空间（安全余量）       ~30 GB
└── 剩余可用空间               ~109 GB

预警时间线：
  当前使用 ~56GB，磁盘剩余 144GB
  按 1GB/月增速，~12年后达到 80% 告警阈值
  实际 Phase 1（500学生）远低于理论值
```

---

## 十、运维 SLA 承诺

| 指标 | 目标 | 测量方法 |
|------|------|---------|
| 服务可用性 | ≥ 99% | 学期内工作日 7:00-22:00 |
| 故障恢复时间（RTO） | ≤ 30 分钟 | 从发现故障到服务恢复 |
| 数据恢复点（RPO） | ≤ 24 小时 | 最多丢失前一天备份后的数据 |
| 批改响应时间 P95 | ≤ 3 秒 | Locust 性能测试验证 |
| 计划维护窗口 | 寒暑假或节假日 | 提前 3 天通知（管理员公告） |

---

## 十一、日常巡检与交接

### 11.1 每日巡检清单（< 5 分钟）

```bash
#!/bin/bash
# 每日巡检脚本
echo "=== $(date '+%Y-%m-%d') 日常巡检 ==="

# 1. 服务健康
echo -n "健康检查: "
curl -sk https://localhost/health | jq -r '.status'

# 2. 容器状态
echo "容器状态:"
docker compose ps --format "{{.Name}}: {{.Status}}"

# 3. HITL 队列
echo -n "待审核队列: "
docker compose exec -T postgres psql -U mathgrader -t \
    -c "SELECT COUNT(*) FROM human_review_queue WHERE status='pending';" | tr -d ' \n'
echo " 条"

# 4. 昨日错误日志
echo "昨日错误数: $(grep -c '"level":"error"' /var/log/mathgrader/app.log 2>/dev/null || echo 0)"

# 5. 磁盘状态
echo "磁盘使用: $(df -h /opt/math-grader | awk 'NR==2 {print $5}')"

echo "=== 巡检完成 ==="
```

### 11.2 每周维护清单（< 15 分钟）

```
每周维护任务：
[ ] 验证最新备份可恢复（bash scripts/verify_backup.sh）
[ ] 运行 Harness MockLLM（python3 scripts/run_harness_ci.py --mock）确认准确率 ≥ 94%
[ ] 检查 LLM API 余额（登录 DeepSeek/Qianwen 控制台查看）
[ ] 检查依赖漏洞（docker compose exec app pip audit）
[ ] 查看系统补丁（sudo apt list --upgradable 2>/dev/null | wc -l）
[ ] 检查 SSL 证书到期时间（openssl x509 -in /etc/nginx/ssl/mathgrader.crt -noout -dates）
[ ] 检查 Docker 磁盘使用（docker system df）
```

### 11.3 紧急联系方式

| 角色 | 联系方式 | 响应时间 |
|------|---------|---------|
| 系统管理员（首选） | 电话：XXXX | 工作日 30 分钟内 |
| 备用管理员 | 电话：XXXX | 同上 |
| 开发团队支持 | 企微群：XX小学-技术支持 | 工作日 2 小时内 |
| DeepSeek 技术支持 | service@deepseek.com | 24 小时内 |
| Qianwen 技术支持 | 阿里云工单系统 | 24 小时内 |
