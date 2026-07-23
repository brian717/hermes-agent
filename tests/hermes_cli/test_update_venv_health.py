"""Tests for the Windows half-updated-venv hardening (July 2026 incident).

Covers three additions to ``hermes update``:

1. ``_venv_core_imports_healthy`` — the venv health probe that lets an
   "Already up to date" checkout still repair a broken dependency install.
2. ``_detect_venv_python_processes`` — the venv-interpreter process guard
   that refuses to mutate the venv while a desktop backend / stray python
   holds .pyd files mapped.
3. The commit_count == 0 repair branch wiring in ``_cmd_update_impl``.

All Windows-specific paths are exercised via ``_is_windows`` patching so
they run on any host (same approach as test_update_concurrent_quarantine).
"""

from __future__ import annotations

import subprocess
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import main as cli_main


# ---------------------------------------------------------------------------
# _venv_core_imports_healthy
# ---------------------------------------------------------------------------


def test_venv_health_reports_healthy_when_no_venv(tmp_path):
    """No venv python in a DEV checkout → nothing to probe → healthy."""
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path):
        healthy, detail = cli_main._venv_core_imports_healthy()
    assert healthy is True
    assert detail == ""


def test_venv_health_missing_venv_unhealthy_on_managed_install(tmp_path):
    """On a managed install (bootstrap marker) the venv IS the install —
    its absence must be reported unhealthy so the repair lane runs instead
    of 'Already up to date!'."""
    (tmp_path / ".hermes-bootstrap-complete").write_text("done")
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path):
        healthy, detail = cli_main._venv_core_imports_healthy()
    assert healthy is False
    assert "venv python missing" in detail


def test_venv_health_missing_venv_unhealthy_with_interrupted_marker(tmp_path):
    """An interrupted-update breadcrumb also flips missing-venv to unhealthy."""
    (tmp_path / ".update-incomplete").write_text("started=1\npid=1\n")
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path):
        healthy, detail = cli_main._venv_core_imports_healthy()
    assert healthy is False
    assert "venv python missing" in detail


def _fake_venv_python(tmp_path, *, windows: bool = False):
    bin_dir = tmp_path / "venv" / ("Scripts" if windows else "bin")
    bin_dir.mkdir(parents=True)
    py = bin_dir / ("python.exe" if windows else "python")
    py.write_bytes(b"")
    return py


def test_venv_health_reports_missing_imports(tmp_path):
    """Probe output lines are surfaced as the unhealthy detail."""
    _fake_venv_python(tmp_path)

    fake = SimpleNamespace(
        returncode=0,
        stdout="fastapi: No module named 'annotated_doc'\n",
        stderr="",
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.object(
        cli_main.subprocess, "run", return_value=fake
    ):
        healthy, detail = cli_main._venv_core_imports_healthy()

    assert healthy is False
    assert "annotated_doc" in detail


def test_venv_health_healthy_when_probe_clean(tmp_path):
    _fake_venv_python(tmp_path)
    fake = SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.object(
        cli_main.subprocess, "run", return_value=fake
    ):
        healthy, detail = cli_main._venv_core_imports_healthy()
    assert healthy is True


def test_venv_health_broken_interpreter_is_unhealthy(tmp_path):
    """Nonzero exit with no module list = interpreter itself is broken."""
    _fake_venv_python(tmp_path)
    fake = SimpleNamespace(returncode=1, stdout="", stderr="Fatal Python error: init failed\n")
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.object(
        cli_main.subprocess, "run", return_value=fake
    ):
        healthy, detail = cli_main._venv_core_imports_healthy()
    assert healthy is False
    assert "Fatal Python error" in detail


def test_venv_health_probe_failure_reports_healthy(tmp_path):
    """A probe that can't run must NOT force needless reinstalls."""
    _fake_venv_python(tmp_path)
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.object(
        cli_main.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="python", timeout=60),
    ):
        healthy, _detail = cli_main._venv_core_imports_healthy()
    assert healthy is True


# ---------------------------------------------------------------------------
# _detect_venv_python_processes
# ---------------------------------------------------------------------------


def _proc(pid: int, exe: str, name: str, cmdline: list[str] | None = None, cwd: str = ""):
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "exe": exe,
        "name": name,
        "cmdline": cmdline or [],
        "cwd": cwd,
    }
    return proc


