"""`python -m evals` — run the planted-defect corpus (RC1-253).

Billed: every case drives a real model through the real review loop. The
run/record/exit plumbing is `agent_evals.runner` (RC1-262); what lives here is
this repo's subject, its corpus, and its summary lines.

Recall and noise are printed as separate lines and stored as separate fields.
They are never combined: a reviewer that flags everything scores perfect recall,
and the whole point of the clean case is to make that visible rather than let it
average away.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from agent_evals import llmobs
from agent_evals.runner import UnknownCase, exit_code, print_result, record_run, select_cases

from app.config import settings
from evals import corpus, subject


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    parser.add_argument("--case", help="run a single case by id")
    parser.add_argument("--list", action="store_true", help="list the corpus and exit")
    parser.add_argument(
        "--repo-path",
        metavar="PATH",
        default=None,
        help="RC1-393: a checkout every case explores (copied per case; the n8n "
        "case's own files are written over it). Without it the corpus is diff-only.",
    )
    parser.add_argument(
        "--no-repo-context",
        action="store_true",
        help="RC1-393: leave the conventions file and callers list out of the "
        "multi-agent prefix — the control run for measuring them.",
    )
    args = parser.parse_args(argv)
    repo_context = not args.no_repo_context

    if args.list:
        for case in corpus.CASES:
            print(f"  {case.id:<28} {case.category or '(clean)':<18} {case.notes}")
        return 0

    try:
        subject.preflight()
    except Exception as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    try:
        cases = select_cases(subject.CASES, args.case)
    except UnknownCase as exc:
        print(exc, file=sys.stderr)
        return 2

    verify = "on" if settings.review_verify_findings else "off"
    multi = "on" if settings.review_multi_agent else "off"
    if settings.review_multi_agent and settings.review_orchestrator != "asyncio":
        multi += f" ({settings.review_orchestrator})"
    checkout = f"checkout {args.repo_path}" if args.repo_path else "diff-only"
    context = "repo context on" if repo_context else "repo context OFF"
    print(
        f"{len(cases)} case(s) against {settings.review_model}, verifier {verify}, "
        f"multi-agent {multi}, {checkout}, {context} — this spends money.\n"
    )
    # RC1-322: billed spend is traced spend; a no-op without DD_API_KEY.
    llmobs.enable("pr-review-agent", service="evals")
    started = datetime.now(UTC)
    results = []
    for case in cases:
        with llmobs.case(case.id) as traced:
            result = subject.run(case, repo_path=args.repo_path, repo_context=repo_context)
            traced.record(result)
        results.append(result)
    for result in results:
        obs = result.observations
        extra = "" if result.error else f"{obs['findings']} finding(s), {obs['noise']} off-target"
        print_result(result, extra=extra)

    by_id = {c.id: c for c in corpus.CASES}
    planted = [r for r in results if by_id[r.case_id].category and not r.error]
    precision = [r for r in results if by_id[r.case_id].trap and not r.error]
    found = sum(
        1
        for r in planted
        for c in r.characteristics
        if c.name == "finds-the-planted-defect" and c.passed
    )
    clean = next((r for r in results if r.case_id == "clean" and not r.error), None)
    print(f"\n  recall   {found}/{len(planted)} planted defect(s) found")
    if clean is not None:
        print(
            f"  noise    {clean.observations['findings']} finding(s) on the clean diff, "
            f"{clean.observations['by_severity']['blocker']} blocker(s)"
        )
    if precision:
        held = sum(
            1
            for r in precision
            for c in r.characteristics
            if c.name == "does-not-flag-the-decoy" and c.passed
        )
        print(f"  precision {held}/{len(precision)} decoy(s) left alone at warning or above")
    verified = [r for r in results if not r.error and r.observations["verifier"]["ran"]]
    if verified:
        dropped = sum(r.observations["verifier"]["dropped"] for r in verified)
        downgraded = sum(r.observations["verifier"]["downgraded"] for r in verified)
        print(
            f"  verifier ran on {len(verified)} case(s): "
            f"{dropped} dropped, {downgraded} downgraded"
        )
    multi_ran = [r for r in results if not r.error and r.observations["multi"]["ran"]]
    if multi_ran:
        # RC1-390: the cache premise, checked per reviewer call across the run.
        cold = [
            r.case_id
            for r in multi_ran
            if r.observations["multi"]["min_reviewer_cache_read"] == 0
        ]
        off_scope = sum(r.observations["multi"]["off_scope"] for r in multi_ran)
        print(
            f"  multi-agent ran on {len(multi_ran)} case(s): "
            f"{len(multi_ran) - len(cold)} with every reviewer reading the prefix from cache, "
            f"{off_scope} off-scope finding(s) discarded"
            + (f"; cold on {', '.join(cold)}" if cold else "")
        )
        # RC1-393: what exploration cost, and what Python put in front of it.
        scouted = [r for r in multi_ran if not r.observations["multi"]["scout_skipped"]]
        scout_cost = sum(
            float(r.observations["multi"]["stages"].get("scout", {}).get("cost_usd", 0))
            for r in scouted
        )
        with_conventions = sum(
            1 for r in multi_ran if r.observations["multi"]["context"]["conventions_file"]
        )
        callers = sum(r.observations["multi"]["context"]["callers"] for r in multi_ran)
        tests = sum(r.observations["multi"]["context"].get("tests", 0) for r in multi_ran)
        complete = sum(
            1 for r in multi_ran if r.observations["multi"]["context"].get("complete")
        )
        print(
            f"  scout ran on {len(scouted)} case(s) for ${scout_cost:.3f}; "
            f"conventions file on {with_conventions}, {callers} caller row(s) and "
            f"{tests} test row(s) by grep, context complete on {complete}"
        )
    print("  (never averaged — see evals/subject.py)")

    record_run(
        subject.version(checkout=args.repo_path is not None, repo_context=repo_context),
        started,
        results,
    )
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
