"""Tests for file.read_segment contract + budget integration (issue #46)."""

import base64
import os

import pytest

from fusion_harness.file_segment import (
    DEFAULT_MAX_BYTES_CAP,
    SegmentBudget,
    SegmentError,
    SegmentPolicy,
    read_segment,
    render_segment_result,
    segment_header,
)
from fusion_harness.funnels import ToolFunnel
from fusion_harness.permissions import PermissionEngine


@pytest.fixture
def text_file(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_bytes(b"x" * 1000)
    return str(p)


@pytest.fixture
def bin_file(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(bytes(range(256)) * 8)
    return str(p)


class TestHardCap:
    def test_oversize_request_clamped_never_errors(self, tmp_path):
        big = str(tmp_path / "big.txt")
        with open(big, "wb") as fh:
            fh.write(b"x" * (DEFAULT_MAX_BYTES_CAP + 1000))
        out = read_segment(big, offset=0, max_bytes=10**9)
        assert out["bytes"] == DEFAULT_MAX_BYTES_CAP
        assert out["eof"] is False
        assert out["next_offset"] == DEFAULT_MAX_BYTES_CAP

    def test_default_cap_when_max_bytes_omitted(self, tmp_path):
        big = str(tmp_path / "big.txt")
        with open(big, "wb") as fh:
            fh.write(b"x" * (DEFAULT_MAX_BYTES_CAP + 1000))
        out = read_segment(big)
        assert out["bytes"] == DEFAULT_MAX_BYTES_CAP
        assert out["eof"] is False

    def test_policy_cap_override(self, text_file):
        out = read_segment(text_file, max_bytes=10**9, policy=SegmentPolicy(max_bytes_cap=100))
        assert out["bytes"] == 100

    def test_zero_or_negative_max_bytes_rejected(self, text_file):
        with pytest.raises(SegmentError, match="max_bytes"):
            read_segment(text_file, max_bytes=0)

    def test_negative_offset_rejected(self, text_file):
        with pytest.raises(SegmentError, match="offset"):
            read_segment(text_file, offset=-1)

    def test_empty_file_ref_rejected(self):
        with pytest.raises(SegmentError, match="file_ref"):
            read_segment("")


class TestRangeSemantics:
    def test_basic_range(self, text_file):
        out = read_segment(text_file, offset=100, max_bytes=50)
        assert out["offset"] == 100
        assert out["end_offset"] == 150
        assert out["total"] == 1000
        assert out["next_offset"] == 150
        assert out["eof"] is False
        assert out["encoding"] == "utf-8"
        assert len(out["content"]) == 50

    def test_offset_past_eof(self, text_file):
        out = read_segment(text_file, offset=5000)
        assert out["eof"] is True
        assert out["content"] == ""
        assert out["bytes"] == 0

    def test_loop_reads_whole_file(self, text_file):
        chunks = []
        offset = 0
        while True:
            out = read_segment(text_file, offset=offset, max_bytes=300)
            chunks.append(out["content"])
            if out["eof"]:
                break
            offset = out["next_offset"]
        assert sum(len(c) for c in chunks) == 1000
        assert offset + len(chunks[-1]) == 1000

    def test_exact_tail_is_eof(self, text_file):
        out = read_segment(text_file, offset=900, max_bytes=100)
        assert out["eof"] is True
        assert out["bytes"] == 100

    def test_missing_file_fail_visible(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_segment(str(tmp_path / "nope.txt"))


class TestBinaryEncoding:
    def test_binary_base64_with_flag(self, bin_file):
        out = read_segment(bin_file, max_bytes=256)
        assert out["encoding"] == "base64"
        assert base64.b64decode(out["content"]) == bytes(range(256))
        assert out["eof"] is False

    def test_binary_tail_eof(self, bin_file):
        out = read_segment(bin_file, offset=256 * 8 - 100, max_bytes=256)
        assert out["eof"] is True
        assert base64.b64decode(out["content"]) == bytes(range(156, 256))


class TestPerTurnBudget:
    def test_budget_clamps_within_turn(self, text_file):
        policy = SegmentPolicy(max_bytes_cap=600, per_turn_bytes=800)
        budget = SegmentBudget(policy)
        out1 = read_segment(text_file, max_bytes=600, policy=policy, budget=budget, turn_id="t1")
        assert out1["bytes"] == 600
        out2 = read_segment(
            text_file, offset=600, max_bytes=600, policy=policy, budget=budget, turn_id="t1"
        )
        assert out2["bytes"] == 200
        out3 = read_segment(
            text_file, offset=800, max_bytes=600, policy=policy, budget=budget, turn_id="t1"
        )
        assert out3["bytes"] == 0
        assert out3["eof"] is False
        assert budget.spent(text_file, "t1") == 800

    def test_budget_is_per_file(self, text_file, tmp_path):
        other = str(tmp_path / "other.txt")
        with open(other, "wb") as fh:
            fh.write(b"y" * 500)
        policy = SegmentPolicy(max_bytes_cap=600, per_turn_bytes=800)
        budget = SegmentBudget(policy)
        assert read_segment(text_file, policy=policy, budget=budget, turn_id="t")["bytes"] == 600
        assert read_segment(other, policy=policy, budget=budget, turn_id="t")["bytes"] == 500

    def test_budget_resets_next_turn(self, text_file):
        policy = SegmentPolicy(max_bytes_cap=600, per_turn_bytes=800)
        budget = SegmentBudget(policy)
        read_segment(text_file, policy=policy, budget=budget, turn_id="t1")
        assert read_segment(text_file, policy=policy, budget=budget, turn_id="t2")["bytes"] == 600

    def test_no_budget_unlimited_calls(self, text_file):
        policy = SegmentPolicy(max_bytes_cap=300)
        out = read_segment(text_file, policy=policy, turn_id="t")
        assert out["bytes"] == 300


class TestHeaderAndRendering:
    def test_header_format(self):
        assert segment_header("f.txt", 0, 256, 1000) == "[segment file=f.txt bytes=0-256/1000]"

    def test_render_prefixes_header(self, text_file):
        out = read_segment(text_file, max_bytes=100)
        rendered = render_segment_result(out)
        assert rendered.splitlines()[0] == out["header"]
        assert "[truncated: continue at next_offset=100]" in rendered

    def test_render_eof_no_truncation_note(self, text_file):
        out = read_segment(text_file, offset=0, max_bytes=1000)
        assert "[truncated" not in render_segment_result(out)

    def test_render_falls_back_when_header_missing(self):
        rendered = render_segment_result(
            {"file_ref": "a", "offset": 1, "end_offset": 2, "total": 3, "content": "z"}
        )
        assert rendered.startswith("[segment file=a bytes=1-2/3]")


class TestFunnelIntegration:
    def test_segment_through_run_tool(self, text_file):
        funnel = ToolFunnel(permissions=PermissionEngine())

        async def executor(args):
            return render_segment_result(
                read_segment(args["file_ref"], args["offset"], args["max_bytes"])
            )

        result, is_error = _run(
            funnel.run_tool(
                _Ctx(),
                "file.read_segment",
                {"file_ref": text_file, "offset": 0, "max_bytes": 100},
                executor,
            )
        )
        assert is_error is False
        assert result.startswith("[segment file=")
        assert "next_offset=100" in result

    def test_exhausted_budget_reports_in_funneled_output(self, text_file):
        policy = SegmentPolicy(max_bytes_cap=600, per_turn_bytes=600)
        budget = SegmentBudget(policy)

        async def executor(args):
            return render_segment_result(
                read_segment(
                    args["file_ref"],
                    args["offset"],
                    args["max_bytes"],
                    policy=policy,
                    budget=budget,
                    turn_id="t1",
                )
            )

        funnel = ToolFunnel(permissions=PermissionEngine())
        _run(
            funnel.run_tool(
                _Ctx(),
                "file.read_segment",
                {"file_ref": text_file, "offset": 0, "max_bytes": 600},
                executor,
            )
        )
        result, is_error = _run(
            funnel.run_tool(
                _Ctx(),
                "file.read_segment",
                {"file_ref": text_file, "offset": 600, "max_bytes": 600},
                executor,
            )
        )
        assert is_error is False
        assert result.startswith("[segment file=")
        assert "bytes=600-600/1000" in result


class _Ctx:
    session_id = "s1"
    graph_id = "g1"


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestCompactionPreservation:
    def test_evicted_stub_keeps_citable_header(self):
        from fusion_harness.compactor import Compactor, _CLEARED_STUB_PREFIX

        seg = render_segment_result(
            {
                "file_ref": "doc.txt",
                "offset": 0,
                "end_offset": 256,
                "total": 1000,
                "content": "y" * 500,
            }
        )
        msgs = []
        for r in range(5):
            msgs.append({"role": "user", "content": "round %d" % r})
            if r == 0:
                msgs.append({"role": "tool", "tool_call_id": "c1", "content": seg})
        cleared = Compactor()._clear_old_tool_results(msgs)
        assert cleared >= 1
        stub = next(m["content"] for m in msgs if m.get("role") == "tool")
        assert stub.startswith(_CLEARED_STUB_PREFIX)
        assert "[segment file=doc.txt bytes=0-256/1000]" in stub

    def test_retention_list_cites_segment_headers(self):
        from fusion_harness.compactor import RetentionList

        seg = "[segment file=doc.txt bytes=0-256/1000]\n" + "z" * 300
        rl = RetentionList.from_messages([{"role": "tool", "content": seg}])
        assert any("[segment file=doc.txt" in c for c in rl.constraints)

    def test_non_segment_stub_unchanged(self):
        from fusion_harness.compactor import Compactor, _CLEARED_STUB_PREFIX

        msgs = []
        for r in range(5):
            msgs.append({"role": "user", "content": "round %d" % r})
            if r == 0:
                msgs.append({"role": "tool", "tool_call_id": "c9", "content": "plain result" * 50})
        assert Compactor()._clear_old_tool_results(msgs) == 1
        stub = next(m["content"] for m in msgs if m.get("role") == "tool")
        assert stub.startswith(_CLEARED_STUB_PREFIX)
        assert "[segment" not in stub


class TestUtf8Boundary:
    def test_cjk_walk_all_utf8_no_base64(self, tmp_path):
        text = "前沿模型注意力机制研究摘要。" * 20
        p = tmp_path / "cjk.txt"
        p.write_text(text, encoding="utf-8")
        pieces = []
        offset = 0
        encodings = set()
        while True:
            out = read_segment(str(p), offset=offset, max_bytes=64)
            encodings.add(out["encoding"])
            pieces.append(out["content"])
            if out["eof"]:
                break
            offset = out["next_offset"]
        assert encodings == {"utf-8"}
        assert "".join(pieces) == text

    def test_boundary_trim_next_offset_backs_off(self, tmp_path):
        text = " abc" * 10
        p = tmp_path / "t.txt"
        p.write_text(text, encoding="utf-8")
        out = read_segment(str(p), offset=0, max_bytes=5)
        assert out["encoding"] == "utf-8"
        assert text.startswith(out["content"])
        assert out["next_offset"] <= 5

    def test_real_binary_still_base64(self, bin_file):
        out = read_segment(bin_file, max_bytes=256)
        assert out["encoding"] == "base64"

    def test_trailing_incomplete_multibyte_carried_forward(self, tmp_path):
        text = "aa研究发现"
        p = tmp_path / "t2.txt"
        raw = text.encode("utf-8")
        p.write_bytes(raw)
        out = read_segment(str(p), offset=0, max_bytes=4)
        assert out["encoding"] == "utf-8"
        assert raw.startswith(out["content"].encode("utf-8"))
        assert out["next_offset"] < len(raw)
        out2 = read_segment(str(p), offset=out["next_offset"], max_bytes=64)
        assert out2["encoding"] == "utf-8"
        assert (out["content"] + out2["content"]).encode("utf-8") == raw
        assert out2["eof"] is True


class TestBudgetExhaustedMarker:
    def test_exhausted_returns_marker_not_empty_success(self, text_file):
        policy = SegmentPolicy(max_bytes_cap=600, per_turn_bytes=600)
        budget = SegmentBudget(policy)
        read_segment(text_file, policy=policy, budget=budget, turn_id="t1")
        out = read_segment(text_file, offset=600, policy=policy, budget=budget, turn_id="t1")
        assert out["budget_exhausted"] is True
        assert out["content"] == ""
        assert out["eof"] is False
        assert out["next_offset"] == 600

    def test_marker_absent_on_normal_reads(self, text_file):
        out = read_segment(text_file, max_bytes=100)
        assert out["budget_exhausted"] is False

    def test_render_notes_exhaustion(self, text_file):
        policy = SegmentPolicy(max_bytes_cap=600, per_turn_bytes=600)
        budget = SegmentBudget(policy)
        read_segment(text_file, policy=policy, budget=budget, turn_id="t1")
        out = read_segment(text_file, offset=600, policy=policy, budget=budget, turn_id="t1")
        rendered = render_segment_result(out)
        assert "[budget exhausted for this turn" in rendered

    def test_past_eof_not_marker(self, text_file):
        out = read_segment(text_file, offset=5000)
        assert out["budget_exhausted"] is False
        assert out["eof"] is True


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "file_segment.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
