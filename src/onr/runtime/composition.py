"""Minimal runtime composition without mission-authority state."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from langchain_openai import ChatOpenAI

from onr.adapters.file_transport import FileTransport
from onr.adapters.fsm_store import JsonFSMStateStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.mission_memory import FileMissionMemoryStore
from onr.application.context_coordination import ContextCoordination
from onr.application.fsm import FSMRunner
from onr.application.hyper_agent import HyperAgent
from onr.application.maneuver_control import ManeuverControl
from onr.application.planning_commands import PlanningCommandHandler
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverDecision, ManeuverFeedback
from onr.contracts.hyper_agent import FrozenMissionSpec, MissionInput
from onr.contracts.maneuver_control import ManeuverCommand, ManeuverControlDecision
from onr.contracts.planning import NormalizedPlan, PlanningOutcome
from onr.contracts.transport import Command, CommandOutcome, TransportEvent
from onr.ports.maneuver import ManeuverAdapter
from onr.ports.transport import Subscription
from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.agents.hyper_agent import (
    DeepAgentsMissionInterpreter,
    create_hyper_agent as create_deep_hyper_agent,
)
from onr.agents.maneuver_control import (
    DeepAgentsDecisionProvider,
    create_maneuver_control_agent as create_deep_maneuver_control_agent,
)


@dataclass(frozen=True, slots=True)
class RuntimeRunResult:
    """Evidence returned by one synchronous, file-backed runtime run."""

    authority: FrozenMissionSpec
    plan: NormalizedPlan
    context_snapshot: MissionSnapshot
    status_before_feedback: FSMStatus
    decision: ManeuverControlDecision
    command: ManeuverCommand
    scene_graph: TransportEvent
    feedback: ManeuverFeedback
    final_status: FSMStatus


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """Validated configuration and the selected transport adapter."""

    config: RuntimeConfig
    transport: FileTransport | InProcessTransport

    @classmethod
    def create(
        cls,
        *,
        repo_root: Path,
        config_path: Path | None = None,
        subscriptions: tuple[Subscription, ...] = (),
    ) -> "RuntimeComposition":
        config = load_runtime_config(config_path, repo_root=repo_root)
        if config.transport.backend == "file":
            transport = FileTransport(config.transport.root, subscriptions)
        else:
            transport = InProcessTransport(subscriptions)
        return cls(config=config, transport=transport)

    def create_chat_model(self) -> ChatOpenAI:
        """Create the configured OpenAI-compatible chat model."""

        llm = self.config.llm
        return ChatOpenAI(
            base_url=llm.base_url,
            model=llm.model,
            api_key=llm.api_key,
            temperature=llm.temperature,
        )

    def run_planning_command(
        self,
        command: Command,
        planner: object,
        *,
        topic: str = "normalized-plans",
    ) -> CommandOutcome:
        """Deliver one command through the configured transport and subscriber."""

        subscription = Subscription(
            service_id=command.target_service,
            mission_id=command.mission_id,
            topic=command.command_kind,
        )
        consumer = self.transport.open_consumer(subscription)
        try:
            self.transport.send_command(command)
            handler = PlanningCommandHandler(self.transport, planner, topic=topic)
            outcome = handler.run_once(consumer)
            if outcome is None:
                raise RuntimeError("planning command was not delivered")
            return outcome
        finally:
            consumer.close()

    def create_fsm_runner(
        self,
        *,
        mission_id: str,
        clock: Callable[[], int | float] | None = None,
    ) -> FSMRunner:
        """Compose the pure FSM service with the selected transport and JSON store."""

        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission ID must be a non-empty string")
        subscription = FSMRunner.subscription_for(
            mission_id,
            service_id=self.config.services.fsm_runner,
        )
        if subscription not in self.transport.subscriptions:
            self.transport.subscriptions = self.transport.subscriptions + (subscription,)
        return FSMRunner(
            self.transport,
            store=JsonFSMStateStore(self.config.storage.root / "fsm" / mission_id),
            clock=clock,
            subscription=subscription,
        )

    def create_context_coordination(
        self,
        *,
        mission_id: str,
        clock: Callable[[], str] | None = None,
        input_topic: str = "normalized-plans",
        snapshot_topic: str = "mission-snapshots",
    ) -> ContextCoordination:
        """Compose Context Coordination and register its static input subscription."""

        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission ID must be a non-empty string")
        subscription = ContextCoordination.subscription_for(
            mission_id,
            input_topic=input_topic,
            service_id=self.config.services.context_coordination,
        )
        if subscription not in self.transport.subscriptions:
            self.transport.subscriptions = self.transport.subscriptions + (subscription,)
        return ContextCoordination(
            cast(Any, self.transport),
            mission_id,
            input_topic=input_topic,
            snapshot_topic=snapshot_topic,
            service_id=self.config.services.context_coordination,
            clock=clock,
            subscription=subscription,
        )

    def create_maneuver_control(
        self,
        adapter: ManeuverAdapter,
        decision_provider: object | None = None,
        *,
        target_service: str = "maneuver-adapter",
        model: Any | None = None,
        mission_id: str | None = None,
        memory_store: object | None = None,
        skill_catalog: object | None = None,
        skill_version: str | None = None,
        backend_root: Path | None = None,
    ) -> ManeuverControl:
        """Compose Maneuver Control without introducing runtime authority state."""

        if decision_provider is None:
            if mission_id is None:
                raise ValueError("create_maneuver_control requires a provider or model and Mission ID")
            if model is None:
                model = self.create_chat_model()
            if memory_store is None:
                memory_store = FileMissionMemoryStore(self.config.storage.root / "mission-memory")
            context_backend_root = backend_root
            if context_backend_root is None and skill_catalog is not None:
                context_backend_root = self.config.storage.root
            decision_provider = DeepAgentsDecisionProvider(
                create_deep_maneuver_control_agent(
                    model=model,
                    mission_id=mission_id,
                    memory_store=memory_store,
                    skill_catalog=skill_catalog,
                    skill_version=skill_version,
                    backend_root=context_backend_root,
                )
            )

        return ManeuverControl(
            cast(Any, self.transport),
            adapter,
            decision_provider,
            target_service=target_service,
        )

    def create_hyper_agent(
        self,
        interpreter: object | None = None,
        planner: object | None = None,
        *,
        planners: dict[object, object] | None = None,
        temporal_planner: object | None = None,
        symbolic_planner: object | None = None,
        model: Any | None = None,
        system_prompt: str | None = None,
        mission_spec_topic: str = "mission-specifications",
        normalized_plan_topic: str = "normalized-plans",
        replan_topic: str = "replan-requests",
        mission_id: str | None = None,
        memory_store: object | None = None,
        skill_catalog: object | None = None,
        skill_version: str | None = None,
        backend_root: Path | None = None,
    ) -> HyperAgent:
        """Compose Hyper Agent with injected interpretation and planning seams."""

        if interpreter is None:
            if model is None:
                model = self.create_chat_model()
            if mission_id is not None and memory_store is None:
                memory_store = FileMissionMemoryStore(self.config.storage.root / "mission-memory")
            context_backend_root = backend_root
            if context_backend_root is None and skill_catalog is not None:
                context_backend_root = self.config.storage.root
            interpreter = DeepAgentsMissionInterpreter(
                create_deep_hyper_agent(
                    model=model,
                    system_prompt=system_prompt,
                    mission_id=mission_id,
                    memory_store=memory_store,
                    skill_catalog=skill_catalog,
                    skill_version=skill_version,
                    backend_root=context_backend_root,
                )
            )
        selected = planners
        if selected is None and (temporal_planner is not None or symbolic_planner is not None):
            selected = {}
            if temporal_planner is not None:
                selected["temporal"] = temporal_planner
            if symbolic_planner is not None:
                selected["symbolic"] = symbolic_planner
        if mission_id is not None:
            subscription = Subscription(
                service_id=self.config.services.hyper_agent,
                mission_id=mission_id,
                topic=replan_topic,
            )
            if subscription not in self.transport.subscriptions:
                self.transport.subscriptions = self.transport.subscriptions + (subscription,)
        return HyperAgent(
            interpreter=interpreter,
            planner=planner,
            planners=selected,
            transport=self.transport,
            mission_spec_topic=mission_spec_topic,
            normalized_plan_topic=normalized_plan_topic,
            replan_topic=replan_topic,
        )

    def run_mission(
        self,
        mission_input: MissionInput,
        *,
        hyper_agent: HyperAgent,
        context_coordination: ContextCoordination,
        fsm_runner: FSMRunner,
        maneuver_control: ManeuverControl,
        environment_step: Callable[[], object],
    ) -> RuntimeRunResult:
        """Run one deterministic MissionInput-to-authoritative-feedback seam."""

        if not isinstance(mission_input, MissionInput):
            raise TypeError("run_mission requires a MissionInput")
        if not callable(environment_step):
            raise TypeError("environment_step must be callable")
        mission_id = mission_input.mission_id
        if context_coordination.subscription.mission_id != mission_id:
            raise ValueError("Context Coordination mission ID does not match MissionInput")
        fsm_subscription = fsm_runner.subscription or FSMRunner.subscription_for(
            mission_id,
            service_id=self.config.services.fsm_runner,
        )
        if fsm_subscription.mission_id != mission_id:
            raise ValueError("FSM Runner mission ID does not match MissionInput")

        required_subscriptions = (
            Subscription(
                self.config.services.hyper_agent,
                mission_id,
                hyper_agent.replan_topic,
            ),
            context_coordination.subscription,
            fsm_subscription,
            Subscription(maneuver_control.target_service, mission_id, "maneuver"),
            Subscription("runtime-scene-observer", mission_id, "operational-scene-graph"),
            Subscription("runtime-feedback-observer", mission_id, "maneuver-feedback"),
            Subscription("runtime-status-observer", mission_id, fsm_runner.status_topic),
        )
        for subscription in required_subscriptions:
            if subscription not in self.transport.subscriptions:
                self.transport.subscriptions = self.transport.subscriptions + (subscription,)

        def consume_event(consumer: Any, event_kind: str) -> TransportEvent:
            delivery = consumer.receive()
            if delivery is None or not isinstance(delivery.message, TransportEvent):
                if delivery is not None:
                    delivery.nack()
                raise RuntimeError(f"expected a {event_kind} transport event")
            if delivery.message.event_kind != event_kind:
                delivery.nack()
                raise RuntimeError(
                    f"expected a {event_kind} transport event, got {delivery.message.event_kind}"
                )
            event = delivery.message
            delivery.ack()
            return event

        def run_async(awaitable: Any) -> Any:
            return asyncio.run(awaitable)

        with ExitStack() as consumers:
            context_consumer = consumers.enter_context(
                self.transport.open_consumer(context_coordination.subscription)
            )
            fsm_consumer = consumers.enter_context(
                self.transport.open_consumer(fsm_subscription)
            )
            scene_consumer = consumers.enter_context(
                self.transport.open_consumer(required_subscriptions[4])
            )
            feedback_consumer = consumers.enter_context(
                self.transport.open_consumer(required_subscriptions[5])
            )
            status_consumer = consumers.enter_context(
                self.transport.open_consumer(required_subscriptions[6])
            )

            authority = hyper_agent.freeze_mission(mission_input)
            if not isinstance(authority, FrozenMissionSpec) or authority.mission_id != mission_id:
                raise RuntimeError("Hyper Agent did not return frozen mission authority")
            if authority.content_hash != authority.sha256:
                raise RuntimeError("frozen mission authority hash is invalid")

            seed = MissionSnapshot(
                mission_id=mission_id,
                version=1,
                created_at="runtime-seed",
            )
            heartbeat = hyper_agent.heartbeat(
                seed,
                mission_id=mission_id,
                snapshot_id=f"{mission_id}:snapshot:0",
            )
            plan = heartbeat.plan
            if (
                not isinstance(plan, NormalizedPlan)
                or plan.outcome is not PlanningOutcome.SOLVED
                or len(plan.maneuvers) != 1
                or plan.mission_spec != authority.mission_spec
            ):
                raise RuntimeError("initial Hyper heartbeat did not produce one solved maneuver")

            plan_snapshot = context_coordination.run_once(context_consumer)
            if plan_snapshot is None or plan_snapshot.plan_revision != plan.plan_revision:
                raise RuntimeError("Context Coordination did not publish the normalized-plan snapshot")
            activated = run_async(fsm_runner.run_once(fsm_consumer))
            if not isinstance(activated, FSMStatus):
                raise RuntimeError("FSM Runner did not activate the normalized plan")
            status_before_feedback = FSMStatus.from_dict(
                consume_event(status_consumer, "fsm-status").payload
            )
            if status_before_feedback != activated:
                raise RuntimeError("FSM status evidence does not match activation")

            invocation_id = f"maneuver-heartbeat:{mission_id}:{plan.plan_revision}"
            maneuver_result = maneuver_control.heartbeat(
                plan_snapshot,
                status_before_feedback,
                event_id=invocation_id,
                plan=plan,
            )
            decision = maneuver_result.decision
            command = maneuver_result.command
            if (
                not isinstance(decision, ManeuverControlDecision)
                or decision.physical_intent is None
                or not isinstance(command, ManeuverCommand)
                or command.mission_id != mission_id
                or command.plan_revision != plan.plan_revision
                or command.maneuver_id != plan.maneuvers[0].maneuver_id
                or command.mission_snapshot_id != f"{mission_id}:snapshot:{plan_snapshot.version}"
            ):
                raise RuntimeError("Maneuver Control did not emit one physical command")

            environment_step()
            scene_graph = consume_event(scene_consumer, "operational_scene_graph")
            feedback_event = consume_event(feedback_consumer, "maneuver-feedback")
            feedback = ManeuverFeedback.from_dict(feedback_event.payload)
            if (
                feedback_event.mission_id != mission_id
                or feedback_event.event_id != feedback.feedback_id
                or feedback.mission_id != command.mission_id
                or feedback.maneuver_id != command.maneuver_id
                or feedback.payload.get("command_id") != command.command_id
                or feedback.payload.get("correlation_id") != command.correlation_id
            ):
                raise RuntimeError("maneuver feedback does not match the physical command")
            graph = scene_graph.payload.get("graph")
            expected_parameters = {
                parameter.name: parameter.value for parameter in command.parameters
            }
            maneuvers = graph.get("maneuvers") if isinstance(graph, Mapping) else None
            maneuver = (
                maneuvers[0]
                if isinstance(maneuvers, (list, tuple)) and len(maneuvers) == 1
                else None
            )
            if (
                scene_graph.mission_id != mission_id
                or not isinstance(graph, Mapping)
                or graph.get("mission_id") != mission_id
                or graph.get("plan_revision") != plan.plan_revision
                or not isinstance(maneuver, Mapping)
                or maneuver.get("maneuver_id") != command.maneuver_id
                or maneuver.get("action") != command.action
                or maneuver.get("parameters") != expected_parameters
            ):
                raise RuntimeError("operational scene graph does not match the physical command")

            context_snapshot = context_coordination.run_once(context_consumer)
            if (
                context_snapshot is None
                or context_snapshot.plan_revision != plan.plan_revision
                or context_snapshot.operational_scene_graph != scene_graph.event_id
            ):
                raise RuntimeError("Context Coordination did not consume the scene graph source fact")

            if status_before_feedback.active_state != activated.active_state:
                raise RuntimeError("FSM active state changed before authoritative feedback")
            candidate = next(
                (
                    item
                    for item in status_before_feedback.transition_candidates
                    if item.event == f"advance:{command.maneuver_id}"
                ),
                None,
            )
            if candidate is None:
                raise RuntimeError("FSM status did not expose the commanded maneuver")
            transition_decision = (
                ManeuverDecision(
                    decision_id=f"transition:{feedback.feedback_id}",
                    mission_id=mission_id,
                    transition_event=candidate.event,
                )
                if candidate.requires_decision
                else None
            )
            applied = run_async(
                fsm_runner.apply(
                    candidate,
                    feedback=feedback,
                    maneuver_decision=transition_decision,
                )
            )
            if not isinstance(applied, FSMStatus) or applied.active_state == status_before_feedback.active_state:
                raise RuntimeError("FSM did not advance after authoritative maneuver feedback")
            final_status = FSMStatus.from_dict(consume_event(status_consumer, "fsm-status").payload)
            if final_status != applied:
                raise RuntimeError("final FSM status evidence does not match the applied transition")

            return RuntimeRunResult(
                authority=authority,
                plan=plan,
                context_snapshot=context_snapshot,
                status_before_feedback=status_before_feedback,
                decision=decision,
                command=command,
                scene_graph=scene_graph,
                feedback=feedback,
                final_status=final_status,
            )



def create_runtime(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    subscriptions: tuple[Subscription, ...] = (),
) -> RuntimeComposition:
    return RuntimeComposition.create(
        repo_root=repo_root,
        config_path=config_path,
        subscriptions=subscriptions,
    )
