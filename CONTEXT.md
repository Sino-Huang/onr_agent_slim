# Autonomous Mission Control

This context describes a system that turns operational missions into plans and real-time maneuver decisions. It distinguishes planning authority, maneuver control, and externally authoritative mission state.

## Language

**Mission**:
A bounded operational objective with its participants, constraints, and desired outcome.
_Avoid_: Task, job

**Mission Intent**:
The raw operator-authored natural-language input supplied in a Mission Activation. Together with its `MissionInput` envelope, it remains the source authority from which Hyper derives planning representations; it is not part of the shared public observation feed.
_Avoid_: Public trace record, Mission Specification

**Planning Intent**:
An opt-in, structured, non-authoritative interpretation derived from the raw MissionInput and operator Mission Intent to support planner selection. It preserves source provenance and may hold flexible planner-selection facts in `details`, but never planner assets or verification evidence; it does not amend the Mission Intent or MissionInput.
_Avoid_: Source authority, planner-native asset, verification record

**Mission Run**:
One concrete execution attempt for a Mission. A Mission can have more than one Mission Run.
_Avoid_: Mission, process lifetime

**Mission Activation**:
A Command that asks the pipeline runtime to begin a Mission Run from supplied mission intent.
_Avoid_: Runtime ownership, UI state change

**Activation Request ID**:
An opaque idempotency identifier supplied with a Mission Activation so a retry returns its original acceptance outcome rather than creating another Mission Run.
_Avoid_: Mission ID, Mission Run ID

**Runtime Host**:
The long-lived local process that accepts Mission Activations, owns Mission Run execution, and publishes public run state.
_Avoid_: TUI runtime, UI server

**Run Worker**:
An isolated process tree that executes one Mission Run on behalf of the Runtime Host and can be terminated without terminating the host.
_Avoid_: Runtime Host, agent process

**Operator Console**:
The local operator interface that activates a Mission, observes its Mission Run, cancels it, and in the future submits requested Human Decisions.
_Avoid_: Runtime Host, agent authority

**Console Session**:
An operator console's authenticated local session that owns the Mission Run it activated. Its opaque credential permits owner-scoped Mission Intent readback, cancellation, and recovery after an ungraceful console loss; a clean console exit cancels its owned Run.
_Avoid_: Runtime Host session, observer session

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

**Run Observation**:
A redacted, non-authoritative public record of a Mission Run's progress or state that may refer to an Artifact.
_Avoid_: Transport Event, runtime authority

**Run Observation Cursor**:
An opaque position after a Run Observation that a consumer supplies to resume its ordered observation feed.
_Avoid_: Event sequence, client state

**Run Activity**:
A correlated, operator-visible unit of work in a Mission Run, deterministically derived by a versioned code-owned mapping from one or more Run Observations. A valid unmapped observation produces a generic Run Activity.
_Avoid_: Raw event, agent thought

**Mission Run Status**:
The explicit public lifecycle state of a Mission Run: queued, running, awaiting human decision, succeeded, failed, or cancelled.
_Avoid_: Last event, inferred state

**Mission Run Record**:
The durable Runtime Host-owned lifecycle record for a Mission Run, including its identifiers, owning Console Session, status, worker identity, terminal classification, and internal checkpoint and Artifact references.
_Avoid_: Run Observation, operational log

**Mission Run Cancellation**:
A Runtime Host action that terminates a Run Worker and records the Mission Run as cancelled. It does not stop the separate environment or retract a submitted Maneuver Command.
_Avoid_: Environment stop, maneuver rollback

**Host-Interrupted Failure**:
A public Mission Run failure classification recorded when the Runtime Host stops unexpectedly and the Run Worker cannot safely continue.
_Avoid_: Recoverable pause, environment failure

**Artifact**:
A durable, read-only file produced by or referenced during a Mission Run, including future perception evidence, logs, or conversation records.
_Avoid_: Transport Event, mission authority

