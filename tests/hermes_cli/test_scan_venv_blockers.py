"""Tests for hermes_cli/_scan_venv_blockers.py.

Tests call the real production functions (``main``, ``_redact_sensitive_cmdline``).
The detector is patched directly so no real process table interaction occurs.
"""

from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.redact as redact_module
from hermes_cli._scan_venv_blockers import (
    _is_pausable_gateway,
    _redact_sensitive_cmdline,
    main,
)


# ---------------------------------------------------------------------------
# main() — stdout, stderr, exit code (with patched detector)
# ---------------------------------------------------------------------------


def _psutil_fake() -> dict:
    """Return a sys.modules dict entry that makes psutil appear available."""
    return {"psutil": types.SimpleNamespace(Process=lambda *a: MagicMock())}






# ---------------------------------------------------------------------------
# _redact_sensitive_cmdline
# ---------------------------------------------------------------------------


def test_redact_long_flag_value_space_separated() -> None:
    """--token SECRET must preserve --token and emit --token <redacted>."""
    raw = "python.exe -m hermes_cli.main serve --token ghp_abc123 --host 10.0.0.1"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m hermes_cli.main serve --token <redacted>"
    assert "ghp_abc123" not in result




def test_redact_sensitive_text_failure_returns_fully_redacted() -> None:
    """When agent.redact.redact_sensitive_text raises, the entire result
    must equal '<redacted>' so PID and name still provide diagnostics."""
    with patch.object(
        redact_module,
        "redact_sensitive_text",
        side_effect=RuntimeError("no redactor"),
    ):
        result = _redact_sensitive_cmdline("python.exe --token abc123")

    assert result == "<redacted>"


def test_redact_session_key() -> None:
    """--session-key <identifier> must redact the value and everything after."""
    raw = "python.exe -m tui_gateway.slash_worker --session-key 20260712-abcdef --model test"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m tui_gateway.slash_worker --session-key <redacted>"


def test_redact_normal_host_port_profile_remain() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 10.0.0.1 --port 9119 --profile work"
    result = _redact_sensitive_cmdline(raw)
    assert "10.0.0.1" in result
    assert "9119" in result
    assert "work" in result


def test_redact_no_sensitive_flags_is_noop() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 127.0.0.1"
    assert _redact_sensitive_cmdline(raw) == raw


def test_redact_empty_string() -> None:
    assert _redact_sensitive_cmdline("") == ""


def test_redact_short_flags_not_redacted() -> None:
    """Short flags -t (toolset), -p (profile), -k are NOT redacted."""
    raw = "python.exe -m hermes_cli.main serve -t web -p default -k somearg"
    result = _redact_sensitive_cmdline(raw)
    assert result == raw  # short flags pass through unchanged


# ---------------------------------------------------------------------------
# _is_pausable_gateway — the gateway exemption
#
# `hermes-setup` always invokes `hermes update --yes --gateway`, whose
# `_pause_windows_gateways_for_update()` stops running gateways itself. The
# Desktop preflight must therefore not report gateway launcher/worker chains
# as blockers — doing so aborts the handoff before the component that can
# handle them ever runs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline",
    [
        # venv-side launcher, exactly as the scheduled task spawns it
        r"C:\Users\u\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
        " -m hermes_cli.main gateway run --replace",
        # uv-side worker re-running the same argv (quoted exe, double space)
        r'"C:\Users\u\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"'
        "  -m hermes_cli.main gateway run --replace",
        # profile-scoped gateway
        "python.exe -m hermes_cli.main --profile work gateway run",
        # a profile literally NAMED "gateway" — the profile value must not
        # shadow the subcommand token (the hand-rolled matcher regressed this)
        "python.exe -m hermes_cli.main --profile gateway gateway run",
        "python.exe -m hermes_cli.main -p gateway gateway run",
        # bare `gateway` defaults to `run` (mirrors the canonical matcher)
        "python.exe -m hermes_cli.main gateway",
        # case variations survive
        "PYTHON.EXE -m hermes_cli.main GATEWAY RUN",
    ],
)
def test_is_pausable_gateway_accepts_gateway_run_chains(cmdline: str) -> None:
    assert _is_pausable_gateway(cmdline) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        # desktop backend: no pause machinery downstream, must keep blocking
        "python.exe -m hermes_cli.main serve --host 127.0.0.1 --port 8756",
        # other gateway subcommands are not running gateways
        "python.exe -m hermes_cli.main gateway stop",
        "python.exe -m hermes_cli.main gateway status",
        "python.exe -m hermes_cli.main gateway install",
        # operator REPL / stray script
        "python.exe",
        "python.exe myscript.py gateway run",  # not a hermes_cli.main invocation
        "",
    ],
)
def test_is_pausable_gateway_rejects_everything_else(cmdline: str) -> None:
    assert _is_pausable_gateway(cmdline) is False


def _run_main_with_detector(monkeypatch, capsys, matches):
    """Run main() with the process detector patched to return *matches*."""
    for name, mod in _psutil_fake().items():
        monkeypatch.setitem(sys.modules, name, mod)
    import hermes_cli.main as cli_main

    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda: matches)
    with pytest.raises(SystemExit) as excinfo:
        main()
    out = capsys.readouterr().out
    return excinfo.value.code, json.loads(out)


