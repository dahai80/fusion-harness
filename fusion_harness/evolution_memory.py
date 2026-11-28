"""Evolution memory (issue #185): SIM-shaped evolution records for
harness-level recursive self-improvement (arXiv 2611.18856).

Every self-modification lands as one structured record: rationale,
predicted outcomes, observed outcomes, and the evaluation context the
verdict was made in. Records are append-only after a keep/rollback
verdict (the gate), persist as JSONL (corrupt lines skipped loudly),
and summarize_for() renders the decisive lessons for the next optimizer
round — grouped per component, never padding across components.

Privacy rule: records carry metric deltas and dataset NAMES only —
never prompts, completions, or dataset content. Zero model calls.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VERDICTS = ("keep", "rollback")


class EvolutionMemoryError(ValueError):
    """Record violates the evolution-memory schema or write gate."""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class EvolutionRecord:
    component: str
    rationale: str
    predicted_outcomes: List[str]
    observed_outcomes: List[str]
    verdict: str
    eval_context: Dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    change_id: str = ""
    ts: int = 0

    def __post_init__(self) -> None:
        if not self.component or not self.component.strip():
            raise EvolutionMemoryError("component required")
        if not self.rationale or not self.rationale.strip():
            raise EvolutionMemoryError("rationale required")
        if not str(self.predicted_outcomes or "").strip() and not self.predicted_outcomes:
            raise EvolutionMemoryError("predicted outcomes required")
        if not str(self.observed_outcomes or "").strip() and not self.observed_outcomes:
            raise EvolutionMemoryError("observed outcomes required")
        if self.verdict not in VERDICTS:
            raise EvolutionMemoryError(
                "verdict must be decided (keep|rollback), got %r" % (self.verdict,)
            )
        self.ts = self.ts or _now_ms()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class EvolutionMemory:
    """Append-only evolution log with SIM semantics: records land only
    after a verdict, summaries group per component, rollback records
    stay as lessons (what failed and why is as valuable as what worked)."""

    def __init__(
        self, path: Optional[str] = None, clock: Optional[Any] = None
    ) -> None:
        self.path = path
        self._clock = clock or time.time
        self._records: List[EvolutionRecord] = []
        if path:
            self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self._records.append(EvolutionRecord(**data))
                except (json.JSONDecodeError, TypeError, EvolutionMemoryError) as exc:
                    logger.error("evolution memory: skipping corrupt record: %s", exc)
        logger.info("evolution memory loaded path=%s records=%d", self.path, len(self._records))

    def append(self, record: EvolutionRecord) -> EvolutionRecord:
        if not record.change_id:
            record.change_id = "evo-%04d" % (len(self._records) + 1)
        self._records.append(record)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(record.to_json() + "\n")
        logger.info(
            "evolution record accepted id=%s component=%s verdict=%s",
            record.change_id,
            record.component,
            record.verdict,
        )
        return record

    def query(
        self,
        component: Optional[str] = None,
        verdict: Optional[str] = None,
        limit: int = 50,
    ) -> List[EvolutionRecord]:
        picked = [
            r
            for r in self._records
            if (component is None or r.component == component)
            and (verdict is None or r.verdict == verdict)
        ]
        return list(reversed(picked))[:limit]

    def summarize_for(self, component: str, limit: int = 5) -> str:
        """Optimizer-facing digest for one component. Empty string when
        nothing recorded — never pads. Rollback records render as
        explicit avoid-lessons with their verdict visible."""
        picked = [r for r in self._records if r.component == component]
        if not picked:
            return ""
        lines = []
        for r in picked[-limit:]:
            observed = "; ".join(r.observed_outcomes)
            lines.append(
                "- [%s] %s (change_id=%s, observed: %s)"
                % (r.verdict, r.rationale, r.change_id, observed)
            )
        header = "evolution lessons for %s (%d records):" % (component, len(picked))
        return header + "\n" + "\n".join(lines)

    def counts(self) -> Dict[str, int]:
        out = {v: 0 for v in VERDICTS}
        for r in self._records:
            out[r.verdict] = out.get(r.verdict, 0) + 1
        return out

    def __len__(self) -> int:
        return len(self._records)


__all__ = ["EvolutionMemory", "EvolutionMemoryError", "EvolutionRecord", "VERDICTS"]
