"""Artifact-summary dual channel for worker outputs (issue #27, P3).

Worker full outputs go to a shared artifact area on disk (per-run
directory); the coordinator ("brain") holds only compressed summaries +
lightweight references, injected at the FRONT of its context (Lost in the
Middle, arXiv 2307.03172 — mid-context recall collapses). Summary schema
is driven by the P2 spawn brief's output_format. Pure stdlib, no model
call — summarization here is deterministic extraction; consumers may swap
in a smarter summarizer via the injectable `summarize` callable.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class WorkerDelivery:
    worker_name: str
    artifact_ref: str
    summary: str


class ArtifactStore:
    """Writes full worker outputs to <root>/<run_id>/ artifacts and returns
    lightweight references for the brain transcript."""

    def __init__(self, root_dir: str) -> None:
        self.root_dir = root_dir

    def write_artifact(self, run_id: str, worker_name: str, content: str, fmt: str = "text") -> str:
        safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
        safe_worker = re.sub(r"[^A-Za-z0-9_.-]", "_", worker_name)
        run_dir = os.path.join(self.root_dir, safe_run)
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, "%s.%s" % (safe_worker, fmt or "text"))
        text = content if isinstance(content, str) else str(content)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        ref = "%s/%s.%s" % (safe_run, safe_worker, fmt or "text")
        logger.info("artifact written run=%s worker=%s bytes=%d", safe_run, safe_worker, len(text))
        return ref

    def artifact_path(self, ref: str) -> str:
        return os.path.join(self.root_dir, ref)


def default_summarize(content: str, output_format: str = "", max_chars: int = 1200) -> str:
    """Deterministic extraction: key lines (decisions/paths/results) first,
    then head. Good enough as a fallback; injectable for smarter policies."""
    key_lines = []
    for line in content.splitlines():
        low = line.lower()
        if any(
            mark in low for mark in ("decision", "result", "path", "error", "conclusion", "todo")
        ):
            key_lines.append(line.strip())
    parts = []
    if output_format:
        parts.append("format=%s" % output_format)
    parts.extend(key_lines[:10])
    summary = "\n".join(parts)[:max_chars]
    if not summary.strip():
        summary = content[:max_chars]
    return summary


class SummaryInjector:
    """Places per-worker summaries at the FRONT of the coordinator context
    (after the system prefix, before all history)."""

    def __init__(self, summarize: Optional[Callable[[str, str], str]] = None) -> None:
        self.summarize = summarize or default_summarize

    def summarize_delivery(self, worker_name: str, content: str, output_format: str = "") -> str:
        return self.summarize(content, output_format)

    def inject_front(
        self,
        messages: List[Dict[str, Any]],
        deliveries: List[WorkerDelivery],
    ) -> List[Dict[str, Any]]:
        """Insert a worker-summaries system segment after the system prefix.
        Existing worker-summary segments are replaced (idempotent re-inject)."""
        body_lines = [
            "%s -> artifact=%s\n%s" % (d.worker_name, d.artifact_ref, d.summary) for d in deliveries
        ]
        segment = {
            "role": "system",
            "content": "[Worker summaries (full artifacts on disk)]\n" + "\n".join(body_lines),
        }
        out = [
            m
            for m in messages
            if not (
                m.get("role") == "system"
                and str(m.get("content") or "").startswith("[Worker summaries")
            )
        ]
        insert_at = 0
        for idx, m in enumerate(out):
            if m.get("role") == "system":
                insert_at = idx + 1
            else:
                break
        logger.info("worker summaries injected count=%d at=%d", len(deliveries), insert_at)
        return out[:insert_at] + [segment] + out[insert_at:]


class ArtifactTools:
    """Tool surface over an existing ArtifactStore (issue #36, K3): capture /
    read / list / diff. Refs stay the ONLY interchange unit — full text never
    crosses process boundaries. Additive; ArtifactStore signatures unchanged."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def capture(
        self,
        name: str,
        content: str,
        kind: str = "text",
        tags: Optional[List[str]] = None,
        run_id: str = "shared",
    ) -> str:
        tags = tags or []
        ref = self.store.write_artifact(run_id, name, content, kind)
        if tags:
            tag_path = self.store.artifact_path("%s/%s.tags" % (ref.split("/")[0], name))
            with open(tag_path, "w", encoding="utf-8") as fh:
                fh.write(",".join(tags))
        logger.info("artifact captured name=%s kind=%s tags=%d", name, kind, len(tags))
        return ref

    def read(self, ref: str) -> str:
        path = self.store.artifact_path(ref)
        if not os.path.exists(path):
            raise FileNotFoundError("no artifact for ref: %s" % ref)
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def list(self, prefix: str = "") -> List[str]:
        refs: List[str] = []
        for dirpath, _dirnames, filenames in os.walk(self.store.root_dir):
            for fname in filenames:
                if fname.endswith(".tags"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fname), self.store.root_dir)
                rel = rel.replace(os.sep, "/")
                if not prefix or rel.startswith(prefix):
                    refs.append(rel)
        return sorted(refs)

    def diff(self, ref_a: str, ref_b: str) -> str:
        import difflib

        text_a = self.read(ref_a)
        text_b = self.read(ref_b)
        return "\n".join(
            difflib.unified_diff(
                text_a.splitlines(), text_b.splitlines(), fromfile=ref_a, tofile=ref_b, lineterm=""
            )
        )


__all__ = [
    "ArtifactStore",
    "SummaryInjector",
    "WorkerDelivery",
    "default_summarize",
    "ArtifactTools",
]