def test_main_exempts_gateway_chain_but_keeps_other_holders(monkeypatch, capsys):
    """A gateway launcher/worker pair alone must scan clear; a non-gateway
    holder alongside it must still block (and be the only reported PID)."""
    gateway_launcher = (
        12,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main gateway run --replace",
    )
    gateway_worker = (
        34,
        "python.exe",
        r'"C:\u\uv\python\python.exe"  -m hermes_cli.main gateway run --replace',
    )
    stray_repl = (56, "python.exe", r"C:\x\venv\Scripts\python.exe")

    # Gateway chain only → clear
    code, data = _run_main_with_detector(
        monkeypatch, capsys, [gateway_launcher, gateway_worker]
    )
    assert code == 0
    assert data["ok"] is True
    assert data["blocked"] is False
    assert data["processes"] == []
    assert data["pausable_gateways"] == 2

    # Gateway chain + stray REPL → blocked, reporting only the REPL
    code, data = _run_main_with_detector(
        monkeypatch, capsys, [gateway_launcher, gateway_worker, stray_repl]
    )
    assert code == 0
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [56]
    assert data["pausable_gateways"] == 2


def test_main_desktop_serve_backend_still_blocks(monkeypatch, capsys):
    """The desktop's own `serve` backend has no downstream pause — it must
    keep blocking exactly as before the exemption."""
    serve = (
        78,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main serve --host 127.0.0.1",
    )
    code, data = _run_main_with_detector(monkeypatch, capsys, [serve])
    assert code == 0
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [78]
    assert data["pausable_gateways"] == 0


# ---------------------------------------------------------------------------
# Hermes-owned vs foreign classification (issue #77422)
#
# The detector matches on paths, so a hand-started `python.exe -m http.server`
# run from the install's venv is reported as a blocker. It must not be
# described as "another Hermes process": the user has no Hermes window to
# close, so the Desktop dialog dead-ends.
# ---------------------------------------------------------------------------

_INSTALL_ROOT = r"C:\Users\u\AppData\Local\hermes\hermes-agent"


@pytest.mark.parametrize(
    "cmdline",
    [
        rf"{_INSTALL_ROOT}\venv\Scripts\python.exe -m hermes_cli.main serve --port 8756",
        rf"{_INSTALL_ROOT}\venv\Scripts\python.exe -m hermes_cli.main gateway run",
        rf"{_INSTALL_ROOT}\venv\Scripts\hermes.exe update --yes",
        rf'"{_INSTALL_ROOT}\venv\Scripts\hermesw.exe" serve',
        r"C:\Users\u\.hermes\hermes-setup.exe update --yes --gateway",
        # case-insensitive: Windows hands back mixed-case paths
        rf"{_INSTALL_ROOT.upper()}\VENV\SCRIPTS\PYTHON.EXE -m HERMES_CLI.main serve",
    ],
)
def test_is_hermes_owned_holder_accepts_hermes_invocations(cmdline: str) -> None:
    from hermes_cli.update_cmd import _is_hermes_owned_holder

    assert _is_hermes_owned_holder(cmdline, _INSTALL_ROOT) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        # the reported case: a preview server started by hand, running the
        # install's interpreter. "hermes" appears only in the install path.
        rf"{_INSTALL_ROOT}\venv\Scripts\python.exe -m http.server 8077",
        # operator REPL from the same venv
        rf"{_INSTALL_ROOT}\venv\Scripts\python.exe",
        # unrelated script that imports from the venv
        rf"{_INSTALL_ROOT}\venv\Scripts\python.exe C:\work\scrape.py --out data.json",
        "",
    ],
)
def test_is_hermes_owned_holder_rejects_foreign_processes(cmdline: str) -> None:
    from hermes_cli.update_cmd import _is_hermes_owned_holder

    assert _is_hermes_owned_holder(cmdline, _INSTALL_ROOT) is False


def test_main_marks_foreign_holder_not_hermes_owned(monkeypatch, capsys):
    """A stray `-m http.server` blocker must be reported with
    hermes_owned=False so the Desktop dialog can name it instead of telling
    the user to close Hermes windows that do not exist."""
    http_server = (
        21544,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m http.server 8077",
    )
    serve = (
        78,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main serve --host 127.0.0.1",
    )
    code, data = _run_main_with_detector(monkeypatch, capsys, [http_server, serve])
    assert code == 0
    assert data["blocked"] is True
    owned = {p["pid"]: p["hermes_owned"] for p in data["processes"]}
    assert owned == {21544: False, 78: True}


def test_main_classifies_on_raw_cmdline_before_redaction(monkeypatch, capsys):
    """Redaction truncates everything after a sensitive flag; classification
    must run on the raw cmdline or a redacted Hermes process looks foreign."""
    serve = (
        90,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe --token ghp_abc123 -m hermes_cli.main serve",
    )
    code, data = _run_main_with_detector(monkeypatch, capsys, [serve])
    assert code == 0
    proc = data["processes"][0]
    assert "hermes_cli" not in proc["cmdline"]  # redaction cut it off
    assert "ghp_abc123" not in proc["cmdline"]
    assert proc["hermes_owned"] is True