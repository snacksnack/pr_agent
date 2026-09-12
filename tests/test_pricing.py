"""The app's price table is a copy of the eval harness's (RC1-395), because the
runtime image cannot import the harness. These tests are what keep the copy
honest: the two tables must agree, model for model, and the cache-aware cost
must match the eval subject's to the token."""
from __future__ import annotations

from decimal import Decimal

import pytest
from agent_evals import pricing as harness_pricing

from app import pricing
from app.config import settings
from app.models import RunMetrics, TokenUsage
from evals import subject

USAGE = TokenUsage(
    input_tokens=8, output_tokens=1000,
    cache_creation_input_tokens=4000, cache_read_input_tokens=6000,
)


def test_price_tables_agree_with_the_harness_in_both_directions():
    assert set(pricing.PRICES) == set(harness_pricing.PRICES)
    for model, price in pricing.PRICES.items():
        theirs = harness_pricing.PRICES[model]
        assert price.input_per_mtok == theirs.input_per_mtok, model
        assert price.output_per_mtok == theirs.output_per_mtok, model
    assert pricing.AS_OF == harness_pricing.AS_OF


def test_cache_rates_agree_with_the_harness():
    assert pricing.CACHE_WRITE == harness_pricing.CACHE_WRITE
    assert pricing.CACHE_READ == harness_pricing.CACHE_READ


def test_cost_matches_the_eval_subject_to_the_token():
    for model in pricing.PRICES:
        assert pricing.cost_usd(model, USAGE) == subject._cost_usd(model, USAGE), model


def test_the_configured_review_model_has_a_price():
    assert settings.review_model in pricing.PRICES


def test_cost_prices_all_four_token_counts():
    price = pricing.PRICES["claude-sonnet-4-6"]
    expected = (
        Decimal(8) * price.input_per_mtok
        + Decimal(1000) * price.output_per_mtok
        + Decimal(4000) * price.input_per_mtok * Decimal("1.25")
        + Decimal(6000) * price.input_per_mtok * Decimal("0.1")
    ) / Decimal(1_000_000)
    assert pricing.cost_usd("claude-sonnet-4-6", USAGE) == expected
    assert pricing.cost_usd("claude-sonnet-4-6", TokenUsage()) == 0


def test_unknown_model_raises_rather_than_pricing_at_zero():
    with pytest.raises(pricing.UnknownModelPrice, match="no price on file for 'm'"):
        pricing.cost_usd("m", TokenUsage(input_tokens=1))


# --- review_cost: the single loop ---------------------------------------------

def _single(**kw):
    return RunMetrics(
        model="claude-sonnet-4-6",
        usage=TokenUsage(
            input_tokens=100, output_tokens=2000,
            cache_creation_input_tokens=5000, cache_read_input_tokens=20000,
        ),
        **kw,
    )


def test_single_loop_without_a_verifier_is_one_stage():
    cost = pricing.review_cost(_single())
    assert cost.stages == {"loop": cost.total}
    assert cost.total == pricing.cost_usd("claude-sonnet-4-6", _single().usage)


def test_single_loop_splits_the_verifier_out_of_the_total():
    verifier = TokenUsage(input_tokens=10, output_tokens=300, cache_read_input_tokens=9000)
    result = _single(verified=True, verifier_usage=verifier, verifier_model="claude-sonnet-4-6")
    cost = pricing.review_cost(result)
    assert set(cost.stages) == {"loop", "verifier"}
    assert cost.stages["verifier"] == pricing.cost_usd("claude-sonnet-4-6", verifier)
    assert cost.stages["loop"] + cost.stages["verifier"] == cost.total
    assert cost.total == pricing.cost_usd("claude-sonnet-4-6", result.usage)


def test_verifier_is_priced_at_its_own_model():
    """A verifier that ran on another model (rows from before RC1-428); its tokens are
    in the totals at the review model's rate, so the split re-prices them."""
    verifier = TokenUsage(input_tokens=10, output_tokens=300, cache_read_input_tokens=9000)
    result = _single(verified=True, verifier_usage=verifier, verifier_model="claude-haiku-4-5")
    cost = pricing.review_cost(result)
    assert cost.stages["verifier"] == pricing.cost_usd("claude-haiku-4-5", verifier)
    loop_only = pricing.cost_usd("claude-sonnet-4-6", result.usage) - pricing.cost_usd(
        "claude-sonnet-4-6", verifier
    )
    assert cost.stages["loop"] == loop_only
    assert cost.total == loop_only + cost.stages["verifier"]


def test_verifier_model_defaults_to_the_review_model_when_unrecorded():
    verifier = TokenUsage(output_tokens=300)
    with_model = pricing.review_cost(
        _single(verified=True, verifier_usage=verifier, verifier_model="claude-sonnet-4-6")
    )
    without = pricing.review_cost(_single(verified=True, verifier_usage=verifier))
    assert with_model == without


# --- review_cost: the multi-agent path ------------------------------------------

def test_multi_prices_every_stage_and_sums_them():
    stages = {
        "scout": TokenUsage(input_tokens=500, output_tokens=200, cache_read_input_tokens=3000),
        "warm_cache": TokenUsage(output_tokens=1, cache_creation_input_tokens=9000),
        "reviewer:diff_local": TokenUsage(output_tokens=400, cache_read_input_tokens=9000),
        "verifier": TokenUsage(output_tokens=100, cache_read_input_tokens=9000),
    }
    result = RunMetrics(
        model="claude-sonnet-4-6", mode="multi", verified=True,
        verifier_model="claude-haiku-4-5", stage_usage=stages,
    )
    cost = pricing.review_cost(result)
    assert set(cost.stages) == set(stages)
    assert cost.stages["scout"] == pricing.cost_usd("claude-sonnet-4-6", stages["scout"])
    assert cost.stages["verifier"] == pricing.cost_usd("claude-haiku-4-5", stages["verifier"])
    assert cost.total == sum(cost.stages.values())


def test_review_cost_raises_on_an_unknown_model():
    with pytest.raises(pricing.UnknownModelPrice):
        pricing.review_cost(RunMetrics(model="m", usage=TokenUsage(output_tokens=1)))