def _fake_psutil(procs, parents=()):
    me = MagicMock()
    me.parents.return_value = list(parents)
    return types.SimpleNamespace(
        process_iter=lambda attrs: iter(list(procs)),
        Process=lambda *a, **k: me,
    )


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_python_finds_backend(_winp, tmp_path):
    venv_py = str(tmp_path / "venv" / "Scripts" / "python.exe")
    other_py = "C:\\Python311\\python.exe"

    me = MagicMock()
    me.parents.return_value = []
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: iter(
            [
                _proc(101, venv_py, "python.exe", ["python.exe", "-m", "hermes_cli.main", "serve"]),
                _proc(102, other_py, "python.exe", ["python.exe", "somescript.py"]),
            ]
        ),
        Process=lambda *a, **k: me,
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        matches = cli_main._detect_venv_python_processes()

    assert [m[0] for m in matches] == [101]
    assert "serve" in matches[0][2]


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_python_excludes_self_and_ancestors(_winp, tmp_path):
    import os as _os

    venv_py = str(tmp_path / "venv" / "Scripts" / "python.exe")
    parent = MagicMock()
    parent.pid = 555
    me = MagicMock()
    me.parents.return_value = [parent]
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: iter(
            [
                _proc(_os.getpid(), venv_py, "python.exe"),
                _proc(555, venv_py, "hermes.exe"),
            ]
        ),
        Process=lambda *a, **k: me,
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert cli_main._detect_venv_python_processes() == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_python_no_psutil_is_empty(_winp, tmp_path):
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": None}
    ):
        assert cli_main._detect_venv_python_processes() == []


def test_format_venv_holders_message_flags_desktop_backend(tmp_path):
    matches = [
        (101, "python.exe", "python.exe -m hermes_cli.main serve --host 127.0.0.1"),
        (102, "pythonw.exe", "pythonw.exe -m hermes_cli.main gateway run"),
    ]
    msg = cli_main._format_venv_python_holders_message(matches)
    assert "101" in msg
    assert "desktop app" in msg.lower()
    assert "gateway" in msg
    assert "hermes update" in msg
    assert "--force-venv" in msg


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_python_catches_outside_venv_trampoline(_winp, tmp_path):
    """uv/base-interpreter trampoline: exe OUTSIDE the venv, but the cmdline
    clearly runs Hermes from this install → must still be flagged as a holder
    (it imports from the venv and holds its .pyd files)."""
    base_py = "C:\\Python311\\python.exe"
    venv_path = str(tmp_path / "venv" / "Scripts" / "python.exe")

    me = MagicMock()
    me.parents.return_value = []
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: iter(
            [
                # cmdline references the venv path directly
                _proc(201, base_py, "python.exe", [base_py, venv_path, "-m", "x"]),
                # `-m hermes_cli.main serve` with the install root as cwd
                _proc(
                    202,
                    base_py,
                    "python.exe",
                    [base_py, "-m", "hermes_cli.main", "serve"],
                    cwd=str(tmp_path),
                ),
                # unrelated base-interpreter python → NOT a holder
                _proc(203, base_py, "python.exe", [base_py, "somescript.py"], cwd="C:\\other"),
            ]
        ),
        Process=lambda *a, **k: me,
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        matches = cli_main._detect_venv_python_processes()

    assert sorted(m[0] for m in matches) == [201, 202]


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_hermes_cli_cmdline_outside_install_not_matched(_winp, tmp_path):
    """A hermes_cli.main process belonging to a DIFFERENT install (neither
    install root in cmdline nor cwd under it) must not be flagged."""
    base_py = "C:\\Python311\\python.exe"
    me = MagicMock()
    me.parents.return_value = []
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: iter(
            [
                _proc(
                    301,
                    base_py,
                    "python.exe",
                    [base_py, "-m", "hermes_cli.main", "serve"],
                    cwd="C:\\other-install",
                ),
            ]
        ),
        Process=lambda *a, **k: me,
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert cli_main._detect_venv_python_processes() == []


