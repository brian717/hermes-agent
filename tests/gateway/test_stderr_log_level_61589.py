"""Regression tests for #61589.

`start_gateway` used to attach a WARNING+ stderr StreamHandler to the root
logger unconditionally. Under launchd/systemd, stderr is redirected to a
never-rotated ``StandardErrorPath`` file, so every WARNING+ record was
duplicated there (the rotating ``errors.log`` already captures them), growing
that file without bound (208MB observed on one host).

``_resolve_stderr_log_level`` fixes this by raising the floor to CRITICAL when
stderr is not a TTY, while preserving today's behavior for interactive runs and
honoring an explicit ``HERMES_STDERR_LOG_LEVEL`` override.
"""

import logging

import pytest

from gateway.run import _resolve_stderr_log_level


class _FakeStream:
    """Minimal stderr stand-in with a controllable ``isatty``."""

    def __init__(self, isatty):
        if isatty is _RAISE:
            self._isatty = None
        else:
            self._isatty = bool(isatty)

    def isatty(self):
        if self._isatty is None:
            raise OSError("isatty not supported")
        return self._isatty


_RAISE = object()


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    # Ensure a stray environment value never leaks into the auto-detect tests.
    monkeypatch.delenv("HERMES_STDERR_LOG_LEVEL", raising=False)


# --- Interactive (TTY): today's behavior is preserved --------------------

def test_tty_default_is_warning():
    assert _resolve_stderr_log_level(0, _FakeStream(True)) == logging.WARNING


def test_tty_verbose_is_info():
    assert _resolve_stderr_log_level(1, _FakeStream(True)) == logging.INFO


def test_tty_very_verbose_is_debug():
    assert _resolve_stderr_log_level(2, _FakeStream(True)) == logging.DEBUG


# --- Redirected (not a TTY): the fix ------------------------------------

def test_redirected_default_is_critical():
    # The core regression: a redirected stderr must NOT receive WARNING records.
    assert _resolve_stderr_log_level(0, _FakeStream(False)) == logging.CRITICAL


def test_redirected_verbose_still_critical():
    # A redirect wins over -v: nobody passes -v under launchd, and the point is
    # to keep the unbounded file quiet.
    assert _resolve_stderr_log_level(1, _FakeStream(False)) == logging.CRITICAL


def test_stream_without_working_isatty_treated_as_redirected():
    assert _resolve_stderr_log_level(0, _FakeStream(_RAISE)) == logging.CRITICAL


# --- Explicit override --------------------------------------------------

def test_env_override_by_name_wins_over_redirect(monkeypatch):
    monkeypatch.setenv("HERMES_STDERR_LOG_LEVEL", "WARNING")
    assert _resolve_stderr_log_level(0, _FakeStream(False)) == logging.WARNING


def test_env_override_by_number(monkeypatch):
    monkeypatch.setenv("HERMES_STDERR_LOG_LEVEL", "50")
    assert _resolve_stderr_log_level(0, _FakeStream(True)) == 50


def test_env_override_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("HERMES_STDERR_LOG_LEVEL", "info")
    assert _resolve_stderr_log_level(0, _FakeStream(False)) == logging.INFO


def test_invalid_env_override_falls_through_to_autodetect(monkeypatch):
    monkeypatch.setenv("HERMES_STDERR_LOG_LEVEL", "banana")
    # Unrecognized value is ignored; redirected stream still resolves to CRITICAL.
    assert _resolve_stderr_log_level(0, _FakeStream(False)) == logging.CRITICAL


def test_blank_env_override_ignored(monkeypatch):
    monkeypatch.setenv("HERMES_STDERR_LOG_LEVEL", "   ")
    assert _resolve_stderr_log_level(0, _FakeStream(True)) == logging.WARNING
