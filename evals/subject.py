"""The PR-agent subject: planted-defect recall, and what it costs (RC1-253).

Driven through `app.review.main` — the real dry-run CLI — with only `fetch`
injected. The reviewer loop, the prompts, the deterministic n8n check, the merge,
the verdict policy and the exit codes are all the shipped ones. Stubbing more
than the GitHub round-trip would mean scoring a pipeline nobody runs.

## Four things are scored, and they are never averaged

* **found** — did a finding land on the planted defect at all
* **categorised** — did it carry the right `category`
* **severity** — did it meet the corpus's floor
* **noise** — how much else came back, and (on the clean case) whether anything
  came back at all

They are separate because the fixes are separate. A missed defect is a rubric
gap; a defect found and mislabelled `general` is a taxonomy problem; a defect
found at `nit` that should block is a calibration problem. One score would move
for all three and point at none of them.

## Only `leaked_secret` gates

`verdict.py` blocks on `settings.block_on`, which is `["leaked_secret"]`. So the
leaked-secret case asserts the **exit code** as well as the finding — it is the
only case where detection has to translate into a blocked merge, and the exit
code is what CI would act on.

Everything else exits advisory by design, including cases with real warnings.
That asymmetry is deliberate in the product and the eval asserts it rather than
quietly accepting whatever came back.
"""

from __future__ import annotations

import io
import tempfile
import time
from pathlib import Path

from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from app import review as review_cli
from app.agent import prompts
from app.config import settings
from app.models import Finding, PullRequest
from evals import corpus

NAME = "pr-review"

#: Ordered weakest to strongest, so "at least this severe" is a comparison.
_SEVERITY_RANK = {"nit": 0, "warning": 1, "blocker": 2}


def _rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "").lower(), -1)


CASES: tuple[Case, ...] = tuple(
    Case(
        id=case.id,
        input={"case_id": case.id},
        expect=(
            ("finds-the-planted-defect", "categorises-it-correctly", "severity-is-calibrated")
            if case.category
            else ("raises-no-blocker-on-a-clean-diff",)
        )
        + ("exit-code-matches-the-verdict-policy",),
        tags=("pr-review", case.category or "clean"),
    )
    for case in corpus.CASES
)


def preflight() -> None:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. This subject drives a real model.")


def prompt_version() -> str:
    """Hash of the rubric and severity calibration together.

    Both, because they are edited independently and a change to either moves
    these scores — the severity checks in particular exist to catch a
    calibration edit that reads as harmless.
    """
    import hashlib

    material = (prompts.REVIEW_RUBRIC + prompts.SEVERITY_GUIDANCE + prompts.SYSTEM_PROMPT).encode()
    return f"rubric-sha256:{hashlib.sha256(material).hexdigest()[:12]}"


def version() -> SubjectVersion:
    return SubjectVersion(
        subject=NAME,
        code_version=_code_version(),
        model=settings.review_model,
        prompt_version=prompt_version(),
    )


def _code_version() -> str:
    from app import __version__

    return __version__


def _about_the_plant(finding: Finding, case: corpus.PlantedCase) -> bool:
    """Is this finding about the planted defect, rather than something else?

    Evidence substrings, deliberately generous about wording — the question is
    "did the reviewer notice", and demanding particular phrasing would fail a
    correct review for style.

    The filename is *not* enough on its own, and the first run showed why: every
    finding mentions the changed file, so a filename fallback marked all of them
    on-target and reported the noise figure as a structural zero for every
    planted case. Recall was unaffected (evidence-only matching independently
    reproduced 13/13), but the number beside it was meaningless. The fallback
    now applies only to a case that declares no evidence at all.
    """
    haystack = f"{finding.message} {finding.suggestion or ''} {finding.file or ''}".lower()
    if case.evidence:
        return any(token.lower() in haystack for token in case.evidence)
    return any(name.split("/")[-1].lower() in haystack for name, _ in case.files)


