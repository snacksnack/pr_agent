"""Model prices, so a review can carry a cost rather than four token counts (RC1-395).

A local snapshot of the first-party Anthropic list prices, per million tokens
in USD, plus the two prompt-cache rates the API bills at: a cache write is
1.25x the input price, a cache read 0.1x. ``AS_OF`` says when a reader last
checked the list.

Duplicated from ``agent_evals.pricing`` (``PRICES``, ``CACHE_WRITE``,
``CACHE_READ``) on purpose: the runtime image is ``python:3.12-slim``
with no git, and the eval harness is pinned by git ref, so the webhook cannot
import it in production without shipping the whole harness (the same reason
``app/observability.py`` exists). ``tests/test_pricing.py`` asserts the two
tables agree for every model in either, so they cannot drift silently.

An unknown model raises rather than pricing at zero — a review that looks
free after a model rename is worse than one that is not priced, because it
looks like a finding. The live path catches the raise at the boundary and
ships no point; the platform's rule is that a gap means "unmeasured" and a
zero never does.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models import ReviewResult, TokenUsage

#: When these prices were last verified against the published price list.
AS_OF = "2026-09-07"

_MILLION = Decimal("1000000")

#: The API's cache rates, as multiples of the input price.
CACHE_WRITE = Decimal("1.25")
CACHE_READ = Decimal("0.1")


class UnknownModelPrice(Exception):
    """No price on file for a model."""


@dataclass(frozen=True)
class ModelPrice:
    """Input and output price per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


def _price(input_per_mtok: str, output_per_mtok: str) -> ModelPrice:
    return ModelPrice(Decimal(input_per_mtok), Decimal(output_per_mtok))


#: Standard list prices; promotional rates are deliberately not used.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": _price("5.00", "25.00"),
    "claude-opus-4-8": _price("5.00", "25.00"),
    "claude-sonnet-5": _price("2.00", "10.00"),  # billed list price (RC1-401)
    "claude-sonnet-4-6": _price("3.00", "15.00"),
    "claude-haiku-4-5": _price("1.00", "5.00"),
}


def cost_usd(model: str, usage: TokenUsage) -> Decimal:
    """Cache-aware cost of ``usage`` at ``model``, exact to the token.

    All four counts are priced: uncached input, output, cache writes at
    1.25x and cache reads at 0.1x. Pricing the uncached input alone
    undercounted every review since RC1-350 by roughly 2.5x (RC1-387).
    """
    try:
        price = PRICES[model]
    except KeyError as exc:
        known = ", ".join(sorted(PRICES))
        raise UnknownModelPrice(
            f"no price on file for {model!r} (known: {known}); add it to app.pricing "
            f"rather than letting the review look free"
        ) from exc
    return (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
        + Decimal(usage.cache_creation_input_tokens) * price.input_per_mtok * CACHE_WRITE
        + Decimal(usage.cache_read_input_tokens) * price.input_per_mtok * CACHE_READ
    ) / _MILLION


@dataclass(frozen=True)
class ReviewCost:
    """What one review cost, in total and per stage."""

    total: Decimal
    stages: dict[str, Decimal]


def review_cost(result: ReviewResult) -> ReviewCost:
    """Price a finished review from the token counts it carries.

    The pipeline (RC1-390) records every stage — ``scout``, ``warm_cache``,
    ``reviewer:<name>``, ``verifier`` — so each is priced on its own and the
    total is their sum. A result with no stage breakdown (the retired single
    loop's rows in the eval store, a bare result in a test) is priced from
    its totals as ``loop`` (the total less the verifier) and, when the pass
    ran, ``verifier``. The verifier
    is priced at the model it actually used (``review_verify_model`` may
    differ from the review model); everything else at the review model.

    Raises :class:`UnknownModelPrice` for a model not in the table.
    """
    verifier_model = result.verifier_model or result.model
    stages: dict[str, Decimal] = {}
    if result.stage_usage:
        for stage, usage in result.stage_usage.items():
            model = verifier_model if stage == "verifier" else result.model
            stages[stage] = cost_usd(model, usage)
        return ReviewCost(total=sum(stages.values(), Decimal(0)), stages=stages)

    total = cost_usd(result.model, result.usage)
    if result.verified:
        verifier = cost_usd(verifier_model, result.verifier_usage)
        # The totals include the verifier at the review model's price; when
        # the verifier ran on a different model, price its share at that one.
        loop = total - cost_usd(result.model, result.verifier_usage)
        stages = {"loop": loop, "verifier": verifier}
        return ReviewCost(total=loop + verifier, stages=stages)
    return ReviewCost(total=total, stages={"loop": total})
