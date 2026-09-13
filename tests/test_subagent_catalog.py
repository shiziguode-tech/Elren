from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

CATALOG_PATH = Path(__file__).parents[1] / "deepdesk" / "subagent_catalog.json"
EXPECTED_FIELDS = {
    "id",
    "category",
    "name_zh",
    "name_en",
    "mission",
    "trigger_hints",
    "boundary",
    "permission",
    "popular",
    "icon_key",
}
ALLOWED_PERMISSIONS = {
    "read_only",
    "draft_only",
    "workspace_write",
    "approval_gated",
}
EXPECTED_CATEGORIES = {
    "orchestration",
    "software_engineering",
    "quality_security",
    "research_knowledge",
    "data_analytics",
    "product_design",
    "documents_office",
    "operations_delivery",
    "communication_productivity",
    "domain_advisory",
}
REQUIRED_POPULAR_ROLES = {
    "orch_task_decomposer",
    "orch_requirements_analyst",
    "eng_backend",
    "eng_frontend",
    "eng_debugger",
    "qa_test_strategist",
    "qa_release",
    "research_web",
    "research_source_verifier",
    "research_technical_docs",
    "data_cleaner",
    "data_spreadsheet",
    "product_manager",
    "design_ux",
    "design_ui",
    "docs_editor",
    "slides_designer",
    "slides_overflow_qa",
    "work_project_manager",
    "work_email_drafter",
}


def _catalog() -> list[dict[str, object]]:
    parsed = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, list)
    return parsed


def test_subagent_catalog_has_unique_stable_ids_without_retired_role() -> None:
    catalog = _catalog()
    ids = [entry["id"] for entry in catalog]

    assert len(catalog) == 99
    assert 'finance_research' not in {preset['id'] for preset in catalog}
    assert len(ids) == len(set(ids))
    assert all(isinstance(role_id, str) for role_id in ids)
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{2,63}", role_id) for role_id in ids)


def test_subagent_catalog_records_have_only_the_required_contract_fields() -> None:
    catalog = _catalog()

    for entry in catalog:
        assert set(entry) == EXPECTED_FIELDS
        for field in ("id", "category", "name_zh", "name_en", "mission", "boundary", "icon_key"):
            assert isinstance(entry[field], str)
            assert entry[field].strip()
        assert isinstance(entry["trigger_hints"], list)
        assert len(entry["trigger_hints"]) >= 2
        assert all(isinstance(hint, str) and hint.strip() for hint in entry["trigger_hints"])
        assert isinstance(entry["popular"], bool)
        assert re.fullmatch(r"[a-z][a-z0-9_]*", entry["icon_key"])


def test_subagent_catalog_uses_the_supported_categories_and_permissions() -> None:
    catalog = _catalog()
    category_counts = Counter(entry["category"] for entry in catalog)

    assert set(category_counts) == EXPECTED_CATEGORIES
    assert category_counts.pop('domain_advisory') == 9
    assert set(category_counts.values()) == {10}
    assert {entry["permission"] for entry in catalog} <= ALLOWED_PERMISSIONS
    assert all(entry["permission"] in ALLOWED_PERMISSIONS for entry in catalog)


def test_subagent_catalog_marks_the_high_frequency_roles_as_popular() -> None:
    catalog = _catalog()
    popular_ids = {entry["id"] for entry in catalog if entry["popular"] is True}

    assert REQUIRED_POPULAR_ROLES <= popular_ids
    assert 20 <= len(popular_ids) <= 30


def test_subagent_catalog_contains_no_secret_or_system_prompt_fields() -> None:
    catalog = _catalog()
    forbidden_fields = {
        "api_key",
        "credential",
        "credentials",
        "instructions",
        "secret",
        "secrets",
        "system_prompt",
        "token",
    }
    serialized = json.dumps(catalog, ensure_ascii=False)

    assert all(forbidden_fields.isdisjoint(entry) for entry in catalog)
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{8,}\b", serialized)
    assert not re.search(
        r"(?i)\b(?:api[_-]?key|token|secret|authorization)\s*[:=]\s*[^\s,}\]]+",
        serialized,
    )
