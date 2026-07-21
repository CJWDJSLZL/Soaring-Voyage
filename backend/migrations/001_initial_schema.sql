-- Soaring Voyage PostgreSQL 16 initial schema.
-- Applied once by the migration container/Makefile. All tenant-facing tables use
-- FORCE RLS so table ownership cannot accidentally bypass tenant isolation.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
$$;

CREATE FUNCTION app_current_role() RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.current_role', true), '')
$$;

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TABLE tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name varchar(100) NOT NULL,
    code varchar(50) NOT NULL UNIQUE,
    curriculum varchar(20) NOT NULL DEFAULT '人教版',
    config jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    role varchar(20) NOT NULL CHECK (role IN ('student','teacher','admin','sysadmin')),
    username varchar(100) NOT NULL,
    display_name varchar(100),
    password_hash varchar(255),
    grade_level smallint CHECK (grade_level BETWEEN 1 AND 6),
    login_fail_count smallint NOT NULL DEFAULT 0 CHECK (login_fail_count >= 0),
    locked_until timestamptz,
    force_change_password boolean NOT NULL DEFAULT false,
    is_deleted boolean NOT NULL DEFAULT false,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, username),
    UNIQUE (tenant_id, id),
    CHECK ((role = 'student') OR grade_level IS NULL)
);

