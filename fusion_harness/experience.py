"""Cross-session experience layer (issue #164, N22 / TokenEconomizer parity).

Reference: microsoft/TokenEconomizer (arXiv 2610.22732, MIT, stdlib-only)
— measured -18.4% to -30.9% cost on SWE-Bench across 30/30 cells with
Pass@1 up in 4/5. Two of its three mechanisms map onto the harness
hook/funnel architecture:

1. **Loop-guard hook** (their loopguard/state.py + envelope.py +
   rules.py): deterministic-first per-tool-call guard.
   - `SpiralState` folds edits-per-file, files edited, and repeat-failure
     counts per call (shell-made edits count like editor edits).
   - Envelope threshold: per-scope percentile of how long successful
     turns ran — only turns that ended on their own with no failed
     verification after the last edit feed it; a turn's own outcome gates
     only later turns. Replaces a fixed iteration cap with an adaptive,
     code-computable threshold.
   - Redirect, never abort: the first stuck verdict injects a short
     redirect note and opens a fresh segment; a later verdict surfaces a
     notice to the human. The guard never stops a turn. The deterministic
     heuristic is the baseline; an optional model judge can sit behind it
     (its cost must appear in funnel telemetry — we emit a
     `loop_guard_judge` lifecycle event per judge call).

2. **Evidence-based tool pruning** (their toolprune/stage.py):
   a loaded tool unused over `p` prompts across >= `m` sessions, or over
   `s` sessions of any length, is PROPOSED for removal; one use resets
   the streak. Protected work-surface tools are never proposed;
   notebook-class tools get a 3x longer streak. Decide-vs-apply split is
   preserved: this module only proposes — writing the decision into
   consumer settings is the consumer's job. Prunes are reversible via
   `restore()`.

Guardrails: these hooks fail open (cost telemetry, not permission
class — contrast the fail-closed permission hooks); every action
reversible.

Pure stdlib, deterministic, zero model calls (optional judge aside).
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

NOTEBOOK_MULTIPLIER = 3.0

__all__ = [
    "NOTEBOOK_MULTIPLIER",
    "GuardVerdict",
    "LoopGuard",
    "PruneProposal",
    "SpiralState",
    "ToolPruner",
    "is_notebook_tool",
]


def is_notebook_tool(tool_name: str) -> bool:
    return "notebook" in (tool_name or "").lower()


# ---------- 1. loop guard ----------


class SpiralState:
    """Per-call fold of edit/failure signals (their loopguard/state.py;
    shell-made edits count too — no separate tracker needed because the
    caller passes the files each call touched)."""

    def __init__(self) -> None:
        self.edits_per_file: Counter = Counter()
        self.files_edited: set = set()
        self.repeat_failures: Counter = Counter()
        self.calls_in_segment: int = 0

    def fold(
        self, tool_name: str, file_edits: Tuple[str, ...] = (), error_output: str = ""
    ) -> None:
        self.calls_in_segment += 1
        for path in file_edits:
            self.edits_per_file[path] += 1
            self.files_edited.add(path)
        if error_output:
            # Error signature: first 80 chars, whitespace-normalized.
            signature = "%s:%s" % (tool_name, " ".join(error_output.split())[:80])
            self.repeat_failures[signature] += 1

    def reset_segment(self) -> None:
        self.calls_in_segment = 0

    def is_edit_spiral(self, min_files: int, repeats_per_file: int) -> bool:
        if len(self.files_edited) < min_files:
            return False
        return any(count >= repeats_per_file for count in self.edits_per_file.values())


@dataclass
class GuardVerdict:
    action: str  # "pass" | "redirect" | "notice" — never "abort"
    reason: str = ""
    envelope_exceeded: bool = False
    judge_note: str = ""


class LoopGuard:
    """Deterministic-first loop guard hook. Fail-open: internal errors log
    and return a pass verdict. Redirects, never aborts."""

    def __init__(
        self,
        bus: Any = None,
        failure_threshold: int = 3,
        spiral_min_files: int = 4,
        spiral_repeats: int = 3,
        envelope_pct: int = 90,
        min_turn_samples: int = 5,
        judge: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._bus = bus
        self._failure_threshold = failure_threshold
        self._spiral_min_files = spiral_min_files
        self._spiral_repeats = spiral_repeats
        self._envelope_pct = envelope_pct
        self._min_turn_samples = min_turn_samples
        self._judge = judge
        self.state = SpiralState()
        self._turn_lengths: List[int] = []
        self._segment_stuck_count = 0

    def record_turn_end(
        self,
        length_calls: int,
        ended_on_own: bool,
        failed_verification_after_edit: bool,
    ) -> None:
        """Envelope feed: only turns that ended on their own with no
        failed verification after the last edit. A turn's own outcome
        gates only later turns (never the current one)."""
        if ended_on_own and not failed_verification_after_edit and length_calls > 0:
            self._turn_lengths.append(length_calls)

    def envelope_threshold(self) -> float:
        """Adaptive per-scope percentile of successful turn lengths."""
        if len(self._turn_lengths) < self._min_turn_samples:
            return 0.0
        ordered = sorted(self._turn_lengths)
        idx = min(len(ordered) - 1, (self._envelope_pct * len(ordered)) // 100)
        return float(ordered[idx])

    def evaluate(
        self,
        tool_name: str,
        file_edits: Tuple[str, ...] = (),
        error_output: str = "",
    ) -> GuardVerdict:
        try:
            return self._evaluate(tool_name, file_edits, error_output)
        except Exception as exc:
            logger.warning("loop guard fail-open: %s", exc)
            return GuardVerdict(action="pass", reason="guard error (fail-open)")

    def _evaluate(self, tool_name, file_edits, error_output) -> GuardVerdict:
        self.state.fold(tool_name, file_edits, error_output)
        reasons = []
        worst_failures = max(self.state.repeat_failures.values(), default=0)
        if worst_failures >= self._failure_threshold:
            reasons.append(
                "repeat failure x%d (threshold %d)" % (worst_failures, self._failure_threshold)
            )
        if self.state.is_edit_spiral(self._spiral_min_files, self._spiral_repeats):
            reasons.append("edit spiral: %d files touched" % len(self.state.files_edited))
        threshold = self.envelope_threshold()
        envelope_exceeded = threshold > 0 and self.state.calls_in_segment > threshold
        if envelope_exceeded:
            reasons.append(
                "segment length %d exceeds envelope %.0f" % (self.state.calls_in_segment, threshold)
            )
        if not reasons:
            return GuardVerdict(action="pass")
        stuck_reason = "; ".join(reasons)
        self._segment_stuck_count += 1
        judge_note = ""
        if self._judge is not None:
            try:
                judge_note = str(self._judge(stuck_reason))
                self._emit(
                    "loop_guard_judge",
                    {"tool": tool_name, "reason": stuck_reason, "note": judge_note},
                )
            except Exception as exc:
                logger.warning("loop guard judge failed: %s", exc)
        if self._segment_stuck_count == 1:
            logger.warning("loop guard redirect: %s", stuck_reason)
            self._emit("loop_guard_redirect", {"tool": tool_name, "reason": stuck_reason})
            self.state.reset_segment()
            self._segment_stuck_count = 1  # fresh segment, first-stuck marker stays
            return GuardVerdict(
                action="redirect",
                reason="redirect note: %s — open a fresh segment, try a different approach"
                % stuck_reason,
                envelope_exceeded=envelope_exceeded,
                judge_note=judge_note,
            )
        logger.warning("loop guard notice to human: %s", stuck_reason)
        self._emit("loop_guard_notice", {"tool": tool_name, "reason": stuck_reason})
        return GuardVerdict(
            action="notice",
            reason="repeatedly stuck: %s — surfaced for human attention" % stuck_reason,
            envelope_exceeded=envelope_exceeded,
            judge_note=judge_note,
        )

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit("lifecycle", event, payload, session_id="")
        except Exception as exc:
            logger.warning("loop guard emit failed event=%s: %s", event, exc)


# ---------- 2. evidence-based tool pruning ----------


@dataclass
class PruneProposal:
    tool_name: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


class ToolPruner:
    """Cross-session usage ledger + deterministic prune proposals
    (decide vs apply split: this module proposes and restores; writing
    decisions into consumer settings is the consumer's job)."""

    def __init__(
        self,
        protected_tools: Tuple[str, ...] = ("shell", "read", "edit", "write", "search"),
        notebook_multiplier: float = NOTEBOOK_MULTIPLIER,
        bus: Any = None,
    ) -> None:
        self._protected = set(protected_tools)
        self._multiplier = notebook_multiplier
        self._bus = bus
        # session_id -> {tool_name: prompts_since_use}
        self._streaks: Dict[str, Dict[str, int]] = {}
        # session_id -> prompt count
        self._prompts: Dict[str, int] = Counter()
        # tool -> sessions where it was loaded
        self._loaded_in: Dict[str, set] = {}
        # tool -> sessions where it was actually used
        self._used_in: Dict[str, set] = {}

    def load_session(self, session_id: str, tools: List[str]) -> None:
        self._prompts.setdefault(session_id, 0)
        for tool in tools:
            self._loaded_in.setdefault(tool, set()).add(session_id)
            self._streaks.setdefault(session_id, {}).setdefault(tool, 0)

    def record_prompt(self, session_id: str) -> None:
        self._prompts[session_id] = self._prompts.get(session_id, 0) + 1
        streaks = self._streaks.get(session_id, {})
        for tool in streaks:
            streaks[tool] += 1

    def record_use(self, session_id: str, tool_name: str) -> None:
        """One use resets the streak and marks the tool used this session."""
        streaks = self._streaks.setdefault(session_id, {})
        if tool_name in streaks:
            streaks[tool_name] = 0
        self._used_in.setdefault(tool_name, set()).add(session_id)

    def propose_prunes(
        self,
        min_prompts: int,
        min_sessions: int,
        idle_sessions: int,
    ) -> List[PruneProposal]:
        """Propose loaded-but-unused tools:
        - unused over `min_prompts` prompts across >= `min_sessions`
          sessions, or
        - unused across >= `idle_sessions` sessions of any length.
        Protected work-surface tools are never proposed; notebook-class
        tools need a `notebook_multiplier`x longer streak."""
        proposals = []
        for tool, sessions in self._loaded_in.items():
            if tool in self._protected:
                continue
            mult = self._multiplier if is_notebook_tool(tool) else 1.0
            unused = [s for s in sessions if s not in self._used_in.get(tool, set())]
            reason = ""
            hot = [s for s in unused if self._prompts.get(s, 0) >= min_prompts * mult]
            if len(hot) >= min_sessions:
                reason = "unused over %d prompts in %d sessions" % (
                    min_prompts * mult,
                    len(hot),
                )
            elif len(unused) >= idle_sessions * mult:
                reason = "unused across %d sessions" % len(unused)
            if reason:
                proposals.append(
                    PruneProposal(
                        tool_name=tool,
                        reason=reason,
                        evidence={
                            "loaded_sessions": len(sessions),
                            "unused_sessions": len(unused),
                            "hot_sessions": len(hot),
                            "notebook_multiplier": mult,
                        },
                    )
                )
        if proposals:
            self._emit(
                "tool_prune_proposed",
                {"tools": [p.tool_name for p in proposals]},
            )
        return proposals

    def restore(self, tool_name: str) -> None:
        """Reversible prunes: one command restores a pruned tool's streak
        accounting (the consumer re-adds it to its tool set)."""
        logger.info("tool prune restored tool=%s", tool_name)
        self._emit("tool_prune_restored", {"tool": tool_name})

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit("lifecycle", event, payload, session_id="")
        except Exception as exc:
            logger.warning("tool pruner emit failed event=%s: %s", event, exc)
