# 数据库设计方案

**项目名称**：翱翔启航  
**文档版本**：v2.0  
**创建日期**：2026-07-19  
**最后更新**：2026-07-20  
**状态**：待确认

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **多租户行级隔离** | 所有业务表携带 `tenant_id`，通过 TenantSession 自动过滤，学校数据物理共存逻辑隔离 |
| **UUID 主键** | 全部使用 UUID v4，避免自增 ID 暴露数据量，支持后期分布式扩展 |
| **时区统一** | 所有时间字段使用 `TIMESTAMPTZ`，存储 UTC，展示时应用层转为 Asia/Shanghai |
| **软删除** | 有关联关系的核心表（users、problems、classes）使用 `is_deleted` 标志，保护历史数据 |
| **JSONB 灵活扩展** | 结构不固定或频繁变化的字段（agent_trace、config、solution_steps）使用 JSONB |
| **最小化冗余** | 严格第三范式（3NF），仅在查询热路径上允许必要的反范式冗余（如 `grading_results.tenant_id`） |
| **部分索引** | 对状态字段使用部分索引（`WHERE status = 'pending'`），减少索引体积 |
| **审计不可删** | `audit_logs` 表通过 PostgreSQL RLS（行级安全策略）实现只追加，禁止 UPDATE/DELETE |

---

## 二、整体 ER 图

```mermaid
erDiagram
    tenants ||--o{ users : "拥有"
    tenants ||--o{ classes : "拥有"
    tenants ||--o{ assignments : "拥有"

    users ||--o{ class_students : "加入"
    classes ||--o{ class_students : "包含"
    classes ||--o{ assignments : "关联"
    users ||--o{ assignments : "创建(教师)"

    assignments ||--o{ submissions : "收到"
    users ||--o{ submissions : "提交(学生)"

    submissions ||--o{ submission_answers : "包含"
    problems ||--o{ submission_answers : "被回答"

    submission_answers ||--o{ grading_results : "产生"
    grading_results ||--o| human_review_queue : "可能进入"

    users ||--o{ student_error_history : "积累"
    problems ||--o{ student_error_history : "关联"

    users ||--o{ audit_logs : "操作产生"

    tenants {
        uuid id PK
        varchar name
        varchar code UK
        varchar curriculum
        jsonb config
        timestamptz created_at
    }

    users {
        uuid id PK
        uuid tenant_id FK
        varchar role
        varchar username UK_per_tenant
        varchar display_name
        varchar password_hash
        smallint grade_level
        smallint login_fail_count
        timestamptz locked_until
        boolean force_change_password
        boolean is_deleted
        timestamptz created_at
    }

    grading_results {
        uuid id PK
        uuid tenant_id FK
        uuid submission_id FK
        uuid problem_id FK
        smallint attempt_number
        boolean is_correct
        float confidence_score
        varchar error_type
        text error_detail
        text feedback_text
        text encouragement
        text next_hint
        varchar sympy_expected
        boolean sympy_is_correct
        text llm_reasoning
        float llm_confidence
        boolean routed_to_human
        varchar source
        jsonb agent_trace
        timestamptz graded_at
    }

    audit_logs {
        uuid id PK
        uuid tenant_id FK
        uuid operator_id FK
        varchar action
        varchar resource_type
        uuid resource_id
        jsonb detail
        inet ip_address
        text user_agent
        varchar result
        timestamptz created_at
    }
```

---

## 三、表结构详细定义

### 3.1 tenants（学校租户表）

```sql
CREATE TABLE tenants (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(100) NOT NULL,              -- 学校全称，如"北京市XX小学"
    code            VARCHAR(50)  UNIQUE NOT NULL,       -- 学校唯一编码，如"BJ-XXXX-001"
    curriculum      VARCHAR(20)  NOT NULL DEFAULT '人教版',
    config          JSONB        NOT NULL DEFAULT '{}',
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- config JSONB 示例（可覆盖系统默认参数）：
-- {
--   "confidence_threshold": 0.85,      -- 触发 HITL 的置信度阈值
--   "max_hint_level": 3,               -- 最大 hint 级别（默认3）
--   "llm_model_preference": "deepseek",-- 模型偏好（deepseek | qianwen）
--   "grading_timeout_seconds": 30,     -- 批改超时
--   "enable_rag": true                 -- 是否启用 RAG 相似题检索
-- }

COMMENT ON TABLE tenants IS '学校租户主表，Phase 1 每校对应一条记录';
COMMENT ON COLUMN tenants.config IS 'JSON 格式租户级配置，可覆盖系统默认参数';

-- 自动维护 updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

---

### 3.2 users（用户表）

```sql
CREATE TABLE users (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id),
    role                VARCHAR(20) NOT NULL
                        CHECK (role IN ('student','teacher','admin','sysadmin')),
    username            VARCHAR(100) NOT NULL,
    display_name        VARCHAR(100),                   -- 真实姓名（展示用，不含真实身份信息）
    password_hash       VARCHAR(255),                   -- bcrypt 哈希（rounds=12）
    grade_level         SMALLINT    CHECK (grade_level BETWEEN 1 AND 6),  -- 仅学生有值
    login_fail_count    SMALLINT    NOT NULL DEFAULT 0, -- 连续登录失败次数
    locked_until        TIMESTAMPTZ,                    -- 账户锁定截止时间
    force_change_password BOOLEAN   NOT NULL DEFAULT FALSE, -- 首次登录强制修改密码
    is_deleted          BOOLEAN     NOT NULL DEFAULT FALSE,
    last_login_at       TIMESTAMPTZ,                    -- 最后登录时间
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, username)                        -- 同一学校内用户名唯一
);

