"""Pre-load artifact scanning + memory write gate (issue #75).

Two verified attack surfaces, one kernel defense layer:

- memory-formation interfaces are an independent injection surface
  (arXiv 2610.14431): all memory writes through `memory_link` pass a
  tool-call-level gate; untrusted content is stored quoted/stamped with
  origin, never silently consolidated, never injected into a prompt.
- pre-load scanning of memory files and SKILL.md bodies: stdlib regex
  sets for prompt-injection and credential-exfiltration patterns,
  English-only findings. Flagged content is quarantined (not loaded)
  and an event is logged.

Scan cost: single pass, O(n) over the payload, ~15 compiled regexes —
well under 1 ms per 10 KB on Apple Silicon; the gate adds no I/O.
Ownership split per issue #68 ruling: the net-value ledger stays in
fusion-memory; this module is the kernel gate + scan interfaces only.
Zero model calls, pure stdlib.
"""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = (
    (
        "ignore_previous_instructions",
        re.compile(r"ignore (all |any )?(previous|prior|above) instructions", re.I),
    ),
    (
        "disregard_rules",
        re.compile(
            r"disregard (all |any |your )?(previous |prior |system |safety )?(rules|instructions|guidelines)",
            re.I,
        ),
    ),
    (
        "system_prompt_override",
        re.compile(r"(new|updated|revised|real) (system )?(prompt|instructions)\s*:", re.I),
    ),
    ("role_override", re.compile(r"you are now (a|an|the) ", re.I)),
    (
        "reveal_instructions",
        re.compile(
            r"(reveal|print|show|repeat|output|expose) (your|the) (system |hidden )?(prompt|instructions)",
            re.I,
        ),
    ),
    ("fake_delimiter_tag", re.compile(r"</sys>|<\|im_start\|>|<\|endoftext\|>", re.I)),
    (
        "exfiltrate_credentials",
        re.compile(
            r"(send|post|upload|exfiltrate|forward)[^.\n]{0,60}(api ?key|secret|token|credential|password)",
            re.I,
        ),
    ),
)

CREDENTIAL_PATTERNS = (
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_header", re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.I)),
    ("webhook_endpoint", re.compile(r"hooks\.slack\.com/services/|webhook\.site/")),
)

ALL_RULES = INJECTION_PATTERNS + CREDENTIAL_PATTERNS

# Issue #92: input-size cap before matching — with the fixed pattern set,
# scan cost is bounded O(MAX_SCAN_INPUT_BYTES) per artifact regardless of
# subject content (ReDoS defense alongside tests/test_regex_hardening.py).
MAX_SCAN_INPUT_BYTES = 262144


@dataclass
class ScanReport:
    clean: bool
    source: str = ""
    kind: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)

    def rule_names(self) -> List[str]:
        return sorted({f["rule"] for f in self.findings})


def scan_artifact(text: str, source: str = "", kind: str = "memory") -> ScanReport:
    """Single-pass regex scan of an artifact about to be loaded into a
    prompt or written to memory. Never raises; findings carry an English
    rule name + bounded excerpt for operator review. Issue #92: subjects
    over MAX_SCAN_INPUT_BYTES are truncated before matching (bounded
    scan cost) with a loud meta finding — oversize alone never silently
    skips the gate."""
    findings: List[Dict[str, Any]] = []
    if not text:
        return ScanReport(clean=True, source=source, kind=kind)
    if len(text) > MAX_SCAN_INPUT_BYTES:
        logger.warning(
            "artifact scan input truncated len=%d cap=%d source=%s",
            len(text),
            MAX_SCAN_INPUT_BYTES,
            source,
        )
        findings.append(
            {
                "rule": "input_truncated",
                "severity": "info",
                "excerpt": "subject exceeded scan cap; tail not scanned",
            }
        )
        text = text[:MAX_SCAN_INPUT_BYTES]
    for rule, pattern in ALL_RULES:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 20)
            excerpt = text[start : match.end() + 20].replace("\n", " ")
            findings.append({"rule": rule, "severity": "high", "excerpt": excerpt[:120]})
    if findings:
        logger.warning(
            "artifact scan flagged source=%s kind=%s rules=%s",
            source,
            kind,
            ",".join(f["rule"] for f in findings),
        )
    # Meta findings (severity=info) do not dirty the report — only real
    # rule hits make it unclean.
    clean = not any(f["severity"] != "info" for f in findings)
    return ScanReport(clean=clean, source=source, kind=kind, findings=findings)


class ArtifactQuarantine:
    """Persistent quarantine list for flagged artifacts — queryable for
    operator review, survives restarts, bounded."""

    def __init__(self, path: Optional[str] = None, max_entries: int = 500) -> None:
        self.path = path
        self.max_entries = max_entries
        self._entries: List[Dict[str, Any]] = []
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._entries = list(json.load(fh))
                logger.info("artifact quarantine restored entries=%d", len(self._entries))
            except (OSError, ValueError) as exc:
                logger.error("artifact quarantine load failed: %s — starting empty", exc)

    def add(self, report: ScanReport, preview: str = "") -> Dict[str, Any]:
        entry = {
            "id": "aq_%s" % uuid.uuid4().hex[:8],
            "ts": time.time(),
            "source": report.source,
            "kind": report.kind,
            "rules": report.rule_names(),
            "preview": (preview or "")[:200],
        }
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries :]
        if self.path:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._entries, fh, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError as exc:
                logger.error("artifact quarantine persist failed: %s", exc)
        logger.info(
            "artifact quarantined id=%s source=%s rules=%s",
            entry["id"],
            entry["source"],
            entry["rules"],
        )
        return entry

    def list_quarantined(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        if kind is None:
            return list(self._entries)
        return [e for e in self._entries if e.get("kind") == kind]

    def count(self) -> int:
        return len(self._entries)


def stamp_untrusted(payload: str, source: str, rules: List[str]) -> str:
    """Quote + stamp untrusted content with its origin so downstream
    consumers can never mistake it for trusted instructions."""
    return "[untrusted-memory origin=%s quarantined=true rules=%s]\n'''\n%s\n'''" % (
        source or "unknown",
        ",".join(rules) or "scan",
        payload,
    )
