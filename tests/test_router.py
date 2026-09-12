"""Tests for the deterministic review router (RC1-390). Pure; no model."""
from __future__ import annotations

from app.agent import router
from app.agent.prompts import CHANGE_INTENT, DIFF_LOCAL, REPO_CONTEXT
from app.models import ChangedFile, PRRef, PullRequest


def _pr(*files: ChangedFile) -> PullRequest:
    return PullRequest(ref=PRRef("o", "r", 1), files=list(files))


def test_code_change_gathers_context_and_runs_all_three_reviewers():
    plan = router.plan_review(_pr(ChangedFile("app/x.py", "modified", patch="+x = 1")))
    assert plan.context is True
    assert plan.names == ["diff_local", "repo_context", "change_intent"]
    assert plan.reviewers[0] == DIFF_LOCAL and plan.reviewers[1] == REPO_CONTEXT


def test_change_intent_reviews_only_drift_and_scale_by_default():
    plan = router.plan_review(_pr(ChangedFile("app/x.py", "modified")))
    intent = plan.reviewers[-1]
    assert intent.name == "change_intent"
    assert intent.categories == ("pr_drift", "infra_scalability")
    assert "dependencies" not in intent.allowed and "n8n" not in intent.allowed
    assert "general" in intent.allowed


def test_manifest_change_adds_dependency_review():
    for name in ("requirements.txt", "svc/requirements-dev.txt", "pyproject.toml", "package.json"):
        plan = router.plan_review(_pr(ChangedFile(name, "modified")))
        assert "dependencies" in plan.reviewers[-1].categories, name
        assert any("manifest changed" in r for r in plan.reasons)


def test_lock_file_counts_as_a_manifest_change():
    plan = router.plan_review(_pr(ChangedFile("uv.lock", "modified")))
    assert "dependencies" in plan.reviewers[-1].categories


def test_workflow_export_adds_n8n_review():
    by_path = router.plan_review(_pr(ChangedFile("workflows/poller.json", "added")))
    patch = '+  "nodes": [\n+    {"type": "n8n-nodes-base.cron"}'
    by_patch = router.plan_review(_pr(ChangedFile("flows/a.json", "added", patch=patch)))
    plain = router.plan_review(_pr(ChangedFile("config/settings.json", "modified", patch="+{}")))
    assert "n8n" in by_path.reviewers[-1].categories
    assert "n8n" in by_patch.reviewers[-1].categories
    assert "n8n" not in plain.reviewers[-1].categories


def test_full_change_intent_when_manifest_and_workflow_both_change():
    plan = router.plan_review(
        _pr(ChangedFile("requirements.txt", "modified"), ChangedFile("workflows/x.json", "added"))
    )
    assert plan.reviewers[-1] == CHANGE_INTENT


def test_documentation_only_change_skips_context_and_repo_context():
    plan = router.plan_review(
        _pr(ChangedFile("README.md", "modified"), ChangedFile("docs/adr.rst", "added"))
    )
    assert plan.context is False
    assert plan.names == ["diff_local", "change_intent"]
    assert any("documentation-only" in r for r in plan.reasons)


def test_requirements_txt_is_not_documentation():
    assert router.is_doc("requirements.txt") is False
    assert router.is_doc("notes.txt") is True
    plan = router.plan_review(_pr(ChangedFile("requirements.txt", "modified")))
    assert plan.context is True


def test_empty_pr_still_gets_the_toolless_reviewers():
    plan = router.plan_review(_pr())
    assert plan.context is True  # nothing says it is docs-only
    assert "diff_local" in plan.names and "change_intent" in plan.names


def test_nothing_to_explore_skips_the_context_but_keeps_every_reviewer():
    plan = router.plan_review(_pr(ChangedFile("app/x.py", "modified")), explorable=False)
    assert plan.context is False
    assert plan.names == ["diff_local", "repo_context", "change_intent"]
    assert any("no repository checkout" in r for r in plan.reasons)