def _score_planted(case: corpus.PlantedCase, findings: list[Finding]) -> list[CharacteristicResult]:
    on_target = [f for f in findings if _about_the_plant(f, case)]
    correct_category = [f for f in on_target if f.category == case.category]

    results = [
        CharacteristicResult(
            name="finds-the-planted-defect",
            passed=bool(on_target),
            detail=(
                f"{len(on_target)} finding(s) on the plant: {on_target[0].message[:90]!r}"
                if on_target
                else f"missed — {len(findings)} finding(s), none about the planted defect"
            ),
        ),
        CharacteristicResult(
            name="categorises-it-correctly",
            passed=bool(correct_category),
            detail=(
                f"categorised {case.category!r}"
                if correct_category
                else "wrong category: "
                + (
                    ", ".join(sorted({f.category for f in on_target}))
                    if on_target
                    else "nothing on target to categorise"
                )
            ),
        ),
    ]

    # Severity is judged on the best on-target finding, and only among those
    # carrying the right category where any do — otherwise a stray `nit` about
    # the same file could satisfy a `blocker` floor.
    pool = correct_category or on_target
    best = max((_rank(f.severity) for f in pool), default=-1)
    floor = _rank(case.min_severity or "nit")
    results.append(
        CharacteristicResult(
            name="severity-is-calibrated",
            passed=best >= floor,
            detail=(
                f"best on-target severity {_name(best)!r} meets the {case.min_severity!r} floor"
                if best >= floor
                else f"best on-target severity {_name(best)!r}, below the "
                f"{case.min_severity!r} floor"
            ),
        )
    )
    return results


def _name(rank: int) -> str:
    for name, value in _SEVERITY_RANK.items():
        if value == rank:
            return name
    return "none"


def run(case: Case) -> CaseResult:
    planted = corpus.BY_ID[case.input["case_id"]]
    pr = corpus.pull_request(planted)
    started = time.perf_counter()
    try:
        exit_code, findings = _review(planted, pr)
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    if planted.category:
        results = _score_planted(planted, findings)
        on_target = sum(1 for f in findings if _about_the_plant(f, planted))
    else:
        blockers = [f for f in findings if f.severity == "blocker"]
        on_target = 0
        results = [
            CharacteristicResult(
                name="raises-no-blocker-on-a-clean-diff",
                passed=not blockers,
                detail=(
                    f"no blocker; {len(findings)} advisory finding(s) — the noise figure"
                    if not blockers
                    else f"{len(blockers)} blocker(s) on a correct change: "
                    f"{blockers[0].message[:90]!r}"
                ),
            )
        ]

    results.append(_verdict(planted, exit_code, findings))
    if planted.category == "n8n":
        results.append(_merged_once(planted, pr, findings))

    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=Usage(latency_ms=latency_ms),
        observations={
            "exit_code": exit_code,
            "findings": len(findings),
            "on_target": on_target,
            # Everything that is not about the plant. Reported per case and
            # never folded into recall — see the module docstring.
            "noise": len(findings) - on_target,
            "by_severity": {
                s: sum(1 for f in findings if f.severity == s) for s in _SEVERITY_RANK
            },
            # Blocker-severity findings the policy does not gate on. Not a
            # failure — see `_verdict` — but the number a human should look at
            # when deciding whether `block_on` is drawn in the right place.
            "blockers_not_gating": sum(
                1
                for f in findings
                if f.severity == "blocker" and f.category not in settings.block_on
            ),
            "messages": [f"[{f.severity}/{f.category}] {f.message[:120]}" for f in findings],
        },
    )


