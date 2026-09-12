"""The verifier tie-break measurement (RC1-398), offline: the boundary cases
are well-formed and the scoring reads synthetic rows the way the record reads
real ones. The billed modes stay out of pytest."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.prompts import CATEGORIES
from evals import boundary, tiebreak

# --- the cases ----------------------------------------------------------------


def test_there_are_at_least_eight_boundary_cases():
    assert len(boundary.CASES) >= 8


def test_case_ids_are_unique():
    ids = [c.id for c in boundary.CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", boundary.CASES, ids=lambda c: c.id)
def test_each_pair_is_one_defect_in_two_categories(case):
    a, b = case.pair
    assert case.intended != case.rival
    assert {case.intended, case.rival} <= set(CATEGORIES)
    assert (a.category, b.category) == (case.intended, case.rival)
    assert (a.file, a.line) == (b.file, b.line), "the merge folds only on file+line+category"
    assert a.severity == b.severity, "at different severities the verifier's rule keeps the higher"
    assert case.evidence and case.notes


@pytest.mark.parametrize("case", boundary.CASES, ids=lambda c: c.id)
def test_each_case_builds_a_pull_request_the_pair_points_into(case):
    pr = boundary.pull_request(case)
    assert pr.files and all(f.patch for f in pr.files)
    assert case.pair[0].file in {f.filename for f in pr.files}


def test_the_pairs_cover_the_directions_the_records_named():
    pairs = {c.categories for c in boundary.CASES}
    # RC1-394: convention should have beaten docs and pr_drift; infra should
    # have beaten security. RC1-393: general lost to error_handling.
    assert ("convention", "docs") in pairs
    assert ("convention", "pr_drift") in pairs
    assert ("infra_scalability", "security") in pairs
    assert ("general", "error_handling") in pairs


def test_boundary_cases_are_not_in_the_recall_corpus():
    from evals import corpus

    assert not {c.id for c in boundary.CASES} & {c.id for c in corpus.CASES}


def test_probe_prefix_carries_the_conventions_only_when_the_case_needs_them():
    needs = next(c for c in boundary.CASES if c.needs_conventions)
    plain = next(c for c in boundary.CASES if not c.needs_conventions)
    assert "Repository conventions, from CLAUDE.md" in tiebreak.probe_prefix(needs)
    assert "Repository conventions" not in tiebreak.probe_prefix(plain)


# --- history scoring ------------------------------------------------------------


def test_categories_on_plant_reads_the_stored_message_shape():
    rows = [
        "[warning/convention] Reads os.environ directly where settings is the convention",
        "[nit/docs] Missing docstring on the new helper",
        "not a stored row",
    ]
    assert tiebreak.categories_on_plant(rows, ("os.environ",)) == ["convention"]


@pytest.mark.parametrize(
    ("kept", "dropped", "expected"),
    [
        (["convention"], ["pr_drift"], tiebreak.SURVIVED),
        (["docs"], ["convention"], tiebreak.VERIFIER_DROPPED),
        (["docs", "pythonic"], [], tiebreak.NEVER_FILED),
        ([], [], tiebreak.NOT_FOUND),
        (["convention", "docs"], ["convention"], tiebreak.SURVIVED),
    ],
)
def test_outcome_of_the_intended_category(kept, dropped, expected):
    assert tiebreak.outcome("convention", kept, dropped) == expected


def _record(run_id, case_id, messages, dropped, *, multi=True, verified=True):
    return SimpleNamespace(
        run_id=run_id,
        results=[
            SimpleNamespace(
                case_id=case_id,
                observations={
                    "messages": messages,
                    "multi": {"ran": multi},
                    "verifier": {"ran": verified, "dropped_messages": dropped},
                },
            )
        ],
    )


def test_history_rows_skip_unverified_results_and_the_clean_case():
    records = [
        _record(
            "pr-review-1",
            "convention-break",
            ["[nit/docs] os.environ under a comment"],
            ["[warning/convention] os.environ read directly"],
        ),
        _record(
            "pr-review-2",
            "convention-break",
            ["[warning/convention] os.environ"],
            [],
            verified=False,
        ),
        _record("pr-review-3", "clean", ["[nit/docs] anything"], []),
    ]
    rows = tiebreak.history_rows(records)
    assert [r.run_id for r in rows] == ["pr-review-1"]
    assert rows[0].outcome == tiebreak.VERIFIER_DROPPED
    assert rows[0].survivors == ("docs",)
    summary = tiebreak.summarize_history(rows)
    assert summary["by_outcome"] == {tiebreak.VERIFIER_DROPPED: 1}
    assert summary["intended_lost_to"] == {"convention -> docs": 1}


# --- probe scoring --------------------------------------------------------------


class _FakeMessages:
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = {"verdicts": self._verdicts.pop(0)}
        return SimpleNamespace(
            content=[{"type": "tool_use", "id": "v", "name": "verify_findings", "input": payload}],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=100,
            ),
        )


class _FakeClient:
    def __init__(self, verdicts):
        self.messages = _FakeMessages(verdicts)


def test_probe_case_scores_the_survivor_and_keeps_the_reason():
    case = boundary.BY_ID["dead-code-after-return"]
    client = _FakeClient(
        [[{"index": 1, "decision": "drop", "reason": "same defect; general names it"}]]
    )
    row = tiebreak.probe_case(
        case, tiebreak.INTENDED_FIRST, client=client, model="claude-sonnet-4-6"
    )
    assert row["kept"] == tiebreak.KEPT_INTENDED
    assert row["first_survived"] is True
    assert row["verdicts"][1] == {
        "category": "error_handling",
        "decision": "drop",
        "reason": "same defect; general names it",
    }
    assert row["cost_usd"] > 0
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert sent[0]["text"] == tiebreak.probe_prefix(case), (
        "the multi path's cached prefix, verbatim"
    )
    assert "[0] warning / general" in sent[1]["text"]


def test_probe_case_in_rival_first_order_lists_the_rival_first():
    case = boundary.BY_ID["dead-code-after-return"]
    client = _FakeClient([[{"index": 0, "decision": "drop", "reason": "redundant"}]])
    row = tiebreak.probe_case(case, tiebreak.RIVAL_FIRST, client=client, model="claude-sonnet-4-6")
    assert row["first"] == "error_handling"
    assert row["kept"] == tiebreak.KEPT_INTENDED
    assert row["first_survived"] is False


def _row(case, order, kept, first_survived, cost=0.001):
    return {
        "case": case,
        "pair": ["a", "b"],
        "order": order,
        "kept": kept,
        "first_survived": first_survived,
        "cost_usd": cost,
        "kept_categories": [],
        "verdicts": [],
    }


def test_summarize_probe_reports_agreement_and_position():
    rows = [
        _row("x", tiebreak.INTENDED_FIRST, tiebreak.KEPT_INTENDED, True),
        _row("x", tiebreak.INTENDED_FIRST, tiebreak.KEPT_BOTH, True),
        _row("x", tiebreak.RIVAL_FIRST, tiebreak.KEPT_RIVAL, True),
        _row("x", tiebreak.RIVAL_FIRST, tiebreak.KEPT_INTENDED, False),
    ]
    s = tiebreak.summarize_probe(rows)
    assert s["per_case"]["x"]["agreement"] == 0.5
    assert s["per_pair"]["a vs b"] == {"agreement": 0.5, "runs": 4}
    # Three decided runs; the first-listed survived in two of them.
    assert s["first_listed_won"] == {"share": pytest.approx(2 / 3), "decided": 3}
    assert s["kept"] == {"intended": 2, "both": 1, "rival": 1}


def test_recording_client_reads_the_numbering_back_from_the_request():
    case = boundary.BY_ID["headers-in-error-log"]
    verdict = [{"index": 0, "decision": "drop", "reason": "r"}]
    client = _FakeClient([verdict, verdict])
    tiebreak.probe_case(case, tiebreak.RIVAL_FIRST, client=client, model="claude-sonnet-4-6")
    recorder = tiebreak.RecordingClient(client)
    recorder.create(**client.messages.calls[0])
    assert recorder.numbered_categories() == {0: "error_handling", 1: "security"}


def test_probe_can_swap_the_tiebreak_sentence_and_restores_it():
    from app.agent import verifier

    case = boundary.BY_ID["dead-code-after-return"]
    client = _FakeClient([[]])
    before = verifier.VERIFIER_INSTRUCTIONS
    row = tiebreak.probe_case(
        case, tiebreak.INTENDED_FIRST, client=client, model="claude-sonnet-4-6", rule="candidate"
    )
    sent = client.messages.calls[0]["messages"][0]["content"][1]["text"]
    assert tiebreak.CANDIDATE_TIEBREAK in sent
    assert tiebreak.SHIPPED_TIEBREAK not in sent
    assert row["rule"] == "candidate"
    assert before == verifier.VERIFIER_INSTRUCTIONS, "module state restored"


def test_the_shipped_tiebreak_sentence_is_the_one_in_the_verifier():
    from app.agent import verifier

    assert tiebreak.SHIPPED_TIEBREAK in verifier.VERIFIER_INSTRUCTIONS
    assert tiebreak.instructions_with("shipped") == verifier.VERIFIER_INSTRUCTIONS
