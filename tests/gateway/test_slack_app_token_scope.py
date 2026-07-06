"""Regression: the Slack Socket Mode app token must be profile-scoped (#59739).

Under ``multiplex_profiles: true`` every profile's Slack adapter runs inside its
own ``_profile_runtime_scope``. The app-level token (``xapp-...``) was read via
``os.getenv("SLACK_APP_TOKEN")``, which in a multiplexed process holds the
DEFAULT profile's ``.env`` value — so every secondary profile opened Socket Mode
against the default profile's Slack app and silently received no events. It must
resolve through the profile secret scope, exactly like the bot token does.
"""
import pytest

from agent import secret_scope as ss
from plugins.platforms.slack.adapter import _resolve_slack_app_token


@pytest.fixture(autouse=True)
def _reset():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_app_token_reads_profile_scope_not_process_env(monkeypatch):
    # Process env holds the DEFAULT profile's app token (the leak source).
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-default-profile")
    ss.set_multiplex_active(True)

    tok = ss.set_secret_scope({"SLACK_APP_TOKEN": "xapp-profile-b"})
    try:
        assert _resolve_slack_app_token() == "xapp-profile-b"
    finally:
        ss.reset_secret_scope(tok)


def test_app_token_two_profiles_isolated():
    ss.set_multiplex_active(True)

    tok_a = ss.set_secret_scope({"SLACK_APP_TOKEN": "xapp-a"})
    try:
        assert _resolve_slack_app_token() == "xapp-a"
    finally:
        ss.reset_secret_scope(tok_a)

    tok_b = ss.set_secret_scope({"SLACK_APP_TOKEN": "xapp-b"})
    try:
        assert _resolve_slack_app_token() == "xapp-b"
    finally:
        ss.reset_secret_scope(tok_b)


def test_app_token_single_profile_falls_back_to_environ(monkeypatch):
    # Multiplex inactive (default deployment): identical to legacy os.getenv.
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-single")
    assert _resolve_slack_app_token() == "xapp-single"


def test_app_token_fails_closed_when_unscoped_under_multiplex(monkeypatch):
    # A missing scope under multiplex must fail loudly rather than leak the
    # process-global (possibly another profile's) token.
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-leak")
    ss.set_multiplex_active(True)
    with pytest.raises(ss.UnscopedSecretError):
        _resolve_slack_app_token()
