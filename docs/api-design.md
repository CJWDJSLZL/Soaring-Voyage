# API 接口设计方案

**项目名称**：翱翔启航  
**文档版本**：v2.0  
**创建日期**：2026-07-19  
**最后更新**：2026-07-21
**状态**：已确认
**架构基线**：v1.0

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **RESTful 风格** | 资源名词复数、HTTP 动词语义化（GET 查询、POST 创建、PUT 全量更新、PATCH 局部更新、DELETE 删除） |
| **统一响应包装** | 所有响应使用统一 Envelope，包含 code、message、data、trace_id |
| **版本化** | URL 路径前缀 `/api/v1/`，升级时新建 `/api/v2/`，旧版本保留 3 个月过渡期 |
| **幂等性** | PUT/PATCH/DELETE 接口保证幂等；POST 批改接口通过 `(student_id, assignment_id)` 唯一索引防重复提交 |
| **安全优先** | 所有接口强制 JWT 认证（`/health`、`/api/v1/auth/login`、`/docs` 除外）；敏感操作记录审计日志 |
| **中文错误信息** | 面向普通用户的 message 使用中文；面向开发者的 detail 使用英文（含行号和字段路径） |
| **性能 SLA** | 批改接口 P95 ≤ 3s；普通查询接口 P95 ≤ 500ms；明确标注每个接口的预期延迟类别 |

---

## 二、全局约定

### 2.1 Base URL

```
生产环境（内网）：https://<学校内网IP或域名>/api/v1
开发环境：        http://localhost:8000/api/v1
```

### 2.2 认证方式

所有接口（除 `/health`、`/api/v1/auth/login`、`/docs`）**必须**携带：

```http
Authorization: Bearer <JWT Token>
```

**JWT Payload 结构**：
```json
{
  "user_id":    "550e8400-e29b-41d4-a716-446655440000",
  "tenant_id":  "660e8400-e29b-41d4-a716-446655440001",
  "role":       "student",
  "username":   "zhang_san_001",
  "grade_level": 3,
  "iat": 1721234567,
  "exp": 1721320967
}
```

字段说明：
- `grade_level`：仅学生角色有值（1-6），教师/管理员为 `null`
- `exp`：Unix 时间戳，24 小时后过期
- `role` 枚举：`student | teacher | admin | sysadmin`

### 2.3 统一响应格式

**成功响应（单对象）**：
```json
{
  "code": 0,
  "message": "success",
  "data": { "key": "value" },
  "trace_id": "req-550e8400-e29b-41d4-a716-446655440000"
}
```

