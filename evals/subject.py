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
* **precision** (RC1-387) — on a case with a planted decoy, whether the decoy
  drew a `warning` or worse. A `nit` on it is recorded, not failed.

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
import shutil
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from agent_evals import pricing
from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from app import review as review_cli
from app.agent import prompts
from app.agent.reviewer import review_pull_request
from app.agent.tools import IGNORED_DIRS
from app.config import settings
from app.models import Finding, PullRequest, ReviewResult, TokenUsage
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
            + (("does-not-flag-the-decoy",) if case.trap else ())
        )
        + ("exit-code-matches-the-verdict-policy",),
        tags=("pr-review", case.category or ("precision" if case.trap else "clean")),
    )
    for case in corpus.CASES
)


def preflight() -> None:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. This subject drives a real model.")


def prompt_version(*, checkout: bool = False, repo_context: bool = True) -> str:
    """Hash of the rubric and severity calibration together.

    Both, because they are edited independently and a change to either moves
    these scores — the severity checks in particular exist to catch a
    calibration edit that reads as harmless.

    ``checkout`` and ``repo_context`` (RC1-393) are run conditions rather
    than prompts, and they end up here for the same reason the flags do: a
    run against a checkout explores on every case where a diff-only run
    explores on one, and a run with the deterministic context off is the
    control for one with it on. Neither pair may be averaged, so each is
    its own subject version.
    """
    import hashlib

    material = (prompts.REVIEW_RUBRIC + prompts.SEVERITY_GUIDANCE + prompts.SYSTEM_PROMPT).encode()
    version = f"rubric-sha256:{hashlib.sha256(material).hexdigest()[:12]}"
    if settings.review_verify_findings:
        # RC1-387: a run with the verifier on is a different subject version —
        # the two are compared against each other, never averaged together.
        from app.agent import verifier

        material = (verifier.VERIFIER_INSTRUCTIONS).encode()
        version += f"+verify-sha256:{hashlib.sha256(material).hexdigest()[:12]}"
    if settings.review_multi_agent:
        # RC1-390: the scout's and reviewers' instructions are the multi-agent
        # path's prompt; a run with the flag on is its own subject version.
        material = (
            prompts.SCOUT_INSTRUCTIONS
            + prompts.SCOUT_CONTEXT_NOTE
            + "".join(prompts.reviewer_instructions(spec) for spec in prompts.REVIEWERS)
        ).encode()
        version += f"+multi-sha256:{hashlib.sha256(material).hexdigest()[:12]}"
        if settings.review_orchestrator != "asyncio":
            # RC1-391: the same graph on a framework is its own subject version;
            # the two are compared, never averaged.
            version += f"+{settings.review_orchestrator}"
    if checkout:
        version += "+checkout"
    if not repo_context:
        version += "+no-context"
    return version


