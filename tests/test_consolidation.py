"""Issue #99: proactive consolidation — FOLD/DROP/REWRITE verbs, state
cap, audit log, archive-backed undo, retention-list protection."""

import pytest

from fusion_harness.compaction_archive import CompactionArchive
from fusion_harness.consolidation import ConsolidationConfig, ConsolidationError, ConsolidationLog, Consolidator


def make_cons(tmp_path, **cfg_kw):
    archive = CompactionArchive(str(tmp_path / "archive.jsonl"))
    log = ConsolidationLog(str(tmp_path / "audit.jsonl"))
    cfg = ConsolidationConfig(enable_proactive=True, **cfg_kw)
    return Consolidator(config=cfg, archive=archive, audit_log=log), archive, log


def msgs(*contents):
    return [{"role": "user", "content": c} for c in contents]


def test_disabled_config_refuses_construction():
    with pytest.raises(ConsolidationError):
        Consolidator(config=ConsolidationConfig(enable_proactive=False))


def test_fold_replaces_range_with_citable_summary(tmp_path):
    c, archive, log = make_cons(tmp_path)
    m = msgs("alpha", "beta", "gamma", "delta")
    out = c.fold(m, 1, 3, "episode one resolved")
    assert len(out) == 3
    assert "[fold]" in out[1]["content"]
    assert "archive://span-0" in out[1]["content"]
    assert out[1]["role"] == "system"
    entry = log.entries()[-1]
    assert entry["verb"] == "FOLD" and entry["scope"] == [1, 3]
    assert c.stats["folds"] == 1


def test_fold_undo_restores_original_text(tmp_path):
    c, archive, log = make_cons(tmp_path)
    original = msgs("alpha", "secret-episode-body", "gamma")
    folded = c.fold(original, 0, 2, "folded away")
    restored = c.undo_fold(folded, 0)
    assert restored[0]["content"] == "alpha"
    assert restored[1]["content"] == "secret-episode-body"
    assert restored[2]["content"] == "gamma"
    assert c.stats["undos"] == 1


def test_fold_requires_summary_and_bounds(tmp_path):
    c, _, _ = make_cons(tmp_path)
    m = msgs("a", "b")
    with pytest.raises(ConsolidationError):
        c.fold(m, 0, 2, "   ")
    with pytest.raises(ConsolidationError):
        c.fold(m, 2, 5, "ok")


def test_drop_requires_reason(tmp_path):
    c, _, _ = make_cons(tmp_path)
    with pytest.raises(ConsolidationError):
        c.drop(msgs("a", "b"), 0, 1, "")


def test_drop_refuses_retention_content(tmp_path):
    c, _, log = make_cons(tmp_path)
    m = msgs("TODO finish the parser", "plain turn", "constraint: keep API stable")
    with pytest.raises(ConsolidationError):
        c.drop(m, 0, 2, "cleanup")
    # Unrelated scope drops fine.
    out = c.drop(m, 1, 2, "useless turn")
    assert len(out) == 2


def test_drop_logs_but_does_not_archive(tmp_path):
    c, archive, log = make_cons(tmp_path)
    out = c.drop(msgs("junk-one", "keep"), 0, 1, "useless entity")
    assert len(archive) == 0
    entry = log.entries()[-1]
    assert entry["verb"] == "DROP" and entry["dropped_bytes"] > 0
    assert out[0]["content"] == "keep"


def test_rewrite_under_cap_merges(tmp_path):
    c, _, log = make_cons(tmp_path, state_cap_tokens=100)
    out = c.rewrite("state so far", "new observation")
    assert "state so far" in out and "[update] new observation" in out
    assert log.entries()[-1]["verb"] == "REWRITE"


def test_rewrite_over_cap_refused_not_truncated(tmp_path):
    c, _, _ = make_cons(tmp_path, state_cap_tokens=10)
    big = "x" * 200
    with pytest.raises(ConsolidationError):
        c.rewrite(big, "more")
    # Deterministic fallback path: caller folds instead — state never
    # silently truncated.


def test_rewrite_custom_counter_and_summarizer(tmp_path):
    calls = []

    def summarizer(prev, obs):
        calls.append((prev, obs))
        return "merged:%s|%s" % (prev, obs)

    c, _, _ = make_cons(tmp_path)
    c.summarizer = summarizer
    out = c.rewrite("a", "b")
    assert out == "merged:a|b"
    assert calls == [("a", "b")]


def test_pre_fold_checkpoint_saved(tmp_path):
    from fusion_harness import CheckpointStore

    store = CheckpointStore(str(tmp_path / "cp.db"))
    c, _, _ = make_cons(tmp_path)
    c.checkpoint_store = store
    c.session_id = "s1"
    m = msgs("one", "two", "three")
    c.fold(m, 0, 1, "first folded")
    loaded = store.load("s1", 1)
    assert loaded is not None
    assert [x["content"] for x in loaded.messages] == ["one", "two", "three"]


def test_audit_log_roundtrip_and_corrupt_line(tmp_path):
    log = ConsolidationLog(str(tmp_path / "audit.jsonl"))
    log.append({"verb": "FOLD"})
    log.append({"verb": "DROP"})
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("{corrupt\n")
    entries = log.entries()
    assert [e["verb"] for e in entries] == ["FOLD", "DROP"]
    assert all("ts" in e for e in entries)
