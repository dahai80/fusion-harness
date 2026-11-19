---
doc_id: fusion-harness/design/issue-97
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "设计文档 issue 97", en: "Tool-layer provenance and capability tags (CaMeL)" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - docs/design/provenance-tags-97.md
---

# Design: Tool-Layer Provenance / Capability Tags (issue #97)

CaMeL-style taint tracking (arXiv:2503.18813). Status: design only —
implementation starts after review.

## Problem

artifact_scan (v0.22.0) scans artifacts *pre-load*. Tool results carry no
provenance from birth, so the kernel cannot tell control-flow-relevant
data from untrusted data at the tool layer.

## Design

### 1. Provenance tag on tool results

Every result crossing `ToolFunnel.run_tool()` gets a sidecar tag, attached
O(1) (metadata only, no content scanning at runtime):

    Provenance(
        source_tool="fetch_url",
        origin="external" | "user_input" | "derived",
        taints=frozenset({"external_content", "contains_private_data"?}),
        ts=..., call_id=...
    )

- `origin=user_input`: argument text the user typed (allowlist of arg keys
  supplied by the host).
- `origin=external`: content fetched/executed outside the kernel.
- `contains_private_data` capability granted only by hosts (never inferred
  by scanning — inference is the pre-load scanner's job).

### 2. Propagation

- Tool result → downstream tool call args: if an argument string is
  derived from a tainted result (host supplies the derivation map, or the
  kernel tracks exact-substring matches it produced), the call inherits
  the union of taints. O(1) per message via handles, not content.
- artifact_channel handles carry tags: `ArtifactStore.write_artifact`
  returns refs; the spill/spill-back layer (tool_spill.py) keeps the tag
  next to the ref so grep-back reads restore taint.

### 3. Policy hooks on outbound calls

Before execution, a tainted call is checked against capability policy:

- taint `external_content` + tool in EXFIL_CAPABLE set (network tools) →
  require `human_gate` approval (reuse ApprovalGate flow, requester="host")
  or a host-granted capability clearance.
- Unknown-taint args → default deny for high-risk nodes (monotonic with
  the permission engine: taint check runs AFTER permission admit, it can
  only narrow).

### 4. Kernel boundary

The kernel tags, propagates and gates. It never inspects network
destinations beyond what permissions.py already does. Consumers (cowork,
agent-studio) supply the derivation map and capability grants.

## Deliverable plan

1. `provenance.py`: Provenance record, tag/propagate helpers, policy hook
   point in ToolFunnel (opt-in, default off — zero behavior change).
2. Tests: tag birth, propagation through handles, exfil-capable gating,
   O(1) perf assertion (no content scan in the tag path).
3. Contract section after the behavior lands.

## Open questions for review

- Exact-substring derivation tracking (cheap, incomplete) vs host-supplied
  dataflow map (complete, more integration work) — start with the former?
- Should `model_fallback_required`-style events carry taint for observability?
