"""Test isolation from the developer's `.env` (RC1-255).

Found while adding the `block_on` contract test: `.env` sets `REVIEW_BLOCK_ON`,
so editing the default in `app/config.py` had no effect locally — but **CI has
no `.env`**, so CI exercises the default. The same test was reading two
different values depending on where it ran.

Both values happen to be `leaked_secret` today, so nothing was failing. That is
what makes it worth fixing now rather than after it bites: a suite whose
behaviour depends on the machine can be green in both places while testing
different things, and the first symptom is usually a confusing CI failure on an
unrelated change.

`launch-planner-agent` hit this for real in RC1-259 and fixed it the same way
(ADR-0032). This is that fix, sized for one settings class.

Real environment variables still win, so any test using `monkeypatch.setenv` is
unaffected. What changes is only that an unset value now resolves to its
declared default instead of to whatever is on disk.
"""

from __future__ import annotations

import pytest

from app import config as app_config

#: A path that does not exist. pydantic-settings treats a missing `env_file` as
#: "no file", which is exactly the state CI runs in.
_NO_ENV = "/nonexistent/.env.for-tests"


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch):
    """Point `Settings` at an `.env` that is not there, for every test.

    The cache is cleared on the way in *and* out: an instance built before the
    redirect would still hold `.env` values, and one built during a test would
    leak into the next.
    """
    monkeypatch.setitem(app_config.Settings.model_config, "env_file", _NO_ENV)
    app_config.get_settings.cache_clear()
    # `app.config.settings` is a module-level singleton built at import time, so
    # rebinding it is what actually reaches the code under test.
    monkeypatch.setattr(app_config, "settings", app_config.Settings())
    yield
    app_config.get_settings.cache_clear()
