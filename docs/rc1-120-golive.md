# RC1-120 — Register & install the GitHub App + end-to-end live test

The final story. The service is built and deployable (RC1-107…119); this turns
it on: register the App, install it account-wide, point it at the live Fly
endpoint, and confirm a real PR gets one correct bundled review.

- **Ticket:** [RC1-120](https://hirereidcollins.atlassian.net/browse/RC1-120)
- **Depends on:** RC1-119 (Dockerize + Fly.io) — done.

Registration and install are **Reid's to do** in the GitHub UI (they need
account-owner access). This doc is the exact step list, plus the hardening that
shipped with the story (retries/backoff + log visibility) and the verification
checklist for the live test.

---

## 0. Prerequisites

- The Fly app is deployed and healthy. Its public base URL is
  `https://pr-review-agent-snacksnack.fly.dev` (from `fly.toml` →
  `app = "pr-review-agent-snacksnack"`).
  ```bash
  fly status
  curl https://pr-review-agent-snacksnack.fly.dev/healthz   # -> {"status":"ok"}
  ```
- You can edit GitHub App settings for the account that owns the repos.

The webhook path is `/webhook`, so the full URL used below is:

```
https://pr-review-agent-snacksnack.fly.dev/webhook
```

---

## 1. Pick the webhook secret first

The same secret value goes in **two** places — the App's webhook config and the
Fly secret store — so generate it once and reuse it.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Keep this handy; it's `GITHUB_WEBHOOK_SECRET` below.

---

## 2. Register the App  (AC: registered with min perms + events + webhook URL)

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
| --- | --- |
| **GitHub App name** | `PR Review Agent` (any unique name) |
| **Homepage URL** | the repo or `https://pr-review-agent-snacksnack.fly.dev` |
| **Webhook → Active** | ✅ checked |
| **Webhook URL** | `https://pr-review-agent-snacksnack.fly.dev/webhook` |
| **Webhook secret** | the value from step 1 |

**Repository permissions** (Permissions & events → Repository permissions) —
grant the minimum the agent needs and nothing more:

| Permission | Access | Why |
| --- | --- | --- |
| **Pull requests** | **Read & write** | read the PR/diff; post the review, summary comment, inline comments |
| **Contents** | **Read-only** | (future-proofing) read repo files for context |

Leave everything else **No access**.

**Subscribe to events** (same page, further down):

- ✅ **Pull request**

That's the only event the receiver acts on (`opened` / `synchronize` /
`reopened`); everything else is acked and ignored.

**Where can this App be installed?** → **Only on this account**.

Click **Create GitHub App**.

### After creation — collect two values

