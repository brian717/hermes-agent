from __future__ import annotations

import json
import pytest

from agent import image_gen_registry
from agent.image_gen_provider import ImageGenProvider


@pytest.fixture(autouse=True)
def _reset_registry():
    image_gen_registry._reset_for_tests()
    yield
    image_gen_registry._reset_for_tests()


class _FakeCodexProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "codex"

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        return {
            "success": True,
            "image": "/tmp/codex-test.png",
            "model": "gpt-5.2-codex",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "provider": "codex",
        }


class TestPluginDispatch:
    def test_dispatch_routes_to_codex_provider(self, monkeypatch, tmp_path):
        from tools import image_generation_tool
        from agent import image_gen_registry as registry_module
        from hermes_cli import plugins as plugins_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("image_gen:\n  provider: codex\n")
        image_gen_registry.register_provider(_FakeCodexProvider())

        monkeypatch.setattr(image_generation_tool, "_read_configured_image_provider", lambda: "codex")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda: None)
        monkeypatch.setattr(registry_module, "get_provider", lambda name: _FakeCodexProvider() if name == "codex" else None)

        dispatched = image_generation_tool._dispatch_to_plugin_provider("draw cat", "square")
        payload = json.loads(dispatched)

        assert payload["success"] is True
        assert payload["provider"] == "codex"
        assert payload["image"] == "/tmp/codex-test.png"
        assert payload["aspect_ratio"] == "square"


    def test_deepinfra_key_alone_does_not_select_image_backend(self, monkeypatch):
        """DeepInfra chat credentials do not imply consent to image billing."""
        from tools import image_generation_tool

        monkeypatch.setenv("DEEPINFRA_API_KEY", "«redacted:sk-…»")
        monkeypatch.delenv("FAL_KEY", raising=False)
        monkeypatch.setattr(image_generation_tool, "_read_configured_image_provider", lambda: None)
        assert image_generation_tool._dispatch_to_plugin_provider("a cat", "square") is None

    def test_requirements_ignore_unselected_paid_plugin(self, monkeypatch):
        from tools import image_generation_tool

        monkeypatch.setattr(image_generation_tool, "check_fal_api_key", lambda: False)
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_provider", lambda: None
        )
        assert image_generation_tool.check_image_generation_requirements() is False


class TestEmptyResponseRetry:
    """#35120 — a backend that completes without emitting an image.

    ``empty_response`` is transient (observed on Codex-routed gpt-image-2),
    so the dispatch seam retries it exactly once and then hands the model
    structured evidence instead of a bare error, so it stops re-calling the
    tool for the rest of the turn.
    """

    def _empty(self, prompt="a cat"):
        return {
            "success": False,
            "image": None,
            "error": "Codex response contained no image_generation_call result",
            "error_type": "empty_response",
            "model": "gpt-image-2-medium",
            "prompt": prompt,
            "aspect_ratio": "square",
            "provider": "openai-codex",
        }

    def test_empty_response_is_retried_once_then_annotated(self, monkeypatch):
        from tools import image_generation_tool

        calls = []

        class _AlwaysEmpty(_FakeCodexProvider):
            def generate(self, prompt, aspect_ratio="landscape", **kwargs):
                calls.append(prompt)
                return TestEmptyResponseRetry()._empty(prompt)

        result = image_generation_tool._generate_with_empty_response_retry(
            _AlwaysEmpty(), {"prompt": "a cat", "aspect_ratio": "square"}
        )

        # Bounded: exactly one retry, never a loop.
        assert len(calls) == 2
        assert result["success"] is False
        # Classification and the original evidence survive untouched.
        assert result["error_type"] == "empty_response"
        assert result["model"] == "gpt-image-2-medium"
        assert result["provider"] == "openai-codex"
        # ...and the retry is now visible to the model, with a recovery hint.
        assert result["attempts"] == 2
        assert result["retried"] is True
        assert isinstance(result["duration_seconds"], float)
        assert "do not call image_generate again" in result["hint"]

    def test_retry_that_succeeds_returns_the_image_untouched(self, monkeypatch):
        from tools import image_generation_tool

        attempts = {"n": 0}

        class _EmptyThenOk(_FakeCodexProvider):
            def generate(self, prompt, aspect_ratio="landscape", **kwargs):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return TestEmptyResponseRetry()._empty(prompt)
                return {
                    "success": True,
                    "image": "/tmp/codex-test.png",
                    "model": "gpt-image-2-medium",
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "provider": "openai-codex",
                }

        result = image_generation_tool._generate_with_empty_response_retry(
            _EmptyThenOk(), {"prompt": "a cat", "aspect_ratio": "square"}
        )

        assert attempts["n"] == 2
        assert result["success"] is True
        assert result["image"] == "/tmp/codex-test.png"
        # A success carries no retry bookkeeping — the existing shape is intact.
        assert "hint" not in result
        assert "attempts" not in result

    def test_other_failure_classes_are_not_retried(self, monkeypatch):
        from tools import image_generation_tool

        calls = []

        class _SafetyReject(_FakeCodexProvider):
            def generate(self, prompt, aspect_ratio="landscape", **kwargs):
                calls.append(prompt)
                return {
                    "success": False,
                    "image": None,
                    "error": "content policy violation",
                    "error_type": "safety_rejection",
                    "model": "gpt-image-2-medium",
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "provider": "openai-codex",
                }

        result = image_generation_tool._generate_with_empty_response_retry(
            _SafetyReject(), {"prompt": "a cat", "aspect_ratio": "square"}
        )

        # Deterministic failures stay single-shot and distinguishable.
        assert len(calls) == 1
        assert result["error_type"] == "safety_rejection"
        assert "hint" not in result

    def test_provider_exception_still_propagates(self, monkeypatch):
        from tools import image_generation_tool

        class _Boom(_FakeCodexProvider):
            def generate(self, prompt, aspect_ratio="landscape", **kwargs):
                raise RuntimeError("network down")

        with pytest.raises(RuntimeError):
            image_generation_tool._generate_with_empty_response_retry(
                _Boom(), {"prompt": "a cat", "aspect_ratio": "square"}
            )

    def test_dispatch_surfaces_evidence_in_the_tool_payload(self, monkeypatch, tmp_path):
        """End-to-end through the dispatch seam the agent actually calls."""
        from tools import image_generation_tool
        from agent import image_gen_registry as registry_module
        from hermes_cli import plugins as plugins_module

        calls = []
        outer = self

        class _AlwaysEmpty(_FakeCodexProvider):
            def generate(self, prompt, aspect_ratio="landscape", **kwargs):
                calls.append(prompt)
                return outer._empty(prompt)

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_provider", lambda: "codex"
        )
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda: None)
        monkeypatch.setattr(registry_module, "get_provider", lambda name: _AlwaysEmpty())

        payload = json.loads(
            image_generation_tool._dispatch_to_plugin_provider("a cat", "square")
        )

        assert len(calls) == 2
        assert payload["success"] is False
        assert payload["error_type"] == "empty_response"
        assert payload["retried"] is True
        assert payload["attempts"] == 2
        assert "duration_seconds" in payload
        assert payload["hint"]