CREATE INDEX idx_users_tenant_role
    ON users(tenant_id, role)
    WHERE is_deleted = FALSE;

CREATE INDEX idx_users_tenant_username
    ON users(tenant_id, username)
    WHERE is_deleted = FALSE;

CREATE TRIGGER users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON COLUMN users.grade_level IS '学生年级（1-6），教师/管理员为 NULL';
COMMENT ON COLUMN users.login_fail_count IS '连续失败次数，超过5次锁定账户15分钟';
COMMENT ON COLUMN users.force_change_password IS '管理员重置密码后设为 true，首次登录后清除';
```

---

### 3.3 classes（班级表）

```sql
CREATE TABLE classes (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id),
    grade_level     SMALLINT    NOT NULL CHECK (grade_level BETWEEN 1 AND 6),
    name            VARCHAR(100) NOT NULL,
    teacher_id      UUID        NOT NULL REFERENCES users(id),
    academic_year   VARCHAR(10) NOT NULL,               -- 如"2024-2025"
    is_deleted      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, name, academic_year)             -- 同年度同班级名唯一
);

CREATE INDEX idx_classes_tenant
    ON classes(tenant_id)
    WHERE is_deleted = FALSE;

CREATE INDEX idx_classes_teacher
    ON classes(teacher_id)
    WHERE is_deleted = FALSE;
```

---

### 3.4 class_students（班级-学生关联表）

```sql
CREATE TABLE class_students (
    class_id        UUID        NOT NULL REFERENCES classes(id),
    student_id      UUID        NOT NULL REFERENCES users(id),
    enrolled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,  -- 学生调班时设为 false
    PRIMARY KEY (class_id, student_id)
);

CREATE INDEX idx_cs_student
    ON class_students(student_id)
    WHERE is_active = TRUE;
-- 支持"查询某学生所在的所有班级"

CREATE INDEX idx_cs_class
    ON class_students(class_id)
    WHERE is_active = TRUE;
```

---

### 3.5 problems（题库表）

```sql
CREATE TABLE problems (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        REFERENCES tenants(id),  -- NULL 表示公共题库，所有学校共享
    problem_type        VARCHAR(30) NOT NULL
                        CHECK (problem_type IN ('arithmetic','multiple_choice','fill_in_blank')),
    grade_level         SMALLINT    NOT NULL CHECK (grade_level BETWEEN 1 AND 6),
    difficulty          VARCHAR(10) NOT NULL CHECK (difficulty IN ('easy','medium','hard')),
    curriculum_version  VARCHAR(20) NOT NULL DEFAULT '人教版',
    problem_text        TEXT        NOT NULL,
    reference_answer    TEXT        NOT NULL,
    solution_steps      JSONB,      -- 解题步骤数组
    common_errors       JSONB,      -- 常见错误列表 [{wrong_answer, error_type, note, frequency}]
    embedding_id        VARCHAR(100),  -- Qdrant point ID（异步填充）
    embedding_status    VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (embedding_status IN ('pending','done','failed')),
    tags                VARCHAR[]   NOT NULL DEFAULT '{}',
    is_deleted          BOOLEAN     NOT NULL DEFAULT FALSE,
    created_by          UUID        REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_problems_grade_type
    ON problems(grade_level, problem_type)
    WHERE is_deleted = FALSE;

CREATE INDEX idx_problems_tenant
    ON problems(tenant_id)
    WHERE is_deleted = FALSE AND tenant_id IS NOT NULL;

CREATE INDEX idx_problems_tags
    ON problems USING GIN(tags);  -- 支持 @> 数组包含查询

-- solution_steps JSONB 示例：
-- ["个位：5+7=12，写2进1", "十位：2+4+1=7，写7", "百位：3，写3", "结果：372"]

-- common_errors JSONB 示例：
-- [{"wrong_answer": "362", "error_type": "进位错误", "note": "十位进1遗漏", "frequency": 0.35}]
```

---

### 3.6 assignments（作业表）

```sql
CREATE TABLE assignments (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id),
    title           VARCHAR(200) NOT NULL,
    problem_ids     JSONB       NOT NULL,    -- 有序题目ID列表 ["uuid1","uuid2",...]，保持出题顺序
    due_date        TIMESTAMPTZ,             -- NULL 表示不设截止时间
    created_by      UUID        REFERENCES users(id),
    is_deleted      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 作业-班级多对多关联
CREATE TABLE assignment_classes (
    assignment_id   UUID        NOT NULL REFERENCES assignments(id),
    class_id        UUID        NOT NULL REFERENCES classes(id),
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (assignment_id, class_id)
);

CREATE INDEX idx_assignments_tenant
    ON assignments(tenant_id, created_at DESC)
    WHERE is_deleted = FALSE;

CREATE INDEX idx_assignments_due
    ON assignments(tenant_id, due_date)
    WHERE is_deleted = FALSE AND due_date IS NOT NULL;

CREATE INDEX idx_ac_class
    ON assignment_classes(class_id);

COMMENT ON COLUMN assignments.problem_ids IS 'JSONB有序数组，保存题目顺序，支持增减题目';
```

---

### 3.7 submissions（提交记录表）

```sql
CREATE TABLE submissions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id),
    assignment_id   UUID        NOT NULL REFERENCES assignments(id),
    student_id      UUID        NOT NULL REFERENCES users(id),
    status          VARCHAR(30) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','graded','partial_human_review','human_review')),
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_submissions_tenant_student
    ON submissions(tenant_id, student_id, submitted_at DESC);

