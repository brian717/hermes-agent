"""`hermes secrets` registration must never take the whole CLI down (#73381).

Building the top-level parser runs on every ``hermes`` invocation, and it
registers the ``hermes secrets bitwarden`` tree from ``hermes_cli.secrets_cli``
-> ``agent.secret_sources.bitwarden`` -> ``cryptography`` (module scope, a
compiled extension).

On Windows a running hermes process holds ``cryptography``'s ``_rust.pyd`` open,
so ``uv pip install -e .`` during ``hermes update`` cannot replace it and leaves
the venv with a half-removed ``cryptography``. Every later ``hermes update``
then died with ``ModuleNotFoundError`` while building the parser — before the
update it was supposed to perform ever started, so the install could not
self-repair.
"""

import argparse
import importlib
import sys

import pytest

from hermes_cli.main import _register_secrets_subcommands

# Purged around the blocked-import tests so the block is actually observed
# rather than served from a warm sys.modules entry.
_PURGE = (
    "agent.secret_sources.bitwarden",
    "hermes_cli.secrets_cli",
    "hermes_cli.onepassword_secrets_cli",
)


class _BlockCryptography:
    """Meta-path finder that makes ``cryptography`` unimportable."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname == "cryptography" or fullname.startswith("cryptography."):
            raise ModuleNotFoundError(
                f"No module named '{fullname}'", name=fullname
            )
        return None


@pytest.fixture
def cryptography_missing():
    """Simulate the half-removed ``cryptography`` a failed update leaves behind."""
    def _is_purged(name):
        return name == "cryptography" or name.startswith("cryptography.") or name in _PURGE

    saved = {name: mod for name, mod in sys.modules.items() if _is_purged(name)}
    # Dropping a submodule from sys.modules is not enough: ``from pkg import sub``
    # is satisfied by the attribute the first import left on the parent package,
    # so a warm ``agent.secret_sources.bitwarden`` would sail straight past the
    # blocked import when this module runs after another secrets test.
    saved_attrs = {}
    for name in saved:
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, child):
            saved_attrs[name] = getattr(parent, child)
            delattr(parent, child)
    for name in saved:
        del sys.modules[name]

    finder = _BlockCryptography()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for name in [n for n in sys.modules if _is_purged(n)]:
            del sys.modules[name]
        sys.modules.update(saved)
        for name, module in saved_attrs.items():
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child, module)


def _secrets_parsers():
    """Stand-ins for the two parsers ``main()`` passes to the registrar."""
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="secrets_command")
    return parser, sub.add_parser("bitwarden"), sub.add_parser("onepassword")


def test_secrets_cli_import_is_the_failure_mode(cryptography_missing):
    """Precondition: the handler module really is unimportable without cryptography."""
    with pytest.raises(ModuleNotFoundError) as excinfo:
        importlib.import_module("hermes_cli.secrets_cli")
    assert "cryptography" in str(excinfo.value)


def test_registration_degrades_instead_of_raising(cryptography_missing):
    """The regression: this used to propagate and abort parser construction."""
    parser, secrets_bw, secrets_op = _secrets_parsers()

    error = _register_secrets_subcommands(secrets_bw, secrets_op)

    assert isinstance(error, ImportError)
    assert "cryptography" in str(error)
    # The rest of the CLI is untouched and still parses.
    assert parser.parse_args(["bitwarden"]).secrets_command == "bitwarden"


def test_degraded_secrets_parser_still_accepts_subcommand_arguments(cryptography_missing):
    """A degraded stub must not fail in argparse before the dispatcher explains why."""
    parser, secrets_bw, secrets_op = _secrets_parsers()

    assert _register_secrets_subcommands(secrets_bw, secrets_op) is not None

    assert parser.parse_args(["bitwarden", "status"]).secrets_command == "bitwarden"
    assert parser.parse_args(["onepassword", "sync"]).secrets_command == "onepassword"
    # No handler is wired up, so the dispatcher's error branch is the only path.
    assert getattr(parser.parse_args(["bitwarden", "status"]), "secrets_bw_command", None) is None


def test_registration_succeeds_when_dependencies_are_present():
    """The healthy path still wires up the real subcommand trees."""
    _parser, secrets_bw, secrets_op = _secrets_parsers()

    assert _register_secrets_subcommands(secrets_bw, secrets_op) is None

    args = secrets_bw.parse_args(["status"])
    assert args.secrets_bw_command == "status"
    assert secrets_op.parse_args(["status"]).secrets_op_command == "status"
