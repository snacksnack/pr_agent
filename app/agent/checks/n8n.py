"""n8n execution-cost static check (RC1-112).

Parses committed n8n workflow JSON and flags patterns that cause excess
workflow executions: aggressive polling/cron intervals, unbounded loops,
sub-workflow fan-out, and misconfigured "execute once" / trigger settings.
Fires only when workflow JSON is actually detected.

Design notes:

- **Version-tolerant.** n8n's export shape drifts across versions and node
  types. Every accessor is defensive: unknown/garbage structures yield *no*
  finding rather than a false alarm (a hard requirement of the ticket). Node
  types are matched on their suffix (``n8n-nodes-base.cron`` -> ``cron``) so a
  package rename doesn't silently disable a rule.
- **Low-noise by construction.** Rules fire only on concretely costly
  configurations (sub-five-minute schedules, sub-minute polling, batch sizes
  that disable batching, per-item sub-workflow fan-out). Anything borderline is
  left for the agent loop / RC1-114 tuning rather than flagged here.
- **Self-contained & offline.** Pure function of the parsed JSON; no network,
  no config reads. Findings are :class:`app.models.Finding` objects with
  category ``n8n``; the caller anchors them to the workflow file.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from app.models import Finding, PullRequest

# Category every finding in this module carries (matches prompts.CATEGORIES).
CATEGORY = "n8n"

# A schedule/poll that fires more often than this many minutes is "aggressive".
# Sub-minute (seconds-based) intervals are always aggressive. Kept as a module
# constant so RC1-114 tuning has a single dial.
AGGRESSIVE_MINUTES = 5

# Node-kind identifiers (lowercased ``type`` suffix; see ``_node_kind``).
_CRON_KIND = "cron"
_SCHEDULE_KIND = "scheduletrigger"
_INTERVAL_KIND = "interval"
_SPLIT_KIND = "splitinbatches"
_SUBWORKFLOW_KINDS = {"executeworkflow"}


# --- detection ------------------------------------------------------------

def looks_like_n8n_workflow(data: Any) -> bool:
    """Heuristic: does this parsed JSON look like an n8n workflow export?

    n8n workflow exports carry a top-level ``nodes`` list and a ``connections``
    map. Kept version-tolerant on purpose — unknown shapes return False so we
    never raise false alarms.
    """
    return (
        isinstance(data, dict)
        and isinstance(data.get("nodes"), list)
        and isinstance(data.get("connections"), dict)
    )


# --- small defensive helpers ----------------------------------------------

def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _node_kind(node: dict) -> str:
    """Normalize a node ``type`` to its lowercased suffix.

    ``n8n-nodes-base.scheduleTrigger`` -> ``scheduletrigger``. Returns ``""``
    when the type is missing or not a string.
    """
    t = node.get("type")
    if not isinstance(t, str):
        return ""
    return t.rsplit(".", 1)[-1].strip().lower()


def _node_label(node: dict, kind: str) -> str:
    name = node.get("name")
    return name if isinstance(name, str) and name else (kind or "node")


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion (n8n stores some values as strings)."""
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _finding(node: dict, kind: str, message: str, suggestion: str) -> Finding:
    return Finding(
        severity="warning",
        category=CATEGORY,
        message=f"Node '{_node_label(node, kind)}': {message}",
        file=None,  # the caller anchors this to the workflow file
        line=None,
        suggestion=suggestion,
    )


# --- cron-expression frequency estimate -----------------------------------

def _cron_is_aggressive(expr: Any) -> tuple[bool, str]:
    """Conservatively decide whether a cron expression fires too often.

    Only flags clearly sub-five-minute schedules: a wildcard minute field (with
    a wildcard hour, so it really is every minute) or a ``*/N`` minute step with
    ``N < AGGRESSIVE_MINUTES``. Six-field (seconds-precision) expressions whose
    seconds field isn't a single fixed value are treated as sub-minute. Anything
    we don't clearly understand returns ``False`` — no false alarms.
    """
    if not isinstance(expr, str) or not expr.strip():
        return False, ""
    fields = expr.split()
    if len(fields) == 6:
        seconds, minute, hour = fields[0], fields[1], fields[2]
        if seconds == "*" or seconds.startswith("*/"):
            return True, "runs every few seconds"
    elif len(fields) == 5:
        minute, hour = fields[0], fields[1]
    else:
        return False, ""  # non-standard field count — don't guess

    if minute == "*" and hour == "*":
        return True, "runs every minute"
    if minute.startswith("*/") and hour == "*":
        try:
            step = int(minute[2:])
        except ValueError:
            return False, ""
        if 0 < step < AGGRESSIVE_MINUTES:
            return True, f"runs every {step} minute(s)"
    return False, ""