def _verdict(
    planted: corpus.PlantedCase, exit_code: int, findings: list[Finding]
) -> CharacteristicResult:
    """The exit code matches the shipped verdict policy, on every case.

    Asserted everywhere rather than only on the gating case, because "does not
    block" is as much a promise as "blocks" — a policy change that started
    failing builds on warnings would be caught here and nowhere else.

    `verdict.py` gates on **category**, not severity: `block_on` is
    `["leaked_secret"]`, so a `blocker`-severity finding in any other category
    is advisory. The corpus run shows that is not hypothetical — `sql-injection`
    and `pr-drift` both drew blocker-severity findings and both exited 0. That
    is the product working as designed, and the tension between a severity that
    says "should stop the merge" and a policy that does not stop it is recorded
    in the observations rather than quietly averaged away.
    """
    gates = planted.category in set(settings.block_on)
    want = review_cli.EXIT_BLOCKED if gates else review_cli.EXIT_OK
    stray = [
        f for f in findings if f.severity == "blocker" and f.category not in settings.block_on
    ]
    verdict = "BLOCKED" if want == review_cli.EXIT_BLOCKED else "advisory"
    detail = f"exit {exit_code} ({verdict})"
    if exit_code == want and stray:
        detail += (
            f"; {len(stray)} blocker-severity finding(s) outside block_on="
            f"{settings.block_on} did not gate, as designed"
        )
    return CharacteristicResult(
        name="exit-code-matches-the-verdict-policy",
        passed=exit_code == want,
        detail=detail if exit_code == want else f"{detail}, expected {want}",
    )


def _merged_once(
    planted: corpus.PlantedCase, pr: PullRequest, findings: list[Finding]
) -> CharacteristicResult:
    """Each deterministic finding reaches the merged result exactly once.

    Identified by recomputing them, not by counting the `n8n` category. The
    first version counted the category and read a legitimate second finding —
    the model spotting a different problem in the same workflow — as a duplicate
    merge. Counting a category answers "how many n8n findings are there", which
    is not the question; the question is whether *this* computed finding was
    dropped or double-merged.
    """
    from app.agent.checks import n8n as n8n_check

    expected = n8n_check.run_checks(
        pr, lambda name: dict(planted.repo_files).get(name)
    )
    if not expected:
        return CharacteristicResult(
            name="merges-the-deterministic-finding-once",
            passed=False,
            detail="the deterministic check produced nothing — the fixture no longer trips it",
        )

    problems = []
    for want in expected:
        seen = sum(1 for f in findings if f.message == want.message)
        if seen != 1:
            problems.append(
                f"{want.message[:60]!r} appears {seen}x "
                + ("(dropped by the loop)" if seen == 0 else "(merged more than once)")
            )
    return CharacteristicResult(
        name="merges-the-deterministic-finding-once",
        passed=not problems,
        detail=(
            f"all {len(expected)} computed finding(s) merged exactly once"
            if not problems
            else "; ".join(problems)
        ),
    )


def _review(planted: corpus.PlantedCase, pr: PullRequest) -> tuple[int, list[Finding]]:
    """Run the real CLI, capturing the merged findings on the way past.

    `main` prints a report and returns an exit code; the findings themselves are
    not returned. Wrapping the review function is how both are obtained without
    reimplementing the pipeline — and the wrapper is transparent, so the n8n
    merge and the verdict still happen exactly as they ship.
    """
    captured: list[Finding] = []
    inner = review_cli._default_review(model=settings.review_model)

    def _capture(pull, tools, precomputed):
        result = inner(pull, tools, precomputed)
        captured.append(result)  # type: ignore[arg-type]
        return result

    argv = ["--pr", f"{pr.ref.owner}/{pr.ref.repo}#{pr.ref.number}"]
    with tempfile.TemporaryDirectory(prefix="pr-eval-") as tmp:
        if planted.repo_files:
            for name, contents in planted.repo_files:
                path = Path(tmp) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            argv += ["--repo-path", tmp]
        exit_code = review_cli.main(
            argv, fetch=lambda _ref: pr, review=_capture, out=io.StringIO()
        )
    findings = list(captured[0].findings) if captured else []
    return exit_code, findings
