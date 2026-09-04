# Autonomous Mission Control

This context describes a system that turns operational missions into plans and real-time maneuver decisions. It distinguishes planning authority, maneuver control, and externally authoritative mission state.

## Language

**Mission**:
A bounded operational objective with its participants, constraints, and desired outcome.
_Avoid_: Task, job

**Mission Intent**:
The raw operator-authored natural-language input supplied in a Mission Activation. Together with its `MissionInput` envelope, it remains the source authority from which Hyper derives planning representations; it is not part of the shared public observation feed.
_Avoid_: Public trace record, derived plan

**Planning Intent**:
A structured, non-authoritative interpretation derived from the raw MissionInput and operator Mission Intent to support planner selection. It retains Mission and source authority identity and may hold flexible planner-selection facts in `details`, but never planner files or verification evidence; it does not amend the Mission Intent or MissionInput.
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

**Operator Debug View**:
A loopback-only Mission Run view that combines authoritative current state with non-authoritative operational and recorded debug evidence for a local operator. It is not a public evidence feed or a source of Mission authority.
_Avoid_: Run Observation feed, agent authority, public dashboard

**Recorded Debug Reasoning**:
Experimental model reasoning captured for local diagnosis when debug recording is enabled. It is non-authoritative, never a Run Observation, and never determines Mission Run, FSM, environment, or planner state.
_Avoid_: Mission rationale, Run Narrative, mission authority

**Console Session**:
An operator console's authenticated local session that owns the Mission Run it activated. Its opaque credential permits owner-scoped Mission Intent readback, cancellation, and recovery after an ungraceful console loss; a clean console exit cancels its owned Run.
_Avoid_: Runtime Host session, observer session

**Hyper Agent**:
The planning agent that owns one Hyper Workflow Episode, derives Planning Intent, chooses and invokes planners through code-owned capabilities, manages correction, and hands verified planning artifacts toward execution.
_Avoid_: Main agent, controller

**Hyper Workflow Episode**:
One checkpointed eight-stage planning episode for a Mission Run in which the Hyper Agent alone owns its live todo state and sequences Planning Intent, planner choice, planner-native file generation and external verification, Statechart validation, and FSM execution handoff. It terminates with either a Planner Plan bound to an accepted Statechart or a recorded rejection. Invoked capabilities return evidence but do not own or update the todo state.
_Avoid_: Planning Intent invocation, shared agent state, planner authority

**Maneuver Control Agent**:
The decision-making authority that uses Mission Snapshot and FSM status to select transitions, maneuvers, escalation, or status.
_Avoid_: Subagent, flight controller

**Context Coordination**:
The service that assembles and publishes versioned Mission Snapshots and coordinates the active closed loop from accepted planning authority through terminal Mission state.
_Avoid_: Context injector, state cache, scheduler wrapper

**Mission Snapshot**:
An immutable versioned manifest of the current authoritative world, belief, plan/FSM, and active-maneuver references resolved by Context Coordination for agent invocations.
_Avoid_: Agent memory, ground truth

**Pending Perception Batch**:
The ordered raw entity and event perceptions accumulated since Maneuver Control last completed belief ingestion. It is transient delivery context, not accumulated belief or an independent heartbeat trigger.
_Avoid_: Bayesian Belief Snapshot, agent memory, perception trigger

**Event Observation**:
A sensor capture of an event occurrence within the controlled drone's field of view, retaining subject position, uncertainty, and source provenance independently of any physical maneuver.
_Avoid_: Maneuver observation, observation-window result

**Event Time**:
The Mission time at which the observed event occurred.
_Avoid_: Observed Time, event duration

**Observed Time**:
The Mission time at which the sensor captured an observation.
_Avoid_: Event Time, observation duration

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
A single-recipient request durably routed to an agent or service. Its transport delivery and any domain outcome are distinct evidence.
_Avoid_: Event, broadcast

**Command Receipt**:
Transport-owned evidence that a Command was durably enqueued for delivery. Its `accepted` status means transport acceptance only, never recipient or environment acceptance.
_Avoid_: Maneuver Feedback, recipient acknowledgement

**Source Authority**:
The identified authority from which MissionInput originates and whose intent derived planning artifacts preserve.
_Avoid_: Planner, executor

**Planning Profile**:
The declared planning semantics for one plan revision: temporal scheduling or symbolic sequential planning.
_Avoid_: Implicit hybrid, planner setting

**Planner Choice**:
The semantic selection of the eligible planner derived from PlanningIntent.
_Avoid_: Executable path, solver command

**Planner Choice Record**:
An immutable Mission-scoped public record of one Planner Choice and its concise rationale.
_Avoid_: Mission authority, private reasoning

**Planner Generation Attempt**:
An immutable accepted-or-rejected record of one planner-native file generation attempt, correlated to its Planner Choice Record and Mission Snapshot and retaining operational file references.
_Avoid_: Planner result, hidden retry

