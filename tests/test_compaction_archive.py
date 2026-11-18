"""Tests for Compaction v2: immutable archive + resolvable links (issue #52)."""

import json
import os

import pytest

from fusion_harness.compaction_archive import (
    DanglingLinkError,
    LINK_PREFIX,
    CompactionArchive,
    cite_link,
    extract_links,
    rehydrate,
)
from fusion_harness.compactor import Compactor


def _msgs():
    return [
        {"role": "user", "content": "round 0"},
        {"role": "tool", "tool_call_id": "c1", "content": "RESULT-FACT-42 " + "z" * 300},
    ]


class TestArchiveImmutability:
    def test_append_returns_resolvable_link(self, tmp_path):
        arch = CompactionArchive(str(tmp_path / "arc.jsonl"))
        (link,) = arch.append(_msgs())
        assert link == LINK_PREFIX + "0"
        assert arch.resolve(link) == _msgs()

    def test_existing_spans_never_mutated_by_later_appends(self, tmp_path):
        path = str(tmp_path / "arc.jsonl")
        arch = CompactionArchive(path)
        (link0,) = arch.append(_msgs())
        snapshot = open(path, encoding="utf-8").read()
        arch.append([{"role": "tool", "content": "later"}])
        after = open(path, encoding="utf-8").read()
        assert after.startswith(snapshot)
        assert arch.resolve(link0) == _msgs()
        assert len(arch) == 2

    def test_compaction_runs_never_mutate_archive(self, tmp_path):
        path = str(tmp_path / "arc.jsonl")
        arch = CompactionArchive(path)
        (link,) = arch.append(_msgs())
        before = open(path, "rb").read()
        comp = Compactor(archive=arch)
        msgs = []
        for r in range(5):
            msgs.append({"role": "user", "content": "round %d" % r})
            if r == 0:
                msgs.append({"role": "tool", "tool_call_id": "c9", "content": "payload " * 100})
        cleared = comp._clear_old_tool_results(msgs)
        assert cleared == 1
        assert open(path, "rb").read()[: len(before)] == before
        assert arch.resolve(link) == _msgs()
        assert len(arch) == 2

    def test_archive_restores_across_restart(self, tmp_path):
        path = str(tmp_path / "arc.jsonl")
        arch = CompactionArchive(path)
        (link,) = arch.append(_msgs())
        arch2 = CompactionArchive(path)
        assert arch2.resolve(link) == _msgs()
        assert len(arch2) == 1


class TestLinkResolution:
    def test_dangling_link_raises(self, tmp_path):
        arch = CompactionArchive(str(tmp_path / "arc.jsonl"))
        with pytest.raises(DanglingLinkError):
            arch.resolve(LINK_PREFIX + "99")

    def test_malformed_link_raises(self, tmp_path):
        arch = CompactionArchive(str(tmp_path / "arc.jsonl"))
        with pytest.raises(DanglingLinkError):
            arch.resolve("archive://other-1")
        with pytest.raises(DanglingLinkError):
            arch.resolve("")

    def test_extract_links(self):
        text = "cleared [tool result cleared: id=c1 bytes=10 archive://span-3] done"
        assert extract_links(text) == [LINK_PREFIX + "3"]

    def test_rehydrate_expands_links(self, tmp_path):
        arch = CompactionArchive(str(tmp_path / "arc.jsonl"))
        (link,) = arch.append(_msgs())
        view = [
            {"role": "user", "content": "resume"},
            {"role": "tool", "content": "[tool result cleared: id=c1 bytes=100 %s]" % link},
        ]
        out = rehydrate(view, arch)
        assert len(out) == 3
        assert out[1] == _msgs()[0]
        assert out[2] == _msgs()[1]

    def test_rehydrate_without_links_passthrough(self, tmp_path):
        arch = CompactionArchive(str(tmp_path / "arc.jsonl"))
        view = [{"role": "user", "content": "plain"}]
        assert rehydrate(view, arch) == view

    def test_cite_link_formats(self):
        assert (
            cite_link("[tool result cleared: id=c1 bytes=5]", LINK_PREFIX + "1")
            == "[tool result cleared: id=c1 bytes=5; archive://span-1]"
        )
        assert cite_link("plain", LINK_PREFIX + "2") == "plain [archive://span-2]"


class TestRecallReplay:
    def test_compacted_with_links_recovers_full_history_content(self, tmp_path):
        """Recall axis: content evicted from the context is exactly
        recoverable via links — no worse than full history (2611.14654)."""
        path = str(tmp_path / "arc.jsonl")
        arch = CompactionArchive(path)
        fact_tool = {"role": "tool", "tool_call_id": "f1", "content": "SECRET-CODE=ZX-777"}
        full_history = [{"role": "user", "content": "start"}, fact_tool]
        for r in range(5):
            full_history.append({"role": "user", "content": "round %d" % r})

        comp = Compactor(archive=arch)
        view = [dict(m) for m in full_history]
        comp._clear_old_tool_results(view)

        stub = next(m for m in view if m.get("role") == "tool")
        assert "SECRET-CODE" not in stub["content"]
        links = extract_links(stub["content"])
        assert links, "stub must cite a resolvable link"
        replayed = rehydrate(view, arch)
        joined = json.dumps(replayed)
        assert "SECRET-CODE=ZX-777" in joined
        assert json.dumps(replayed[:2]) == json.dumps(full_history[:2])


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(
                os.path.dirname(__file__), "..", "fusion_harness", "compaction_archive.py"
            ),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