CREATE INDEX idx_submissions_assignment
    ON submissions(assignment_id, submitted_at DESC);

-- 每个学生对同一作业只能有一条提交（通过唯一索引强制）
CREATE UNIQUE INDEX idx_submissions_student_assignment
    ON submissions(student_id, assignment_id);

-- 状态统计（教师端作业进度）
CREATE INDEX idx_submissions_assignment_status
    ON submissions(assignment_id, status);
```

---

### 3.8 submission_answers（提交答案明细表）

```sql
CREATE TABLE submission_answers (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id   UUID        NOT NULL REFERENCES submissions(id),
    problem_id      UUID        NOT NULL REFERENCES problems(id),
    answer_text     TEXT        NOT NULL,
    hint_level      SMALLINT    NOT NULL DEFAULT 0 CHECK (hint_level BETWEEN 0 AND 3),
    attempt_number  SMALLINT    NOT NULL DEFAULT 1  CHECK (attempt_number BETWEEN 1 AND 4)
);

CREATE INDEX idx_sa_submission ON submission_answers(submission_id);
CREATE INDEX idx_sa_problem    ON submission_answers(problem_id);

-- 同一次提交中每道题+每次尝试唯一（防止幂等问题）
CREATE UNIQUE INDEX idx_sa_submission_problem_attempt
    ON submission_answers(submission_id, problem_id, attempt_number);

COMMENT ON COLUMN submission_answers.hint_level IS '0=首次提交, 1-3=hint递增, 对应 feedback 层级';
COMMENT ON COLUMN submission_answers.attempt_number IS '同一道题第N次尝试（1-4），第4次会展示完整解法';
```

---

### 3.9 grading_results（批改结果表）

```sql
CREATE TABLE grading_results (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id),  -- 冗余，加速多租户查询
    submission_id       UUID        NOT NULL REFERENCES submissions(id),
    problem_id          UUID        NOT NULL REFERENCES problems(id),
    attempt_number      SMALLINT    NOT NULL DEFAULT 1,

    -- 最终批改结论
    is_correct          BOOLEAN,                    -- NULL = 人工审核中
    confidence_score    FLOAT       NOT NULL DEFAULT 0.0
                        CHECK (confidence_score BETWEEN 0 AND 1),
    error_type          VARCHAR(30)
                        CHECK (error_type IN ('计算错误','审题错误','进位错误','概念错误','无错误')),
    error_detail        TEXT,

    -- 面向学生的反馈
    feedback_text       TEXT,
    encouragement       TEXT,
    next_hint           TEXT,

    -- SymPy 验证结果
    sympy_expected      VARCHAR(200),
    sympy_is_correct    BOOLEAN,
    sympy_carry_error   BOOLEAN     NOT NULL DEFAULT FALSE,

    -- LLM 评估结果
    llm_reasoning       TEXT,
    llm_confidence      FLOAT,
    llm_model_used      VARCHAR(50),                -- 记录使用的具体模型版本

    -- 路由信息
    routed_to_human     BOOLEAN     NOT NULL DEFAULT FALSE,
    human_review_reason VARCHAR(50)
                        CHECK (human_review_reason IN ('low_confidence','sympy_llm_conflict','parse_error','llm_fallback', NULL)),
    source              VARCHAR(20) NOT NULL DEFAULT 'agent'
                        CHECK (source IN ('agent','human_override','rule_fallback','pending_human_review')),

    -- 完整推理链路（供 HITL 审核使用）
    agent_trace         JSONB,

    graded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_grading_submission
    ON grading_results(submission_id);

CREATE INDEX idx_grading_tenant_problem
    ON grading_results(tenant_id, problem_id);

-- 教师查看某个学生的批改记录（个人分析页面）
CREATE INDEX idx_grading_tenant_submission
    ON grading_results(tenant_id, submission_id, graded_at DESC);

-- HITL 筛选（高频查询：所有 routed_to_human=true 的记录）
CREATE INDEX idx_grading_human_review
    ON grading_results(tenant_id, routed_to_human)
    WHERE routed_to_human = TRUE;

-- 班级错误统计（分析仪表盘）
CREATE INDEX idx_grading_tenant_error_type
    ON grading_results(tenant_id, error_type, graded_at)
    WHERE is_correct = FALSE;

-- agent_trace JSONB 示例：
-- {
--   "processing_log": [
--     "parser_node: ok, type=arithmetic, expr='325+47', normalized='362'",
--     "sympy_verifier_node: expected='372', student='362', correct=false, carry=true",
--     "llm_evaluator_node: is_correct=true(?), conf=0.72, model=deepseek-chat, retry=0",
--     "confidence_router_node: CONFLICT, final=false, conf=0.75, route=human"
--   ],
--   "total_duration_ms": 1847,
--   "llm_calls": 2,
--   "cache_hit": true
-- }
```

---

### 3.10 human_review_queue（人工审核队列表）

```sql
CREATE TABLE human_review_queue (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id),
    grading_result_id   UUID        NOT NULL UNIQUE REFERENCES grading_results(id),
    reason              VARCHAR(50) NOT NULL DEFAULT 'low_confidence'
                        CHECK (reason IN ('low_confidence','sympy_llm_conflict','parse_error','llm_fallback')),
    priority            SMALLINT    NOT NULL DEFAULT 2,  -- 1=最高, 2=普通, 3=低
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','reviewing','reviewed')),

    -- 审核人信息
    reviewer_id         UUID        REFERENCES users(id),
    reviewed_at         TIMESTAMPTZ,

    -- 教师覆盖内容
    override_correct    BOOLEAN,
    override_error_type VARCHAR(30),
    override_feedback   TEXT,
    reviewer_notes      TEXT,

    -- 训练样例标记
    is_training_example BOOLEAN     NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 最高频查询：教师获取待审核列表
