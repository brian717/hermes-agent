"""Regression coverage: the tail cut must never point past the end (#75588).

``_find_tail_cut_by_tokens`` ends with a forward-progress floor of
``head_end + 1`` so compression always claims at least one message.  But a
transcript that ends in a tool-result run pushes ``_align_boundary_forward``
to ``len(messages)``, and the floor then handed back ``len(messages) + 1``.

Callers do not merely slice with that value.  ``_resolve_compact_cursor``
walks ``range(head_end, tail_start)`` and indexes ``messages[idx]``, so an
eight-message transcript raised ``IndexError`` out of the compression path and
failed the active gateway turn.  ``has_content_to_compress()`` — the gateway
``/compress`` preflight gate — also read ``start < end`` as "there is a middle
to summarise" and burned a summariser call on an empty window.

``len(messages)`` is the only valid exclusive end when the aligned head
reaches the end of the list; it makes every caller's
``compress_start >= compress_end`` check take its existing
no-compressible-window path.  The summary scan additionally clamps its own
bounds, so a bad window narrows the scan instead of raising.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def compressor():
    from agent.context_compressor import ContextCompressor

    return ContextCompressor(model="main-model", quiet_mode=True)


def _tool_suffix_transcript():
    """Live-shaped 8-message transcript ending in a parallel tool-call group.

    The five ``tool`` rows run to the end of the list, so the aligned head
    lands on ``len(messages)`` — the shape reported in #75588
    (``len(messages) = 8``, ``compress_start = 8``, ``compress_end = 9``).
    """
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u" * 60},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
                for i in range(5)
            ],
        },
    ]
    for i in range(5):
        messages.append(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "r" * 60}
        )
    return messages


class TestTailCutStaysInBounds:
    def test_aligned_head_at_end_yields_len_not_len_plus_one(self, compressor):
        """The reported shape: the head consumes the list, so the cut is ``n``."""
        messages = _tool_suffix_transcript()
        n = len(messages)
        assert n == 8

        head_end = compressor._align_boundary_forward(
            messages, compressor._protect_head_size(messages)
        )
        assert head_end == n, "fixture must reproduce the aligned-head-at-end shape"

        assert compressor._find_tail_cut_by_tokens(messages, head_end) == n

    def test_head_end_at_len_returns_len(self, compressor):
        """Direct contract check, independent of how the head got there."""
        messages = _tool_suffix_transcript()
        n = len(messages)
        assert compressor._find_tail_cut_by_tokens(messages, head_end=n) == n

    def test_head_end_past_len_returns_len(self, compressor):
        messages = _tool_suffix_transcript()
        n = len(messages)
        assert compressor._find_tail_cut_by_tokens(messages, head_end=n + 3) == n

    def test_empty_transcript_returns_zero(self, compressor):
        assert compressor._find_tail_cut_by_tokens([], head_end=0) == 0

    def test_cut_never_exceeds_len_across_tool_suffix_lengths(self, compressor):
        """Sweep the whole family of tool-suffix shapes, not just the reported one."""
        for results in range(1, 9):
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u" * 60},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                        for i in range(results)
                    ],
                },
            ]
            for i in range(results):
                messages.append(
                    {"role": "tool", "tool_call_id": f"call_{i}", "content": "r" * 60}
                )

            head_end = compressor._align_boundary_forward(
                messages, compressor._protect_head_size(messages)
            )
            cut = compressor._find_tail_cut_by_tokens(messages, head_end)
            assert cut <= len(messages), (
                f"results={results}: cut {cut} points past len {len(messages)}"
            )


class TestNoCompressibleWindowIsReportedHonestly:
    def test_compress_does_not_raise_and_changes_nothing(self, compressor):
        messages = _tool_suffix_transcript()
        original = [dict(m) for m in messages]

        result = compressor.compress(messages)

        assert result == original, "no message may be removed or rewritten"

    def test_compress_calls_no_summary_model(self, compressor, monkeypatch):
        """There is nothing to summarise — the summariser must not be invoked."""
        messages = _tool_suffix_transcript()

        def _boom(*args, **kwargs):
            raise AssertionError("summary model called for an empty window")

        monkeypatch.setattr(compressor, "_generate_summary", _boom)

        compressor.compress(messages)

    def test_preflight_gate_reports_nothing_to_compress(self, compressor):
        """``/compress`` skips the LLM call only if this gate tells the truth."""
        messages = _tool_suffix_transcript()

        assert compressor.has_content_to_compress(messages) is False

    def test_micro_compaction_does_not_raise(self, compressor):
        """The path that actually raised ``IndexError`` in the report."""
        messages = _tool_suffix_transcript()
        compressor._micro_compact_enabled = True

        result = compressor._micro_compact(messages)

        assert len(result) == len(messages)


class TestSummaryScanClampsItsWindow:
    def test_end_past_len_scans_only_existing_rows(self, compressor):
        messages = _tool_suffix_transcript()

        assert (
            compressor._find_context_summaries(messages, 0, len(messages) + 5) == []
        )
        assert compressor._find_latest_context_summary(
            messages, 0, len(messages) + 5
        ) == (None, "")

    def test_negative_start_is_clamped(self, compressor):
        messages = _tool_suffix_transcript()

        assert compressor._find_context_summaries(messages, -4, len(messages)) == []

    def test_start_greater_than_end_scans_nothing(self, compressor):
        messages = _tool_suffix_transcript()

        assert compressor._find_context_summaries(messages, 6, 2) == []

    def test_empty_messages(self, compressor):
        assert compressor._find_context_summaries([], 0, 5) == []
        assert compressor._find_latest_context_summary([], 0, 5) == (None, "")

    def test_malformed_row_is_skipped_not_indexed(self, compressor):
        messages = _tool_suffix_transcript()
        messages.insert(2, None)  # type: ignore[arg-type]

        assert compressor._find_context_summaries(messages, 0, len(messages)) == []

    def test_real_summary_inside_an_overshooting_window_is_still_found(
        self, compressor
    ):
        """Clamping must narrow the scan, not blind it."""
        from agent.context_compressor import ContextCompressor

        messages = _tool_suffix_transcript()
        body = "prior context recap"
        messages.insert(
            1,
            {
                "role": "user",
                "content": ContextCompressor._with_summary_prefix(body),
            },
        )

        hits = compressor._find_context_summaries(messages, 0, len(messages) + 10)

        assert [idx for idx, _ in hits] == [1]
