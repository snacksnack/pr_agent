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

from agent_evals.runner import UnknownCase, exit_code, print_result, record_run, select_cases

from app.config import settings
from evals import corpus, subject


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    parser.add_argument("--case", help="run a single case by id")
    parser.add_argument("--list", action="store_true", help="list the corpus and exit")
    args = parser.parse_args(argv)

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

    print(f"{len(cases)} case(s) against {settings.review_model} — this spends money.\n")
    started = datetime.now(UTC)
    results = [subject.run(case) for case in cases]
    for result in results:
        obs = result.observations
        extra = "" if result.error else f"{obs['findings']} finding(s), {obs['noise']} off-target"
        print_result(result, extra=extra)

    planted = [r for r in results if r.case_id != "clean" and not r.error]
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
    print("  (never averaged — see evals/subject.py)")

    record_run(subject.version(), started, results)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
