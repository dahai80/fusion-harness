"""ReDoS hardening for scanning/redaction regex surfaces (issue #92).

PydanticAI shipped two HIGH ReDoS advisories (GHSA-m4cr-m4qc-9hh3,
GHSA-mfx5-pc32-32m8) in exactly this layer: quadratic/exponential
backtracking in secret-redaction and ANSI-strip regexes froze the event
loop. The stdlib `re` engine has no per-match timeout, so the defense
here is input- and pattern-size caps plus a compile cache:

- every guard_search() call caps the subject at MAX_INPUT_LEN before
  matching — cost is bounded by O(MAX_INPUT_LEN) for any sane pattern;
- host-supplied patterns (hook matchers, permission rules, confirmation
  overrides) are capped at MAX_PATTERN_LEN and compile-checked once —
  an invalid or oversized pattern matches nothing and logs loudly
  instead of raising mid-fire;
- never raises: a guard failure denies by not matching (fail-closed for
  deny-scan paths is the caller's concern; here a guard error simply
  logs and returns False).

Known limitation, stated plainly: a short but catastrophic pattern from
trusted host config can still backtrack — caps bound the blast radius,
they do not replace RE2. All kernel-owned pattern tables are proven
backtrack-safe by tests/test_regex_hardening.py fuzz cases.
"""

import logging
import re
import threading

logger = logging.getLogger(__name__)

MAX_PATTERN_LEN = 256
MAX_INPUT_LEN = 262144

_cache_lock = threading.Lock()
_cache: dict = {}


def _compile(pattern: str, flags: int):
    key = (pattern, flags)
    with _cache_lock:
        compiled = _cache.get(key)
    if compiled is not None:
        return compiled
    if len(pattern) > MAX_PATTERN_LEN:
        logger.warning(
            "regex guard rejected oversized pattern len=%d cap=%d",
            len(pattern),
            MAX_PATTERN_LEN,
        )
        compiled = None
    else:
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            logger.warning("regex guard rejected invalid pattern %r: %s", pattern[:60], exc)
            compiled = None
    with _cache_lock:
        if len(_cache) < 4096:
            _cache[key] = compiled
    return compiled


def guard_search(pattern: str, text: str, flags: int = 0) -> bool:
    """Cap-guarded re.search — returns False on invalid/oversized pattern,
    truncates oversized subjects, never raises."""
    compiled = _compile(pattern, flags)
    if compiled is None:
        return False
    if text is None:
        return False
    if len(text) > MAX_INPUT_LEN:
        logger.info(
            "regex guard truncated subject len=%d cap=%d pattern=%r",
            len(text),
            MAX_INPUT_LEN,
            pattern[:40],
        )
        text = text[:MAX_INPUT_LEN]
    try:
        return compiled.search(text) is not None
    except Exception as exc:
        logger.warning("regex guard match failed pattern=%r: %s", pattern[:60], exc)
        return False


__all__ = ["MAX_INPUT_LEN", "MAX_PATTERN_LEN", "guard_search"]