**成功响应（分页列表）**：
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [ ],
    "total": 100,
    "page": 1,
    "page_size": 20,
    "has_next": true
  },
  "trace_id": "req-..."
}
```

**成功响应（空结果）**：
```json
{
  "code": 0,
  "message": "success",
  "data": null,
  "trace_id": "req-..."
}
```

**错误响应（业务错误）**：
```json
{
  "code": 4004,
  "message": "作业不存在",
  "detail": "Assignment with id=xxx not found (tenant=yyy)",
  "trace_id": "req-550e8400-e29b"
}
```

**错误响应（参数校验失败，Pydantic 422）**：
```json
{
  "code": 4022,
  "message": "请求参数校验失败",
  "detail": [
    {
      "loc": ["body", "answers", 0, "answer_text"],
      "msg": "ensure this value has at most 500 characters",
      "type": "value_error.any_str.max_length"
    }
  ],
  "trace_id": "req-..."
}
```

### 2.4 错误码规范

| 错误码 | HTTP 状态码 | 中文含义 | 触发场景 |
|--------|------------|---------|---------|
| 0 | 200 | 成功 | — |
| 4001 | 401 | Token 无效或过期 | JWT 解码失败、signature error、过期 |
| 4002 | 401 | 未登录 | 未携带 Authorization 头 |
| 4003 | 403 | 权限不足 | 学生访问教师接口、跨班访问 |
| 4004 | 404 | 资源不存在 | ID 对应记录不存在 |
| 4005 | 409 | 资源冲突 | 重复提交、用户名已存在 |
| 4006 | 410 | 作业已截止 | 截止时间后提交 |
| 4007 | 409 | Hint 次数已达上限 | hint_level > 3 |
| 4022 | 422 | 参数校验失败 | Pydantic 校验不通过 |
| 4029 | 429 | 请求频率超限 | 超过限流阈值 |
| 5001 | 500 | 内部服务错误 | 未预期的异常（附 trace_id，用于日志查询） |
| 5002 | 503 | LLM 服务不可用 | 已降级处理，批改仍返回结果（来源=rule_fallback） |
| 5003 | 503 | 数据库连接失败 | PostgreSQL 不可用 |

### 2.5 限流规则

| 接口分组 | 规则 | 存储 | 说明 |
|---------|------|------|------|
| 批改提交 | 每 tenant 200次/分钟（滑动窗口） | Redis | 防单校高峰期 LLM 调用雪崩 |
| Hint 请求 | 每 student 每 problem 4次（业务约束） | DB | 本质是业务规则，非速率限制 |
| 登录接口 | 每 IP 10次/分钟 | Redis | 防暴力破解 |
| 管理接口 | 每 admin 60次/分钟 | Redis | 正常运维操作足够 |
| Harness 触发 | 每 sysadmin 5次/小时 | Redis | 防误触发浪费 LLM Token |

**限流响应（429）**：
```json
{
  "code": 4029,
  "message": "请求过于频繁，请稍后再试",
  "detail": "Rate limit: 200 requests/minute per tenant exceeded",
  "retry_after_seconds": 15,
  "trace_id": "req-..."
}
```

### 2.6 分页约定

```
GET /api/v1/submissions?page=1&page_size=20&order_by=submitted_at&order=desc
```

- 默认：`page=1`，`page_size=20`，`order_by=created_at`，`order=desc`
- `page_size` 最大 100，超出返回 422
- `order_by` 枚举值在各接口文档中说明；传入不支持的字段返回 422

### 2.7 接口性能 SLA

| 接口类型 | P95 延迟目标 | 说明 |
|---------|------------|------|
| AI 批改（含 LLM 调用） | ≤ 3s | 正常负载（≤ 50 并发） |
| Hint 请求（含 LLM 调用） | ≤ 3s | 同上 |
| 数据查询（列表、详情） | ≤ 500ms | 含数据库索引命中 |
| 统计分析（班级分析） | ≤ 1s | 可使用 Redis 缓存 5 分钟 |
| 批量操作（导入、Harness） | 异步返回 `job_id` | 不阻塞请求 |
| 健康检查 | ≤ 100ms | 无数据库查询 |

---

## 三、接口详细设计

### 3.1 认证模块 `/auth`

#### 3.1.1 用户登录

```
POST /api/v1/auth/login
权限：公开
延迟类别：< 500ms
```

**请求体**：
```json
{
  "username": "zhang_san_001",
  "password": "Pass@2026"
}
```

**字段约束**：
- `username`：非空，长度 1-100，不含空格
- `password`：非空，长度 6-128

**成功响应（200）**：
```json
{
  "code": 0,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "bearer",
    "expires_in": 86400,
    "user": {
      "user_id": "uuid",
      "display_name": "张三",
      "username": "zhang_san_001",
      "role": "student",
      "grade_level": 3,
      "tenant_id": "uuid"
    }
  }
}
```

**错误响应**：
- `4001`（401）：`{"message": "用户名或密码错误"}`（不区分哪个错误，防枚举）
- `4001`（401）：`{"message": "账户已锁定，请 12 分钟后重试", "locked_until": "2026-07-19T14:45:00+08:00"}`
- `4029`（429）：`{"message": "登录尝试过于频繁，请 1 分钟后重试"}`

**Curl 示例**：
```bash
curl -X POST https://school-grader.internal/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"zhang_san_001","password":"Pass@2026"}'
```

---

#### 3.1.2 修改密码

```
POST /api/v1/auth/change-password
权限：已登录用户（所有角色）
延迟类别：< 500ms
```

**请求体**：
```json
{
  "old_password": "OldPass@2026",
  "new_password": "NewPass@2026!"
}
```

**新密码约束**：
- 学生/教师：6-128 位，无复杂度要求（考虑易记性）
- 管理员/sysadmin：≥ 8 位，必须包含字母和数字

**成功响应**：`{"code": 0, "data": null}`

**错误响应**：
- `4001`：旧密码错误

---

#### 3.1.3 退出登录

```
POST /api/v1/auth/logout
权限：已登录用户（所有角色）
延迟类别：< 100ms
```

**说明**：Phase 1 实现为"前端清除 Token"的服务端确认接口。后端接收请求后记录审计日志，返回成功。由于 JWT 无状态，服务端无法实际使 Token 失效（Token 在 24h 内仍然有效，但用户已清除本地存储）。

**请求体**：空

**成功响应**：`{"code": 0, "data": null, "message": "已退出登录"}`

> **Note**：Phase 2 可通过 Redis 维护 Token 黑名单实现真正的服务端吊销。

---

#### 3.1.4 签发 SSE 一次性票据

```
POST /api/v1/auth/sse-ticket
权限：已登录用户（Bearer JWT）
延迟类别：< 200ms
```

**请求体**：`{"submission_id":"uuid"}`

**成功响应（200）**：`{"code":0,"data":{"ticket":"opaque-random-token","expires_in":60}}`

服务端将票据保存到 Redis，TTL 为 60 秒，并绑定 `tenant_id`、`user_id`、`submission_id` 和允许的事件类型。SSE 建连时原子读取并删除票据，确保单次使用；校验用户角色与提交归属后才建立连接。票据不得写入访问日志，长期 JWT 不得放入 URL。

---

### 3.2 作业模块 `/assignments`

#### 3.2.1 创建作业

```
POST /api/v1/assignments/
权限：teacher、admin
延迟类别：< 500ms
```

**请求体**：
```json
{
  "title": "第三单元加减法练习",
  "class_ids": ["uuid1", "uuid2"],
  "due_date": "2026-07-25T18:00:00+08:00",
  "problem_ids": ["problem-uuid-1", "problem-uuid-2", "problem-uuid-3"]
}
```

**字段约束**：
- `title`：非空，长度 1-200
- `class_ids`：非空，数组长度 1-20；每个 class_id 必须属于当前 tenant
- `due_date`：ISO 8601，必须晚于当前时间；`null` 表示不设截止时间
- `problem_ids`：非空，数组长度 1-50；每个 problem_id 必须存在

**成功响应（201）**：
```json
{
  "code": 0,
  "data": {
    "assignment_id": "uuid",
    "title": "第三单元加减法练习",
    "classes": [
      {"class_id": "uuid1", "class_name": "三年级一班"},
      {"class_id": "uuid2", "class_name": "三年级二班"}
    ],
    "due_date": "2026-07-25T18:00:00+08:00",
    "problem_count": 3,
    "created_at": "2026-07-19T10:00:00+08:00",
    "status": "active"
  }
}
```

**错误响应**：
- `4003`：教师试图布置给其他班级的作业
- `4004`：`problem_ids` 中某个题目不存在

---

#### 3.2.2 获取作业列表

```
GET /api/v1/assignments/?class_id=uuid&status=active&page=1&page_size=20
权限：teacher（本班）、student（本班，仅看已发布）、admin（全校）
延迟类别：< 500ms
```

**查询参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `class_id` | uuid（可选） | 按班级过滤 |
| `status` | string（可选） | `active`（未截止）\| `expired`（已截止）\| `all`（默认） |
| `page` / `page_size` | int | 分页，page_size 最大 100 |
| `order_by` | string | `created_at`（默认）\| `due_date` |
| `order` | string | `desc`（默认）\| `asc` |

**响应 data.items 元素**：
```json
{
  "assignment_id": "uuid",
  "title": "第三单元加减法练习",
  "class_name": "三年级二班",
  "due_date": "2026-07-25T18:00:00+08:00",
  "problem_count": 3,
  "status": "active",
  "is_expiring_soon": false,
  "submission_status": "not_submitted",
  "created_at": "2026-07-19T10:00:00+08:00"
}
```

字段说明：
- `is_expiring_soon`：截止时间 < 1 小时时为 true（学生端显示红色标识）
- `submission_status`：仅学生角色返回；未提交为 `not_submitted`，已提交后与 `submissions.status` 一致：`pending | grading | graded | partial_human_review | reviewed`

---

#### 3.2.3 获取作业详情

```
GET /api/v1/assignments/{assignment_id}
权限：teacher（本班）、student（本班）、admin（全校）
延迟类别：< 500ms
```

**响应 data**（教师视角）：
```json
{
  "assignment_id": "uuid",
  "title": "第三单元加减法练习",
  "class_name": "三年级二班",
  "due_date": "2026-07-25T18:00:00+08:00",
  "status": "active",
  "problems": [
    {
      "problem_id": "uuid",
      "sequence": 1,
      "problem_text": "325 + 47 = ___",
      "problem_type": "arithmetic",
      "grade_level": 3,
      "difficulty": "medium",
      "tags": ["三位数加法", "进位"]
    }
  ],
  "submission_stats": {
    "total_students": 42,
    "submitted_count": 38,
    "submission_rate": 0.905
  }
}
```

**响应 data**（学生视角，隐藏参考答案）：
```json
{
  "assignment_id": "uuid",
  "title": "第三单元加减法练习",
  "due_date": "2026-07-25T18:00:00+08:00",
  "status": "active",
  "is_expiring_soon": false,
  "problems": [
    {
      "problem_id": "uuid",
      "sequence": 1,
      "problem_text": "325 + 47 = ___",
      "problem_type": "arithmetic",
      "difficulty": "medium"
    }
  ],
  "my_submission": null
}
```

---

#### 3.2.4 修改作业（局部更新）

```
PATCH /api/v1/assignments/{assignment_id}
权限：teacher（本班）、admin
延迟类别：< 500ms
```

**请求体**（只传需要修改的字段）：
```json
{
  "due_date": "2026-07-26T18:00:00+08:00",
  "add_problem_ids": ["new-problem-uuid"],
  "remove_problem_ids": []
}
```

**注意**：`remove_problem_ids` 中若包含已有提交记录的题目，后端拒绝并返回 409，附说明"该题目已有学生提交，不可移除"。

---

#### 3.2.5 获取作业统计（教师/管理员）

```
GET /api/v1/assignments/{assignment_id}/stats
权限：teacher（本班）、admin
延迟类别：< 1s（可缓存 5 分钟）
```

**响应 data**：
```json
{
  "assignment_id": "uuid",
  "total_students": 42,
  "submitted_count": 38,
  "submission_rate": 0.905,
  "average_accuracy": 0.762,
  "problem_stats": [
    {
      "problem_id": "uuid",
      "sequence": 1,
      "problem_text": "325 + 47 = ___",
      "total_attempts": 38,
      "correct_first_try": 25,
      "correct_after_hint": 8,
      "still_wrong": 5,
      "accuracy_first_try": 0.658,
      "top_error_types": [
        {"error_type": "进位错误", "count": 7, "percentage": 0.184},
        {"error_type": "计算错误", "count": 3, "percentage": 0.079}
      ],
      "avg_hint_used": 0.63
    }
  ],
  "error_distribution": {
    "计算错误": 45,
    "进位错误": 23,
    "审题错误": 12,
    "概念错误": 5
  },
  "knowledge_point_alerts": [
    {
      "knowledge_point": "三位数加法进位",
      "problem_ids": ["uuid"],
      "class_error_rate": 0.54,
      "alert_message": "超过40%学生在此知识点出错，建议重点讲解",
      "affected_student_count": 23
    }
  ]
}
```

---

### 3.3 提交批改模块 `/submissions`

#### 3.3.1 提交作业答案（核心接口）

```
POST /api/v1/submissions/
权限：student 专属
延迟类别：≤ 3s（P95，含 LLM 调用）
```

**请求体**：
```json
{
  "assignment_id": "uuid",
  "answers": [
    {"problem_id": "uuid1", "answer_text": "372"},
    {"problem_id": "uuid2", "answer_text": "B"},
    {"problem_id": "uuid3", "answer_text": ""}
  ]
}
```

**字段约束**：
- `assignment_id`：必须属于当前学生所在班级
- `answers`：数组长度 1-50；`answer_text` 字段必须存在且最长 500 字符，允许空字符串或纯空白，服务端 `trim` 后标记为 `unanswered` 并按错误答案处理，不阻止同一作业中其他题目批改
- 每道题提交一个答案（`answers` 中 `problem_id` 不重复）
- 不允许对同一作业重复提交（返回 409）；允许通过 Hint 接口逐题重提交

**成功响应（201，全部自动批改）**：
```json
{
  "code": 0,
  "data": {
    "submission_id": "uuid",
    "status": "graded",
    "submitted_at": "2026-07-19T14:30:00+08:00",
    "results": [
      {
        "problem_id": "uuid1",
        "sequence": 1,
        "problem_text": "325 + 47 = ___",
        "student_answer": "372",
        "is_correct": true,
        "confidence_score": 0.97,
        "feedback_text": "太棒了！325+47=372，进位计算完全正确！",
        "encouragement": "真棒，一次就答对了！",
        "next_hint": null,
        "error_type": null,
        "hint_level": 0,
        "attempt_number": 1,
        "routed_to_human": false,
        "grading_source": "agent"
      },
      {
        "problem_id": "uuid2",
        "sequence": 2,
        "problem_text": "下面哪个算式结果等于12？",
        "student_answer": "A",
        "is_correct": false,
        "confidence_score": 0.95,
        "feedback_text": "这道题再想想哦！仔细算一下每个选项。",
        "encouragement": "加油，你可以的！",
        "next_hint": "想一想：A选项 3×5 等于多少？",
        "error_type": "计算错误",
        "hint_level": 0,
        "attempt_number": 1,
        "routed_to_human": false,
        "grading_source": "agent"
      },
      {
        "problem_id": "uuid3",
        "sequence": 3,
        "problem_text": "56 - 28 = ___",
        "student_answer": "",
        "is_correct": false,
        "confidence_score": 1.0,
        "feedback_text": "这道题还没有作答，先试着算一算56减28吧。",
        "encouragement": "勇敢写下答案就是进步！",
        "next_hint": "可以先算个位：6减8不够减，需要向十位借1。",
        "error_type": "未作答",
        "hint_level": 0,
        "attempt_number": 1,
        "routed_to_human": false,
        "grading_source": "rule_fallback"
      }
    ],
    "summary": {
      "total": 3,
      "correct": 1,
      "wrong": 2,
      "pending_review": 0,
      "accuracy": 0.333
    }
  }
}
```

**字段说明**：
- `grading_source`：`agent`（AI 自动批改）/ `rule_fallback`（规则降级）/ `pending_human_review`（待人工审核）/ `human_override`（教师覆盖后）
- `is_correct`：人工审核中的题目为 `null`
- `confidence_score`：0.0-1.0，学生端不直接展示，仅用于前端决定是否显示"老师审核中"状态

**错误响应**：
- `4005`（409）：`{"message": "该作业已提交，不可重复提交"}`
- `4006`（410）：`{"message": "作业已截止，无法提交"}`
- `4003`（403）：`{"message": "该作业不属于你所在的班级"}`

---

#### 3.3.2 查询提交结果

```
GET /api/v1/submissions/{submission_id}
权限：student（只看自己）、teacher（看本班）
延迟类别：< 500ms
```

**响应 data**：同 3.3.1 的完整结构，额外增加：
```json
{
  "agent_trace_available": true,
  "last_updated_at": "2026-07-19T14:35:00+08:00"
}
```

**SSE 建议**（前端低置信度结果刷新）：
- `status = "partial_human_review"` 时，前端订阅 `/submissions/{submission_id}/events`
- 收到 `grading_update` 且所有 `routed_to_human=false` 时关闭连接
- 连接断开时使用指数退避重连；页面长期挂起或浏览器不支持 SSE 时提供手动刷新兜底

---

#### 3.3.3 请求 Hint（学习循环）

```
POST /api/v1/submissions/{submission_id}/hint
权限：student（只能操作自己的提交）
延迟类别：≤ 3s（P95，含 LLM 调用）
```

**请求体**：
```json
{
  "problem_id": "uuid1",
  "new_answer": "362"
}
```

**字段约束**：
- `submission_id` 必须属于当前用户
- `problem_id` 必须属于该提交的作业
- 该题目的当前 `hint_level < 3`（否则返回 4007）
- 该题目的当前 `is_correct != true`（已答对无需 hint）

**成功响应（200）**：
```json
{
  "code": 0,
  "data": {
    "problem_id": "uuid1",
    "student_answer": "362",
    "is_correct": false,
    "hint_level": 1,
    "attempt_number": 2,
    "feedback_text": "你的思路是对的！仔细看一下个位相加的时候，7+5=12，有没有向十位进1呢？",
    "encouragement": "再想想，你快了！",
    "next_hint": "下一次提示会告诉你具体是哪一步算错了",
    "remaining_hints": 2,
    "routed_to_human": false,
    "confidence_score": 0.95
  }
}
```

**hint_level=3 达到上限后继续提交（完整解法）**：
```json
{
  "code": 0,
  "data": {
    "hint_level": 3,
    "is_correct": false,
    "feedback_text": "这道题的完整解法：\n① 个位：5+7=12，写2进1\n② 十位：2+4+1=7，写7\n③ 百位：3，写3\n所以 325+47=372",
    "encouragement": "下次一定能自己算出来！",
    "next_hint": null,
    "remaining_hints": 0,
    "show_full_solution": true,
    "knowledge_point_recorded": "三位数加法进位"
  }
}
```

**错误响应**：
- `4007`（409）：`{"message": "该题已展示完整解法，无法继续提交"}`
- `4003`（403）：`{"message": "这道题不属于你的提交记录"}`

---

#### 3.3.4 获取学生提交历史

```
GET /api/v1/submissions/?student_id=uuid&assignment_id=uuid&page=1&page_size=20
权限：student（只看自己，不需要 student_id 参数）、teacher（看本班，需要 student_id）
延迟类别：< 500ms
```

**响应 data.items 元素**：
```json
{
  "submission_id": "uuid",
  "assignment_title": "第三单元加减法练习",
  "submitted_at": "2026-07-19T14:30:00+08:00",
  "status": "graded",
  "accuracy": 0.75,
  "total_problems": 4,
  "correct_count": 3,
  "hints_used": 1,
  "pending_review_count": 0
}
```

---

### 3.4 教师 HITL 模块 `/teacher`

#### 3.4.1 获取待审核队列

```
GET /api/v1/teacher/human-review-queue?status=pending&page=1&page_size=20
权限：teacher（本班）、admin（全校）
延迟类别：< 500ms
```

**查询参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | string | `pending`（默认）\| `reviewed` \| `all` |
| `class_id` | uuid（可选） | 按班级过滤（admin 用） |
| `confidence_max` | float（可选） | 筛选置信度低于此值的（如 `0.8`） |
| `reason` | string（可选） | `low_confidence` \| `sympy_llm_conflict` \| `parse_error` \| `llm_fallback` |
| `order_by` | string | `created_at`（默认）\| `confidence_score` |

**响应 data.items 元素**：
```json
{
  "review_id": "uuid",
  "student_name": "李小明",
  "class_name": "三年级二班",
  "assignment_title": "第三单元加减法练习",
  "problem_text": "325 + 47 = ___",
  "problem_type": "arithmetic",
  "student_answer": "362",
  "reference_answer": "372",
  "ai_conclusion": "错误",
  "ai_confidence": 0.72,
  "confidence_percentage": "72%",
  "human_review_reason": "sympy_llm_conflict",
  "reason_display": "SymPy 与 AI 结论冲突",
  "created_at": "2026-07-19T14:30:00+08:00",
  "waiting_minutes": 35
}
```

**响应头**（用于红点数量更新）：
```
X-Pending-Review-Count: 7
```

---

#### 3.4.2 查看审核详情（含完整 agent_trace）

```
GET /api/v1/teacher/human-review/{review_id}
权限：teacher（本班）
延迟类别：< 500ms
```

**响应 data**：
```json
{
  "review_id": "uuid",
  "student_name": "李小明",
  "class_name": "三年级二班",
  "problem_text": "325 + 47 = ___",
  "problem_type": "arithmetic",
  "student_answer": "362",
  "reference_answer": "372",
  "hint_level": 0,
  "attempt_number": 1,

  "sympy_result": {
    "expression": "325 + 47",
    "expected": "372",
    "student_correct": false,
    "carry_error_detected": true,
    "success": true
  },

  "llm_result": {
    "is_correct": true,
    "reasoning": "理解题意：加法计算 325+47 | 正确解法：325+47=372 | 学生答案：362 | 分析：362与372差10，可能是进位错误 | 结论：答案错误",
    "confidence": 0.72,
    "model_used": "deepseek-chat",
    "retry_count": 0
  },

  "ai_overall": {
    "final_conclusion": "错误",
    "confidence_score": 0.72,
    "human_review_reason": "sympy_llm_conflict",
    "fallback_used": false
  },

  "processing_log": [
    "parser_node: ok, type=arithmetic, expr='325 + 47', normalized_answer='362'",
    "sympy_verifier_node: expected='372', student='362', correct=false, carry_error=true",
    "llm_evaluator_node: is_correct=true(?), conf=0.72, model=deepseek-chat",
    "confidence_router_node: CONFLICT(sympy=false, llm=true), final=false, conf=0.75, route=human",
    "human_review_queue_node: enqueued, reason=sympy_llm_conflict"
  ],

  "submitted_at": "2026-07-19T14:30:00+08:00",
  "created_at": "2026-07-19T14:30:01+08:00"
}
```

---

#### 3.4.3 提交审核覆盖

```
POST /api/v1/teacher/human-review/{review_id}
权限：teacher（本班）
延迟类别：< 500ms
审计：记录到 audit_logs
```

**请求体**：
```json
{
  "override_correct": false,
  "override_error_type": "进位错误",
  "override_feedback": "小明，这道题在十位进位时出错了。325+47：个位7+5=12，写2进1；十位2+4+1=7；所以答案是372哦！",
  "reviewer_notes": "SymPy 正确判断为错误，LLM 判断有误。进位错误典型案例，标记为训练样本。",
  "is_training_example": true
}
```

**字段约束**：
- `override_correct`：布尔值，必填
- `override_error_type`：当 `override_correct=false` 时必填，枚举：`计算错误|审题错误|进位错误|概念错误`
- `override_feedback`：可选，最长 1000 字符；不填时学生将看到 AI 原有反馈加上"已经过老师审核"标注
- `reviewer_notes`：可选，最长 500 字符，仅教师内部可见

**事务与通知约束**：审核服务必须在同一 PostgreSQL 事务内更新 `grading_results.is_correct`、`source=human_override`、`routed_to_human=false`、`human_review_queue.status=reviewed` 并重算 `submissions.status`；事务提交成功后再发布 `grading_update` SSE 事件。

**成功响应（200）**：
```json
{
  "code": 0,
  "data": {
    "review_id": "uuid",
    "status": "reviewed",
    "override_correct": false,
    "student_notified": true,
    "notify_eta_seconds": 30,
    "is_training_example": true
  }
}
```

---

#### 3.4.4 班级分析仪表盘

```
GET /api/v1/teacher/dashboard?class_id=uuid&assignment_id=uuid&days=30
权限：teacher（本班）、admin（全校，不传 class_id 时汇总）
延迟类别：< 1s（缓存 5 分钟）
```

**查询参数**：
- `class_id`：可选，不传时教师看本人负责的所有班级汇总
- `assignment_id`：可选，不传时显示 `days` 天内的累计统计
- `days`：可选，默认 30，最大 90

**响应 data**：
```json
{
  "class_name": "三年级二班",
  "period": {
    "days": 30,
    "start_date": "2026-06-19",
    "end_date": "2026-07-19"
  },
  "overview": {
    "total_submissions": 156,
    "average_accuracy": 0.762,
    "submission_rate": 0.905,
    "human_review_rate": 0.035
  },
  "error_distribution": {
    "计算错误": 45,
    "进位错误": 23,
    "审题错误": 12,
    "概念错误": 5
  },
  "knowledge_point_alerts": [
    {
      "knowledge_point": "三位数加法进位",
      "error_rate": 0.54,
      "alert_level": "high",
      "alert": "超过40%学生在此知识点出错，建议重点讲解",
      "affected_student_count": 23
    }
  ],
  "students_needing_attention": [
    {
      "student_id": "uuid",
      "student_name": "王小红",
      "recent_accuracy": 0.42,
      "weak_points": ["进位错误", "计算错误"],
      "hint_dependency_rate": 0.80,
      "consecutive_wrong_count": 3
    }
  ],
  "pending_review_count": 3,
  "accuracy_trend": [
    {"week": "2026-07-07", "accuracy": 0.71},
    {"week": "2026-07-14", "accuracy": 0.74},
    {"week": "2026-07-19", "accuracy": 0.76}
  ]
}
```

---

#### 3.4.5 学生个人分析

```
GET /api/v1/teacher/students/{student_id}/analytics?days=30
权限：teacher（只能查自己班学生）、admin
延迟类别：< 1s
```

**响应 data**：
```json
{
  "student_name": "王小红",
  "grade_level": 3,
  "class_name": "三年级二班",
  "period_days": 30,
  "total_submissions": 45,
  "total_problems_answered": 180,
  "overall_accuracy": 0.68,
  "accuracy_trend": [
    {"date": "2026-07-01", "accuracy": 0.60, "problems_count": 10},
    {"date": "2026-07-08", "accuracy": 0.65, "problems_count": 15},
    {"date": "2026-07-15", "accuracy": 0.72, "problems_count": 20}
  ],
  "weak_knowledge_points": [
    {
      "point": "三位数加法进位",
      "error_count": 8,
      "last_error_at": "2026-07-18T14:22:00+08:00",
      "trend": "improving"
    },
    {
      "point": "两位数乘法",
      "error_count": 5,
      "last_error_at": "2026-07-16T09:15:00+08:00",
      "trend": "stable"
    }
  ],
  "hint_usage": {
    "total_hints_used": 12,
    "hint_dependency_rate": 0.27,
    "max_hint_reached_count": 3,
    "average_hints_per_wrong_answer": 1.2
  },
  "error_type_breakdown": {
    "进位错误": 12,
    "计算错误": 8,
    "审题错误": 3,
    "概念错误": 1
  }
}
```

---

#### 3.4.6 数据导出（Excel）

```
GET /api/v1/teacher/export/assignment/{assignment_id}?format=excel
权限：teacher（本班）、admin
延迟类别：< 3s（生成文件）
审计：记录到 audit_logs
```

**响应**：
```
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="assignment_report_20260719.xlsx"
```

Excel 包含两个 Sheet：
- Sheet1（题目维度）：每题的班级正确率、错误类型分布
- Sheet2（学生维度）：每个学生每题的对错、使用 hint 次数

---

### 3.5 题库模块 `/problems`

#### 3.5.1 创建题目

```
POST /api/v1/problems/
权限：teacher、admin
延迟类别：< 1s（含异步触发向量化）
```

**请求体**：
```json
{
  "problem_text": "325 + 47 = ___",
  "problem_type": "arithmetic",
  "reference_answer": "372",
  "grade_level": 3,
  "difficulty": "medium",
  "curriculum_version": "人教版",
  "solution_steps": [
    "个位：5+7=12，写2进1",
    "十位：2+4+1=7，写7",
    "百位：3，写3",
    "结果：372"
  ],
  "common_errors": [
    {"wrong_answer": "362", "error_type": "进位错误", "note": "十位进位时遗漏了+1"},
    {"wrong_answer": "369", "error_type": "计算错误"}
  ],
  "tags": ["三位数加法", "进位", "第三单元"]
}
```

**字段约束**：
- `problem_text`：非空，长度 1-500
- `problem_type`：枚举 `arithmetic | fill_in_blank | multiple_choice`
- `reference_answer`：非空，长度 1-200
- `grade_level`：整数 1-6
- `difficulty`：枚举 `easy | medium | hard`
- `curriculum_version`：枚举 `人教版 | 北师大版`
- `tags`：数组，每个 tag 长度 1-50，总数 ≤ 20
- `solution_steps`：可选，数组，每步 ≤ 200 字符，总数 ≤ 20
- `common_errors`：可选，数组长度 ≤ 10

**成功响应（201）**：
```json
{
  "code": 0,
  "data": {
    "problem_id": "uuid",
    "embedding_status": "pending",
    "message": "题目已创建，向量化处理中（预计 5 秒内完成）"
  }
}
```

---

#### 3.5.2 题目列表查询

```
GET /api/v1/problems/?grade_level=3&problem_type=arithmetic&difficulty=medium&tag=进位&source=all&page=1
权限：teacher、admin
延迟类别：< 500ms
```

**查询参数**：

| 参数 | 说明 |
|------|------|
| `grade_level` | 1-6，可多值（`grade_level=1&grade_level=2`） |
| `problem_type` | `arithmetic \| fill_in_blank \| multiple_choice` |
| `difficulty` | `easy \| medium \| hard` |
| `tag` | 标签精确匹配，可多值 |
| `keyword` | 题目文本模糊搜索 |
| `source` | `public`（公共题库）\| `school`（校本题库）\| `all`（默认） |

---

#### 3.5.3 批量导入题目

```
POST /api/v1/problems/bulk-import
权限：teacher、admin
Content-Type: multipart/form-data
延迟类别：异步（返回 job_id）
```

**表单字段**：
- `file`：Excel 或 CSV 文件（最大 10MB）
- `curriculum_version`：`人教版 | 北师大版`

**Excel 模板列**（可通过 `GET /api/v1/problems/bulk-import/template` 下载）：
```
problem_text（必填）| problem_type（必填）| reference_answer（必填）| grade_level（必填）
| difficulty（必填）| solution_steps（可选，分号分隔）| common_errors（可选）| tags（可选，逗号分隔）
```

**成功响应（202，异步）**：
```json
{
  "code": 0,
  "data": {
    "import_job_id": "uuid",
    "status": "queued",
    "estimated_seconds": 30
  }
}
```

**任务完成后，查询 `GET /api/v1/ops/jobs/{job_id}` 获取结果**：
```json
{
  "status": "completed",
  "total": 50,
  "success": 48,
  "failed": 2,
  "failed_rows": [
    {"row": 15, "problem_text": "xxx+yyy=___", "reason": "参考答案字段为空"},
    {"row": 32, "problem_text": "...", "reason": "problem_type='问答题' 不在枚举范围内，有效值：arithmetic/fill_in_blank/multiple_choice"}
  ]
}
```

---

### 3.6 用户与班级管理模块 `/admin`

#### 3.6.1 批量创建学生账户

```
POST /api/v1/admin/students/bulk-create
权限：admin
Content-Type: multipart/form-data
延迟类别：异步（> 200 条时）
审计：记录到 audit_logs
```

**表单字段**：
- `file`：Excel 文件（最大 10MB）
- 必填列：`姓名 | 用户名 | 初始密码 | 年级 | 班级名称`

**≤ 200 条同步响应**：
```json
{
  "code": 0,
  "data": {
    "created": 42,
    "skipped": 2,
    "failed": 0,
    "skipped_reasons": [
      {"row": 15, "username": "zhang_san_001", "reason": "用户名已存在，已跳过"}
    ],
    "failed_rows": []
  }
}
```

**> 200 条异步响应（202）**：
```json
{
  "code": 0,
  "data": {
    "import_job_id": "uuid",
    "status": "queued"
  }
}
```

---

#### 3.6.2 创建班级

```
POST /api/v1/admin/classes/
权限：admin
延迟类别：< 500ms
```

**请求体**：
```json
{
  "name": "三年级二班",
  "grade_level": 3,
  "teacher_id": "uuid",
  "academic_year": "2024-2025"
}
```

---

#### 3.6.3 重置用户密码

```
POST /api/v1/admin/users/{user_id}/reset-password
权限：admin
延迟类别：< 500ms
审计：记录到 audit_logs
```

**请求体**：
```json
{
  "new_password": "TempPass@2026"
}
```

**成功响应**：
```json
{
  "code": 0,
  "data": {
    "user_id": "uuid",
    "username": "zhang_san_001",
    "display_name": "张三",
    "force_change_on_next_login": true
  }
}
```

---

#### 3.6.4 全校统计概览

```
GET /api/v1/admin/stats/overview
权限：admin、sysadmin
延迟类别：< 1s（缓存 1 小时）
```

**响应 data**：
```json
{
  "tenant_name": "北京市XX小学",
  "active_school_year": "2024-2025",
  "users": {
    "total_students": 520,
    "total_teachers": 18,
    "total_classes": 12,
    "active_students_today": 156,
    "active_teachers_today": 8
  },
  "submissions": {
    "total_all_time": 15670,
    "today": 234,
    "this_week": 1240,
    "this_month": 4560
  },
  "grading": {
    "ai_graded_count": 15120,
    "human_review_count": 550,
    "human_review_rate": 0.035,
    "average_accuracy": 0.781,
    "rule_fallback_rate": 0.008
  },
  "performance": {
    "avg_grading_latency_ms": 1847,
    "p95_grading_latency_ms": 2840
  }
}
```

---

### 3.7 运维模块 `/ops`

#### 3.7.1 手动触发 Harness

```
POST /api/v1/ops/harness/run
权限：sysadmin
延迟类别：异步（返回 run_id）
限流：每 sysadmin 5次/小时
```

**请求体**：
```json
{
  "use_mock": true,
  "sample_rate": 1.0,
  "dataset": "all",
  "grade_levels": [1, 2, 3]
}
```

**响应（202）**：
```json
{
  "code": 0,
  "data": {
    "run_id": "uuid",
    "status": "running",
    "estimated_seconds": 45,
    "use_mock": true,
    "total_cases": 180
  }
}
```

---

#### 3.7.2 查询 Harness 运行结果

```
GET /api/v1/ops/harness/runs/{run_id}
权限：sysadmin
延迟类别：< 500ms
```

**响应 data**（完成后）：
```json
{
  "run_id": "uuid",
  "status": "completed",
  "passed": true,
  "prompt_version": "a3f8c2d",
  "use_mock": true,
  "total_cases": 180,
  "passed_cases": 170,
  "accuracy": 0.9444,
  "false_positive_rate": 0.030,
  "false_negative_rate": 0.025,
  "error_cls_accuracy": 0.950,
  "confusion_matrix": {"actual_correct": 80, "actual_wrong": 100, "false_positive": 3, "false_negative": 2},
  "calibration_error": 0.034,
  "coverage_matrix": {
    "grade1": {
      "arithmetic": {"easy": 5, "medium": 5, "hard": 5, "accuracy": 0.987},
      "fill_in_blank": {"easy": 5, "medium": 5, "hard": 5, "accuracy": 0.960}
    },
    "grade2": {...},
    "grade3": {...}
  },
  "failed_cases": [
    {
      "case_id": "G3-ARITH-CARRY-007",
      "problem_text": "38 + 45 = ___",
      "student_answer": "73",
      "expected_correct": false,
      "actual_correct": true,
      "expected_error_type": "进位错误",
      "actual_error_type": "计算错误",
      "actual_confidence": 0.89,
      "issue": "error_classification_mismatch"
    }
  ],
  "run_at": "2026-07-19T10:00:00+08:00",
  "duration_seconds": 42
}
```

---

#### 3.7.3 题库 RAG 导入

```
POST /api/v1/ops/rag/ingest
权限：sysadmin
延迟类别：异步
```

**请求体**：
```json
{
  "source": "problems_table",
  "grade_levels": [1, 2, 3],
  "batch_size": 100,
  "force_reingest": false
}
```

---

#### 3.7.4 后台任务查询

```
GET /api/v1/ops/jobs/{job_id}
权限：sysadmin、admin（仅限自己触发的任务）
延迟类别：< 200ms
```

**响应 data**：
```json
{
  "job_id": "uuid",
  "job_type": "bulk_import_problems",
  "status": "running",
  "progress": 0.6,
  "created_at": "2026-07-19T10:00:00+08:00",
  "result": null
}
```

---

### 3.8 健康检查

```
GET /health
权限：公开
延迟类别：< 100ms
```

**成功响应（200）**：
```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "production",
  "uptime_seconds": 86400,
  "services": {
    "database": {"status": "ok", "latency_ms": 2},
    "qdrant": {"status": "ok", "latency_ms": 5},
    "redis": {"status": "ok", "latency_ms": 1},
    "deepseek": {"status": "ok", "latency_ms": 234},
    "qianwen": {"status": "degraded", "latency_ms": null, "error": "connection timeout"}
  },
  "grading": {
    "active_requests": 3,
    "pending_hitl_count": 7
  }
}
```

**部分降级响应（200，系统可用但部分服务异常）**：
```json
{
  "status": "degraded",
  "services": {
    "qianwen": {"status": "unavailable"},
    "database": {"status": "ok"}
  }
}
```

---

## 四、异步场景处理

### 4.1 低置信度批改通知

提交聚合状态为 `partial_human_review`，待审核题目的 `grading_source=pending_human_review`。前端通过 **Server-Sent Events（SSE）** 获取最新状态：

```
GET /api/v1/submissions/{submission_id}/events?sse_ticket=<一次性票据>
Accept: text/event-stream

