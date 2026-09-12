"""Price a review of a real PR at its own head (RC1-393/394/396).

    python scripts/measure_pr.py 35 33 39
    python scripts/measure_pr.py 8 \
        --repo-dir ../n8n-concert-intelligence --overlay CLAUDE.md

For each PR: a git worktree at the PR's head SHA (the review must see the
repository as the PR did, not as ``main`` is now — RC1-393 produced a
spurious blocker measuring against the wrong checkout), the shipped
``review_pull_request`` with the flags asked for, and ``app.pricing.review_cost``
over the result. One JSON line per review on stdout; a human summary on
stderr. Billed: every PR drives a real model.

``--repo-dir`` points at another checkout (its origin names the repository,
its PRs are the ones measured); ``--overlay`` copies files from that
checkout's working tree into the worktree before the review, which is how a
conventions file is measured against a PR that predates it (RC1-396).

Written down because it had been rebuilt from a memory note three times.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.agent.local_repository import LocalRepository
from app.agent.pipeline import review_pull_request
from app.github import fetch_pull_request
from app.pricing import review_cost


def _gh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, cwd=cwd
    ).stdout


def _origin(cwd: Path) -> tuple[str, str]:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
    ).stdout.strip()
    path = url.split(":")[-1].split("github.com/")[-1].removesuffix(".git")
    owner, repo = path.split("/")[-2:]
    return owner, repo


async def measure(
    number: int,
    *,
    repo_dir: Path = Path("."),
    overlay: tuple[str, ...] = (),
    repo_context: bool = True,
) -> dict:
    repo_dir = repo_dir.resolve()
    owner, repo = _origin(repo_dir)
    head = _gh(
        "pr", "view", str(number), "--json", "headRefOid", "-q", ".headRefOid", cwd=repo_dir
    ).strip()
    pr = fetch_pull_request(owner, repo, number)  # gh CLI token, then GITHUB_TOKEN (RC1-430)
    with tempfile.TemporaryDirectory(prefix=f"pr-{number}-") as tmp:
        worktree = Path(tmp) / "wt"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), head],
            check=True,
            capture_output=True,
            cwd=repo_dir,
        )
        try:
            for rel in overlay:
                target = worktree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repo_dir / rel, target)
            started = time.perf_counter()
            outcome = await review_pull_request(
                pr, LocalRepository(worktree), repo_context=repo_context
            )
            wall_s = time.perf_counter() - started
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
                cwd=repo_dir,
            )
    review, metrics = outcome.review, outcome.metrics
    cost = review_cost(metrics)
    return {
        "repo": f"{owner}/{repo}",
        "pr": number,
        "overlay": list(overlay),
        "repo_context": repo_context,
        "head": head[:7],
        "files": len(pr.files),
        "mode": metrics.mode,
        "cost_usd": float(cost.total),
        "stages_usd": {k: float(v) for k, v in cost.stages.items()},
        "wall_s": round(wall_s, 1),
        "stage_latency_ms": {k: round(v) for k, v in metrics.stage_latency_ms.items()},
        "findings": len(review.findings),
        "by_severity": {
            s: sum(1 for f in review.findings if f.severity == s)
            for s in ("blocker", "warning", "nit")
        },
        "verifier_dropped": len(metrics.verifier_dropped),
        "context_complete": metrics.context_complete,
        "min_reviewer_cache_read": min(
            (
                u.cache_read_input_tokens
                for k, u in metrics.stage_usage.items()
                if k.startswith("reviewer:")
            ),
            default=0,
        ),
        "messages": [f"[{f.severity}/{f.category}] {f.file}:{f.line} {f.message[:100]}"
                     for f in review.findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("numbers", nargs="+", type=int)
    parser.add_argument(
        "--no-repo-context", action="store_true", help="RC1-393 control: no context in the prefix"
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path("."),
        help="checkout whose origin and PRs to measure (default: the current directory)",
    )
    parser.add_argument(
        "--overlay",
        nargs="*",
        default=[],
        metavar="PATH",
        help="files copied from --repo-dir's working tree into the worktree first",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s %(message)s")
    for number in args.numbers:
        # The script's edge (RC1-426): one loop per PR, the pipeline builds
        # and closes its own client inside it.
        row = asyncio.run(
            measure(
                number,
                repo_dir=args.repo_dir,
                overlay=tuple(args.overlay),
                repo_context=not args.no_repo_context,
            )
        )
        print(json.dumps(row), flush=True)
        print(
            f"{row['repo']}#{row['pr']} {row['mode']}: ${row['cost_usd']:.4f} "
            f"{row['wall_s']}s "
            f"{row['findings']} finding(s) {row['by_severity']}",
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