# ---------------------------------------------------------------------------
# _detect_venv_python_processes on POSIX (#70201)
#
# POSIX lets the dependency sync rewrite packages under a live interpreter, so
# the survivor mixes its in-memory version with the new files on disk. The
# detector must therefore run off Windows too — but psutil resolves `exe`
# through venv/bin/python's symlink to the base interpreter, so argv[0] is the
# only reliable venv fingerprint, and the Windows anywhere-in-cmdline fallbacks
# would flag ordinary shell commands.
# ---------------------------------------------------------------------------


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_posix_matches_venv_argv0(_winp, tmp_path):
    """The venv launcher shows up in argv[0] even when psutil resolves `exe`
    to the base interpreter behind the symlink."""
    venv_py = str(tmp_path / "venv" / "bin" / "python")
    base_py = "/usr/lib/python3.11/bin/python3.11"

    fake_psutil = _fake_psutil(
        [
            _proc(101, base_py, "python3.11", [venv_py, "-m", "hermes_cli.main", "serve"]),
            _proc(102, base_py, "python3.11", [base_py, "somescript.py"], cwd="/home/dev"),
        ]
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        matches = cli_main._detect_venv_python_processes()

    assert [m[0] for m in matches] == [101]
    assert matches[0][1] == "python3.11"
    assert venv_py[:60] in matches[0][2]


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_posix_matches_exe_under_venv(_winp, tmp_path):
    """A copied (non-symlinked) venv interpreter still matches on `exe`."""
    venv_py = str(tmp_path / "venv" / "bin" / "python")
    fake_psutil = _fake_psutil([_proc(111, venv_py, "python", [venv_py, "-m", "http.server"])])
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert [m[0] for m in cli_main._detect_venv_python_processes()] == [111]


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_posix_ignores_shell_naming_the_venv(_winp, tmp_path):
    """A shell that merely mentions the venv path holds no imports."""
    venv_py = str(tmp_path / "venv" / "bin" / "python")
    fake_psutil = _fake_psutil(
        [
            _proc(121, "/bin/bash", "bash", ["/bin/bash", "-c", f"tail -f {venv_py}.log"]),
            _proc(122, "/usr/bin/grep", "grep", ["grep", "-r", "anyio", str(tmp_path / "venv")]),
        ]
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert cli_main._detect_venv_python_processes() == []


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_posix_ignores_shell_naming_hermes_cli_main(_winp, tmp_path):
    """The `hermes_cli.main` fallback is restricted to real interpreters, so a
    shell command containing that text inside the install dir is not a holder."""
    fake_psutil = _fake_psutil(
        [
            _proc(
                131,
                "/bin/sh",
                "sh",
                ["/bin/sh", "-c", "echo python -m hermes_cli.main serve"],
                cwd=str(tmp_path),
            ),
        ]
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert cli_main._detect_venv_python_processes() == []


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_posix_matches_module_trampoline(_winp, tmp_path):
    """A real interpreter running this install's module IS a holder."""
    base_py = "/usr/bin/python3"
    fake_psutil = _fake_psutil(
        [
            _proc(
                141,
                base_py,
                "python3",
                [base_py, "-m", "hermes_cli.main", "serve"],
                cwd=str(tmp_path),
            ),
        ]
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert [m[0] for m in cli_main._detect_venv_python_processes()] == [141]


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_posix_excludes_only_self(_winp, tmp_path):
    """No setuptools launcher off Windows: `hermes` IS this process, so an
    ancestor running from the venv is a genuine holder and must be reported."""
    import os as _os

    venv_py = str(tmp_path / "venv" / "bin" / "python")
    parent = MagicMock()
    parent.pid = 555
    fake_psutil = _fake_psutil(
        [
            _proc(_os.getpid(), venv_py, "python", [venv_py, "-m", "hermes_cli.main", "update"]),
            _proc(555, venv_py, "python", [venv_py, "-m", "hermes_cli.main", "serve"]),
        ],
        parents=[parent],
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert [m[0] for m in cli_main._detect_venv_python_processes()] == [555]


@patch.object(cli_main, "_is_windows", return_value=False)
def test_detect_venv_python_honors_exclude_pids(_winp, tmp_path):
    venv_py = str(tmp_path / "venv" / "bin" / "python")
    fake_psutil = _fake_psutil(
        [
            _proc(161, venv_py, "python", [venv_py, "-m", "hermes_cli.main", "gateway", "run"]),
            _proc(162, venv_py, "python", [venv_py, "-m", "hermes_cli.main", "serve"]),
        ]
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        matches = cli_main._detect_venv_python_processes(exclude_pids={161})

    assert [m[0] for m in matches] == [162]


def test_format_venv_holders_message_explains_posix_mixed_runtime():
    matches = [(101, "python3", "/opt/hermes/venv/bin/python -m hermes_cli.main serve")]
    msg = cli_main._format_venv_python_holders_message(matches, windows=False)
    assert ".pyd" not in msg
    assert "mixed runtime" in msg
    assert "--force-venv" in msg


# ---------------------------------------------------------------------------
# --force vs --force-venv gating of the venv-holder guard
# ---------------------------------------------------------------------------


def _update_args(**overrides):
    defaults = dict(
        gateway=False,
        check=False,
        no_backup=True,
        backup=False,
        yes=True,
        branch=None,
        force=False,
        force_venv=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _run_update_until_guard(args, *, windows=True, holders=None, gateway_pids=frozenset()):
    """Drive _cmd_update_impl just far enough to hit the venv-holder guard.

    Everything before the guard is stubbed; the guard firing is observed via
    SystemExit(2). The first statement AFTER the guard is
    ``git_dir = PROJECT_ROOT / ".git"`` — a PROJECT_ROOT sentinel whose
    ``__truediv__`` raises marks 'guard passed'."""

    class _PastGuard(Exception):
        pass

    class _RootSentinel:
        def __truediv__(self, _other):
            raise _PastGuard

    if holders is None:
        holders = [(101, "python.exe", "python.exe -m hermes_cli.main serve")]

    def _detect(*, exclude_pids=None):
        skip = set(exclude_pids or set())
        return [h for h in holders if h[0] not in skip]

    with patch.object(cli_main, "_is_windows", return_value=windows), patch.object(
        cli_main, "_venv_scripts_dir", return_value=None
    ), patch.object(cli_main, "_run_pre_update_backup"), patch.object(
        cli_main, "_pause_windows_gateways_for_update", return_value=None
    ), patch.object(
        cli_main, "_resume_windows_gateways_after_update"
    ), patch.object(
        cli_main, "_detect_venv_python_processes", side_effect=_detect
    ), patch.object(
        cli_main, "_venv_guard_excluded_gateway_pids", return_value=set(gateway_pids)
    ), patch.object(
        cli_main, "PROJECT_ROOT", _RootSentinel()
    ):
        try:
            cli_main._cmd_update_impl(args, gateway_mode=False)
        except _PastGuard:
            return "past_guard"
        except SystemExit as exc:
            return f"exit_{exc.code}"
    return "returned"


@pytest.mark.parametrize(
    "force,force_venv,expected",
    [
        (False, False, "exit_2"),   # guard fires
        (True, False, "exit_2"),    # plain --force does NOT bypass the venv guard
        (False, True, "past_guard"),  # --force-venv is the explicit escape hatch
        (True, True, "past_guard"),
    ],
)
def test_venv_holder_guard_force_semantics(force, force_venv, expected, capsys):
    result = _run_update_until_guard(_update_args(force=force, force_venv=force_venv))
    assert result == expected, capsys.readouterr().out


@pytest.mark.parametrize(
    "force,force_venv,expected",
    [
        (False, False, "exit_2"),
        (True, False, "exit_2"),
        (False, True, "past_guard"),
    ],
)
def test_venv_holder_guard_runs_on_posix(force, force_venv, expected, capsys):
    """#70201: the guard must stop the dependency sync off Windows too."""
    result = _run_update_until_guard(
        _update_args(force=force, force_venv=force_venv), windows=False
    )
    assert result == expected, capsys.readouterr().out


def test_venv_holder_guard_posix_exempts_managed_gateways(capsys):
    """A running gateway must not block the update off Windows: the updater
    restarts every gateway itself, and the gateway's own /update spawns this
    process while still alive."""
    gateway = (321, "python", "/opt/hermes/venv/bin/python -m hermes_cli.main gateway run")
    result = _run_update_until_guard(
        _update_args(), windows=False, holders=[gateway], gateway_pids={321}
    )
    assert result == "past_guard", capsys.readouterr().out


def test_venv_holder_guard_posix_still_faults_on_unmanaged_holder(capsys):
    """Exempting gateways must not exempt the desktop backend beside them."""
    holders = [
        (321, "python", "/opt/hermes/venv/bin/python -m hermes_cli.main gateway run"),
        (322, "python", "/opt/hermes/venv/bin/python -m hermes_cli.main serve"),
    ]
    result = _run_update_until_guard(
        _update_args(), windows=False, holders=holders, gateway_pids={321}
    )
    assert result == "exit_2", capsys.readouterr().out