CREATE INDEX idx_hrq_tenant_status_pending
    ON human_review_queue(tenant_id, priority, created_at)
    WHERE status = 'pending';

-- 教师历史审核记录
CREATE INDEX idx_hrq_reviewer
    ON human_review_queue(reviewer_id, reviewed_at DESC)
    WHERE reviewer_id IS NOT NULL;

-- 训练样例统计（周批处理）
CREATE INDEX idx_hrq_training
    ON human_review_queue(tenant_id, reviewed_at)
    WHERE is_training_example = TRUE AND status = 'reviewed';

COMMENT ON COLUMN human_review_queue.priority IS '1=sympy_llm_conflict(最高), 2=low_confidence, 3=其他';
```

---

### 3.11 student_error_history（学生错误历史表）

```sql
CREATE TABLE student_error_history (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id),
    student_id      UUID        NOT NULL REFERENCES users(id),
    problem_id      UUID        REFERENCES problems(id),  -- 可为 NULL（题目被删除时）
    problem_type    VARCHAR(30),
    error_type      VARCHAR(30),
    error_detail    TEXT,
    knowledge_point VARCHAR(100),    -- 关联知识点标签，如"三位数加法进位"
    grade_level     SMALLINT,
    hint_level_used SMALLINT,        -- 这次错误用了几级 hint（0=首次即错）
    problem_snapshot JSONB,          -- 题目快照（防止题目被修改后历史失真）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Context 层滑动窗口查询的核心索引（每次批改都触发）
CREATE INDEX idx_seh_student_recent
    ON student_error_history(tenant_id, student_id, created_at DESC);

CREATE INDEX idx_seh_student_error_type
    ON student_error_history(tenant_id, student_id, error_type, created_at DESC);

-- 知识点薄弱分析（教师分析仪表盘）
CREATE INDEX idx_seh_knowledge_point
    ON student_error_history(tenant_id, knowledge_point, created_at DESC);

-- problem_snapshot JSONB 示例：
-- {
--   "problem_text": "325 + 47 = ___",
--   "reference_answer": "372",
--   "problem_type": "arithmetic",
--   "grade_level": 3,
--   "snapshot_at": "2026-07-19T14:30:00Z"
-- }

-- 触发器：批改答错时自动写入错误历史
CREATE OR REPLACE FUNCTION auto_record_error_history()
RETURNS TRIGGER AS $$
BEGIN
    -- 仅在 is_correct = false 且 source = 'agent' 或 'human_override' 时记录
    IF NEW.is_correct = FALSE AND NEW.source IN ('agent', 'human_override') THEN
        INSERT INTO student_error_history (
            tenant_id, student_id, problem_id, problem_type, error_type,
            error_detail, knowledge_point, grade_level, hint_level_used
        )
        SELECT
            NEW.tenant_id,
            s.student_id,
            NEW.problem_id,
            p.problem_type,
            NEW.error_type,
            NEW.error_detail,
            (NEW.agent_trace->>'knowledge_point'),
            p.grade_level,
            sa.hint_level
        FROM submissions s
        JOIN submission_answers sa ON sa.submission_id = NEW.submission_id
            AND sa.problem_id = NEW.problem_id
            AND sa.attempt_number = NEW.attempt_number
        JOIN problems p ON p.id = NEW.problem_id
        WHERE s.id = NEW.submission_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER grading_results_error_history
    AFTER INSERT OR UPDATE OF is_correct, source ON grading_results
    FOR EACH ROW
    WHEN (NEW.is_correct = FALSE AND NEW.source IN ('agent', 'human_override'))
    EXECUTE FUNCTION auto_record_error_history();
```

---

### 3.12 harness_runs（Harness 运行记录表）

```sql
CREATE TABLE harness_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    triggered_by        VARCHAR(50) NOT NULL DEFAULT 'manual'
                        CHECK (triggered_by IN ('ci','manual','scheduled')),
    prompt_version      VARCHAR(100),   -- Git commit hash of prompts/
    use_mock            BOOLEAN     NOT NULL DEFAULT TRUE,
    total_cases         INTEGER,
    passed_cases        INTEGER,
    failed_cases_json   JSONB,          -- 失败用例详情（case_id、预期/实际结论）
    accuracy            FLOAT,
    false_positive_rate FLOAT,
    false_negative_rate FLOAT,
    error_cls_accuracy  FLOAT,
    calibration_error   FLOAT,
    coverage_matrix     JSONB,          -- 覆盖矩阵（题型×年级×难度）
    passed              BOOLEAN,
    accuracy_threshold  FLOAT,          -- 运行时设置的通过阈值（默认0.94）
    duration_seconds    INTEGER,
    run_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_harness_runs_recent ON harness_runs(run_at DESC);
CREATE INDEX idx_harness_runs_ci ON harness_runs(triggered_by, run_at DESC) WHERE triggered_by = 'ci';

