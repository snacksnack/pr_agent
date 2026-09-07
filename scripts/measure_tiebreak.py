"""Measure the verifier's category choice on overlapping findings (RC1-398).

    python scripts/measure_tiebreak.py history                 # the eval store; free
    python scripts/measure_tiebreak.py probe --runs 5          # BILLED: the verifier alone
    python scripts/measure_tiebreak.py pipeline --runs 3       # BILLED: whole multi-agent reviews
    python scripts/measure_tiebreak.py probe --case retry-forever --runs 1

Each mode prints a markdown summary to stdout and writes one JSON row per
measurement to ``--out`` (default: a file under the system temp directory,
named on stderr). ``history`` needs ``EVAL_DATABASE_URL``; the billed modes
need ``ANTHROPIC_API_KEY`` (from ``.env`` via settings). Run from the repo
root with ``PYTHONPATH=.`` — the script imports the app.

The scoring lives in ``evals/tiebreak.py``; the cases in ``evals/boundary.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from app.agent.reviewer import REQUEST_TIMEOUT_S
from app.config import settings
from evals import boundary, tiebreak


def _out_path(mode: str, given: Path | None) -> Path:
    if given is not None:
        return given
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return Path(tempfile.gettempdir()) / f"tiebreak-{mode}-{stamp}.jsonl"


def _write(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f} %"


def cmd_history(args: argparse.Namespace) -> int:
    from agent_evals.sql_store import SqlRunStore  # needs the [sql] extra; history only

    dsn = os.environ.get("EVAL_DATABASE_URL")
    if not dsn:
        print("EVAL_DATABASE_URL is not set (it lives in ~/.zshrc)", file=sys.stderr)
        return 2
    records = [r for r in SqlRunStore(dsn).all() if r.run_id.startswith("pr-review-")]
    rows = tiebreak.history_rows(records)
    out = _out_path("history", args.out)
    for r in rows:
        _write(out, {**r.__dict__, "outcome": r.outcome})
    summary = tiebreak.summarize_history(rows)
    print(
        f"# History: {summary['case_runs']} verified case-runs over {summary['runs']} runs "
        f"({summary['multi_runs']} multi-agent)\n"
    )
    print("| outcome | case-runs |\n| --- | --- |")
    for k, v in summary["by_outcome"].items():
        print(f"| {k} | {v} |")
    print("\n## Verifier dropped the intended category\n")
    print(
        "| run | mode | case | intended | kept on plant | dropped on plant |\n"
        "| --- | --- | --- | --- | --- | --- |"
    )
    for r in rows:
        if r.outcome == tiebreak.VERIFIER_DROPPED:
            print(
                f"| {r.run_id[-20:]} | {r.mode} | {r.case_id} | {r.intended} | "
                f"{', '.join(r.kept_on)} | {', '.join(r.dropped_on)} |"
            )
    print("\n## Intended lost to\n")
    print("| pair | times |\n| --- | --- |")
    for k, v in summary["intended_lost_to"].items():
        print(f"| {k} | {v} |")
    print("\n## Intended never filed\n")
    for r in rows:
        if r.outcome == tiebreak.NEVER_FILED:
            print(
                f"- {r.run_id[-20:]} {r.mode} {r.case_id}: "
                f"kept {list(r.kept_on)}, dropped {list(r.dropped_on)}"
            )
    print(f"\nrows: {out}", file=sys.stderr)
    return 0


def _cases(args: argparse.Namespace) -> list[boundary.BoundaryCase]:
    if args.case:
        missing = [c for c in args.case if c not in boundary.BY_ID]
        if missing:
            print(f"unknown case(s): {', '.join(missing)}", file=sys.stderr)
            sys.exit(2)
        return [boundary.BY_ID[c] for c in args.case]
    return list(boundary.CASES)


def _client() -> Anthropic:
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        sys.exit(2)
    return Anthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)


def cmd_probe(args: argparse.Namespace) -> int:
    client = _client()
    out = _out_path("probe", args.out)
    rows: list[dict[str, Any]] = []
    orders = tiebreak.ORDERS if args.order == "both" else (args.order,)
    for case in _cases(args):
        for order in orders:
            for i in range(args.runs):
                row = tiebreak.probe_case(case, order, client=client, rule=args.rule)
                row["run"] = i
                rows.append(row)
                _write(out, row)
                print(
                    f"{case.id:38} {order:14} run {i}: kept={row['kept']:8} "
                    f"({', '.join(row['kept_categories']) or 'none'}) "
                    f"${row['cost_usd']:.4f} {row['latency_ms']} ms",
                    file=sys.stderr,
                    flush=True,
                )
    summary = tiebreak.summarize_probe(rows)
    print(
        f"# Probe: {summary['rows']} verifier calls over {summary['cases']} cases, "
        f"${summary['cost_usd']:.2f}\n"
    )
    print(
        f"Intended kept alone: {_pct(summary['agreement'])}. "
        f"Kept: {summary['kept']}. First-listed won "
        f"{_pct(summary['first_listed_won']['share'])} of the "
        f"{summary['first_listed_won']['decided']} decided runs.\n"
    )
    print(
        "| case | pair | runs | intended | rival | both | neither | "
        "intended-first → | rival-first → |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for case_id, c in summary["per_case"].items():
        k = c["kept"]
        bo = c["by_order"]
        fmt = lambda d: ", ".join(f"{v} {key}" for key, v in sorted(d.items()))  # noqa: E731
        print(
            f"| {case_id} | {c['pair']} | {c['runs']} | {k.get('intended', 0)} | "
            f"{k.get('rival', 0)} | {k.get('both', 0)} | {k.get('neither', 0)} | "
            f"{fmt(bo.get(tiebreak.INTENDED_FIRST, {}))} | "
            f"{fmt(bo.get(tiebreak.RIVAL_FIRST, {}))} |"
        )
    print("\n| pair | agreement | runs |\n| --- | --- | --- |")
    for pair, p in summary["per_pair"].items():
        print(f"| {pair} | {_pct(p['agreement'])} | {p['runs']} |")
    print("\n## Drop reasons (rival kept)\n")
    for r in rows:
        if r["kept"] == tiebreak.KEPT_RIVAL:
            dropped = [v for v in r["verdicts"] if v["decision"] == "drop"]
            for v in dropped:
                print(f"- {r['case']} ({r['order']}): dropped {v['category']}: {v['reason']}")
    print(f"\nrows: {out}", file=sys.stderr)
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    client = _client()
    out = _out_path("pipeline", args.out)
    rows: list[dict[str, Any]] = []
    for case in _cases(args):
        for i in range(args.runs):
            row = tiebreak.pipeline_case(case, client=client)
            row["run"] = i
            rows.append(row)
            _write(out, row)
            print(
                f"{case.id:38} run {i}: {row['outcome']:16} kept={row['kept_on_plant']} "
                f"dropped={row['dropped_on_plant']} both_filed={row['pair_both_filed']} "
                f"${row['cost_usd']:.4f} {row['wall_s']}s",
                file=sys.stderr,
                flush=True,
            )
    summary = tiebreak.summarize_pipeline(rows)
    print(f"# Pipeline: {summary['rows']} multi-agent reviews, ${summary['cost_usd']:.2f}\n")
    print(
        f"Outcomes: {summary['by_outcome']}. Both categories of the pair filed on the plant "
        f"in {summary['pair_both_filed']} of {summary['rows']} reviews.\n"
    )
    print("| case | pair | outcomes | both filed | cost |\n| --- | --- | --- | --- | --- |")
    for case_id, c in summary["per_case"].items():
        print(
            f"| {case_id} | {c['pair']} | {c['outcomes']} | {c['both_filed']} | "
            f"${c['cost_usd']:.3f} |"
        )
    print("\n## Drop reasons\n")
    for r in rows:
        for v in r["drop_reasons"]:
            print(f"- {r['case']} run {r['run']}: dropped {v['category']}: {v['reason']}")
    print(f"\nrows: {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    for name, fn in (("history", cmd_history), ("probe", cmd_probe), ("pipeline", cmd_pipeline)):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, default=None, help="JSONL rows (default: temp dir)")
        p.set_defaults(fn=fn)
        if name != "history":
            p.add_argument(
                "--runs", type=int, default=3, help="runs per case (per order, for probe)"
            )
            p.add_argument("--case", action="append", help="only these case ids (repeatable)")
        if name == "probe":
            p.add_argument("--order", choices=("both", *tiebreak.ORDERS), default="both")
            p.add_argument(
                "--rule",
                choices=tuple(tiebreak.RULES),
                default="shipped",
                help="tie-break sentence in the verifier's instructions (candidate: not shipped)",
            )
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
