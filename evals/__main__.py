"""`python -m evals` — run the planted-defect corpus (RC1-253).

Billed: every case drives a real model through the real review loop. Exit codes
match the other repos' harnesses — `0` all passed, `1` a case failed, `2` a case
errored, meaning the subject produced nothing to score.

Recall and noise are printed as separate lines and stored as separate fields.
They are never combined: a reviewer that flags everything scores perfect recall,
and the whole point of the clean case is to make that visible rather than let it
average away.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_evals.record import RunRecord, RunStore, new_run_id

from app.config import settings
from evals import corpus, subject

RUNS_PATH = Path(os.environ.get("EVAL_RUNS_PATH", "./eval-runs/runs.jsonl"))


def _print(result) -> None:
    if result.error:
        print(f"  ERROR {result.case_id}: {result.error}")
        return
    status = "pass" if result.passed else "FAIL"
    obs = result.observations
    print(
        f"  {status} {result.case_id}  ({result.usage.latency_ms / 1000:.0f}s, "
        f"{obs['findings']} finding(s), {obs['noise']} off-target)"
    )
    for characteristic in result.characteristics:
        if characteristic.passed and not characteristic.advisory:
            continue
        mark = "~" if characteristic.advisory else "✗"
        print(f"    {mark} {characteristic.name}: {characteristic.detail}")


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

    cases = subject.CASES
    if args.case:
        cases = tuple(c for c in cases if c.id == args.case)
        if not cases:
            print(f"no case {args.case!r}", file=sys.stderr)
            return 2

    print(f"{len(cases)} case(s) against {settings.review_model} — this spends money.\n")
    started = datetime.now(UTC)
    results = [subject.run(case) for case in cases]
    for result in results:
        _print(result)

    record = RunRecord(
        run_id=new_run_id(subject.NAME),
        subject_version=subject.version(),
        started_at=started,
        finished_at=datetime.now(UTC),
        results=results,
    )
    RunStore(RUNS_PATH).append(record)

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
    print(f"\nrun {record.run_id} recorded")

    if any(r.error for r in results):
        return 2
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