-- coverage_matrix JSONB 示例：
-- {
--   "grade1": {
--     "arithmetic":   {"easy": {"count":5, "accuracy":1.0}, "medium":{...}, "hard":{...}},
--     "fill_in_blank": {"easy": {...}, ...}
--   },
--   "grade2": {...},
--   "grade3": {...}
-- }
```

---

### 3.13 audit_logs（审计日志表）

```sql
CREATE TABLE audit_logs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        REFERENCES tenants(id),      -- sysadmin 操作可为 NULL
    operator_id     UUID        REFERENCES users(id),
    action          VARCHAR(50) NOT NULL,   -- 操作类型（枚举见下方）
    resource_type   VARCHAR(50),            -- 操作的资源类型
    resource_id     UUID,                   -- 操作的资源 ID
    detail          JSONB,                  -- 操作详情（旧值/新值/变更内容）
    ip_address      INET,
    user_agent      TEXT,
    result          VARCHAR(20) NOT NULL
                    CHECK (result IN ('success', 'failure')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- action 枚举（常用值）：
-- LOGIN, LOGOUT, LOGIN_FAILED, PASSWORD_CHANGED, PASSWORD_RESET
-- GRADE_OVERRIDE, HITL_REVIEW_STARTED
-- USER_CREATED, USER_DELETED, USER_SUSPENDED
-- CLASS_CREATED, CLASS_DELETED
-- BULK_IMPORT_USERS, BULK_IMPORT_PROBLEMS
-- DATA_EXPORTED
-- SYSTEM_CONFIG_CHANGED
-- HARNESS_TRIGGERED

CREATE INDEX idx_audit_tenant_created
    ON audit_logs(tenant_id, created_at DESC);

CREATE INDEX idx_audit_operator
    ON audit_logs(operator_id, created_at DESC);

CREATE INDEX idx_audit_action
    ON audit_logs(action, created_at DESC);

-- ── 行级安全策略：实现只追加 ────────────────────────────────────
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

-- 允许 INSERT（应用程序通过 mathgrader 角色）
CREATE POLICY audit_insert_policy ON audit_logs
    FOR INSERT WITH CHECK (true);

-- 仅 sysadmin 可读（通过应用层设置 app.current_role 参数）
CREATE POLICY audit_select_policy ON audit_logs
    FOR SELECT USING (
        current_setting('app.current_role', true) = 'sysadmin'
    );

-- 禁止 UPDATE 和 DELETE（不创建对应策略，默认拒绝）
COMMENT ON TABLE audit_logs IS '审计日志表，只追加，保留1年，不可删除';
```

---

## 四、物化视图（分析加速）

### 4.1 班级作业统计物化视图

```sql
-- 用于教师仪表盘的快速统计，避免每次实时聚合
CREATE MATERIALIZED VIEW mv_assignment_class_stats AS
SELECT
    gr.tenant_id,
    s.assignment_id,
    cs.class_id,
    COUNT(DISTINCT s.student_id)                          AS total_students,
    COUNT(DISTINCT CASE WHEN s.status != 'pending' THEN s.student_id END) AS submitted_count,
    COUNT(gr.id)                                          AS total_answers,
    SUM(CASE WHEN gr.is_correct = TRUE THEN 1 ELSE 0 END) AS correct_count,
    ROUND(AVG(CASE WHEN gr.is_correct IS NOT NULL THEN
        CASE WHEN gr.is_correct THEN 1.0 ELSE 0.0 END
    END)::NUMERIC, 4)                                     AS accuracy,
    NOW()                                                 AS refreshed_at
FROM grading_results gr
JOIN submissions s ON gr.submission_id = s.id
JOIN assignment_classes ac ON ac.assignment_id = s.assignment_id
JOIN class_students cs ON cs.class_id = ac.class_id AND cs.student_id = s.student_id
WHERE gr.attempt_number = 1  -- 只统计首次作答
GROUP BY gr.tenant_id, s.assignment_id, cs.class_id;

CREATE UNIQUE INDEX ON mv_assignment_class_stats(tenant_id, assignment_id, class_id);

-- 刷新策略：每次新提交批改后（由 GradingService 触发，或定时每5分钟）
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_assignment_class_stats;
```

### 4.2 学生薄弱知识点视图

```sql
-- 近30天学生薄弱知识点统计（按 error 频率排序）
CREATE MATERIALIZED VIEW mv_student_weak_points AS
SELECT
    tenant_id,
    student_id,
    knowledge_point,
    error_type,
    COUNT(*)                                    AS error_count,
    MAX(created_at)                             AS last_error_at,
    COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') AS recent_7d_count
FROM student_error_history
WHERE created_at > NOW() - INTERVAL '30 days'
  AND knowledge_point IS NOT NULL
GROUP BY tenant_id, student_id, knowledge_point, error_type
ORDER BY tenant_id, student_id, error_count DESC;

CREATE INDEX ON mv_student_weak_points(tenant_id, student_id);

-- 每天凌晨3点刷新（cron任务）
```

---

## 五、多租户隔离策略

### 5.1 行级隔离实现（Python）

```python
from contextvars import ContextVar
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

_tenant_ctx: ContextVar[str] = ContextVar('tenant_id')

def get_tenant_id() -> str:
    """从 contextvars 获取当前请求的 tenant_id（由认证中间件注入）"""
    try:
        return _tenant_ctx.get()
    except LookupError:
        raise RuntimeError("tenant_id 未设置，请确认认证中间件已正确运行")

class TenantSession:
    """
    包装 AsyncSession，所有查询自动注入 tenant_id 过滤条件。
    防止开发者忘记添加 tenant_id 导致跨租户数据泄漏。
    """
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, model, id: str):
        tenant_id = get_tenant_id()
        result = await self._session.execute(
            select(model).where(
                model.id == id,
                model.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def query(self, stmt):
        tenant_id = get_tenant_id()
        # 自动附加 WHERE tenant_id = :tid（假设 Model 有 tenant_id 字段）
        if hasattr(stmt.froms[0].entity_zero.class_, 'tenant_id'):
            stmt = stmt.where(stmt.froms[0].entity_zero.class_.tenant_id == tenant_id)
        return await self._session.execute(stmt)
```

### 5.2 各表 tenant_id 说明

| 表 | tenant_id | 说明 |
|---|-----------|------|
| tenants | 自身 | — |
| users | 必填 | 用户归属学校 |
| classes | 必填 | 班级归属学校 |
| problems | 可为 NULL | NULL = 公共题库（所有学校共享） |
| assignments | 必填 | 作业归属学校 |
| submissions | 必填（冗余自 assignment） | 加速批改热路径查询 |
| grading_results | 必填（冗余） | 加速分析查询 |
| human_review_queue | 必填（冗余） | 加速教师队列查询 |
| student_error_history | 必填 | 错误历史归属学校 |
| audit_logs | 可为 NULL | sysadmin 的全局操作可为 NULL |
| harness_runs | 不含 | 全局运维数据，不区分租户 |

---

## 六、关键查询分析

### 6.1 学生查看批改结果（最高频热路径）

```sql
-- 学生提交后立即查看所有题目的批改结果
EXPLAIN (ANALYZE, BUFFERS)
SELECT gr.*
FROM grading_results gr
WHERE gr.submission_id = '550e8400-e29b-41d4-a716-446655440000'
  AND gr.tenant_id = 'tenant-uuid'
ORDER BY gr.graded_at DESC;

-- 执行计划：Index Scan on idx_grading_submission
-- 估计行数：5-20（单次提交通常 5-20 道题）
-- 估计耗时：< 2ms（SSD）
```

### 6.2 Context 层读取学生错误历史（高频热路径）

```sql
-- 每次批改都会触发，读取学生近10条错误
EXPLAIN (ANALYZE, BUFFERS)
SELECT error_type, error_detail, problem_type, knowledge_point, hint_level_used
FROM student_error_history
WHERE tenant_id = 'tenant-uuid'
  AND student_id = 'student-uuid'
ORDER BY created_at DESC
LIMIT 10;

-- 执行计划：Index Scan using idx_seh_student_recent (covering index)
-- 估计行数：10
-- 估计耗时：< 1ms（覆盖索引，无需回表）
```

### 6.3 教师 HITL 队列查询（中频）

```sql
-- 教师打开审核队列（按优先级排序）
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    hrq.*,
    gr.confidence_score,
    gr.llm_reasoning,
    gr.human_review_reason,
    gr.problem_id,
    sa.answer_text AS student_answer,
    p.problem_text,
    p.reference_answer,
    u.display_name AS student_name,
    c.name AS class_name
FROM human_review_queue hrq
JOIN grading_results gr ON hrq.grading_result_id = gr.id
JOIN submissions s ON gr.submission_id = s.id
JOIN submission_answers sa ON sa.submission_id = s.id
    AND sa.problem_id = gr.problem_id
    AND sa.attempt_number = gr.attempt_number
JOIN problems p ON p.id = gr.problem_id
JOIN users u ON u.id = s.student_id
JOIN class_students cs ON cs.student_id = u.id
JOIN classes c ON c.id = cs.class_id
WHERE hrq.tenant_id = 'tenant-uuid'
  AND hrq.status = 'pending'
ORDER BY hrq.priority ASC, hrq.created_at ASC
LIMIT 20;

-- 执行计划：Bitmap Index Scan on idx_hrq_tenant_status_pending
-- 估计行数：< 50（一般积压不多）
-- 估计耗时：< 10ms
```

### 6.4 班级错误分布统计（低频，可缓存）

```sql
-- 教师查看班级错误分布（仪表盘）
-- 建议：结果缓存 Redis 5分钟（key: mg:stats:{tenant_id}:{assignment_id}）
SELECT
    gr.error_type,
    COUNT(*) AS error_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) AS percentage
FROM grading_results gr
JOIN submissions s ON gr.submission_id = s.id
JOIN assignment_classes ac ON ac.assignment_id = s.assignment_id
WHERE gr.tenant_id = 'tenant-uuid'
  AND ac.class_id = 'class-uuid'
  AND s.assignment_id = 'assignment-uuid'
  AND gr.is_correct = FALSE
  AND gr.attempt_number = 1
GROUP BY gr.error_type
ORDER BY error_count DESC;

-- 执行计划：Hash Join + Seq Scan（小数据集，全班~40人×10题=400行，可接受）
-- 估计耗时：< 30ms（未缓存时）
```

### 6.5 学生薄弱知识点（中频）

```sql
-- 学生近30天薄弱知识点（Top-5）
SELECT
    knowledge_point,
    error_type,
    COUNT(*) AS error_count,
    MAX(created_at) AS last_error_at
FROM student_error_history
WHERE tenant_id = 'tenant-uuid'
  AND student_id = 'student-uuid'
  AND created_at > NOW() - INTERVAL '30 days'
  AND knowledge_point IS NOT NULL
GROUP BY knowledge_point, error_type
ORDER BY error_count DESC
LIMIT 5;

-- 执行计划：Index Scan on idx_seh_student_recent，Filter by date range
-- 估计行数：< 100（30天学习记录）
-- 估计耗时：< 5ms
```

### 6.6 HITL 训练样例周批处理（低频）

```sql
-- 查询过去7天的训练样例（用于 Prompt 优化建议）
SELECT
    hrq.override_correct,
    hrq.override_error_type,
    hrq.reviewer_notes,
    gr.error_type AS ai_error_type,
    gr.confidence_score,
    gr.llm_reasoning,
    sa.answer_text AS student_answer,
    p.problem_text,
    p.reference_answer
FROM human_review_queue hrq
JOIN grading_results gr ON hrq.grading_result_id = gr.id
JOIN submissions s ON gr.submission_id = s.id
JOIN submission_answers sa ON sa.submission_id = s.id
    AND sa.problem_id = gr.problem_id
    AND sa.attempt_number = gr.attempt_number
JOIN problems p ON p.id = gr.problem_id
WHERE hrq.tenant_id = 'tenant-uuid'
  AND hrq.is_training_example = TRUE
  AND hrq.status = 'reviewed'
  AND hrq.reviewed_at > NOW() - INTERVAL '7 days'
  AND hrq.override_correct != gr.is_correct;  -- 只看 AI 判断与教师覆盖不一致的

-- 命中索引：idx_hrq_training
-- 估计行数：< 100（每周误判案例不多）
```

---

## 七、索引汇总

| 表 | 索引名 | 列 | 类型 | 用途 |
|---|--------|---|------|------|
| users | idx_users_tenant_role | (tenant_id, role) WHERE !deleted | B-tree 部分 | 按角色查用户 |
| problems | idx_problems_grade_type | (grade_level, problem_type) WHERE !deleted | B-tree 部分 | 题库筛选 |
| problems | idx_problems_tags | (tags) | GIN | 标签数组查询 @> |
| assignments | idx_assignments_tenant | (tenant_id, created_at) WHERE !deleted | B-tree 部分 | 作业列表 |
| submissions | idx_submissions_tenant_student | (tenant_id, student_id, submitted_at) | B-tree | 学生提交历史 |
| submissions | idx_submissions_student_assignment | (student_id, assignment_id) | UNIQUE | 防重复提交 |
| submission_answers | idx_sa_submission | (submission_id) | B-tree | 提交答案查询 |
| grading_results | idx_grading_submission | (submission_id) | B-tree | 批改结果查询 |
| grading_results | idx_grading_tenant_problem | (tenant_id, problem_id) | B-tree | 题目维度统计 |
| grading_results | idx_grading_human_review | (tenant_id, routed_to_human) WHERE TRUE | B-tree 部分 | HITL 筛选 |
| human_review_queue | idx_hrq_tenant_status_pending | (tenant_id, priority, created_at) WHERE pending | B-tree 部分 | 待审核队列 |
| student_error_history | idx_seh_student_recent | (tenant_id, student_id, created_at) | B-tree | Context 层滑窗 |
| audit_logs | idx_audit_tenant_created | (tenant_id, created_at) | B-tree | 审计查询 |

---

## 八、数据库迁移方案（Alembic）

### 8.1 版本管理规范

```
alembic/
├── env.py               -- 迁移环境配置（asyncpg + contextvars）
├── script.py.mako       -- 迁移文件模板
└── versions/
    ├── 001_initial_schema.py          -- 初始建表（全量）
    ├── 002_add_audit_logs.py          -- 审计日志表
    ├── 003_add_embedding_status.py    -- problems.embedding_status 字段
    ├── 004_add_assignment_classes.py  -- 作业-班级多对多关联
    └── ...

命名规范：{三位序号}_{描述}.py
描述：下划线分隔，最多5个词，说明"做了什么"而非"为什么"
```

### 8.2 迁移执行规范

```bash
# 开发环境：直接升级
alembic upgrade head

# 生产环境：先 dry-run 检查 SQL，人工 review 后再执行
alembic upgrade head --sql > migration_preview.sql
# ── 人工检查 migration_preview.sql ──
# 确认：无全表锁（LOCK TABLE）、无大表全量扫描、无数据丢失
alembic upgrade head

# 查看当前迁移版本
alembic current

# 查看迁移历史
alembic history --verbose
```

### 8.3 不可逆操作保护

```python
# alembic/versions/005_drop_old_column.py
"""
WARNING: 此迁移删除了 grading_results.old_field 列。
执行前请确认：
1. 已完成全量数据备份
2. 确认该字段已不被任何代码引用（grep -r 'old_field' --include='*.py'）
downgrade 无法恢复已删除的数据。
"""

def upgrade():
    # 先软删除（Phase 1：重命名为 _deprecated_old_field）
    op.alter_column('grading_results', 'old_field', new_column_name='_deprecated_old_field')
    # 下个迭代再物理删除

def downgrade():
    op.alter_column('grading_results', '_deprecated_old_field', new_column_name='old_field')
```

---

## 九、数据归档策略

| 数据类型 | 保留周期 | 归档策略 | 执行时机 |
|---------|---------|---------|---------|
| grading_results | 永久 | 3年后迁移至 `grading_results_archive` 冷存储表 | 每学期末评估 |
| submission_answers | 永久 | 同上 | 同上 |
| student_error_history | 2年 | 定期清理 2 年前记录 | 每月1日凌晨3点 |
| human_review_queue（已审核） | 1年 | 1年后归档 | 每月1日 |
| harness_runs | 6个月 | 超期自动删除，保留最近50条 | 每月1日 |
| audit_logs | 1年 | 法规要求不可删除，满1年后转冷存储 | 每年 |

```sql
-- 定期清理示例（由 cron 定时任务触发）
-- 清理2年前的学生错误历史
DELETE FROM student_error_history
WHERE created_at < NOW() - INTERVAL '2 years'
  AND tenant_id = :tenant_id;

-- 清理6个月前的 harness_runs（保留最近50条）
DELETE FROM harness_runs
WHERE run_at < NOW() - INTERVAL '6 months'
  AND id NOT IN (
    SELECT id FROM harness_runs ORDER BY run_at DESC LIMIT 50
  );
```

---

## 十、数据库配置参数

```ini
# postgresql.conf 推荐配置（基于 8核 16GB 服务器）

# ── 内存 ────────────────────────────────────────────────────
shared_buffers = 4GB                  # 总内存的 25%
effective_cache_size = 12GB           # 总内存的 75%（查询规划器参考）
work_mem = 64MB                       # 每个排序/Hash Join 操作的内存
maintenance_work_mem = 1GB            # VACUUM/CREATE INDEX 内存

# ── 连接 ─────────────────────────────────────────────────────
max_connections = 100                 # 应用层用 asyncpg 连接池，DB 连接数不宜过多
# asyncpg 连接池配置（在 app 端）：
# min_size=5, max_size=25, command_timeout=30,
# max_inactive_connection_lifetime=300

# ── WAL 与可靠性 ─────────────────────────────────────────────
wal_level = replica                   # 支持流复制（为将来高可用准备）
checkpoint_completion_target = 0.9    # 平滑 checkpoint，减少 IO 突刺
wal_buffers = 64MB
synchronous_commit = on               # 确保 WAL 落盘，防止数据丢失

# ── SSD 性能优化 ─────────────────────────────────────────────
random_page_cost = 1.1                # SSD 随机IO代价降低（默认4.0）
effective_io_concurrency = 200        # SSD 并发 IO 数

# ── 统计与监控 ────────────────────────────────────────────────
log_min_duration_statement = 500      # 记录超过 500ms 的慢查询
pg_stat_statements.track = all        # 启用查询统计（需安装扩展）
log_connections = on
log_disconnections = on

# ── 自动清理（VACUUM） ────────────────────────────────────────
autovacuum = on
autovacuum_vacuum_cost_delay = 2ms    # 减少 VACUUM 对 IO 的影响
```

---

## 十一、备份方案

| 备份类型 | 频率 | 保留时间 | 工具 | 存储位置 |
|---------|------|---------|------|---------|
| 全量备份 | 每天凌晨 2:00 | 保留 30 天 | `pg_dump -Fc` | `/backup/db_YYYYMMDD.dump` |
| WAL 增量 | 连续 | 保留 7 天 | WAL 归档 | `/backup/wal/` |
| Qdrant 向量数据 | 每天凌晨 2:30 | 保留 7 天 | `tar -czf` | `/backup/qdrant_YYYYMMDD.tar.gz` |
| 备份验证 | 每周日 3:00 | — | 恢复到临时库并验证行数 | — |

```bash
# 全量备份脚本（/opt/math-grader/scripts/backup.sh）
BACKUP_DIR="/opt/math-grader/backup"
DATE=$(date +%Y%m%d_%H%M%S)

docker compose exec -T postgres \
  pg_dump -U mathgrader -Fc --no-password mathgrader \
  > $BACKUP_DIR/db_${DATE}.dump

# 验证备份文件完整性
pg_restore --list $BACKUP_DIR/db_${DATE}.dump > /dev/null
echo "[$(date)] Backup OK: db_${DATE}.dump ($(du -sh $BACKUP_DIR/db_${DATE}.dump | cut -f1))"

# 删除 30 天前旧备份
find $BACKUP_DIR -name "db_*.dump" -mtime +30 -delete

# 备份恢复命令（灾难恢复）
# docker compose exec -T postgres pg_restore -U mathgrader -d mathgrader --no-owner < db_20260719.dump
```

---

## 十二、敏感数据处理

| 字段 | 数据级别 | 处理方式 |
|------|---------|---------|
| `users.password_hash` | 第四级（最高） | bcrypt 单向哈希，rounds=12，不可逆 |
| `users.display_name` | 第四级 | 明文存储（学校内网，Phase 2 评估字段级加密）；不发送给 LLM |
| `grading_results.agent_trace` | 第三级 | JSONB 中不含学生姓名，仅 UUID 和答案文本 |
| LLM 调用 Prompt | 第二级 | 仅含题目文本、答案、年级数字；不含姓名/学号/UUID |
| `audit_logs.detail` | 第三级 | 包含操作详情，通过 RLS 限制只有 sysadmin 可读 |

### LLM 脱敏规则（强制执行）

发送给 DeepSeek/Qianwen 的请求**只允许包含**：
- 题目文本（如"325 + 47 = ___"）
- 学生答案文本（如"362"）
- 参考答案（如"372"）
- 年级数字（如"3"）
- 课程版本（如"人教版"）
- 学生错误历史（仅 error_type/error_detail，不含学生标识）

**严禁包含**：学生姓名、学号、班级名称、学校名称、用户 UUID、IP 地址。
