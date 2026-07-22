import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parents[3]


def test_second_migration_adds_nonnegative_token_version_without_rewriting_initial_schema() -> None:
    migrations = ROOT / "backend" / "migrations"
    initial = (migrations / "001_initial_schema.sql").read_text(encoding="utf-8")
    second = (migrations / "002_user_token_version.sql").read_text(encoding="utf-8").lower()

    assert "token_version" not in initial.lower()
    assert "alter table users" in second
    assert "token_version integer not null default 0" in second
    assert "check (token_version >= 0)" in second


def test_ci_prepares_and_runs_runtime_role_postgres_integration() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "backend-ci.yml").read_text(encoding="utf-8")
    workflow = workflow_text.lower()

    assert "prepare runtime-role integration database" in workflow
    assert "alter role soaring_voyage_app login password" in workflow
    assert "test_database_url" in workflow
    assert "test_default_tenant_id" in workflow

    admin_url_match = re.search(r"^\s{6}DATABASE_URL:\s*(\S+)\s*$", workflow_text, re.MULTILINE)
    assert admin_url_match is not None
    admin_url = urlsplit(admin_url_match.group(1))
    assert admin_url.username == "postgres"
    assert admin_url.password
