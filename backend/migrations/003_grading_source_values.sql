-- Align persisted grading sources with the current grading engine routes.

ALTER TABLE grading_results
DROP CONSTRAINT IF EXISTS grading_results_source_check;

ALTER TABLE grading_results
ADD CONSTRAINT grading_results_source_check CHECK (
    source IN (
        'agent',
        'human_override',
        'rule_fallback',
        'pending_human_review',
        'empty_answer',
        'llm_only',
        'sympy_llm_consensus',
        'sympy_llm_conflict'
    )
);

CREATE OR REPLACE FUNCTION record_student_error() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
    IF NEW.is_correct = false AND NEW.source <> 'pending_human_review'
       AND (TG_OP = 'INSERT' OR OLD.is_correct IS DISTINCT FROM NEW.is_correct OR OLD.source IS DISTINCT FROM NEW.source) THEN
        INSERT INTO student_error_history (
            tenant_id, grading_result_id, student_id, problem_id, problem_type, error_type, error_detail,
            knowledge_point, grade_level, hint_level_used, problem_snapshot
        )
        SELECT NEW.tenant_id, NEW.id, s.student_id, p.id, p.problem_type, NEW.error_type,
               NEW.error_detail,
               COALESCE(NEW.agent_trace->>'knowledge_point', p.tags[1]),
               p.grade_level,
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
            knowledge_point = EXCLUDED.knowledge_point,
            hint_level_used = EXCLUDED.hint_level_used,
            problem_snapshot = EXCLUDED.problem_snapshot;
    END IF;
    RETURN NEW;
END;
$$;
