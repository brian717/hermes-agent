"""Regression: the Windows installer must reject a phantom managed interpreter.

A user on Windows 10 reported (#73333) that Hermes Desktop's bootstrap looped on
"Update didn't finish" because the venv stage kept driving a uv-managed Python
that does not exist::

    Failed to inspect Python interpreter from managed installations
    Python interpreter not found
    -> Creating virtual environment with Python 3.11...
    Failed to create virtual environment (uv venv exited with 2)

Root cause: an interrupted managed download leaves
``%APPDATA%\\uv\\python\\cpython-3.11.15-windows-x86_64-none\\`` populated
(``python311.dll``, ``pythonw.exe``, ``Lib\\``, ``Scripts\\``) but *without*
``python.exe``.  ``uv python find 3.11`` answers from that layout and prints the
phantom path, so the installer accepted 3.11 as available and never fell back to
the Python 3.12 the same machine had — ``uv venv venv --python 3.11`` then died
with exit 2.

The fix gates every ``uv python find`` result on ``Test-UvPythonUsable``, which
requires the reported interpreter to exist and run, so a broken managed install
is skipped and a healthy fallback wins.  These tests lock that contract at the
source level (the script only runs on Windows, so there's no runner to execute it
on Linux CI) plus a Windows-only behavioral check of the resolver itself.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_INSTALL_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"

_ON_WINDOWS = sys.platform == "win32"


@pytest.fixture(scope="module")
def source() -> str:
    return _INSTALL_PS1.read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    """Return the full text of a PowerShell ``function <name> { ... }`` block."""
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unterminated function body for {name}")


# ── source-level contract ────────────────────────────────────────────────────

def test_usability_helper_is_defined(source: str):
    """A helper that validates `uv python find` output must exist."""
    assert "function Test-UvPythonUsable" in source, (
        "expected a Test-UvPythonUsable helper that rejects an interpreter path "
        "uv reported but that cannot actually run"
    )


def test_usability_helper_checks_existence_and_execution(source: str):
    """Existence alone isn't enough — the interpreter must also run."""
    helper = _function_source(source, "Test-UvPythonUsable")
    assert "Test-Path" in helper, (
        "the helper must check that the reported interpreter exists on disk"
    )
    assert "--version" in helper and "LASTEXITCODE" in helper, (
        "the helper must run the interpreter and honor its exit code, so a "
        "present-but-broken python.exe is rejected too"
    )


def test_resolver_gates_candidates_on_usability(source: str):
    """Resolve-AvailablePythonVersion must not accept a phantom interpreter.

    Returning a version whose interpreter is missing is what sends the venv
    stage into `uv venv --python 3.11` against nothing.
    """
    resolver = _function_source(source, "Resolve-AvailablePythonVersion")
    assert "Test-UvPythonUsable" in resolver, (
        "the resolver must gate each candidate on Test-UvPythonUsable instead "
        "of trusting a non-empty `uv python find` result"
    )


def test_test_python_gates_every_find_on_usability(source: str):
    """Test-Python's three `uv python find` sites must all be gated.

    The initial probe, the post-install verification, and the fallback loop each
    accepted a bare non-empty path; any one of them left ungated re-opens the
    "reported success, then venv exit 2" path.
    """
    body = _function_source(source, "Test-Python")
    finds = body.count("& $UvCmd python find")
    gates = body.count("Test-UvPythonUsable")
    assert finds >= 3, "expected Test-Python to probe uv at least three times"
    assert gates == finds, (
        f"every `uv python find` in Test-Python must be gated on "
        f"Test-UvPythonUsable ({finds} probes, {gates} gated)"
    )


# ── behavioral check (Windows only: install.ps1 needs a real PowerShell) ─────

def _write_uv_stub(tmp_path: Path, answers: dict) -> Path:
    """Write a `uv` stub whose `python find <ver>` prints answers[ver]."""
    lines = ["@echo off"]
    for version, path in answers.items():
        lines.append(f'if "%3"=="{version}" (')
        lines.append(f"echo {path}")
        lines.append("exit /b 0")
        lines.append(")")
    lines.append("exit /b 1")
    stub = tmp_path / "uv.cmd"
    stub.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return stub


def _run_resolver(tmp_path: Path, source: str, answers: dict) -> str:
    """Run Resolve-AvailablePythonVersion against a stubbed uv, return its result."""
    stub = _write_uv_stub(tmp_path, answers)
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        textwrap.dedent(
            f"""
            $PythonVersion = "3.11"
            $PythonFallbackVersions = @("3.12", "3.13", "3.10")
            $UvCmd = "{stub}"
            """
        )
        + _function_source(source, "Test-UvPythonUsable")
        + "\n"
        + _function_source(source, "Resolve-AvailablePythonVersion")
        + "\nWrite-Output ([string](Resolve-AvailablePythonVersion))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(harness),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stdout}\n{proc.stderr}"
    return (proc.stdout or "").strip()


@pytest.mark.skipif(not _ON_WINDOWS, reason="install.ps1 only runs on Windows")
def test_resolver_skips_phantom_managed_python(tmp_path: Path, source: str):
    """The reported shape: 3.11 resolves to a missing python.exe, 3.12 is real."""
    phantom = tmp_path / "uv" / "python" / "cpython-3.11.15-windows-x86_64-none" / "python.exe"
    phantom.parent.mkdir(parents=True)
    (phantom.parent / "python311.dll").write_bytes(b"")  # partial download, no python.exe

    resolved = _run_resolver(
        tmp_path, source, {"3.11": str(phantom), "3.12": sys.executable}
    )
    assert resolved == "3.12", (
        "the resolver must skip the broken managed 3.11 and settle on the "
        "healthy fallback instead of handing 3.11 to `uv venv`"
    )


@pytest.mark.skipif(not _ON_WINDOWS, reason="install.ps1 only runs on Windows")
def test_resolver_returns_nothing_when_only_phantom_exists(tmp_path: Path, source: str):
    """No usable interpreter must read as unresolved, not as a false success."""
    phantom = tmp_path / "ghost" / "python.exe"
    phantom.parent.mkdir(parents=True)

    assert _run_resolver(tmp_path, source, {"3.11": str(phantom)}) == ""


@pytest.mark.skipif(not _ON_WINDOWS, reason="install.ps1 only runs on Windows")
def test_resolver_still_prefers_a_healthy_requested_version(tmp_path: Path, source: str):
    """The guard must not reject a working interpreter."""
    resolved = _run_resolver(
        tmp_path, source, {"3.11": sys.executable, "3.12": sys.executable}
    )
    assert resolved == "3.11"