# --- schedule / cron trigger rules ----------------------------------------

def _eval_everyx(item: dict) -> tuple[bool, str]:
    """Evaluate an ``everyX`` value/unit pair (cron + poll share this shape)."""
    value = _to_number(item.get("value"))
    unit = str(item.get("unit") or "").lower()
    if value is None:
        return False, ""
    if unit in ("second", "seconds"):
        return True, f"runs every {int(value)} second(s)"
    if unit in ("minute", "minutes") and value < AGGRESSIVE_MINUTES:
        return True, f"runs every {int(value)} minute(s)"
    return False, ""


def _eval_cron_item(item: Any) -> tuple[bool, str]:
    """Evaluate one entry of a cron node's ``triggerTimes`` / a poll's ``pollTimes``."""
    item = _as_dict(item)
    mode = str(item.get("mode") or "").lower()
    if mode == "everyminute":
        return True, "fires every minute"
    if mode == "everyx":
        return _eval_everyx(item)
    if mode in ("custom", "cronexpression"):
        return _cron_is_aggressive(item.get("cronExpression") or item.get("expression"))
    # everyHour / everyDay / everyWeek / everyMonth / unknown -> not aggressive
    return False, ""


def _eval_schedule_interval(item: Any) -> tuple[bool, str]:
    """Evaluate one ``rule.interval`` entry of a scheduleTrigger node."""
    item = _as_dict(item)
    field = str(item.get("field") or "").lower()
    if field in ("second", "seconds"):
        n = _to_number(item.get("secondsInterval") or item.get("value"))
        return True, (f"runs every {int(n)} second(s)" if n else "runs on a sub-minute schedule")
    if field in ("minute", "minutes"):
        n = _to_number(item.get("minutesInterval") or item.get("value"))
        if n is not None and n < AGGRESSIVE_MINUTES:
            return True, f"runs every {int(n)} minute(s)"
        return False, ""
    if field in ("cronexpression", "cron"):
        return _cron_is_aggressive(item.get("expression") or item.get("cronExpression"))
    # hours / days / weeks / months / unknown -> not aggressive
    return False, ""


def _check_schedule(node: dict, kind: str, params: dict) -> Iterable[Finding]:
    if kind == _CRON_KIND:
        for item in _as_list(_as_dict(params.get("triggerTimes")).get("item")):
            hot, why = _eval_cron_item(item)
            if hot:
                yield _finding(
                    node, kind,
                    f"cron trigger {why}, which can run up to ~525k times/year and "
                    "burn workflow executions.",
                    "Increase the interval (e.g. every 15+ minutes) or switch to an "
                    "event/webhook trigger instead of frequent polling.",
                )
    elif kind == _SCHEDULE_KIND:
        for item in _as_list(_as_dict(params.get("rule")).get("interval")):
            hot, why = _eval_schedule_interval(item)
            if hot:
                yield _finding(
                    node, kind,
                    f"schedule trigger {why}, a high-frequency schedule that burns "
                    "workflow executions.",
                    "Increase the interval (e.g. every 15+ minutes) or move to an "
                    "event/webhook trigger.",
                )
    elif kind == _INTERVAL_KIND:
        value = _to_number(params.get("interval"))
        unit = str(params.get("unit") or "seconds").lower()
        why = ""
        if value is not None:
            if unit in ("second", "seconds") and value < 60:
                why = f"every {int(value)} second(s)"
            elif unit in ("minute", "minutes") and value < AGGRESSIVE_MINUTES:
                why = f"every {int(value)} minute(s)"
        if why:
            yield _finding(
                node, kind,
                f"interval trigger fires {why}, a high-frequency schedule that "
                "burns workflow executions.",
                "Increase the interval, or use an event-driven trigger instead.",
            )


# --- polling rule (any trigger with pollTimes) ----------------------------

def _check_polling(node: dict, kind: str, params: dict) -> Iterable[Finding]:
    for item in _as_list(_as_dict(params.get("pollTimes")).get("item")):
        hot, why = _eval_cron_item(item)
        if hot:
            yield _finding(
                node, kind,
                f"polls {why}; frequent polling consumes a workflow execution on "
                "every poll, whether or not there is new data.",
                "Poll less often (e.g. every 15+ minutes) or replace polling with a "
                "webhook/event trigger where the service supports it.",
            )


# --- unbounded / misconfigured loop rule ----------------------------------