**Planning Record**:
A future durable aggregate that correlates Planner Choice and generation-attempt records with external verifier evidence and the resulting Planner Plan by Mission identity, revisions, event identity, and references.
_Avoid_: Mission authority, planner-selection input

**Operational Scene Graph**:
The agent-visible representation of operational entities, attributes, predicates, and relationships used for planning.
_Avoid_: Ground truth, sensor dump

**Bayesian Belief Snapshot**:
An immutable versioned estimate of uncertainty about operational entities and events used as planning input.
_Avoid_: Ground truth, agent memory

**Planner Plan**:
A Mission- and revision-bound reference envelope for a planner-native plan accepted by its external authority. It identifies the planner artifact without embedding, normalizing, or turning that artifact into execution semantics.
_Avoid_: Normalized Plan, maneuver list, Statechart

**Planner Asset Generator**:
A non-authoritative Hyper capability that writes planner-native assets from Mission Intent and snapshot-authorized evidence.
_Avoid_: Planner verifier, mission authority

**Planner Correction Feedback**:
A notice preserving exact external verifier or planner diagnostics and directing the Hyper Agent to repair and resubmit the same planner files.
_Avoid_: New planner workspace, private reasoning

**Invocation Overlay**:
A direct caller request considered for one agent invocation without becoming durable mission authority state.
_Avoid_: Mission fact, snapshot update

**Hyper Heartbeat**:
A periodic Hyper Agent invocation that uses the latest snapshot-authorized operational evidence for planner selection and generation.
_Avoid_: Maneuver heartbeat, plan revision

**Replan Request**:
A Maneuver Control Agent's advisory request for Hyper Agent to evaluate a new plan.
_Avoid_: Plan revision, command to execute

**Active Maneuver**:
A current maneuver intent with lifecycle status and result information represented in a Mission Snapshot.
_Avoid_: FSM state, ground truth

**Maneuver Heartbeat**:
A Maneuver Control Agent decision cycle over one injected evidence snapshot, with at most one FSM transition.
_Avoid_: FSM runner invocation, plan revision

**Statechart**:
A validated declarative representation of the states and legal transitions for one plan revision. Once activated, its FSM is the execution semantics; planner-native plan artifacts remain planning evidence.
_Avoid_: Planner Plan, generated script, environment model

**FSM Runner**:
The service that maintains Statechart control state, publishes FSM status, and mechanically applies enabled transitions.
_Avoid_: Transition authority, environment interpreter

**FSM Status**:
The current Statechart configuration and its enabled transition candidates.
_Avoid_: Mission Snapshot, scene graph

**Transition Candidate**:
A legal target state and unchanged Statechart condition that Maneuver Control may select from the current FSM Status.
_Avoid_: Arbitrary target state, environment signal

**Transition Intent**:
Maneuver Control's durable selection of one current Transition Candidate target and its unchanged condition for a single Statechart state entry.
_Avoid_: FSM transition, rewritten condition, physical maneuver

**FSM Execution Record**:
The durable control-state record for one active Statechart revision.
_Avoid_: Machine pickle, scene snapshot

**Maneuver Decision**:
A Maneuver Control Agent outcome that may select a transition, one physical maneuver, replanning, communication, or no change.
_Avoid_: Automatic transition, plan revision

**Maneuver Command**:
An abstract request for one physical maneuver, identified by action, Mission, and plan context.
_Avoid_: Environment command format, scene graph payload

**Maneuver Deadline**:
An absolute non-negative Mission time by which a supported physical maneuver should finish; it expresses best-effort timing rather than a failure boundary.
_Avoid_: Duration, timeout

**Maneuver Feedback**:
An environment-authoritative lifecycle fact for a Maneuver Command.
_Avoid_: Agent assertion, tool return text

**Actionable Maneuver Feedback**:
Completed, failed, or cancelled Maneuver Feedback that immediately triggers Maneuver Control; active feedback remains authoritative progress evidence and is coalesced until another trigger.
_Avoid_: Every lifecycle update, periodic heartbeat

**Maneuver Adapter**:
An environment-side consumer that applies durably delivered Maneuver Commands and acknowledges delivery after local application. It publishes Maneuver Feedback rather than returning lifecycle truth to Maneuver Control.
_Avoid_: In-process callback, Maneuver Control dependency

**Environment Profile**:
The non-agent configuration that declares one environment integration's protocols, update ownership, transport routing, physical capabilities, and environment-specific operating settings.
_Avoid_: Agent prompt, action parameter schema

**Environment Update Ownership**:
The Environment Profile choice of whether Context Coordination requests each update or the environment advances independently at its declared cadence.
_Avoid_: Agent scheduling, transport delivery ownership

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