**Public Artifact Inbox**:
The configured Mission Run directory from which the Runtime Host automatically discovers atomically published Artifacts with valid public metadata. Files outside it are not public Artifacts.
_Avoid_: Arbitrary storage, scratch directory

**Perception Rationale**:
A producer-authored, redacted public explanation of a perception conclusion, with references to supporting evidence Artifacts. It excludes private model reasoning.
_Avoid_: Chain of thought, raw reasoning

**Conversation Artifact**:
An append-only Artifact directory containing atomically published, monotonically sequenced typed conversation-entry files with author, time, audience, kind, and content or a content reference.
_Avoid_: Chat transcript, arbitrary log

**Run Narrative**:
An optional non-authoritative summary generated from sanitized Mission Run evidence. It does not determine Mission Run status or Run Activity.
_Avoid_: Progress authority, agent reasoning

**Command**:
A single-recipient request to an agent or service that produces an accepted, completed, or failed outcome.
_Avoid_: Event, broadcast

**Source Authority**:
The identified authority from which a Mission Specification originates and whose intent its derived plans preserve.
_Avoid_: Planner, executor

**Mission Specification**:
The existing immutable structured runtime description of a Mission, its objective, constraints, and chosen planning profile. It remains supported as the legacy runtime mode; opting into PlanningIntent does not change the raw MissionInput or operator Mission Intent as source authority.
_Avoid_: Prompt, untyped mission

**Planning Profile**:
The declared planning semantics for one plan revision: temporal scheduling or symbolic sequential planning.
_Avoid_: Implicit hybrid, planner setting

**Planner Choice**:
The semantic selection of the eligible planner derived from an opt-in PlanningIntent or the legacy MissionSpec mode.
_Avoid_: Executable path, solver command

**Planner Choice Record**:
An immutable public record binding one Planner Choice and concise rationale to the raw Mission Input and accepted PlanningIntent provenance.
_Avoid_: Mission authority, private reasoning

**Planner Generation Attempt**:
An immutable accepted-or-rejected record of one planner-native asset generation attempt, bound to its Planner Choice Record and Mission Snapshot.
_Avoid_: Planner result, hidden retry

**Planning Record**:
A future durable aggregate that binds Planner Choice and generation-attempt records to solver evidence, code-owned verification checks/outcome, and a NormalizedPlan reference/hash. Planner assets and solver evidence are Planning Record outputs, never PlanningIntent `details`.
_Avoid_: Mission authority, planner-selection input

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
A code-owned translation slice that validates generated planner assets, invokes the selected planner, independently checks its result, and normalizes only a verified result.
_Avoid_: Planner Asset Generator, planner runner

**Planner Asset Generator**:
A non-authoritative Hyper capability that proposes planner-native assets and a normalization template from Mission Intent and snapshot-authorized evidence.
_Avoid_: Planner Translator, mission authority

**Planner Correction Feedback**:
A bounded, sanitized notice from the Planner Translator that identifies whether static validation or independent solution checking rejected a generated asset set.
_Avoid_: Raw solver diagnostic, private reasoning

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

**Human Decision Request**:
A request that pauses a Mission Run for a human decision, with permitted responses and an eventual recorded decision. It is distinct from a Human Question because it seeks an action, not only information.
_Avoid_: Human Question, live HITL session

**Human Decision**:
An immutable recorded response, selected from the permitted choices in a Human Decision Request, that permits a paused Mission Run to resume.
_Avoid_: UI action, unrecorded approval

**Run Checkpoint**:
A durable continuation point for a paused Mission Run that allows the Runtime Host to resume it after receiving a Human Decision.
_Avoid_: UI session, transient process state

**Mission Memory**:
A role-owned durable context retained across future episodes of one Mission.
_Avoid_: Operational authority, shared state

**Role Skill**:
Read-only role-specific guidance that shapes agent judgment without becoming mission authority.
_Avoid_: Runtime state, writable policy

**ONR Runtime Configuration**:
The non-secret stable settings for this mission-control system, distinct from live Mission authority data.
_Avoid_: Mission state, environment snapshot
