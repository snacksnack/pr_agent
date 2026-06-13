"""Local dry-run CLI (RC1-113).

Runs the full review against a real PR and prints the would-be review to the
terminal. Nothing is posted to GitHub — this is the tool used to tune review
quality before the App and hosting exist.

Usage (once implemented in RC1-113):

    python -m app.review --pr owner/repo#123
"""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.review",
        description="Dry-run the PR review agent against a real pull request "
        "and print the would-be review (nothing is posted to GitHub).",
    )
    parser.add_argument(
        "--pr",
        required=True,
        metavar="owner/repo#N",
        help="Target pull request, e.g. octocat/hello-world#42",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    # TODO(RC1-113): ingest PR (RC1-108) -> agent loop (RC1-110) -> rubric
    # (RC1-111) -> n8n check (RC1-112), then print the formatted review.
    raise NotImplementedError("RC1-113: dry-run CLI not yet implemented")


if __name__ == "__main__":
    raise SystemExit(main())