def _check_loop(node: dict, kind: str, params: dict) -> Iterable[Finding]:
    if kind != _SPLIT_KIND:
        return
    if "batchSize" not in params:
        return  # absent -> uses n8n's default; don't guess
    size = _to_number(params.get("batchSize"))
    if size is not None and size <= 0:
        yield _finding(
            node, kind,
            "Loop Over Items has a batch size of 0, which disables batching and "
            "processes every item in a single unbounded pass — on large inputs "
            "this can run away and exhaust memory/executions.",
            "Set an explicit positive batch size (e.g. 10–100) so the loop is "
            "bounded per iteration.",
        )


# --- sub-workflow fan-out rule --------------------------------------------

def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _check_subworkflow(node: dict, kind: str, params: dict) -> Iterable[Finding]:
    if kind not in _SUBWORKFLOW_KINDS:
        return
    # "Execute once" can live at node level (executeOnce) or in params depending
    # on the n8n version; treat either truthy value as bounded.
    execute_once = _is_truthy(node.get("executeOnce")) or _is_truthy(params.get("executeOnce"))
    if not execute_once:
        yield _finding(
            node, kind,
            "Execute Sub-workflow runs once per incoming item ('execute once' is "
            "off), so it fans out to one sub-workflow execution per item — an "
            "upstream node emitting N items triggers N runs.",
            "Enable 'Execute once' if a single run suffices, or batch/limit items "
            "upstream so the fan-out is bounded.",
        )


# --- public entry point ----------------------------------------------------

_RULES = (_check_schedule, _check_polling, _check_loop, _check_subworkflow)


def check_workflow(data: Any) -> list[Finding]:
    """Return execution-cost findings for one parsed n8n workflow.

    Returns an empty list for anything that isn't a recognizable n8n workflow,
    or that uses only sane settings. Never raises on malformed node data: a
    single bad node is skipped, not propagated, so one odd export can't sink the
    review.
    """
    if not looks_like_n8n_workflow(data):
        return []
    findings: list[Finding] = []
    for node in _as_list(data.get("nodes")):
        if not isinstance(node, dict):
            continue
        try:
            kind = _node_kind(node)
            params = _as_dict(node.get("parameters"))
            for rule in _RULES:
                findings.extend(rule(node, kind, params))
        except Exception:
            continue  # robustness: never let one node break the whole check
    return findings


# --- PR-level runner (source-agnostic) ------------------------------------

# Sources one changed file's text by repo-relative path. Returns ``None`` to
# skip it — the file is absent, binary/oversized, or otherwise unreadable. The
# dry-run CLI reads from a local checkout; the live webhook fetches at the PR
# head via the GitHub Contents API. Either way the check sees the same bytes.
ReadText = Callable[[str], "str | None"]


def _coerce_finding(item: Any) -> Finding | None:
    """Normalize whatever a rule/check returns into a :class:`Finding`."""
    if isinstance(item, Finding):
        return item
    if isinstance(item, dict):
        severity, message = item.get("severity"), item.get("message")
        if not severity or not message:
            return None
        line = item.get("line")
        return Finding(
            severity=str(severity),
            category=str(item.get("category") or CATEGORY),
            message=str(message),
            file=item.get("file"),
            line=int(line) if isinstance(line, int) else None,
            suggestion=item.get("suggestion"),
        )
    return None


def run_checks(pr: PullRequest, read_text: ReadText) -> list[Finding]:
    """Run the execution-cost check over a PR's changed n8n workflow JSON.

    ``read_text(filename) -> str | None`` sources each changed file's contents
    from wherever the caller has them (a local checkout, the GitHub Contents
    API, …); returning ``None`` skips that file. This keeps the dry-run CLI and
    the live webhook on one code path — only the byte source differs.

    Recoverable on every axis (a hard requirement of the check): removed files
    and non-``.json`` paths are ignored, and a file that's missing, isn't valid
    JSON, isn't an n8n workflow, or trips the check is skipped rather than
    propagated — one bad file never sinks the review. Findings with no file are
    anchored to their workflow.
    """
    findings: list[Finding] = []
    for f in pr.files:
        if f.status == "removed" or not f.filename.endswith(".json"):
            continue
        text = read_text(f.filename)
        if text is None:
            continue  # missing / unreadable at head — skip
        try:
            data = json.loads(text)
        except ValueError:
            continue  # not valid JSON — skip
        if not looks_like_n8n_workflow(data):
            continue
        try:
            raw = check_workflow(data)
        except Exception:
            continue  # a single bad workflow shouldn't sink the review
        for item in raw or []:
            finding = _coerce_finding(item)
            if finding is not None:
                if finding.file is None:
                    finding.file = f.filename
                findings.append(finding)
    return findings
