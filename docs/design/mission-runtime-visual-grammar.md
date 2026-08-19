# Mission runtime visual grammar

The operator console is a read-only view of the selected mission's public
`/api/runtime`, `/api/trace`, and loopback `/api/debug` evidence. Highlighting
means evidence was observed; it never grants authority to the browser.

## Architecture

The map shows real runtime modules: **Context Coordination**, **Bayesian Belief
Manager**, **Hyper Agent**, **PDDL Planner**, **MiniZinc Planner**, **Maneuver
Control**, **FSM Runner**, **Transport**, **Environment**, and **Mission
Summary** when its public artifact exists. There is no Skills & Advisory node,
tab, or edge. Advisory-shaped records remain trace records if present, but are
not represented as a fictional runtime component.

The primary directed paths are context to belief, belief to Hyper Agent, Hyper
Agent to the symbolic or temporal planner, planner to Maneuver Control,
Maneuver Control to FSM Runner, FSM Runner to transport, transport to the
environment, and environment back to context. A mission summary may feed Hyper
Agent. They are declared architecture, not observed activity.

Green flow lines require an explicit `parent_id` resolved to an observed source
record in the current replay window; no default path activates an edge. Amber
indicates the selected correlation. Every line is a keyboard-accessible button.
Selecting one separates its observed traversal evidence from its static declared
contract classes. An unlinked record remains visible on its mapped component but
does not imply a flow. Evidence rows use exact projection component/event-kind
mappings for declared types such as `TransportEvent`, `MissionSnapshot`,
`NormalizedPlan`, `FSMStatus`, or `BayesianBeliefSnapshot`; unknown records keep
their original event kind and are marked as unclassified rather than synthesized.

## Inspection and debug evidence

Selecting any component opens grouped durable input and output history from the
current trace window. PDDL Planner is populated only by symbolic/PDDL planning
evidence; MiniZinc Planner is populated only by temporal/MiniZinc evidence.
Either shows “No observed history” when that evidence does not exist.

Selecting Hyper Agent or Maneuver Control also adds its `/api/debug` API profile
and conversation inspector. Profiles list only reported tools and skills.
Conversations are ordered by debug role then sequence and visibly separate:

1. input or request;
2. provider reasoning;
3. output, content, and tool calls.

These debug values are shown raw only because they came from the same-origin
loopback debug endpoint. Missing debug evidence has an explicit empty state.
The inspector and its conversation list scroll independently so a long
reasoning record does not make the map unusable.

## Replay, selection, and responsive use

The mission selector is populated from public runtime mission IDs. Changing it
clears local replay and selection state before loading the new mission. Replay
controls change only the browser cursor. Selecting an event advances to that
observation and synchronizes its card, correlation highlight, and event
inspector. Mission records are never merged across missions.

Desktop places the diagram beside the inspector. On small screens the inspector
moves below the map and observed flow lines become compact clickable flow chips.
The fixed mobile map and bounded scrollers avoid global horizontal overflow.

Trace details remain allowlisted and bounded. Redacted fields are called out as
warnings; missing public fields are errors. The console cannot issue commands,
modify runtime state, invoke an LLM, or create summaries.