def version(*, checkout: bool = False, repo_context: bool = True) -> SubjectVersion:
    return SubjectVersion(
        subject=NAME,
        code_version=_code_version(),
        model=settings.review_model,
        prompt_version=prompt_version(checkout=checkout, repo_context=repo_context),
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


def _about_the_decoy(finding: Finding, case: corpus.PlantedCase) -> bool:
    """Is this finding about the planted decoy? Same generous matching as
    `_about_the_plant`, over `trap` tokens instead of `evidence`."""
    haystack = f"{finding.message} {finding.suggestion or ''}".lower()
    return any(token.lower() in haystack for token in case.trap)


def _score_decoy(case: corpus.PlantedCase, findings: list[Finding]) -> CharacteristicResult:
    """The decoy drew nothing at `warning` or above (RC1-387).

    A `nit` is tolerated and counted in the observations. The rubric's own
    warning is that over-flagging trains people to ignore reviews, and the
    thing people learn to ignore is a warning that was wrong — a hedged nit
    on a deliberate pattern is a smaller cost, and failing it would push the
    reviewer toward silence rather than calibration.
    """
    on_decoy = [f for f in findings if _about_the_decoy(f, case)]
    raised = [f for f in on_decoy if _rank(f.severity) >= _SEVERITY_RANK["warning"]]
    if raised:
        detail = (
            f"{len(raised)} finding(s) at warning or above on the decoy: "
            f"[{raised[0].severity}/{raised[0].category}] {raised[0].message[:90]!r}"
        )
    elif on_decoy:
        detail = f"decoy drew {len(on_decoy)} nit(s) only, tolerated"
    else:
        detail = "decoy drew nothing"
    return CharacteristicResult(name="does-not-flag-the-decoy", passed=not raised, detail=detail)


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


def run(
    case: Case, *, repo_path: str | Path | None = None, repo_context: bool = True
) -> CaseResult:
    """Score one case.

    ``repo_path`` (RC1-393) gives every case a checkout to explore — the
    corpus is diff-only, so without one the scout runs on the single case
    that materialises files, and the cost of exploration is invisible.
    ``repo_context`` is the deterministic context switch, off for the
    control run.
    """
    planted = corpus.BY_ID[case.input["case_id"]]
    pr = corpus.pull_request(planted)
    started = time.perf_counter()
    try:
        exit_code, findings, result = _review(
            planted, pr, repo_path=repo_path, repo_context=repo_context
        )
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
        if planted.trap:
            results.append(_score_decoy(planted, findings))

    results.append(_verdict(planted, exit_code, findings))
    if planted.category == "n8n":
        results.append(_merged_once(planted, pr, findings))

    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=_usage(latency_ms, result),
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
            # RC1-387: the four token counts, so cache behaviour is visible.
            "tokens": _token_breakdown(result.usage) if result else {},
            # RC1-387: how the loop ended, so a zero-finding miss can be read
            # as "the model submitted nothing" versus "it ran out of turns"
            # versus "it submitted findings the loop could not parse".
            "loop": {
                "tool_turns": result.tool_turns,
                "files_read": result.files_read,
                "truncated": result.truncated,
                "malformed_findings": result.malformed_findings,
                "coerced_findings": result.coerced_findings,
            }
            if result
            else {},
            # RC1-387: what the decoy drew, by severity, on a precision case.
            "decoy_by_severity": {
                s: sum(
                    1 for f in findings if _about_the_decoy(f, planted) and f.severity == s
                )
                for s in _SEVERITY_RANK
            }
            if planted.trap
            else {},
            # RC1-387: what the verifier did, when it ran. Zero and false when
            # the flag is off, so a flag-off run reads as such in the record.
            "verifier": _verifier_observations(result),
            # RC1-390: which path ran and, when it was the multi-agent one,
            # what each stage cost — the cache premise is read per reviewer
            # call here, not inferred from the case total.
            "multi": _multi_observations(result, checkout=repo_path is not None),
        },
    )


def _multi_observations(result: ReviewResult | None, *, checkout: bool = False) -> dict:
    if result is None or result.mode != "multi":
        return {"ran": False}
    reviewer_reads = [
        usage.cache_read_input_tokens
        for stage, usage in result.stage_usage.items()
        if stage.startswith("reviewer:")
    ]
    return {
        "ran": True,
        "reviewers": list(result.reviewers_run),
        "scout_skipped": result.brief.startswith("(scout skipped"),
        "brief_chars": len(result.brief),
        "stages": {
            stage: {
                **_token_breakdown(usage),
                "cost_usd": str(_cost_usd(result.model, usage)),
            }
            for stage, usage in result.stage_usage.items()
        },
        # The design's premise: every reviewer read the shared prefix from
        # cache. Zero on any of them means it was written, not read.
        "min_reviewer_cache_read": min(reviewer_reads, default=0),
        "latency_ms": {k: round(v) for k, v in result.stage_latency_ms.items()},
        "off_scope": result.off_scope_findings,
        "deduplicated": result.deduplicated_findings,
        "unusable_reviewer_calls": result.unusable_reviewer_calls,
        # RC1-393: whether the case had a repository to explore, and what
        # Python put in the prefix before the scout ran.
        "checkout": checkout,
        # RC1-391: which orchestration ran the graph.
        "orchestrator": settings.review_orchestrator,
        "context": {
            "conventions_file": result.conventions_file,
            "callers": result.callers_found,
            # RC1-394: the tests rows, and whether the context was complete
            # enough for the router to skip the scout.
            "tests": result.tests_found,
            "complete": result.context_complete,
        },
    }


