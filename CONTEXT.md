# Autonomous Mission Control

This context describes a system that turns operational missions into plans and real-time maneuver decisions. It distinguishes planning authority, maneuver control, and externally authoritative mission state.

## Language

**Mission**:
A bounded operational objective with its participants, constraints, and desired outcome.
_Avoid_: Task, job

**Hyper Agent**:
The planning authority that interprets a Mission, chooses a planner, and owns plan revision.
_Avoid_: Main agent, controller

**Maneuver Control Agent**:
The decision-making authority that uses Mission Snapshot and FSM status to select transitions, maneuvers, escalation, or status.
_Avoid_: Subagent, flight controller

**Context Coordination**:
The service that assembles and publishes versioned Mission Snapshots from authoritative mission updates.
_Avoid_: Context injector, state cache

**Mission Snapshot**:
An immutable versioned manifest of the current authoritative world, belief, plan/FSM, and active-maneuver references supplied to a Maneuver Control Agent invocation.
_Avoid_: Agent memory, ground truth

**Transport Event**:
An immutable published fact or outcome that may be consumed by more than one service.
_Avoid_: Command, notification

**Command**:
A single-recipient request to an agent or service that produces an accepted, completed, or failed outcome.
_Avoid_: Event, broadcast

**Source Authority**:
The identified authority from which a Mission Specification originates and whose intent its derived plans preserve.
_Avoid_: Planner, executor

**Mission Specification**:
An immutable structured description of a Mission, its objective, constraints, and chosen planning profile.
_Avoid_: Prompt, untyped mission

**Planning Profile**:
The declared planning semantics for one plan revision: temporal scheduling or symbolic sequential planning.
_Avoid_: Implicit hybrid, planner setting

**Planner Choice**:
The semantic selection of the eligible planner for a Mission Specification.
_Avoid_: Executable path, solver command

**Operational Scene Graph**:
The agent-visible representation of operational entities, attributes, predicates, and relationships used for planning.
_Avoid_: Ground truth, sensor dump

**Bayesian Belief Snapshot**:
An immutable versioned estimate of uncertainty about operational entities and events used as planning input.
_Avoid_: Ground truth, agent memory

**Normalized Plan**:
A planner-independent revision of abstract maneuver intent and execution constraints, derived from one planner result.
_Avoid_: Planner output, scene snapshot

**Planner Translator**:
The deterministic transformation from a Mission Specification, Planner Choice, and planning inputs into planner-native assets.
_Avoid_: LLM code generation, planner runner

**Invocation Overlay**:
A direct caller request considered for one agent invocation without becoming durable mission authority state.
_Avoid_: Mission fact, snapshot update

**Hyper Heartbeat**:
A periodic Hyper Agent invocation that evaluates whether the latest operational scene and belief warrant replanning.
_Avoid_: Maneuver heartbeat, plan revision

**Replan Request**:
A Maneuver Control Agent's advisory request for Hyper Agent to evaluate a new plan.
_Avoid_: Plan revision, command to execute

**Active Maneuver**:
A current maneuver intent with lifecycle status and result information represented in a Mission Snapshot.
_Avoid_: FSM state, ground truth

**Maneuver Heartbeat**:
A periodic Maneuver Control Agent invocation that evaluates the current Mission Snapshot and FSM transition candidates.
_Avoid_: FSM runner invocation, plan revision

**Statechart**:
A validated declarative representation of the states and legal transitions for one plan revision.
_Avoid_: Generated script, environment model

**FSM Runner**:
The service that maintains Statechart control state, publishes FSM status, and mechanically applies enabled transitions.
_Avoid_: Transition authority, environment interpreter

**FSM Status**:
The current Statechart configuration and its enabled transition candidates.
_Avoid_: Mission Snapshot, scene graph

**Transition Candidate**:
A legal Statechart event that Maneuver Control may select from the current FSM Status.
_Avoid_: Arbitrary target state, environment signal

**FSM Execution Record**:
The durable control-state record for one active Statechart revision.
_Avoid_: Machine pickle, scene snapshot

**Maneuver Decision**:
A Maneuver Control Agent outcome that may select a transition, one physical maneuver, replanning, communication, or no change.
_Avoid_: Automatic transition, plan revision

**Maneuver Command**:
An abstract request for one physical maneuver, identified by action, Mission, and plan context.
_Avoid_: Environment command format, scene graph payload

**Maneuver Feedback**:
An environment-authoritative lifecycle fact for a Maneuver Command.
_Avoid_: Agent assertion, tool return text

**Maneuver Adapter**:
The port that submits a Maneuver Command to an environment without deciding its lifecycle outcome.
_Avoid_: Environment authority, lifecycle simulator

**Hyper Query**:
A Maneuver Control Agent question sent to Hyper Agent for an answer, plan outcome, or report.
_Avoid_: Human question, replan decision

**Human Question**:
A reserved Hyper Agent escalation message for information only a human can provide.
_Avoid_: Maneuver query, live HITL session

**Mission Memory**:
A role-owned durable context retained across future episodes of one Mission.
_Avoid_: Operational authority, shared state

**Role Skill**:
Read-only role-specific guidance that shapes agent judgment without becoming mission authority.
_Avoid_: Runtime state, writable policy

**ONR Runtime Configuration**:
The non-secret stable settings for this mission-control system, distinct from live Mission authority data.
_Avoid_: Mission state, environment snapshot
