"""Compaction v2: compact state with resolvable source links (issue #52).

CarryOn (arXiv 2611.17808): compaction output should be a compact state
whose entries link into a replayable base archive, not a free summary —
later turns re-fetch via links and are more accurate than guessing from
a summary. arXiv 2611.14654: clearing old history damages recall, so
compaction is NOT deletion: the base archive is immutable (append-only
file; a compaction run never rewrites or removes existing spans — it
only appends new ones and changes the view entering the context). Every
link emitted is resolvable by the harness rehydration path; a dangling
link raises DanglingLinkError (a bug, never a degraded mode).
"""

import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

LINK_PREFIX = "archive://span-"
_LINK_RE = re.compile(r"archive://span-(\d+)")


class DanglingLinkError(KeyError):
    """Link points at a span the archive cannot replay — bug, not degraded mode."""


class CompactionArchive:
    """Immutable append-only message archive. One JSONL file; existing
    lines are never rewritten. resolve() replays any emitted link."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._spans: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._spans.append(json.loads(line))
                    except ValueError as exc:
                        logger.error("archive corrupt line skipped: %s", exc)
            logger.info("compaction archive restored spans=%d", len(self._spans))
        except OSError as exc:
            logger.error("compaction archive load failed: %s", exc)

    def append(self, messages: list[dict]) -> list[str]:
        """Archive messages as one span; returns its resolvable link.
        Append-only — previously stored spans are untouched."""
        with self._lock:
            span = {"id": len(self._spans), "messages": list(messages or [])}
            self._spans.append(span)
            tmp = self.path + ".tmp"
            mode = "a" if os.path.exists(self.path) else "w"
            if mode == "w":
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(span, ensure_ascii=False) + "\n")
                os.replace(tmp, self.path)
            else:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(span, ensure_ascii=False) + "\n")
            link = LINK_PREFIX + str(span["id"])
            logger.info("archive span appended id=%d messages=%d", span["id"], len(span["messages"]))
            return [link]

    def resolve(self, link: str) -> list[dict]:
        """Replay a link back into messages. Dangling links raise —
        a link that cannot be replayed is a bug, not a degraded mode."""
        m = _LINK_RE.fullmatch(link or "")
        if not m:
            raise DanglingLinkError("malformed archive link: %r" % (link,))
        idx = int(m.group(1))
        with self._lock:
            if idx < 0 or idx >= len(self._spans):
                logger.error("dangling archive link resolved=%s spans=%d", link, len(self._spans))
                raise DanglingLinkError(link)
            return list(self._spans[idx]["messages"])

    def __len__(self) -> int:
        with self._lock:
            return len(self._spans)


def cite_link(content: str, link: str) -> str:
    """Embed a resolvable link in a cleared-tool stub so summaries and
    later turns can re-fetch the original content."""
    return "%s; %s]" % (content.rstrip("]"), link) if content.endswith("]") else "%s [%s]" % (content, link)


def extract_links(text: str) -> list[str]:
    return ["archive://span-" + m for m in _LINK_RE.findall(text or "")]


def rehydrate(messages: list[dict], archive: CompactionArchive) -> list[dict]:
    """Rehydration path: expand archive links found in message content
    back into the original tool messages. Dangling links raise."""
    out = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        links = extract_links(content if isinstance(content, str) else "")
        if not links:
            out.append(m)
            continue
        for link in links:
            out.extend(archive.resolve(link))
    return out


__all__ = [
    "DanglingLinkError",
    "LINK_PREFIX",
    "CompactionArchive",
    "cite_link",
    "extract_links",
    "rehydrate",
]
