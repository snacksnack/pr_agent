"""n8n execution-cost static check (RC1-112).

Parses committed n8n workflow JSON and flags patterns that cause excess
workflow executions: aggressive polling/cron intervals, unbounded loops,
sub-workflow fan-out, and misconfigured "execute once" / trigger settings.
Fires only when workflow JSON is actually detected. Implemented in RC1-112.
"""
from __future__ import annotations


def looks_like_n8n_workflow(data: dict) -> bool:
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


def check_workflow(data: dict) -> list:
    """Return a list of findings for one n8n workflow.

    Placeholder — rules implemented in RC1-112.
    """
    raise NotImplementedError("RC1-112: n8n execution-cost rules not yet implemented")
