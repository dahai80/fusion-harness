# ADR 0001: Composition unit stays service-level; in-process everything-is-a-plugin is rejected

- Status: Accepted
- Date: 2026-10-28
- References: `architecture/fusion-vs-claude-all-insight-report-round2-1027.md` §4.5, §7 (round-2 benchmark report)

## Context

The round-2 harness benchmark compared Fusion against dsh-style architectures built
around in-process everything-is-a-plugin: a pluginized agent loop where the loop
itself, effect handlers (reversible-effects systems such as Cordis), approval, and
tool dispatch are all in-process plugins over a shared event log.

Fusion's composition unit is the **service**: supervisor orchestration
(`fusion-supervisor`), gateway routing (`fusion-gateway`), and the union-web manifest
registry (`fusion-web-shell`). Processes compose at their boundaries through JSON-RPC
over UDS, HTTP on loopback, and manifest registration — not by loading each other's
code into one address space.

## Decision

The composition unit stays service-level. In-process everything-is-a-plugin is
explicitly **rejected** for the Fusion ecosystem.

Rationale: pluginizing the agent loop in-process buys reversible effects and
runtime-swappable loop stages, but at the cost of a second composition system
coexisting with the existing service-level one — every capability must then be
expressed twice (as a service and as an in-process plugin), failure isolation
degrades (a plugin crash takes down the host process that the service boundary
currently contains), and the version-coupling surface widens from a wire contract
to internal Python APIs. Fusion already has process-level isolation, crash recovery,
and independent deployability from its service architecture; the in-process variant
duplicates those guarantees weaker inside a single process. The benefit/complexity
ratio is negative at Fusion's scale (issue #6, round-2 report §4.5, §7).

What Fusion **does** adopt from that architecture are three narrow disciplines,
implemented in this kernel rather than as a plugin system:

1. **Append-only session event log** — durable, replayable record of what the agent
   did (issue #4; see `LifecycleBus` for the live in-memory tier).
2. **Segmented tool pipeline** — explicit pre-execute / execute / post-execute
   stages with monotonic guards, instead of a monolithic tool-call path
   (issue #5; see `ToolFunnel`).
3. **Fail-closed approval** — an absent or broken permission gate denies by default,
   never allows (issue #1, extended by issue #5; see `PermissionEngine`).

These are kernel disciplines with a stable contract (`docs/contract.md`), not
plugins: consumers wire them in at construction time and hold them to the cross-repo
contract, keeping one composition system (services) instead of two.

## Consequences

- New harness capabilities land as modules with explicit constructor injection, not
  as a plugin registry inside this package.
- Consumers needing runtime extension compose at the service boundary (gateway
  routes, supervisor units, web-shell manifests), not by injecting loop plugins.
- The three adopted disciplines remain the only in-loop extension points this
  kernel commits to.
