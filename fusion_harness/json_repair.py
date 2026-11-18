"""Stdlib-only JSON-repair parse for LLM output (issue #67).

Consumers across cowork / agent-studio each grew a `_parse_json`/`_repair`
variant; this is the one shared kernel helper. Strict `json.loads` runs
first — repair passes only run on failure, in a fixed order, and every
repair is deterministic (zero model calls, stdlib imports only). Repair
failure raises JSONRepairError: no case may silently return wrong data.

Repair ladder (cheapest/most-safe first):
1. strip markdown fences (```json ... ``` / ``` ... ```)
2. extract the first balanced JSON object/array out of surrounding prose
3. single-quoted strings -> double-quoted (key + value positions only,
   outside already-double-quoted spans)
4. trailing commas removed
5. truncated-output best-effort: close unterminated strings, then close
   open brackets in reverse order
"""

import json
import logging
import re
from typing import Any, Union

logger = logging.getLogger(__name__)


class JSONRepairError(ValueError):
    """Input could not be parsed even after the full repair ladder."""


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SINGLE_QUOTE_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def _extract_balanced(text: str) -> Union[str, None]:
    """First balanced {...} or [...] span in the text (string-literal
    aware). Returns None when no balanced span exists."""
    start = None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and start is not None:
            in_str = True
            continue
        if ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    return text[start : i + 1]
    return None


def _single_to_double_quotes(text: str) -> str:
    """Rewrite single-quoted spans to double-quoted, escaping embedded
    double quotes. Spans inside existing double-quoted strings are left
    alone (string-aware scan)."""
    out = []
    in_dq = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_dq:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == '"':
            in_dq = True
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            match = _SINGLE_QUOTE_RE.match(text, i)
            if match:
                out.append('"%s"' % match.group(1).replace('"', '\\"'))
                i = match.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(lambda m: m.group(1), text)


def _close_truncated(text: str) -> str:
    """Best-effort completion of truncated JSON: close an unterminated
    string literal (dropping trailing escape backslashes), drop dangling
    trailing separators, then close open brackets in reverse nesting
    order (string-aware scan)."""
    chars = list(text)
    in_str = False
    escape = False
    stack = []
    for ch in chars:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str:
        while chars and chars[-1] == "\\":
            chars.pop()
        chars.append('"')
    trimmed = re.sub(r",\s*$", "", "".join(chars).rstrip())
    trimmed = trimmed.rstrip(": ")
    for opener in reversed(stack):
        trimmed += "}" if opener == "{" else "]"
    return trimmed


def _drop_incomplete_tail(text: str) -> str:
    """Cut a truncated document back to its last complete member boundary:
    after the last top-level comma, or to the first opener when no comma
    exists (dangling-key case like '{"a":')."""
    depth = 0
    in_str = False
    escape = False
    last_comma = -1
    first_opener = -1
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if first_opener < 0:
                first_opener = i
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth >= 1:
            last_comma = i
    if last_comma >= 0:
        return text[:last_comma]
    if first_opener >= 0:
        return text[: first_opener + 1]
    return text


def _take_open(text: str) -> str:
    """Slice from the first JSON opener ({ or [) to the end — used for
    prose-wrapped truncated output where no balanced span exists."""
    for i, ch in enumerate(text):
        if ch in "{[":
            return text[i:]
    return text


def _is_truncated(text: str) -> bool:
    """String-aware scan: true when the document ends inside a string
    literal or with unclosed brackets — i.e. truncated-output repair rungs
    are legitimate. Guards against the truncated rungs 'repairing'
    well-formed-but-quirky input into wrong data."""
    in_str = False
    escape = False
    depth = 0
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
    return in_str or depth > 0


def parse_repair(text: str) -> Any:
    """Parse JSON with deterministic repair for common LLM failure modes.
    Strict json.loads first; on failure the repair ladder runs in a fixed
    order. Raises JSONRepairError when every rung fails — never returns
    wrong data silently."""
    if not isinstance(text, str):
        raise JSONRepairError("input must be str, got %s" % type(text).__name__)
    try:
        return json.loads(text)
    except ValueError:
        pass
    candidates = []
    fenced = _strip_fences(text)
    bases = [fenced]
    extracted = _extract_balanced(fenced)
    if extracted is not None:
        bases.append(extracted)
    for base in bases:
        quoted = _single_to_double_quotes(base)
        for variant in (base, quoted):
            candidates.append(variant)
            candidates.append(_strip_trailing_commas(variant))
        # Truncated best-effort rungs only when the variant actually looks
        # truncated — they must never rewrite well-formed input.
        if _is_truncated(variant):
            for variant2 in (base, quoted):
                candidates.append(_close_truncated(_take_open(variant2)))
                candidates.append(_close_truncated(_drop_incomplete_tail(variant2)))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    logger.warning("json repair exhausted ladder input=%r", text[:120])
    raise JSONRepairError("unparseable JSON after full repair ladder: %s" % text[:120])


__all__ = ["JSONRepairError", "parse_repair"]