CREATE TABLE classes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    grade_level smallint NOT NULL CHECK (grade_level BETWEEN 1 AND 6),
    name varchar(100) NOT NULL,
    teacher_id uuid NOT NULL,
    academic_year varchar(10) NOT NULL CHECK (academic_year ~ '^20[0-9]{2}-20[0-9]{2}$'),
    is_deleted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name, academic_year),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, teacher_id) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE class_students (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    class_id uuid NOT NULL,
    student_id uuid NOT NULL,
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, class_id, student_id),
    FOREIGN KEY (tenant_id, class_id) REFERENCES classes(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, student_id) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE problems (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
    problem_type varchar(30) NOT NULL CHECK (problem_type IN ('arithmetic','multiple_choice','fill_in_blank')),
    grade_level smallint NOT NULL CHECK (grade_level BETWEEN 1 AND 6),
    difficulty varchar(10) NOT NULL CHECK (difficulty IN ('easy','medium','hard')),
    curriculum_version varchar(20) NOT NULL DEFAULT '人教版',
    problem_text text NOT NULL CHECK (length(problem_text) > 0),
    reference_answer text NOT NULL,
    solution_steps jsonb,
    common_errors jsonb,
    embedding_id varchar(100),
    embedding_status varchar(20) NOT NULL DEFAULT 'pending' CHECK (embedding_status IN ('pending','done','failed')),
    tags varchar[] NOT NULL DEFAULT '{}'::varchar[],
    is_deleted boolean NOT NULL DEFAULT false,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    title varchar(200) NOT NULL CHECK (length(title) > 0),
    due_date timestamptz,
    created_by uuid NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE assignment_classes (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    assignment_id uuid NOT NULL,
    class_id uuid NOT NULL,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, assignment_id, class_id),
    FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignments(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, class_id) REFERENCES classes(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE assignment_problems (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    assignment_id uuid NOT NULL,
    problem_id uuid NOT NULL REFERENCES problems(id) ON DELETE RESTRICT,
    position smallint NOT NULL CHECK (position > 0),
    points numeric(7,2) NOT NULL DEFAULT 1 CHECK (points > 0),
    PRIMARY KEY (tenant_id, assignment_id, problem_id),
    UNIQUE (assignment_id, problem_id),
    UNIQUE (tenant_id, assignment_id, position),
    FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignments(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE submissions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    assignment_id uuid NOT NULL,
    student_id uuid NOT NULL,
    status varchar(30) NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','grading','graded','partial_human_review','reviewed','failed')
    ),
    submitted_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (student_id, assignment_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignments(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, student_id) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE submission_answers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    submission_id uuid NOT NULL,
    problem_id uuid NOT NULL REFERENCES problems(id) ON DELETE RESTRICT,
    answer_text text NOT NULL,
    hint_level smallint NOT NULL DEFAULT 0 CHECK (hint_level BETWEEN 0 AND 3),
    attempt_number smallint NOT NULL DEFAULT 1 CHECK (attempt_number BETWEEN 1 AND 4),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (submission_id, problem_id, attempt_number),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, submission_id) REFERENCES submissions(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE grading_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    submission_id uuid NOT NULL,
    problem_id uuid NOT NULL REFERENCES problems(id) ON DELETE RESTRICT,
    attempt_number smallint NOT NULL DEFAULT 1 CHECK (attempt_number BETWEEN 1 AND 4),
    is_correct boolean,
    confidence_score double precision NOT NULL DEFAULT 0 CHECK (confidence_score BETWEEN 0 AND 1),
    error_type varchar(30) CHECK (error_type IN ('计算错误','审题错误','进位错误','概念错误','未作答','无错误')),
    error_detail text,
    feedback_text text,
    encouragement text,
    next_hint text,
    sympy_expected varchar(200),
    sympy_is_correct boolean,
    sympy_carry_error boolean NOT NULL DEFAULT false,
    llm_reasoning text,
    llm_confidence double precision CHECK (llm_confidence BETWEEN 0 AND 1),
    llm_model_used varchar(100),
    routed_to_human boolean NOT NULL DEFAULT false,
    human_review_reason varchar(50) CHECK (
        human_review_reason IS NULL OR human_review_reason IN ('low_confidence','sympy_llm_conflict','parse_error','llm_fallback')
    ),
    source varchar(30) NOT NULL DEFAULT 'agent' CHECK (
        source IN ('agent','human_override','rule_fallback','pending_human_review')
    ),
    agent_trace jsonb,
    graded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (submission_id, problem_id, attempt_number),
    FOREIGN KEY (tenant_id, submission_id) REFERENCES submissions(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE human_review_queue (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    grading_result_id uuid NOT NULL UNIQUE,
    reason varchar(50) NOT NULL DEFAULT 'low_confidence' CHECK (
        reason IN ('low_confidence','sympy_llm_conflict','parse_error','llm_fallback')
    ),
    priority smallint NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    status varchar(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','reviewing','reviewed')),
    reviewer_id uuid,
    reviewed_at timestamptz,
    override_correct boolean,
    override_error_type varchar(30),
    override_feedback text,
    reviewer_notes text,
    is_training_example boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, grading_result_id) REFERENCES grading_results(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, reviewer_id) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE student_error_history (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    grading_result_id uuid NOT NULL UNIQUE REFERENCES grading_results(id) ON DELETE CASCADE,
    student_id uuid NOT NULL,
    problem_id uuid REFERENCES problems(id) ON DELETE SET NULL,
    problem_type varchar(30),
    error_type varchar(30),
    error_detail text,
    knowledge_point varchar(100),
    grade_level smallint CHECK (grade_level BETWEEN 1 AND 6),
    hint_level_used smallint CHECK (hint_level_used BETWEEN 0 AND 3),
    problem_snapshot jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, student_id) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE harness_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status varchar(20) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed')),
    triggered_by varchar(50) NOT NULL DEFAULT 'manual' CHECK (triggered_by IN ('ci','manual','scheduled')),
    prompt_version varchar(100),
    use_mock boolean NOT NULL DEFAULT true,
    total_cases integer CHECK (total_cases >= 0),
    passed_cases integer CHECK (passed_cases >= 0),
    failed_cases_json jsonb,
    accuracy double precision CHECK (accuracy BETWEEN 0 AND 1),
    false_positive_rate double precision CHECK (false_positive_rate BETWEEN 0 AND 1),
    false_negative_rate double precision CHECK (false_negative_rate BETWEEN 0 AND 1),
    error_cls_accuracy double precision CHECK (error_cls_accuracy BETWEEN 0 AND 1),
    calibration_error double precision CHECK (calibration_error BETWEEN 0 AND 1),
    coverage_matrix jsonb,
    passed boolean,
    accuracy_threshold double precision NOT NULL DEFAULT 0.94 CHECK (accuracy_threshold BETWEEN 0 AND 1),
    duration_seconds integer CHECK (duration_seconds >= 0),
    run_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    job_type varchar(50) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    error_message text,
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts smallint NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    locked_by varchar(100),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE audit_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
    operator_id uuid REFERENCES users(id) ON DELETE SET NULL,
    action varchar(50) NOT NULL,
    resource_type varchar(50),
    resource_id uuid,
    detail jsonb,
    ip_address inet,
    user_agent text,
    result varchar(20) NOT NULL CHECK (result IN ('success','failure')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Updated timestamp triggers.
CREATE TRIGGER tenants_updated_at BEFORE UPDATE ON tenants FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER classes_updated_at BEFORE UPDATE ON classes FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER problems_updated_at BEFORE UPDATE ON problems FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER assignments_updated_at BEFORE UPDATE ON assignments FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER submissions_updated_at BEFORE UPDATE ON submissions FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER human_review_queue_updated_at BEFORE UPDATE ON human_review_queue FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Hot-path and partial indexes.
CREATE INDEX idx_users_tenant_role ON users (tenant_id, role) WHERE NOT is_deleted;
CREATE INDEX idx_classes_teacher ON classes (tenant_id, teacher_id) WHERE NOT is_deleted;
CREATE INDEX idx_cs_student ON class_students (tenant_id, student_id) WHERE is_active;
CREATE INDEX idx_problems_grade_type ON problems (grade_level, problem_type) WHERE NOT is_deleted;
CREATE INDEX idx_problems_tenant ON problems (tenant_id) WHERE NOT is_deleted AND tenant_id IS NOT NULL;
CREATE INDEX idx_problems_tags ON problems USING gin (tags);
CREATE INDEX idx_assignments_recent ON assignments (tenant_id, created_at DESC) WHERE NOT is_deleted;
CREATE INDEX idx_assignments_due ON assignments (tenant_id, due_date) WHERE NOT is_deleted AND due_date IS NOT NULL;
CREATE INDEX idx_assignment_classes_class ON assignment_classes (tenant_id, class_id);
CREATE INDEX idx_assignment_problems_order ON assignment_problems (tenant_id, assignment_id, position);
CREATE INDEX idx_submissions_student_recent ON submissions (tenant_id, student_id, submitted_at DESC);
CREATE INDEX idx_submissions_assignment_status ON submissions (tenant_id, assignment_id, status);
CREATE INDEX idx_answers_submission ON submission_answers (tenant_id, submission_id);
CREATE INDEX idx_answers_problem ON submission_answers (problem_id);
CREATE INDEX idx_grading_submission ON grading_results (tenant_id, submission_id, graded_at DESC);
CREATE INDEX idx_grading_problem ON grading_results (tenant_id, problem_id);
CREATE INDEX idx_grading_human ON grading_results (tenant_id, graded_at) WHERE routed_to_human;
CREATE INDEX idx_grading_errors ON grading_results (tenant_id, error_type, graded_at DESC) WHERE is_correct = false;
CREATE INDEX idx_hrq_pending ON human_review_queue (tenant_id, priority, created_at) WHERE status = 'pending';
CREATE INDEX idx_hrq_reviewer ON human_review_queue (tenant_id, reviewer_id, reviewed_at DESC) WHERE reviewer_id IS NOT NULL;
CREATE INDEX idx_error_student_recent ON student_error_history (tenant_id, student_id, created_at DESC);
CREATE INDEX idx_error_knowledge ON student_error_history (tenant_id, knowledge_point, created_at DESC);
CREATE INDEX idx_harness_recent ON harness_runs (run_at DESC);
CREATE INDEX idx_harness_ci ON harness_runs (run_at DESC) WHERE triggered_by = 'ci';
CREATE INDEX idx_jobs_claim ON jobs (available_at, created_at) WHERE status = 'queued';
CREATE INDEX idx_jobs_tenant ON jobs (tenant_id, created_at DESC);
CREATE INDEX idx_audit_tenant_created ON audit_logs (tenant_id, created_at DESC);
CREATE INDEX idx_audit_operator ON audit_logs (operator_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_logs (action, created_at DESC);

-- A plain problem_id FK proves existence but not tenant ownership. Shared
-- problems (tenant_id IS NULL) are valid; tenant-owned problems must match.
CREATE FUNCTION enforce_problem_tenant() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM problems p
        WHERE p.id = NEW.problem_id
          AND (p.tenant_id IS NULL OR p.tenant_id = NEW.tenant_id)
    ) THEN
        RAISE EXCEPTION 'problem does not belong to tenant' USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER assignment_problems_problem_tenant
BEFORE INSERT OR UPDATE OF tenant_id, problem_id ON assignment_problems
FOR EACH ROW EXECUTE FUNCTION enforce_problem_tenant();
CREATE TRIGGER submission_answers_problem_tenant
BEFORE INSERT OR UPDATE OF tenant_id, problem_id ON submission_answers
FOR EACH ROW EXECUTE FUNCTION enforce_problem_tenant();
CREATE TRIGGER grading_results_problem_tenant
BEFORE INSERT OR UPDATE OF tenant_id, problem_id ON grading_results
FOR EACH ROW EXECUTE FUNCTION enforce_problem_tenant();
CREATE TRIGGER student_error_history_problem_tenant
BEFORE INSERT OR UPDATE OF tenant_id, problem_id ON student_error_history
FOR EACH ROW EXECUTE FUNCTION enforce_problem_tenant();

CREATE FUNCTION enforce_grading_result_tenant() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM grading_results g
        WHERE g.id = NEW.grading_result_id AND g.tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'grading result does not belong to tenant' USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER student_error_history_grading_tenant
BEFORE INSERT OR UPDATE OF tenant_id, grading_result_id ON student_error_history
FOR EACH ROW EXECUTE FUNCTION enforce_grading_result_tenant();

-- Automatically preserve a snapshot of every final wrong result.
CREATE FUNCTION record_student_error() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
    IF NEW.is_correct = false AND NEW.source IN ('agent', 'human_override')
       AND (TG_OP = 'INSERT' OR OLD.is_correct IS DISTINCT FROM NEW.is_correct OR OLD.source IS DISTINCT FROM NEW.source) THEN
        INSERT INTO student_error_history (
            tenant_id, grading_result_id, student_id, problem_id, problem_type, error_type, error_detail,
            knowledge_point, grade_level, hint_level_used, problem_snapshot
        )
        SELECT NEW.tenant_id, NEW.id, s.student_id, p.id, p.problem_type, NEW.error_type,
               NEW.error_detail, NEW.agent_trace->>'knowledge_point', p.grade_level,
               sa.hint_level,
               jsonb_build_object('problem_text', p.problem_text,
                                  'reference_answer', p.reference_answer,
                                  'problem_type', p.problem_type,
                                  'grade_level', p.grade_level,
                                  'snapshot_at', now())
        FROM submissions s
        JOIN problems p ON p.id = NEW.problem_id
        LEFT JOIN submission_answers sa
          ON sa.submission_id = NEW.submission_id
         AND sa.problem_id = NEW.problem_id
         AND sa.attempt_number = NEW.attempt_number
        WHERE s.id = NEW.submission_id AND s.tenant_id = NEW.tenant_id
        ON CONFLICT (grading_result_id) DO UPDATE SET
            error_type = EXCLUDED.error_type,
            error_detail = EXCLUDED.error_detail,
            hint_level_used = EXCLUDED.hint_level_used,
            problem_snapshot = EXCLUDED.problem_snapshot;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER grading_results_error_history
AFTER INSERT OR UPDATE OF is_correct, source ON grading_results
FOR EACH ROW EXECUTE FUNCTION record_student_error();

-- Tenant RLS. FORCE makes policies apply to the migration/table owner as well.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_isolation ON tenants
    USING (id = app_current_tenant())
    WITH CHECK (id = app_current_tenant());

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_tenant_isolation ON users USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE classes ENABLE ROW LEVEL SECURITY;
ALTER TABLE classes FORCE ROW LEVEL SECURITY;
CREATE POLICY classes_tenant_isolation ON classes USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE class_students ENABLE ROW LEVEL SECURITY;
ALTER TABLE class_students FORCE ROW LEVEL SECURITY;
CREATE POLICY class_students_tenant_isolation ON class_students USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY assignments_tenant_isolation ON assignments USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE assignment_classes ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignment_classes FORCE ROW LEVEL SECURITY;
CREATE POLICY assignment_classes_tenant_isolation ON assignment_classes USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE assignment_problems ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignment_problems FORCE ROW LEVEL SECURITY;
CREATE POLICY assignment_problems_tenant_isolation ON assignment_problems USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE submissions FORCE ROW LEVEL SECURITY;
CREATE POLICY submissions_tenant_isolation ON submissions USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE submission_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY submission_answers_tenant_isolation ON submission_answers USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE grading_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE grading_results FORCE ROW LEVEL SECURITY;
CREATE POLICY grading_results_tenant_isolation ON grading_results USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE human_review_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_review_queue FORCE ROW LEVEL SECURITY;
CREATE POLICY human_review_queue_tenant_isolation ON human_review_queue USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE student_error_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE student_error_history FORCE ROW LEVEL SECURITY;
CREATE POLICY student_error_history_tenant_isolation ON student_error_history USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY jobs_tenant_isolation ON jobs USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());

-- Shared problems are visible to every tenant; only tenant-owned problems are mutable.
ALTER TABLE problems ENABLE ROW LEVEL SECURITY;
ALTER TABLE problems FORCE ROW LEVEL SECURITY;
CREATE POLICY problems_select_policy ON problems FOR SELECT
    USING (tenant_id IS NULL OR tenant_id = app_current_tenant());
CREATE POLICY problems_insert_policy ON problems FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY problems_update_policy ON problems FOR UPDATE
    USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY problems_delete_policy ON problems FOR DELETE
    USING (tenant_id = app_current_tenant());

-- Audit logs are append-only. A sysadmin can read; nobody can update/delete.
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_insert_policy ON audit_logs FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant() OR (tenant_id IS NULL AND app_current_role() = 'sysadmin'));
CREATE POLICY audit_select_policy ON audit_logs FOR SELECT
    USING (app_current_role() = 'sysadmin');

CREATE FUNCTION reject_audit_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs are append-only' USING ERRCODE = '42501';
END;
$$;
CREATE TRIGGER audit_logs_immutable
BEFORE UPDATE OR DELETE ON audit_logs
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

COMMENT ON TABLE audit_logs IS 'Append-only security and compliance audit trail';
COMMENT ON COLUMN grading_results.agent_trace IS 'Machine trace only; must not contain student PII';

-- Runtime credentials are intentionally non-owner/non-superuser. Migrations
-- run with MIGRATION_DATABASE_URL and grant only data-plane privileges here.
GRANT USAGE ON SCHEMA public TO soaring_voyage_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO soaring_voyage_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO soaring_voyage_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO soaring_voyage_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO soaring_voyage_app;
