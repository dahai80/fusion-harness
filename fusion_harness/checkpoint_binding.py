"""Canonical DAG-node checkpoint binding (issue #35, K2).

Promotes the cowork kernel_checkpoint wiring pattern into a kernel API:
per-node state persists in CheckpointStore, emitted side effects dedupe
through the OutboxStore receipt cache (committed effects never re-execute
on crash-recovery replay). Zero model calls.
"""

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from fusion_harness.checkpoint import CheckpointState, CheckpointStore
from fusion_harness.outbox import OutboxStore

logger = logging.getLogger(__name__)

_RECEIPT_AGG = "harness.node"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class NodeStateBinder:
    def __init__(self, store: CheckpointStore, outbox: OutboxStore, run_id: str) -> None:
        self.store = store
        self.outbox = outbox
        self.run_id = run_id

    def _session(self, node_id: str) -> str:
        return "%s:%s" % (self.run_id, node_id)

    def _receipt_key(self, node_id: str, event: str) -> str:
        return "%s:%s:%s" % (self.run_id, node_id, event)

    def save(self, node_id: str, state: Dict[str, Any]) -> int:
        snap = CheckpointState(messages=[], permission_mode="workflow")
        snap.metadata = {"node_state": _json_safe(state or {})}
        seq = self.store.save(self._session(node_id), snap)
        logger.info("node state saved run=%s node=%s seq=%d", self.run_id, node_id, seq)
        return seq

    def load(self, node_id: str) -> Optional[Dict[str, Any]]:
        snap = self.store.latest(self._session(node_id))
        if snap is None:
            return None
        state = snap.metadata.get("node_state")
        return dict(state) if isinstance(state, dict) else None

    def mark_emitted(
        self,
        node_id: str,
        event: str,
        executor: Optional[Callable[[], Any]] = None,
    ) -> Tuple[Any, bool]:
        """Idempotent side-effect marker: the executor runs at most once per
        (run_id, node_id, event); a replay returns the first recorded result
        with replayed=True. No executor means a pure marker receipt."""

        def _noop() -> None:
            return None

        result, replayed = self.outbox.record_command(
            self._receipt_key(node_id, event), _RECEIPT_AGG, executor or _noop
        )
        if replayed:
            logger.info("node effect replayed run=%s node=%s event=%s", self.run_id, node_id, event)
        return result, replayed

    def has_emitted(self, node_id: str, event: str) -> bool:
        return self.outbox.has_receipt(self._receipt_key(node_id, event))


def bind_node_state(store: CheckpointStore, outbox: OutboxStore, run_id: str) -> NodeStateBinder:
    return NodeStateBinder(store, outbox, run_id)


__all__ = ["NodeStateBinder", "bind_node_state"]
