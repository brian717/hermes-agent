"""Tests for the DuckDuckGo (ddgs) web search provider.

Covers:
- DDGSWebSearchProvider.is_available() — reflects package importability
- DDGSWebSearchProvider.search() — happy path, missing package, runtime error
- Result normalization (title, url, description, position)
- _is_backend_available("ddgs") / _get_backend() integration
- web_extract returns a search-only error when ddgs is active
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from tests.tools.conftest import register_all_web_providers


def _install_fake_ddgs(monkeypatch, *, text_results=None, text_raises=None, text_sleep=None):
    """Install a stub ``ddgs`` module in sys.modules for the duration of a test.

    ``text_results``: iterable of dicts to yield from DDGS().text(...).
    ``text_raises``: if set, DDGS().text raises this exception instead.
    ``text_sleep``: if set, DDGS().text blocks for this many seconds before
        yielding — simulates a hung/slow search for the timeout test.
    """
    import time as _time

    fake = types.ModuleType("ddgs")

    class _FakeDDGS:
        def __init__(self, **kwargs):
            # Accept timeout= (and any other constructor kwargs) — the provider
            # now passes DDGS(timeout=10).
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def text(self, query, max_results=5):
            if text_sleep is not None:
                _time.sleep(text_sleep)
            if text_raises is not None:
                raise text_raises
            for hit in (text_results or []):
                yield hit

    fake.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    # The provider now lazy-installs ddgs on first use (#60425). Under test the
    # injected fake module has no distribution metadata, so the real ensure()
    # would attempt an actual pip install; neutralize it — presence of the fake
    # module is what these tests exercise.
    _neutralize_ddgs_lazy_install(monkeypatch)
    return fake


def _neutralize_ddgs_lazy_install(monkeypatch):
    """Stub lazy-install so tests never shell out to pip.

    The provider imports ``tools.lazy_deps.ensure`` lazily *inside*
    ``_ensure_ddgs_installed`` at call time, so patching that boundary is
    robust even when a test re-imports the provider module fresh.
    """
    import tools.lazy_deps as _ld

    monkeypatch.setattr(_ld, "ensure", lambda *a, **k: None, raising=True)


# ---------------------------------------------------------------------------
# DDGSWebSearchProvider unit tests
# ---------------------------------------------------------------------------


class TestDDGSProviderIsConfigured:
    def test_configured_when_package_importable(self, monkeypatch):
        _install_fake_ddgs(monkeypatch)
        # Drop any cached ``plugins.web.ddgs.provider`` so is_configured re-imports ddgs fresh
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert DDGSWebSearchProvider().is_available() is True

    def test_not_configured_when_package_missing(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "ddgs", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        # Block the import so ``import ddgs`` raises ImportError even if the package is actually installed
        import builtins
        orig_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "ddgs":
                raise ImportError("blocked for test")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert DDGSWebSearchProvider().is_available() is False

    def test_provider_name(self):
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert DDGSWebSearchProvider().name == "ddgs"

    def test_implements_web_search_provider(self):
        from agent.web_search_provider import WebSearchProvider
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert issubclass(DDGSWebSearchProvider, WebSearchProvider)


class TestDDGSProviderSearch:
    def test_happy_path_normalizes_results(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": "A", "href": "https://a.example.com", "body": "desc A"},
            {"title": "B", "href": "https://b.example.com", "body": "desc B"},
            {"title": "C", "href": "https://c.example.com", "body": "desc C"},
        ])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 3
        assert web[0] == {"title": "A", "url": "https://a.example.com", "description": "desc A", "position": 1}
        assert web[2]["position"] == 3

    def test_accepts_url_key_as_fallback_for_href(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": "A", "url": "https://a.example.com", "body": "desc A"},
        ])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        assert result["data"]["web"][0]["url"] == "https://a.example.com"

    def test_limit_is_respected(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": f"R{i}", "href": f"https://r{i}.example.com", "body": ""}
            for i in range(10)
        ])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=3)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 3

    def test_missing_package_returns_failure(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "ddgs", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        import builtins
        orig_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "ddgs":
                raise ImportError("blocked for test")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        # search() now lazy-installs on first use; keep the test hermetic.
        _neutralize_ddgs_lazy_install(monkeypatch)
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)
        assert result["success"] is False
        assert "ddgs" in result["error"].lower()

    def test_runtime_error_returns_failure(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_raises=RuntimeError("rate limited 202"))
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)
        assert result["success"] is False
        assert "rate limited" in result["error"] or "failed" in result["error"].lower()

    def test_empty_results(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("nothing", limit=5)
        assert result["success"] is True
        assert result["data"]["web"] == []

    def test_hung_search_times_out_and_returns_failure(self, monkeypatch):
        """#36776: a ddgs call that never returns must be bounded by the
        wall-clock timeout and surface a failure instead of hanging the
        shared agent loop. We patch the blocking helper to wait on an Event
        (released in finally so no worker thread leaks past the test) and
        shrink the timeout; search() must return success=False promptly."""
        import threading
        import time

        # ddgs must import-probe True for search() to proceed.
        _install_fake_ddgs(monkeypatch)
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        import plugins.web.ddgs.provider as _prov

        release = threading.Event()

        def _blocking_search(query, safe_limit):
            release.wait(timeout=10)  # bounded so the worker can never truly leak
            return []

        monkeypatch.setattr(_prov, "_run_ddgs_search", _blocking_search, raising=True)
        monkeypatch.setattr(_prov, "_SEARCH_TIMEOUT_SECS", 0.3, raising=True)

        try:
            start = time.monotonic()
            result = _prov.DDGSWebSearchProvider().search("hangs forever", limit=5)
            elapsed = time.monotonic() - start

            assert result["success"] is False
            assert "timed out" in result["error"].lower()
            # Returned well before the worker's 10s wait — proves the cap fired.
            assert elapsed < 3.0, f"search did not return promptly ({elapsed:.1f}s)"
        finally:
            release.set()  # let the orphaned worker finish immediately

    def test_fast_search_not_affected_by_timeout_wrapper(self, monkeypatch):
        """Happy-path guard: the timeout wrapper must not break a normal,
        fast search — results flow through unchanged."""
        _install_fake_ddgs(
            monkeypatch,
            text_results=[{"title": "T", "href": "https://e.com", "body": "B"}],
        )
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)
        assert result["success"] is True
        assert result["data"]["web"][0]["url"] == "https://e.com"
        assert result["data"]["web"][0]["title"] == "T"


# ---------------------------------------------------------------------------
# Integration: _is_backend_available / _get_backend / check_web_api_key
# ---------------------------------------------------------------------------


class TestDDGSBackendWiring:
    def test_is_backend_available_true_when_package_importable(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._is_backend_available("ddgs") is True

    def test_is_backend_available_false_when_package_missing(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        assert web_tools._is_backend_available("ddgs") is False

    def test_configured_backend_accepted(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._get_backend() == "ddgs"

    def test_ddgs_trails_paid_providers_in_auto_detect(self, monkeypatch):
        """Exa (priority) should win over ddgs in auto-detect."""
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "PARALLEL_API_KEY",
                    "TAVILY_API_KEY", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("EXA_API_KEY", "exa-key")
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._get_backend() == "exa"

    def test_auto_detect_picks_ddgs_as_last_resort(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "PARALLEL_API_KEY",
                    "TAVILY_API_KEY", "EXA_API_KEY", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._get_backend() == "ddgs"

    def test_check_web_api_key_true_when_ddgs_configured(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools.check_web_api_key() is True


# ---------------------------------------------------------------------------
# ddgs is search-only: web_extract returns a clear error
# ---------------------------------------------------------------------------


class TestDDGSSearchOnlyErrors:
    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_web_extract_returns_search_only_error(self, monkeypatch):
        import asyncio
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        async def _allow_ssrf(_url: str) -> bool:
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        result_str = asyncio.get_event_loop().run_until_complete(
            web_tools.web_extract_tool(["https://example.com"])
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "search-only" in result["error"].lower()
        assert "duckduckgo" in result["error"].lower() or "ddgs" in result["error"].lower()


# ---------------------------------------------------------------------------
# Lazy-install wiring (#60425): ddgs self-installs on first use like the other
# web backends, and the tool lights up on a fresh/sealed image where ddgs is
# the configured (keyless) default but not yet installed.
# ---------------------------------------------------------------------------


class TestDDGSLazyInstall:
    def test_ddgs_registered_in_lazy_deps(self):
        """ddgs must be an allowlisted lazy feature so ensure() can install it
        (and the durable-target redirect applies on sealed images)."""
        from tools.lazy_deps import LAZY_DEPS

        assert LAZY_DEPS.get("search.ddgs") == ("ddgs==9.14.4",)

    def test_lazy_deps_pin_matches_pyproject_extra(self):
        """The LAZY_DEPS pin and the pyproject `ddgs` extra must agree so
        `hermes update` can't downgrade the package below the lazy pin."""
        import tomllib
        from pathlib import Path

        from tools.lazy_deps import LAZY_DEPS

        pyproject = tomllib.loads(
            (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        )
        extra = pyproject["project"]["optional-dependencies"]["ddgs"]
        assert extra == list(LAZY_DEPS["search.ddgs"])

    def test_search_triggers_lazy_install_before_import(self, monkeypatch):
        """search() must call ensure('search.ddgs') so a missing package
        self-installs on first use (mirrors exa/firecrawl/parallel)."""
        calls = []

        import tools.lazy_deps as _ld

        def _fake_ensure(feature, **kwargs):
            calls.append((feature, kwargs))

        monkeypatch.setattr(_ld, "ensure", _fake_ensure, raising=True)
        # A working fake package so search() succeeds past the install step.
        fake = types.ModuleType("ddgs")

        class _FakeDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def text(self, query, max_results=5):
                return iter(())

        fake.DDGS = _FakeDDGS
        monkeypatch.setitem(sys.modules, "ddgs", fake)

        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        assert ("search.ddgs", {"prompt": False}) in calls

    def test_ensure_helper_swallows_import_error(self, monkeypatch):
        """_ensure_ddgs_installed must not raise when lazy_deps is unusable —
        the subsequent `import ddgs` is the real availability gate."""
        import plugins.web.ddgs.provider as _prov
        import tools.lazy_deps as _ld

        def _boom(*_a, **_k):
            raise ImportError("lazy_deps unavailable")

        monkeypatch.setattr(_ld, "ensure", _boom, raising=True)
        # Should not raise.
        _prov._ensure_ddgs_installed()


class TestDDGSGateLightsUpWhenInstallable:
    """check_web_api_key must report the keyless ddgs default as available when
    it can be lazy-installed, even before the package is present (#60425)."""

    def test_configured_ddgs_available_when_lazy_installable(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_lazy_installable", lambda: True)
        assert web_tools.check_web_api_key() is True

    def test_configured_ddgs_unavailable_when_lazy_disabled(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_lazy_installable", lambda: False)
        # No other backend configured/credentialed → tool stays dark.
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "PARALLEL_API_KEY",
                    "TAVILY_API_KEY", "EXA_API_KEY", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        assert web_tools.check_web_api_key() is False

    def test_lazy_installable_probe_respects_allow_flag(self, monkeypatch):
        """_ddgs_lazy_installable reflects the lazy-install kill switch and
        stays network-free (it must not import ddgs)."""
        from tools import web_tools
        import tools.lazy_deps as _ld

        monkeypatch.setattr(_ld, "_allow_lazy_installs", lambda: True)
        assert web_tools._ddgs_lazy_installable() is True

        monkeypatch.setattr(_ld, "_allow_lazy_installs", lambda: False)
        assert web_tools._ddgs_lazy_installable() is False
