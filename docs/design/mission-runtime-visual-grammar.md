# Mission runtime visual grammar

The operator console at `src/onr/viewer/web/index.html` is a read-only view of
the public runtime evidence returned by `/api/runtime` and
`/api/trace?mission_id=<id>`. Its visual grammar distinguishes observation from
authority: a highlighted component means a validated public record was
observed; it does not transfer authority to the Web or to the viewer projection.

## Components and authority

The architecture map uses eight component nodes. Hyper Agent represents frozen
mission authority and replan evaluation. Context Coordination represents public
context assembly. Maneuver Control represents bounded decision authority. The
Snapshot and FSM Runner nodes separate derived snapshot evidence from durable
state-machine evidence. Skills is advisory only. Command and outcome records
show issued and terminal command evidence. Feedback represents authoritative
environment lifecycle facts and other validated feedback paths.

The event inspector always shows the projected `component` and `authority`
through its component copy and Authority field. These identities are assigned
by `TraceProjection`, not accepted from arbitrary source payloads. Green focus
indicates observed evidence, amber indicates the current view or correlation,
and red safety notes identify missing public evidence. None of these cues claim
that a summary, advisory record, or viewer state is mission authority.

## Observed flows

Edges are hidden until the active mission has a public observation that maps to
that flow. The browser derives an edge from validated parent relationships when
possible and otherwise uses the component's documented default path. Green
animated edges and mobile flow chips therefore mean "observed in the current
replay window," never "configured," "expected," or "commanded." Amber edges
show records related to the selected correlation. Idle architecture has no
active edge, chip, replay animation, event strip, or summary section.

## Mission overview and drill-downs

Mission overview shows every projected public record for the selected mission,
the architecture map, replay controls, event order, and the latest public
non-authoritative summary with bounded history.

The seven tabs have fixed semantics:

1. **Mission overview** shows the complete selected-mission observation stream.
2. **Hyper Agent** shows Hyper Agent and coordinated public-context records,
   including validated replan requests.
3. **Maneuver Control** shows maneuver decisions and relevant FSM transition
   evidence.
4. **Snapshot & FSM** shows snapshots, coordinated context, statecharts,
   execution records, and FSM status.
5. **Skills & advisory** shows only advisory skill/context observations.
6. **Command & outcome** shows commands, transport receipts, outcomes, and
   command-routed operational evidence.
7. **Feedback paths** shows validated environment and lifecycle feedback.

Changing tabs filters the architecture focus and event strip together. A
selection that does not belong to the next drill-down is cleared rather than
shown out of context.

## Mission, replay, and correlation

The mission selector is populated from the active runtime's public
`mission_ids`. Selecting a mission clears the prior replay cursor, event
selection, and correlation before requesting that mission's trace. Records from
different missions are never merged.

The horizontal event strip is the projected observation order. Play/pause,
restart, speed, and the scrubber change only the local replay cursor. Selecting
an event advances the cursor to that observation and synchronizes the event
card, component focus, inspector, and correlated edges. Correlation uses only
public correlation and parent identifiers. "Clear correlation" removes the
related highlight without altering runtime artifacts.

## Idle and active states

Idle mode retains the architecture for inspection but labels the runtime idle
and hides observations, summaries, replay controls, and active flows. Active
mode requires a current runtime lease, displays only missions discovered from
validated public artifacts, and polls the same-origin read-only endpoints.
Unavailable or stale runtime state does not preserve a previous mission trace
as if it were live.

## Summary lifecycle

The runtime, not the Web, periodically creates one mission-level
`SummaryArtifact` from incremental operational-log records. Atomic files under
`storage.root/summaries/<mission>/` are non-authoritative digests. Mission
overview displays the newest summary, its input range, and up to eight prior
summary observations. Selecting history synchronizes it with replay and the
inspector. Missing summary data displays an unavailable state without inventing
content. A summary never replaces component source-of-truth records.

## Safety and replay states

The inspector renders only the browser allowlist of identity, status, bounded
payload, and evidence fields. Redacted fields appear as amber safety notes;
missing fields appear in red. Malformed evidence uses fixed viewer diagnostics
without source strings. Replay disposition distinguishes normal, duplicate,
replayed, stale, gap, resynchronized, conflict, and malformed observations.
These states remain evidence about delivery and projection, separate from the
business `status` and `outcome` fields.

## Responsive behavior

Desktop uses the architecture map beside the inspector. At narrower widths the
inspector moves below the map and replay controls stack. Mobile keeps the same
eight components in a fixed-height map, replaces hidden geometric edges with
observed flow chips, wraps header and replay controls, and keeps tabs, events,
and summary history as bounded horizontal scrollers. Global horizontal overflow
is not part of the grammar; long public values wrap or truncate inside their
owned surfaces.

The Web cannot issue commands, mutate runtime state, start or stop leases,
invoke an LLM, or request summarization. It can only poll the two same-origin
GET endpoints and change local presentation state.