event: grading_update
data: {"submission_id":"uuid","problem_id":"uuid","routed_to_human":false,"is_correct":false,"feedback_text":"..."}

event: heartbeat
data: {"timestamp":"2026-07-20T12:00:00Z"}
```

客户端先用 Bearer JWT 调用 `POST /auth/sse-ticket`，再用返回的一次性票据创建 `EventSource`。票据有效期 60 秒、绑定用户/租户/提交记录并在首次建连时消费；服务端每 30 秒发送 heartbeat。断线重连时客户端重新申请票据，长期 JWT 不进入 URL。WebSocket 不纳入 Phase 1。

### 4.2 批量操作异步模式

超过 200 条的批量操作（批量导入学生、批量 RAG 摄取）返回 `job_id`：

```
POST /api/v1/problems/bulk-import → 202 { "import_job_id": "uuid", "status": "queued" }
GET  /api/v1/ops/jobs/{job_id}    → { "status": "queued|running|completed|failed", "progress": 0.6 }
```

任务状态轮询间隔建议：2 秒（批量导入通常 10-30 秒完成）

---

## 五、SSE 连接管理与 WebSocket 评估边界

Phase 1 统一使用 SSE。连接管理要求：每用户限制并发连接数、30 秒心跳、支持 `Last-Event-ID` 恢复、反向代理关闭响应缓冲。只有未来出现实时协作等双向通信需求时，才评估 WebSocket；以下草案不作为当前实现范围。

WebSocket 评估触发条件：需要客户端向服务器持续发送实时事件，或出现多人实时协作。普通批改完成、HITL 完成和角标变化均继续使用 SSE，不重复建设两套实时通道。

---

## 六、接口权限矩阵

| 接口 | student | teacher | admin | sysadmin |
|------|---------|---------|-------|---------|
| POST /auth/login | ✓ | ✓ | ✓ | ✓ |
| POST /auth/logout | ✓ | ✓ | ✓ | ✓ |
| POST /auth/sse-ticket | ✓（自己） | ✓（本班） | ✓ | ✓ |
| POST /auth/change-password | ✓（本人） | ✓ | ✓ | ✓ |
| GET /assignments/ | ✓（本班） | ✓（本班） | ✓（全校） | ✓ |
| POST /assignments/ | ✗ | ✓（本班） | ✓ | ✓ |
| PATCH /assignments/{id} | ✗ | ✓（本班，未截止） | ✓ | ✓ |
| GET /assignments/{id}/stats | ✗ | ✓（本班） | ✓ | ✓ |
| POST /submissions/ | ✓（本班，未截止） | ✗ | ✗ | ✗ |
| POST /submissions/{id}/hint | ✓（自己） | ✗ | ✗ | ✗ |
| GET /submissions/ | ✓（自己） | ✓（本班） | ✓ | ✓ |
| GET /submissions/{id}/events | ✓（自己，SSE） | ✓（本班） | ✓ | ✓ |
| GET /teacher/human-review-queue | ✗ | ✓（本班） | ✓ | ✓ |
| POST /teacher/human-review/{id} | ✗ | ✓（本班） | ✓ | ✗ |
| GET /teacher/dashboard | ✗ | ✓（本班） | ✓（全校） | ✗ |
| GET /teacher/students/{id}/analytics | ✗ | ✓（本班学生） | ✓ | ✗ |
| GET /teacher/export/assignment/{id} | ✗ | ✓（本班） | ✓ | ✓ |
| POST /problems/ | ✗ | ✓ | ✓ | ✓ |
| DELETE /problems/{id} | ✗ | ✓（自己创建，未被引用） | ✓ | ✓ |
| POST /problems/bulk-import | ✗ | ✓ | ✓ | ✓ |
| POST /admin/students/bulk-create | ✗ | ✗ | ✓ | ✓ |
| POST /admin/users/{id}/reset-password | ✗ | ✗ | ✓ | ✓ |
| GET /admin/stats/overview | ✗ | ✗ | ✓ | ✓ |
| POST /ops/harness/run | ✗ | ✗ | ✗ | ✓ |
| GET /ops/harness/runs/{id} | ✗ | ✗ | ✗ | ✓ |
| POST /ops/rag/ingest | ✗ | ✗ | ✗ | ✓ |
| GET /health | ✓ | ✓ | ✓ | ✓ |

---

## 七、接口变更管理

| 场景 | 策略 | 通知方式 |
|------|------|---------|
| 新增响应字段（向前兼容） | 直接新增，不升版本 | Changelog |
| 修改现有字段类型/名称 | 发布 `/api/v2/`，旧版本保留 3 个月 | 书面通知 + Changelog |
| 删除字段 | 发布 `/api/v2/`，旧版本先标记 deprecated | 书面通知 |
| 修复 Bug（行为不变） | 直接修复，更新 Changelog | Changelog |
| 安全漏洞修复 | 立即修复，不需要版本升级 | 紧急通知 |

---

## 八、SDK 集成示例（Python）

```python
import httpx
import asyncio

BASE_URL = "https://school-grader.internal/api/v1"

async def grade_submission(token: str, assignment_id: str, answers: list[dict]) -> dict:
    async with httpx.AsyncClient(verify=False) as client:  # 内网自签名证书
        resp = await client.post(
            f"{BASE_URL}/submissions/",
            json={"assignment_id": assignment_id, "answers": answers},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,  # 批改超时 10 秒
        )
        resp.raise_for_status()
        data = resp.json()
        if data["code"] != 0:
            raise ValueError(f"API Error {data['code']}: {data['message']}")
        return data["data"]

# 使用示例
async def main():
    token = "eyJhbGci..."
    result = await grade_submission(
        token=token,
        assignment_id="uuid",
        answers=[{"problem_id": "p1", "answer_text": "372"}]
    )
    for r in result["results"]:
        print(f"题目 {r['problem_id']}: {'✓' if r['is_correct'] else '✗'} - {r['feedback_text']}")

asyncio.run(main())
```
