"""Skill retrieval + prompt injection (issue #174, fusion-trainer#123).

Consumes the fusion-trainer skill-library contract (GUI-SkillEvo
pattern): load_skill_library (fail-visible reader) + render_skill_block
(the deterministic prompt-injection block with success_count/verifier
citations). Consume, don't fork — the block format stays single-sourced
in fusion_trainer.skills (lazy import; declared-free kernel keeps zero
pip dependencies, trainer is an optional runtime dep, missing = typed
error, fail visibly).

Deterministic retrieval (decide with code, not tokens):
1. exact/normalized task_id match first (skill.task_ids);
2. then tool-name overlap with the task's declared toolset;
3. both pools ordered by success_count desc, deduped, capped at
   max_skills — budgets are explicit, never silently exceeded.

Feedback loop: the consumer keeps the injected skill_ids in the run's
outcome sidecar (`used_skills_entry`) so the next mining round
strengthens each skill's success_count — closing the evolution loop.

Pure stdlib orchestration; zero model calls.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SkillRetrievalError",
    "SkillRetriever",
    "used_skills_entry",
]


class SkillRetrievalError(RuntimeError):
    """Skill library unreadable or fusion-trainer contract unavailable."""


def used_skills_entry(skill_ids: Sequence[str]) -> Dict[str, List[str]]:
    """Outcome sidecar record for skills actually followed in a
    successful run — merge into the run's outcome record so the next
    mining round strengthens those skills' success_count."""
    ids = [sid for sid in skill_ids if sid]
    return {"used_skills": ids}


class SkillRetriever:
    """Deterministic retrieval over a fusion-trainer skill library."""

    def __init__(
        self,
        library_path: str = "",
        library: Optional[List[Dict[str, Any]]] = None,
        max_skills: int = 5,
        max_chars: int = 2000,
        bus: Any = None,
    ) -> None:
        self._max_skills = max_skills
        self._max_chars = max_chars
        self._bus = bus
        if library is not None:
            self._library = library
        elif library_path:
            self._library = self._load(library_path)
        else:
            raise SkillRetrievalError("library_path or library is required")
        self._render = None

    @staticmethod
    def _load(path: str) -> List[Dict[str, Any]]:
        try:
            from fusion_trainer.skills import load_skill_library
        except ImportError as exc:
            raise SkillRetrievalError(
                "fusion_trainer.skills unavailable (consume contract, don't fork): %s" % exc
            ) from exc
        try:
            return load_skill_library(path)
        except Exception as exc:
            raise SkillRetrievalError("skill library unreadable at %s: %s" % (path, exc)) from exc

    def _renderer(self):
        if self._render is None:
            try:
                from fusion_trainer.skills import render_skill_block
            except ImportError as exc:
                raise SkillRetrievalError(
                    "fusion_trainer.skills unavailable (consume contract, don't fork): %s" % exc
                ) from exc
            self._render = render_skill_block
        return self._render

    def retrieve(self, task_id: str = "", toolset: Iterable[str] = ()) -> List[Dict[str, Any]]:
        """Task-id matches first, then tool-name overlap; each pool
        ordered by success_count desc; deduped; capped at max_skills."""
        normalized_task = (task_id or "").strip().lower()
        wanted_tools = {t for t in toolset if t}
        by_id: List[Dict[str, Any]] = []
        by_tools: List[Dict[str, Any]] = []
        for skill in self._library:
            skill_task_ids = {str(t).strip().lower() for t in skill.get("task_ids", []) if t}
            if normalized_task and normalized_task in skill_task_ids:
                by_id.append(skill)
            elif wanted_tools and wanted_tools & set(skill.get("tools", [])):
                by_tools.append(skill)
        by_id.sort(key=lambda s: s.get("success_count", 0), reverse=True)
        by_tools.sort(key=lambda s: s.get("success_count", 0), reverse=True)
        seen: set = set()
        picked: List[Dict[str, Any]] = []
        for skill in by_id + by_tools:
            key = skill.get("skill_id", "")
            if key in seen:
                continue
            seen.add(key)
            picked.append(skill)
            if len(picked) >= self._max_skills:
                break
        if picked:
            logger.info(
                "skills retrieved task_id=%s count=%d (id-matched=%d)",
                task_id,
                len(picked),
                len(by_id),
            )
        return picked

    def render_block(self, skills: List[Dict[str, Any]]) -> str:
        """Single-sourced block format from fusion_trainer.skills."""
        if not skills:
            return ""
        try:
            return self._renderer()(skills, max_skills=self._max_skills, max_chars=self._max_chars)
        except SkillRetrievalError:
            raise
        except Exception as exc:
            raise SkillRetrievalError("skill block render failed: %s" % exc) from exc

    def inject(self, task_id: str = "", toolset: Iterable[str] = ()) -> str:
        """Retrieve + render the injection block (empty string when no
        skill is relevant — never pad the prompt)."""
        try:
            skills = self.retrieve(task_id=task_id, toolset=toolset)
            block = self.render_block(skills)
        except Exception as exc:
            logger.warning("skill injection failed (empty block): %s", exc)
            self._emit("skill_injection_failed", {"task_id": task_id, "reason": str(exc)})
            return ""
        if block:
            self._emit(
                "skill_block_injected",
                {"task_id": task_id, "skill_ids": [s.get("skill_id", "") for s in skills]},
            )
        return block

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit("lifecycle", event, payload, session_id="")
        except Exception as exc:
            logger.warning("skill retrieval emit failed event=%s: %s", event, exc)