def _verifier_observations(result: ReviewResult | None) -> dict:
    if result is None:
        return {"ran": False}
    return {
        "ran": result.verified,
        "dropped": len(result.verifier_dropped),
        "downgraded": result.verifier_downgraded,
        "tokens": _token_breakdown(result.verifier_usage),
        "cost_usd": str(_cost_usd(result.model, result.verifier_usage)) if result.verified else "0",
        "dropped_messages": [
            f"[{f.severity}/{f.category}] {f.message[:120]}" for f in result.verifier_dropped
        ],
    }


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


def materialise_checkout(
    into: Path, repo_path: str | Path | None, repo_files: tuple[tuple[str, str], ...]
) -> None:
    """Build one case's checkout: a copy of ``repo_path`` (RC1-393; noise
    directories and ``.git`` left behind), then the case's own files written
    over it. Without a repo path it is the case's files alone, which is what
    the n8n case has always had."""
    if repo_path is not None:
        shutil.copytree(
            Path(repo_path).expanduser(),
            into,
            ignore=shutil.ignore_patterns(*IGNORED_DIRS, ".git", ".env"),
            dirs_exist_ok=True,
        )
    for name, contents in repo_files:
        path = into / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


def _cost_usd(model: str, usage: TokenUsage) -> Decimal:
    """Price a review's four token counts at the harness's rates.

    The harness has known the two cache rates since v0.6.0 (RC1-392); this
    kept its RC1-387 stopgap's name so the observations that call it did not
    move. Raises on an unknown model — a review that looks free is worse
    than one that is not priced.
    """
    return pricing.cost_usd(
        model,
        usage.input_tokens,
        usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )


def _token_breakdown(usage: TokenUsage) -> dict[str, int]:
    return {
        "input": usage.input_tokens,
        "cache_creation": usage.cache_creation_input_tokens,
        "cache_read": usage.cache_read_input_tokens,
        "output": usage.output_tokens,
    }


def _usage(latency_ms: float, result: ReviewResult | None) -> Usage:
    """Priced from the loop's summed token counts (RC1-269), cache included.

    Recording $0 for a billed suite is RC1-254's exact finding; the guard stays
    honest when nothing was captured — no measured tokens, no invented cost.
    The four counts go on the record as the API reported them (the harness's
    convention since v0.6.0, RC1-392): `input_tokens` is the uncached
    remainder, the cache counts carry the rest, and `Usage.context_tokens`
    is the whole prompt for anyone comparing against pre-caching runs.
    """
    if result is None:
        return Usage(latency_ms=latency_ms)
    usage = result.usage
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cost_usd=_cost_usd(result.model, usage),
        latency_ms=latency_ms,
    )


def _review(
    planted: corpus.PlantedCase,
    pr: PullRequest,
    *,
    repo_path: str | Path | None = None,
    repo_context: bool = True,
) -> tuple[int, list[Finding], ReviewResult | None]:
    """Run the real CLI, capturing the merged result on the way past.

    `main` prints a report and returns an exit code; the findings themselves are
    not returned. Wrapping the review function is how both are obtained without
    reimplementing the pipeline — and the wrapper is transparent, so the n8n
    merge and the verdict still happen exactly as they ship. The captured
    `ReviewResult` also carries the loop's token counts for pricing.

    The review function is the shipped `review_pull_request` with the CLI's
    defaults (`client=None`, so the SDK is built from settings) plus the one
    switch the CLI does not expose, `repo_context` (RC1-393).
    """
    captured: list[ReviewResult] = []

    def _capture(pull, tools, precomputed):
        result = review_pull_request(
            pull,
            tools,
            client=None,
            model=settings.review_model,
            precomputed_findings=precomputed,
            repo_context=repo_context,
        )
        captured.append(result)
        return result

    argv = ["--pr", f"{pr.ref.owner}/{pr.ref.repo}#{pr.ref.number}"]
    with tempfile.TemporaryDirectory(prefix="pr-eval-") as tmp:
        if repo_path is not None or planted.repo_files:
            materialise_checkout(Path(tmp), repo_path, planted.repo_files)
            argv += ["--repo-path", tmp]
        exit_code = review_cli.main(
            argv, fetch=lambda _ref: pr, review=_capture, out=io.StringIO()
        )
    result = captured[0] if captured else None
    findings = list(result.findings) if result else []
    return exit_code, findings, result
