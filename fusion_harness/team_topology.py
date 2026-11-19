"""Role-aware communication topology scheduling (issue #101).

Topology bank as data + per-step selector scoring role fit vs token cost.
Proposes the send set; budget funnels still dispose. No team_runtime edits
in this deliverable — wiring lands behind a flag in a follow-up PR.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_UNSEEN_MESSAGE_TOKENS = 64
DEFAULT_COST_WEIGHT = 0.001
DEFAULT_FIT_WEIGHT = 1.0


class TopologyError(ValueError):
    pass


def _chain_edges(roles: Dict[str, str]) -> List[Tuple[str, str]]:
    names = sorted(roles)
    return [(names[i], names[i + 1]) for i in range(len(names) - 1)]


def _star_edges(roles: Dict[str, str]) -> List[Tuple[str, str]]:
    lead = _pick_lead(roles)
    return [(lead, name) for name in sorted(roles) if name != lead]


def _mesh_edges(roles: Dict[str, str]) -> List[Tuple[str, str]]:
    names = sorted(roles)
    return [(a, b) for a in names for b in names if a != b]


def _role_filtered_edges(roles: Dict[str, str]) -> List[Tuple[str, str]]:
    names = sorted(roles)
    return [(a, b) for a in names for b in names if a != b and roles[a] == roles[b]]


def _lead_only_edges(roles: Dict[str, str]) -> List[Tuple[str, str]]:
    return []


def _pick_lead(roles: Dict[str, str]) -> str:
    for name in sorted(roles):
        if roles[name] == "lead":
            return name
    return sorted(roles)[0]


@dataclass(frozen=True)
class TopologySpec:
    name: str
    edges_fn: Callable[[Dict[str, str]], List[Tuple[str, str]]]
    max_fanout: int

    def edges(self, roles: Dict[str, str]) -> List[Tuple[str, str]]:
        if not roles:
            raise TopologyError("topology %s needs at least one member" % self.name)
        return self.edges_fn(roles)


def _fanout(edges: List[Tuple[str, str]], roles: Dict[str, str]) -> int:
    out_degree: Dict[str, int] = {name: 0 for name in roles}
    for src, _dst in edges:
        out_degree[src] = out_degree.get(src, 0) + 1
    return max(out_degree.values()) if out_degree else 0


def _build_bank() -> Dict[str, TopologySpec]:
    return {
        "chain": TopologySpec("chain", _chain_edges, max_fanout=2),
        "star": TopologySpec("star", _star_edges, max_fanout=64),
        "mesh": TopologySpec("mesh", _mesh_edges, max_fanout=256),
        "role_filtered": TopologySpec("role_filtered", _role_filtered_edges, max_fanout=256),
        "lead_only": TopologySpec("lead_only", _lead_only_edges, max_fanout=0),
    }


TOPOLOGY_BANK: Dict[str, TopologySpec] = _build_bank()


@dataclass
class TopologyTelemetry:
    steps: List[Dict[str, object]] = field(default_factory=list)

    def record_step(
        self,
        step: int,
        topology: str,
        edge_sends: Optional[Dict[Tuple[str, str], int]] = None,
        skipped_edges: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        entry: Dict[str, object] = {
            "step": step,
            "topology": topology,
            "edge_sends": {"%s->%s" % e: t for e, t in sorted((edge_sends or {}).items())},
            "skipped_edges": ["%s->%s" % e for e in sorted(skipped_edges or [])],
        }
        self.steps.append(entry)
        logger.info(
            "topology telemetry step=%d topology=%s sends=%d skipped=%d",
            step,
            topology,
            len(entry["edge_sends"]),
            len(entry["skipped_edges"]),
        )

    def summary(self) -> List[Dict[str, object]]:
        return list(self.steps)


class TopologySelector:
    def __init__(
        self,
        role_complements: Optional[Dict[str, Sequence[str]]] = None,
        fit_weight: float = DEFAULT_FIT_WEIGHT,
        cost_weight: float = DEFAULT_COST_WEIGHT,
        unseen_message_tokens: int = DEFAULT_UNSEEN_MESSAGE_TOKENS,
        bank: Optional[Dict[str, TopologySpec]] = None,
        telemetry: Optional[TopologyTelemetry] = None,
    ) -> None:
        self.role_complements = {role: set(comp) for role, comp in (role_complements or {}).items()}
        self.fit_weight = fit_weight
        self.cost_weight = cost_weight
        self.unseen_message_tokens = unseen_message_tokens
        self.bank = dict(bank) if bank is not None else dict(TOPOLOGY_BANK)
        self.telemetry = telemetry if telemetry is not None else TopologyTelemetry()
        self._last_size: Dict[str, int] = {}
        self._last_selected: Optional[str] = None
        self._step = 0
        logger.info("topology selector initialized bank=%s", ",".join(sorted(self.bank)))

    def observe(self, member: str, tokens: int) -> None:
        if tokens < 0:
            raise TopologyError("observed token count must be >= 0: %s" % tokens)
        self._last_size[member] = int(tokens)

    def _est_token_cost(self, edges: List[Tuple[str, str]]) -> int:
        total = 0
        for src, dst in edges:
            total += self._last_size.get(src, self.unseen_message_tokens)
            total += self._last_size.get(dst, self.unseen_message_tokens)
        return total

    def _role_fit(self, edges: List[Tuple[str, str]], roles: Dict[str, str]) -> float:
        if not edges:
            return 0.0
        if not self.role_complements:
            return 1.0
        hits = 0
        for src, dst in edges:
            complements = self.role_complements.get(roles[src], set())
            if roles[dst] in complements:
                hits += 1
        return hits / len(edges)

    def eligible(self, roles: Dict[str, str], remaining_budget: int) -> List[str]:
        if not roles:
            raise TopologyError("member roles required")
        if remaining_budget < 0:
            raise TopologyError("remaining budget must be >= 0: %s" % remaining_budget)
        eligible: List[str] = []
        for name in sorted(self.bank):
            spec = self.bank[name]
            try:
                edges = spec.edges(roles)
            except TopologyError:
                logger.warning("topology %s failed to build edges; skipping", name)
                continue
            fanout = _fanout(edges, roles)
            if fanout > spec.max_fanout:
                logger.debug(
                    "topology %s fanout %d exceeds max_fanout %d; filtered",
                    name,
                    fanout,
                    spec.max_fanout,
                )
                continue
            if self._est_token_cost(edges) > remaining_budget:
                logger.debug(
                    "topology %s est cost exceeds remaining budget %d; filtered",
                    name,
                    remaining_budget,
                )
                continue
            eligible.append(name)
        return eligible

    def select(
        self,
        roles: Dict[str, str],
        remaining_budget: int,
    ) -> Tuple[str, List[Tuple[str, str]]]:
        candidates = self.eligible(roles, remaining_budget)
        if not candidates:
            raise TopologyError(
                "no topology fits remaining budget %d for %d members"
                % (remaining_budget, len(roles))
            )
        best_name = candidates[0]
        best_score: Optional[float] = None
        for name in candidates:
            edges = self.bank[name].edges(roles)
            score = self.fit_weight * self._role_fit(edges, roles) - self.cost_weight * float(
                self._est_token_cost(edges)
            )
            if name == self._last_selected:
                score += 1e-9
            if best_score is None or score > best_score:
                best_score = score
                best_name = name
        if best_name != self._last_selected:
            logger.info("topology switch %s -> %s", self._last_selected, best_name)
        self._last_selected = best_name
        self._step += 1
        edges = self.bank[best_name].edges(roles)
        logger.info(
            "selected topology %s step=%d edges=%d budget=%d",
            best_name,
            self._step,
            len(edges),
            remaining_budget,
        )
        return best_name, edges

    def record_send(self, edge: Tuple[str, str], tokens: int) -> None:
        self.observe(edge[0], tokens)
        logger.debug("edge send %s->%s tokens=%d", edge[0], edge[1], tokens)

    def record_step(
        self,
        topology: str,
        edge_sends: Optional[Dict[Tuple[str, str], int]] = None,
        skipped_edges: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self.telemetry.record_step(self._step, topology, edge_sends, skipped_edges)
