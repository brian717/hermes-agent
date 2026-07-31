"""Tests for the cost profile of ``_detect_venv_python_processes`` (#75460).

On Windows ``cmdline`` and ``cwd`` are each a separate per-process query, so
prefetching them through ``psutil.process_iter`` costs roughly 25ms *per
process on the machine*. A 500+ entry process table therefore ran the scan
past the Desktop update preflight's timeout, which surfaced as a generic
"could not verify the Hermes installation is free" abort on every attempt.

These tests pin the fix: only the cheap attributes are prefetched, and the
expensive ones are queried lazily for the handful of processes that could
plausibly be importing from the venv. The holder-matching contract itself
(venv exe, trampoline cmdline, ``hermes_cli.main`` + install cwd) must be
unchanged, so it is re-asserted here against a lazy psutil fake.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import main as cli_main


class _LazyProc:
    """psutil.Process stand-in that only prefetches what it was asked for.

    ``cmdline()``/``cwd()`` record every call so a test can assert they were
    never reached for processes that cannot hold the venv's ``.pyd`` files.
    """

    def __init__(self, pid, exe, name, cmdline=None, cwd="", *, calls, raises=False):
        self.info = {"pid": pid, "exe": exe, "name": name}
        self._cmdline = list(cmdline or [])
        self._cwd = cwd
        self._calls = calls
        self._raises = raises

    def cmdline(self):
        self._calls.append(("cmdline", self.info["pid"]))
        if self._raises:
            raise RuntimeError("process vanished")
        return self._cmdline

    def cwd(self):
        self._calls.append(("cwd", self.info["pid"]))
        if self._raises:
            raise RuntimeError("process vanished")
        return self._cwd


def _fake_psutil(procs, requested_attrs):
    """psutil stand-in recording the attrs ``process_iter`` was asked to prefetch."""

    def process_iter(attrs):
        requested_attrs.append(list(attrs))
        return iter(procs)

    me = MagicMock()
    me.parents.return_value = []
    return types.SimpleNamespace(process_iter=process_iter, Process=lambda *a, **k: me)


def _run(tmp_path, procs, requested_attrs):
    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.dict(sys.modules, {"psutil": _fake_psutil(procs, requested_attrs)}):
        return cli_main._detect_venv_python_processes()


def _venv_python(tmp_path) -> str:
    return str(tmp_path / "venv" / "Scripts" / "python.exe")


def test_expensive_attributes_are_not_prefetched(tmp_path):
    """process_iter must ask for the cheap attributes only."""
    calls: list = []
    requested: list = []

    _run(tmp_path, [], requested)

    assert requested == [["pid", "exe", "name"]]
    assert calls == []


def test_unrelated_processes_are_never_queried(tmp_path):
    """A browser/service process cannot import from the venv — skip it cheaply."""
    calls: list = []
    requested: list = []
    procs = [
        _LazyProc(101, r"C:\Program Files\Chrome\chrome.exe", "chrome.exe", calls=calls),
        _LazyProc(102, r"C:\Windows\System32\svchost.exe", "svchost.exe", calls=calls),
        _LazyProc(103, r"C:\Apps\Code\Code.exe", "Code.exe", calls=calls),
    ]

    assert _run(tmp_path, procs, requested) == []
    assert calls == [], "expensive per-process queries must not run for non-candidates"


def test_venv_exe_holder_is_found_and_reports_its_cmdline(tmp_path):
    """The desktop-backend case still matches, with cmdline fetched lazily."""
    calls: list = []
    requested: list = []
    procs = [
        _LazyProc(200, r"C:\Windows\System32\svchost.exe", "svchost.exe", calls=calls),
        _LazyProc(
            201,
            _venv_python(tmp_path),
            "python.exe",
            cmdline=["python", "-m", "hermes_cli.main", "serve"],
            calls=calls,
        ),
    ]

    matches = _run(tmp_path, procs, requested)

    assert [m[0] for m in matches] == [201]
    assert matches[0][1] == "python.exe"
    assert "hermes_cli.main" in matches[0][2]
    assert ("cmdline", 200) not in calls


def test_uv_trampoline_outside_the_venv_still_matches(tmp_path):
    """Fallback match: exe is outside the venv but the cmdline references it."""
    calls: list = []
    requested: list = []
    procs = [
        _LazyProc(
            300,
            r"C:\Users\dev\.local\bin\uv.exe",
            "uv.exe",
            cmdline=["uv", "run", "--python", _venv_python(tmp_path), "script.py"],
            calls=calls,
        )
    ]

    assert [m[0] for m in _run(tmp_path, procs, requested)] == [300]


def test_base_interpreter_running_hermes_from_the_install_matches(tmp_path):
    """Fallback match: `-m hermes_cli.main` with the install root as cwd."""
    calls: list = []
    requested: list = []
    procs = [
        _LazyProc(
            400,
            r"C:\Python313\python.exe",
            "python.exe",
            cmdline=["python", "-m", "hermes_cli.main", "serve"],
            cwd=str(tmp_path / "sub"),
            calls=calls,
        )
    ]

    assert [m[0] for m in _run(tmp_path, procs, requested)] == [400]


def test_lazy_lookup_failure_does_not_abort_the_scan(tmp_path):
    """A process that dies mid-scan must not lose the rest of the results."""
    calls: list = []
    requested: list = []
    procs = [
        _LazyProc(500, r"C:\Python313\python.exe", "python.exe", calls=calls, raises=True),
        _LazyProc(
            501,
            _venv_python(tmp_path),
            "python.exe",
            cmdline=["python", "-m", "hermes_cli.main", "serve"],
            calls=calls,
        ),
    ]

    assert [m[0] for m in _run(tmp_path, procs, requested)] == [501]


@pytest.mark.parametrize(
    "exe,name,expected",
    [
        (r"c:\python313\python.exe", "python.exe", True),
        (r"c:\python313\python3.13.exe", "python3.13.exe", True),
        (r"c:\python313\pythonw.exe", "pythonw.exe", True),
        (r"c:\users\dev\.local\bin\uv.exe", "uv.exe", True),
        (r"c:\windows\py.exe", "py.exe", True),
        (r"c:\apps\hermes.exe", "hermes.exe", True),
        (r"c:\windows\system32\svchost.exe", "svchost.exe", False),
        (r"c:\program files\chrome\chrome.exe", "chrome.exe", False),
    ],
)
def test_candidate_prefilter_admits_interpreters_and_trampolines(exe, name, expected):
    from hermes_cli import update_cmd

    venv_prefix = f"c:{os.sep}install{os.sep}venv{os.sep}"
    root_prefix = f"c:{os.sep}install{os.sep}"

    assert (
        update_cmd._may_hold_venv_extensions(exe, name, venv_prefix, root_prefix) is expected
    )


def test_candidate_prefilter_admits_anything_under_the_install_tree():
    from hermes_cli import update_cmd

    venv_prefix = f"c:{os.sep}install{os.sep}venv{os.sep}"
    root_prefix = f"c:{os.sep}install{os.sep}"

    assert update_cmd._may_hold_venv_extensions(
        f"c:{os.sep}install{os.sep}venv{os.sep}scripts{os.sep}anything.exe",
        "anything.exe",
        venv_prefix,
        root_prefix,
    )
