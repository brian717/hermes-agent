"""Tests for the proactive-monitor optional skill's classifier script.

The classifier polls -> scores -> surfaces only above-threshold items, staying
silent otherwise (Poke's urgency-monitor pattern). These exercise the threshold
gate, output formats, and the silent paths without making a real LLM call.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = (
    _REPO_ROOT
    / "optional-skills"
    / "productivity"
    / "proactive-monitor"
    / "scripts"
    / "classify_items.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("classify_items_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ci():
    return _load_module()


ITEMS = [
    {"id": "a", "subject": "Lunch?", "from": "friend@x.com"},
    {"id": "b", "subject": "URGENT: prod down", "from": "manager@x.com"},
    {"id": "c", "subject": "Newsletter weekly digest", "from": "news@x.com"},
]


def _fake_llm_scores(scores):
    def _call(**kwargs):
        resp = MagicMock()
        resp.choices[0].message.content = json.dumps(
            [{"index": i, "score": s, "reason": "r"} for i, s in enumerate(scores)]
        )
        return resp

    return _call


def _run(ci, threshold, scores, fmt="text", items=None, capsys=None):
    items = ITEMS if items is None else items
    argv = ["classify_items.py", "--criteria", "from manager or prod", "--threshold", str(threshold), "--format", fmt]
    import agent.auxiliary_client as aux

    with patch.object(aux, "call_llm", _fake_llm_scores(scores)), patch.object(
        ci.sys, "argv", argv
    ), patch.object(ci, "_load_items", lambda f: items):
        rc = ci.main()
    out = capsys.readouterr().out
    return rc, out


def test_only_above_threshold_surfaces(ci, capsys):
    rc, out = _run(ci, 7, [3, 9, 1], capsys=capsys)
    assert rc == 0
    assert "prod down" in out          # score 9 >= 7
    assert "Lunch" not in out          # score 3 < 7
    assert "Newsletter" not in out     # score 1 < 7
    assert "9/10" in out


def test_nothing_above_threshold_is_silent(ci, capsys):
    rc, out = _run(ci, 10, [3, 9, 1], capsys=capsys)
    assert rc == 0
    assert out.strip() == ""           # silent -> cron suppresses delivery


def test_json_format(ci, capsys):
    rc, out = _run(ci, 7, [3, 9, 1], fmt="json", capsys=capsys)
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["id"] == "b"
    assert parsed[0]["score"] == 9


def test_empty_input_is_silent(ci, capsys):
    rc, out = _run(ci, 7, [], items=[], capsys=capsys)
    assert rc == 0
    assert out.strip() == ""


def test_classifier_failure_is_loud(ci, capsys):
    """A failed classify call must exit non-zero, not silently swallow items."""
    argv = ["classify_items.py", "--criteria", "x", "--threshold", "7"]
    import agent.auxiliary_client as aux

    def _boom(**kwargs):
        raise RuntimeError("model down")

    with patch.object(aux, "call_llm", _boom), patch.object(
        ci.sys, "argv", argv
    ), patch.object(ci, "_load_items", lambda f: ITEMS):
        rc = ci.main()
    assert rc == 4  # non-zero -> cron watchdog alerts


def test_monitor_aux_task_config_default():
    from hermes_cli.config import DEFAULT_CONFIG

    assert "monitor" in DEFAULT_CONFIG["auxiliary"]
    assert DEFAULT_CONFIG["auxiliary"]["monitor"]["provider"] == "auto"
