"""file.read_segment tool contract + budget-funnel integration (issue #46).

Bounded file access is the harness's contract: agents never read a whole
attachment in one gulp. Reference implementation returns byte-range
segments (HTTP-Range parity) with a HARD cap enforced inside this layer —
an oversize request is clamped, never errored whole. Binary content is
base64 with an `encoding` flag. Every result carries a short first-line
header (`[segment file=... bytes=START-END/TOTAL]`) so compaction eviction
(keeps tool_result_head) retains citable offset metadata. Optional
per-turn attachment budget bounds total consumption per file independent
of call count; segment bytes also flow through the existing tool funnel
like any other tool output (no special casing downstream).
"""

import base64
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES_CAP = 256 * 1024


class SegmentError(ValueError):
    """Malformed segment request."""


@dataclass
class SegmentPolicy:
    max_bytes_cap: int = DEFAULT_MAX_BYTES_CAP
    per_turn_bytes: int = 0  # 0 = unlimited per attachment per turn

    def __post_init__(self) -> None:
        if self.max_bytes_cap <= 0 or self.per_turn_bytes < 0:
            raise SegmentError("cap and per_turn_bytes must be positive/non-negative")


class SegmentBudget:
    """Per-(turn, file_ref) byte ledger. admit() clamps the request to the
    remaining allowance; consumption is monotonic within a turn."""

    def __init__(self, policy: SegmentPolicy) -> None:
        self.policy = policy
        self._spent: dict = {}

    def admit(self, file_ref: str, requested: int, turn_id: str) -> int:
        allowance = self.policy.per_turn_bytes
        if allowance <= 0:
            return requested
        key = (turn_id, file_ref)
        remaining = allowance - self._spent.get(key, 0)
        allowed = max(0, min(requested, remaining))
        self._spent[key] = self._spent.get(key, 0) + allowed
        if allowed < requested:
            logger.info(
                "segment clamped by per-turn budget file=%s turn=%s requested=%d allowed=%d",
                file_ref,
                turn_id,
                requested,
                allowed,
            )
        return allowed

    def spent(self, file_ref: str, turn_id: str) -> int:
        return self._spent.get((turn_id, file_ref), 0)


def segment_header(file_ref: str, start: int, end: int, total: int) -> str:
    """Short citable first line; survives compaction eviction (tool_result_head)."""
    return "[segment file=%s bytes=%d-%d/%d]" % (file_ref, start, end, total)


def read_segment(
    file_ref: str,
    offset: int = 0,
    max_bytes: int = None,
    policy: SegmentPolicy = None,
    budget: SegmentBudget = None,
    turn_id: str = "",
) -> dict:
    """Read one bounded byte range. Canonical form is BYTES (HTTP Range
    parity — fusion-rag #156 serves the same shape). Oversize requests are
    clamped to the policy cap (and per-turn budget when wired), never
    errored whole. Missing file raises FileNotFoundError (fail-visible)."""
    policy = policy or SegmentPolicy()
    if not file_ref:
        raise SegmentError("file_ref is required")
    if offset < 0:
        raise SegmentError("offset must be >= 0, got %d" % offset)
    requested = policy.max_bytes_cap if max_bytes is None else int(max_bytes)
    if requested <= 0:
        raise SegmentError("max_bytes must be > 0, got %d" % requested)
    capped = min(requested, policy.max_bytes_cap)
    if capped < requested:
        logger.info(
            "segment clamped to cap file=%s requested=%d cap=%d",
            file_ref,
            requested,
            policy.max_bytes_cap,
        )
    if budget is not None:
        capped = budget.admit(file_ref, capped, turn_id)

    total = os.path.getsize(file_ref)
    if offset >= total:
        logger.info("segment past eof file=%s offset=%d total=%d", file_ref, offset, total)
        return _result(file_ref, offset, offset, total, "", "utf-8", 0, eof=True)

    if capped == 0:
        # Budget admitted zero bytes mid-file: an empty non-eof success would
        # invite a same-offset retry loop (issue #48) — return the explicit
        # exhausted marker instead.
        logger.warning(
            "segment budget exhausted file=%s offset=%d turn=%s", file_ref, offset, turn_id
        )
        out = _result(file_ref, offset, offset, total, "", "utf-8", 0, eof=False)
        out["budget_exhausted"] = True
        return out

    with open(file_ref, "rb") as fh:
        fh.seek(offset)
        data = fh.read(capped)

    kept, encoding = _decode_segment(data)
    text = kept.decode("utf-8") if encoding == "utf-8" else base64.b64encode(data).decode("ascii")
    end = offset + len(kept) if encoding == "utf-8" else offset + len(data)
    eof = end >= total
    logger.info(
        "segment read file=%s range=%d-%d/%d encoding=%s eof=%s",
        file_ref,
        offset,
        end,
        total,
        encoding,
        eof,
    )
    return _result(file_ref, offset, end, total, text, encoding, len(kept), eof)


def _decode_segment(data: bytes) -> tuple:
    """Decode a segment to text, holding back an incomplete multibyte UTF-8
    tail (up to 3 bytes) so the next segment carries it — a fixed byte range
    must not split a character and misclassify the whole segment as binary
    (issue #48). Returns (kept_bytes, encoding)."""
    try:
        data.decode("utf-8")
        return data, "utf-8"
    except UnicodeDecodeError:
        pass
    hold = 0
    n = len(data)
    while hold < 3 and n - hold > 0 and (data[n - hold - 1] & 0xC0) == 0x80:
        hold += 1
    if hold < 3 and n - hold > 0:
        lead = data[n - hold - 1]
        seq_len = _utf8_seq_len(lead)
        # Incomplete sequence: lead byte + held continuations < expected length.
        if seq_len > 1 and hold + 1 < seq_len:
            hold = min(3, hold + 1)
    kept = data[: n - hold] if hold else data
    try:
        kept.decode("utf-8")
        if hold:
            logger.info("segment utf-8 boundary trim held=%d bytes", hold)
        return kept, "utf-8"
    except UnicodeDecodeError:
        return data, "base64"


def _utf8_seq_len(lead: int) -> int:
    if lead & 0xE0 == 0xC0:
        return 2
    if lead & 0xF0 == 0xE0:
        return 3
    if lead & 0xF8 == 0xF0:
        return 4
    return 1


def _result(file_ref, start, end, total, content, encoding, nbytes, eof) -> dict:
    return {
        "file_ref": file_ref,
        "offset": start,
        "end_offset": end,
        "total": total,
        "next_offset": end,
        "eof": eof,
        "encoding": encoding,
        "content": content,
        "bytes": nbytes,
        "budget_exhausted": False,
        "header": segment_header(file_ref, start, end, total),
    }


def render_segment_result(result: dict) -> str:
    """Funnel-ready string form: citable header first line + content. Goes
    through ToolFunnel.run_tool like any tool output — compaction treats it
    as a plain tool message, and the header survives eviction in the head."""
    header = result.get("header") or segment_header(
        result.get("file_ref", "?"),
        result.get("offset", 0),
        result.get("end_offset", 0),
        result.get("total", 0),
    )
    suffix = (
        ""
        if result.get("eof")
        else "\n[truncated: continue at next_offset=%d]" % result.get("next_offset", 0)
    )
    if result.get("budget_exhausted"):
        suffix += "\n[budget exhausted for this turn: stop reading this file]"
    return "%s\n%s%s" % (header, result.get("content", ""), suffix)


__all__ = [
    "DEFAULT_MAX_BYTES_CAP",
    "SegmentBudget",
    "SegmentError",
    "SegmentPolicy",
    "read_segment",
    "render_segment_result",
    "segment_header",
]
