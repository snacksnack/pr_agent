# RC1-426 — The pipeline is async end to end; the edges own the loop

The pipeline mixed two Anthropic clients — an async one for the warm call
and the reviewers, a sync one for the verifier — and called `asyncio.run`
in the middle of `_review` to run the fan-out. Every caller was
synchronous, the webhook's worker ran in a Starlette thread, and the
client the pipeline built had to be closed on the loop it created
(RC1-394's "Event loop is closed"). This story makes the pipeline one
coroutine over one async client and moves the sync boundary to the entry
points.

- **Ticket:** [RC1-426](https://hirereidcollins.atlassian.net/browse/RC1-426).
- **Related:** RC1-421 (LangGraph removed; asyncio is the orchestrator),
  RC1-422 (one pipeline), RC1-429 (the verifier returns a `Verification`,
  which made its move to async a two-line change), RC1-394 (the
  close-on-the-right-loop bug).
- **No model call changed.** The requests the warm call, the reviewers
  and the verifier send are byte-identical; only the client type behind
  the verifier's call changed (`Anthropic` → the review's
  `AsyncAnthropic`). No corpus band run was owed; the n8n case was run
  once through the eval subject to prove the path with the real SDK
  client (below).

## The boundary

| Where | What happens |
|---|---|
| `app/agent/pipeline.py` | `review_pull_request` is a coroutine. `client` is the one async client for the whole review (built from settings and closed here, on the caller's loop, in a `finally`; an injected client is the caller's). `_review` awaits the fan-out and the verifier. No `asyncio.run`. |
| `app/agent/verifier.py` | `verify_findings` is a coroutine and awaits `client.messages.create`. |
| `app/webhook.py` | `process_event` is a coroutine; Starlette runs it on the receiver's loop after the 202. It awaits the pipeline directly. |
| `app/review.py` | `_default_review` is the CLI's one bridge: `asyncio.run(review_pull_request(...))`. The application's only `asyncio.run`, and a test asserts it. |
| `evals/subject.py` | `_capture` — the eval's replacement for the CLI's review callable — is the eval's bridge, one `asyncio.run` per case. |
| `evals/tiebreak.py` | `probe_case` and `pipeline_case` are coroutines; `RecordingClient` is an async wrapper. |
| `scripts/measure_pr.py`, `scripts/measure_tiebreak.py` | one `asyncio.run` per PR / per command at the script's edge; the tiebreak script builds its `AsyncAnthropic` inside the loop and closes it there. |

## Blocking I/O stays off the loop

The GitHub side is synchronous httpx and stays so: the token mint, the PR
fetch and the posting in the webhook, and the two pipeline stages that
read the repository — the deterministic checks and the context — through
`GitHubRepository` on the live path. Awaiting the pipeline on the
receiver's loop would have blocked it for the length of those calls (a
tree fetch and up to thirty file reads under the budget), which is the
loop that acknowledges deliveries within GitHub's ten seconds and answers
Fly's health check. Each of those sections runs in the default executor
(`asyncio.to_thread`); the stage spans are opened on the loop around
them, so the trace is unchanged.

## What is preserved

- The API budget, the read caps and the request timeout (`REQUEST_TIMEOUT_S`
  on the client) are untouched.
- Cache accounting is the same per stage; the verifier's usage is folded
  into the totals by the pipeline as since RC1-429.
- A reviewer that raises fails the review: `gather` propagates the first
  error, the owned client is closed, the webhook logs `review_failed`.
- A caller's cancellation (a timeout around the coroutine) propagates the
  same way, with the same close.

## Evidence

Offline: the suite is green with coverage at 95 % against the 88 % floor.
`tests/test_pipeline.py` drives the coroutine under `asyncio.run` through
one scripted async client, and adds: the built client is closed on the
caller's loop and an injected one is not; a model error propagates and
still closes; a cancellation mid-call propagates and still closes; and
`asyncio.run` appears under `app/` only in `app/review.py`. The verifier,
tiebreak and webhook tests await their fakes.

Billed, the n8n corpus case with a checkout through the eval subject, on
the real `AsyncAnthropic` for every call including the verifier:

| Run `pr-review-20260912T112938.814738Z` | value |
|---|---|
| characteristics | 5/5 pass (found, category `n8n`, warning floor, exit 0, merged once) |
| verifier | ran on the same async client, 0 dropped, 0 downgraded |
| cache premise | every reviewer read the prefix from cache (min 4 325 tokens) |
| stage latency | checks 1 ms · context 9 ms · fan-out 10.6 s · verifier 2.2 s |
| cost / latency | $0.037 / 13.2 s |
| loop warnings | none ("Event loop is closed" absent from the log) |

## Left open

- **Live check.** The first production review after the deploy: the same
  log lines as before, and `/healthz` answering during a review (the
  executor did its job).
- The ticket text names a scout and a `ReviewResult`; both predate
  RC1-427 and RC1-429. The surviving stages are what went async.