1. **App ID** — shown at the top of the App's General page → `GITHUB_APP_ID`.
2. **Private key** — *Private keys* section → **Generate a private key**.
   Downloads a `.pem`. This is `GITHUB_APP_PRIVATE_KEY` (the file's contents).
   Store it somewhere safe; GitHub only shows it once.

---

## 3. Push the three secrets to Fly  (no secrets in git)

```bash
fly secrets set \
  ANTHROPIC_API_KEY="sk-ant-..." \
  GITHUB_APP_ID="<App ID from step 2>" \
  GITHUB_WEBHOOK_SECRET="<value from step 1>" \
  GITHUB_APP_PRIVATE_KEY="$(cat ~/Downloads/pr-review-agent.*.private-key.pem)"
```

`GITHUB_APP_PRIVATE_KEY` accepts the multi-line PEM as-is; the auth layer also
normalizes literal `\n` escapes if your shell flattens it (see `app/auth.py`).
Setting secrets restarts the machine. Confirm:

```bash
fly secrets list        # names only, never values: expect the 4 above
fly status
```

> The non-secret `GITHUB_MAX_ATTEMPTS` / `LOG_LEVEL` knobs already have sane
> defaults (4 and `INFO`); set them via `fly secrets set` only to override.

---

## 4. Install the App account-wide  (AC: "All repositories incl. future")

App page → **Install App** → choose the account → **Install**.

- Repository access → **All repositories**.

GitHub's "All repositories" install **includes repos created later**, which is
the account-wide / future-repos requirement. Confirm the install lands on the
right account and shows "All repositories".

On install (and on save in step 2) GitHub sends a **ping** delivery; the service
acks it with `200` and logs `ping delivery=...`.

---

## 5. End-to-end live test  (AC: one correct bundled review)

1. Open (or reopen, or push a new commit to) a **real PR** in any repo on the
   account. A small PR with an obvious issue is the best smoke test.
2. Fly cold-starts the machine on the delivery (idle auto-stop is expected).
3. Within ~30–60s the PR should show **one** review from the App:
   - a single **summary comment** (carries the hidden `pr-review-agent:summary`
     marker), leading with the model's summary + a verdict note;
   - **inline comments** anchored to changed lines, severity-tagged;
   - verdict is **Comment** (advisory) unless a `leaked_secret`-category finding
     is present, which escalates to **Request changes**.

### Watch the logs while you test

```bash
fly logs        # live tail
```

Expected happy-path sequence for one delivery (structured, INFO level):

```
... app.webhook accepted               delivery=<id> repo=<o/r> pr=<n> ...
... app.webhook review_posted          findings=<k> event=COMMENT summary=created new_comments=<k> dismissed=0
```

Re-push the branch and confirm tidiness (RC1-118): the summary comment is
**edited in place** (`summary=updated`), already-posted inline comments aren't
repeated, and a stale head logs `skip_stale_head`. A duplicate delivery logs
`skip_duplicate_delivery`.

### Verification checklist

- [ ] `/healthz` returns `{"status":"ok"}` over HTTPS.
- [ ] Webhook **Recent Deliveries** (App → Advanced) show `202` for
      `pull_request` and `200` for the `ping`. A `401` means the webhook secret
      doesn't match between the App and `fly secrets`.
- [ ] The PR has exactly **one** App review, not a scatter of comments.
- [ ] Inline comments land on the intended lines with correct severity tags.
- [ ] A second push refreshes (not duplicates) the review.
- [ ] Logs show `accepted` → `review_posted`; no `review_failed`.

---

## Hardening shipped with this story

Code changes that landed for RC1-120 (the parts that aren't Reid's UI clicks):

- **Retries / backoff on the GitHub API** — `app/retry.py`. Every GitHub call
  (reads, writes, *and* App-auth token minting) re-sends on transient failures:
  dropped connections, `5xx`, `429`, and the rate-limited `403` (secondary rate
  limit). Backoff is exponential with jitter and honors GitHub's `Retry-After` /
  `X-RateLimit-Reset` hints, all bounded (`GITHUB_MAX_ATTEMPTS`, default 4) so a
  bad case can't hang the background worker. A plain `403` (real permission
  denial), `404`, and `422` are **not** retried — they're terminal.
- **Error visibility in logs** — `configure_logging()` in `app/webhook.py`
  attaches a stdout handler to the `app.*` loggers at `LOG_LEVEL` so the INFO
  lifecycle lines (`accepted`, `review_posted`, dedup skips) and retry WARNINGs
  actually surface under uvicorn instead of being swallowed at the root's
  default WARNING level. The existing worker already logs `review_failed` with a
  traceback on any failure, and signature/secret values are never logged.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Delivery shows `401` | webhook secret mismatch | re-set `GITHUB_WEBHOOK_SECRET` to match the App's webhook secret |
| Delivery shows `500` | `GITHUB_WEBHOOK_SECRET` unset on Fly | `fly secrets set GITHUB_WEBHOOK_SECRET=...` |
| `202` but no review, `review_failed` in logs | bad App key / not installed on repo / missing `ANTHROPIC_API_KEY` | check `fly secrets list`; confirm install covers the repo |
| Review posts but no inline comments | findings fell outside the diff (folded into summary), or a 422 anchor reject retried summary-only | expected behavior; check the summary's "Findings not tied to a specific line" |
| No delivery at all | event not subscribed, or wrong webhook URL | App → Permissions & events: Pull request checked; URL ends in `/webhook` |
