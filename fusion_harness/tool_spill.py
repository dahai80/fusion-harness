"""Tool-output spill-then-grep (issue #94; PydanticAI ToolOutputLimits).

Second half of tool-output governance: after the funnel's truncation cap,
oversized tool results are written whole to the artifact store and the
in-context copy becomes a handle + short head/tail preview — the content
stays reachable without re-entering context wholesale. `grep` searches a
spilled artifact (handle-scoped, path-guarded) and `read` returns
byte-capped windows. Telemetry: spill count + bytes spilled per run.

Spilled artifacts live in the run-scoped artifact store on disk, so
handles survive checkpoint lineage forks (verified in tests). Zero model
calls, pure stdlib.
"""

import logging
import os
import re
from typing import Any, Dict, List

from fusion_harness.artifact_channel import ArtifactStore

logger = logging.getLogger(__name__)

DEFAULT_MAX_IN_CONTEXT_BYTES = 8192
DEFAULT_PREVIEW_BYTES = 400
DEFAULT_READ_CAP_BYTES = 2048
MAX_GREP_RESULTS = 50
_GREP_LINE_CAP = 4096


class SpillError(ValueError):
    """Invalid spill handle or read request."""


class ToolOutputSpill:
    def __init__(
        self,
        store: ArtifactStore,
        max_in_context_bytes: int = DEFAULT_MAX_IN_CONTEXT_BYTES,
        preview_bytes: int = DEFAULT_PREVIEW_BYTES,
    ) -> None:
        self.store = store
        self.max_in_context_bytes = max_in_context_bytes
        self.preview_bytes = preview_bytes
        # Run-scoped telemetry: {"run_id", "ref", "tool", "bytes"}
        self.spills: List[Dict[str, Any]] = []

    # ---------- spill ----------

    def maybe_spill(self, run_id: str, tool_name: str, output: Any) -> Dict[str, Any]:
        """Over-budget output becomes a handle + head/tail preview. Under
        budget output passes through untouched. Never raises on content —
        only the store write can fail, which is logged and degrades to
        pass-through (spilling must never lose a tool result)."""
        text = output if isinstance(output, str) else str(output)
        if len(text.encode("utf-8")) <= self.max_in_context_bytes:
            return {"spilled": False, "output": output}
        try:
            ref = self.store.write_artifact(run_id, "spill_%s" % _safe(tool_name), text)
        except OSError as exc:
            logger.warning(
                "spill write failed run=%s tool=%s: %s — passing through", run_id, tool_name, exc
            )
            return {"spilled": False, "output": output, "spill_error": str(exc)[:120]}
        preview = _preview(text, self.preview_bytes)
        entry = {
            "run_id": run_id,
            "ref": ref,
            "tool": tool_name,
            "bytes": len(text.encode("utf-8")),
        }
        self.spills.append(entry)
        logger.info(
            "tool output spilled run=%s tool=%s ref=%s bytes=%d",
            run_id,
            tool_name,
            ref,
            entry["bytes"],
        )
        return {
            "spilled": True,
            "ref": ref,
            "bytes": entry["bytes"],
            "head": preview["head"],
            "tail": preview["tail"],
        }

    # ---------- grep-back ----------

    def grep(self, ref: str, pattern: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """Handle-scoped pattern search over one spilled artifact. Returns
        line-numbered matches (bounded per line and in count)."""
        path = self._resolve(ref)
        if max_results > MAX_GREP_RESULTS:
            max_results = MAX_GREP_RESULTS
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise SpillError("invalid grep pattern: %s" % exc)
        matches: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line_no, line in enumerate(fh, 1):
                    if len(matches) >= max_results:
                        break
                    line = line.rstrip("\n")
                    if len(line) > _GREP_LINE_CAP:
                        line = line[:_GREP_LINE_CAP]
                    if compiled.search(line):
                        matches.append({"line": line_no, "text": line})
        except OSError as exc:
            raise SpillError("grep failed on %s: %s" % (ref, exc))
        logger.info("spill grep ref=%s pattern=%r hits=%d", ref, pattern[:40], len(matches))
        return matches

    def read(self, ref: str, offset: int = 0, max_bytes: int = DEFAULT_READ_CAP_BYTES) -> str:
        """Byte-capped window read; caps are enforced, not advisory."""
        path = self._resolve(ref)
        if max_bytes > DEFAULT_READ_CAP_BYTES:
            max_bytes = DEFAULT_READ_CAP_BYTES
        if max_bytes <= 0 or offset < 0:
            raise SpillError("invalid read window offset=%d max_bytes=%d" % (offset, max_bytes))
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(max_bytes)
        except OSError as exc:
            raise SpillError("read failed on %s: %s" % (ref, exc))
        return data.decode("utf-8", errors="replace")

    def run_telemetry(self, run_id: str = "") -> Dict[str, Any]:
        rows = [s for s in self.spills if not run_id or s["run_id"] == run_id]
        return {
            "spill_count": len(rows),
            "bytes_spilled": sum(s["bytes"] for s in rows),
            "refs": [s["ref"] for s in rows],
        }

    # ---------- safety ----------

    def _resolve(self, ref: str) -> str:
        root = os.path.realpath(self.store.root_dir)
        path = os.path.realpath(self.store.artifact_path(ref))
        if not path.startswith(root + os.sep):
            logger.warning("spill handle escapes store root, refused ref=%r", ref[:80])
            raise SpillError("handle does not belong to this store: %r" % ref[:80])
        if not os.path.isfile(path):
            raise SpillError("unknown spill handle: %r" % ref[:80])
        return path


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "tool")[:40]


def _preview(text: str, preview_bytes: int) -> Dict[str, str]:
    if preview_bytes <= 0:
        return {"head": "", "tail": ""}
    head = text[:preview_bytes]
    tail = text[-preview_bytes:] if len(text) > preview_bytes else ""
    return {"head": head, "tail": tail}


__all__ = [
    "DEFAULT_MAX_IN_CONTEXT_BYTES",
    "DEFAULT_READ_CAP_BYTES",
    "SpillError",
    "ToolOutputSpill",
]
