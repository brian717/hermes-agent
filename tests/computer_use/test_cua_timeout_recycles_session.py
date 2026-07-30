"""Regression test: a timed-out cua-driver MCP call must recycle the session.

``_CuaDriverSession.call_tool()`` recovered from two failure shapes — the
daemon-proxy EAGAIN congestion error (retry over the CLI transport) and a
closed stdio session (reconnect once) — but not from a bridge-level
timeout. ``_AsyncBridge.run()`` raises ``TimeoutError`` from
``fut.result(timeout=...)`` while the abandoned coroutine still owns the
request, so the session was left half-open and every later
``computer_use`` call in the run failed against it with an error unrelated
to the original wait (NousResearch/hermes-agent#74799).
"""

import concurrent.futures
import threading

import pytest

from tools.computer_use.cua_backend import _CuaDriverSession


def _make_session(exc: BaseException) -> _CuaDriverSession:
    """A started session whose first bridge call fails with ``exc``.

    Bypass __init__ (as the sibling call_tool tests do) so the test needs
    no real _AsyncBridge or driver process.
    """
    session = _CuaDriverSession.__new__(_CuaDriverSession)
    session._started = True
    session._session = object()
    session._lock = threading.Lock()
    session._declared_session_id = None
    session._require_started = lambda: None
    session._is_transient_daemon_error = lambda e: False
    session._is_closed_session_error = lambda e: False

    class _Bridge:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, coro, timeout=None):
            self.calls += 1
            coro.close()
            raise exc

    async def call(name, args):
        return {}

    session._bridge = _Bridge()
    session._call_tool_async = call
    return session


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("bridge wait elapsed"), concurrent.futures.TimeoutError()],
    ids=["builtin", "concurrent-futures"],
)
def test_timeout_recycles_the_session_and_raises_a_named_error(exc):
    session = _make_session(exc)
    restarts = []
    session._restart_session_locked = lambda: restarts.append(True)

    with pytest.raises(RuntimeError) as excinfo:
        session.call_tool("get_window_state", {"pid": 1}, timeout=5.0)

    assert restarts == [True], "a timed-out call must rebuild the session"
    # Named error, so the caller sees the original wait rather than a
    # downstream failure against the dead session.
    assert "timed out" in str(excinfo.value)
    assert "get_window_state" in str(excinfo.value)
    assert excinfo.value.__cause__ is exc
    # The call is not replayed: it may simply be slow, and a retry would
    # double the wait the caller already spent.
    assert session._bridge.calls == 1


def test_timeout_leaves_session_dead_when_the_restart_itself_fails():
    """A failed rebuild must not mask the timeout, and must leave the
    session marked dead so the next call_tool's not-started guard starts a
    fresh one instead of reusing the half-open session."""
    session = _make_session(TimeoutError("bridge wait elapsed"))

    def failing_restart():
        session._started = False
        raise RuntimeError("driver spawn failed")

    session._restart_session_locked = failing_restart

    with pytest.raises(RuntimeError) as excinfo:
        session.call_tool("click", {"pid": 1}, timeout=5.0)

    assert "timed out" in str(excinfo.value)
    assert session._started is False


def test_non_timeout_errors_are_still_surfaced_untouched():
    """The timeout branch must not swallow unrelated transport errors."""
    boom = ValueError("unrelated driver failure")
    session = _make_session(boom)
    restarts = []
    session._restart_session_locked = lambda: restarts.append(True)

    with pytest.raises(ValueError) as excinfo:
        session.call_tool("click", {"pid": 1}, timeout=5.0)

    assert excinfo.value is boom
    assert restarts == []
