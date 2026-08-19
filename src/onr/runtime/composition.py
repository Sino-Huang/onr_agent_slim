"""Minimal runtime composition without mission-authority state."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, cast

from langchain_openai import ChatOpenAI

from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.adapters.fsm_store import JsonFSMStateStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.mission_memory import FileMissionMemoryStore
from onr.adapters.minizinc import MiniZincExecutor
from onr.adapters.operational_log import FileOperationalLog
from onr.adapters.mission_log_summarizer import (
    FileMissionLogSummarizer,
    SummarizationError,
)
from onr.adapters.vllm_reachability import probe_vllm_reachability
from onr.application.context_coordination import ContextCoordination
from onr.application.bayesian_belief import (
    BayesianBeliefManager,
    BayesianBeliefService,
)
from onr.application.fsm import FSMRunner
from onr.application.hyper_agent import (
    HyperAgent,
    HyperHeartbeatResult,
    HyperPlanningHeartbeatResult,
    PlanningHeartbeatOutcome,
)
from onr.application.maneuver_control import ManeuverControl
from onr.application.symbolic_planning import SymbolicPlanning
from onr.application.planning_commands import PlanningCommandHandler
from onr.application.temporal_planning import TemporalPlanning
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.bayesian_belief import (
    BayesianBeliefSnapshot,
    BeliefKey,
    ForbiddenBeliefCombination,
)
from onr.contracts.fsm import FSMStatus, ManeuverDecision, ManeuverFeedback
from onr.contracts.hyper_agent import FrozenMissionSpec, MissionInput
from onr.contracts.maneuver_control import ManeuverCommand, ManeuverControlDecision
from onr.contracts.planning import NormalizedPlan, PlanningOutcome
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
)
from onr.contracts.transport import Command, CommandOutcome, TransportEvent
from onr.ports.maneuver import ManeuverAdapter
from onr.ports.operational_log import OperationalLog
from onr.ports.mission_log_summarizer import MissionLogSummarizer, SummaryArtifact
from onr.ports.transport import Subscription
from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.runtime.agent_debug import AgentDebugRecorder
from onr.runtime.lease import RuntimeLeaseStore
from onr.runtime.llm_debug import LLMResponseRecorder
from onr.agents.hyper_agent import (
    DeepAgentsMissionInterpreter,
    DeepAgentsPlanningIntentInterpreter,
    create_hyper_agent as create_deep_hyper_agent,
    create_planning_intent_agent,
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
    belief_snapshot: BayesianBeliefSnapshot | None = None
    belief_context_snapshot: MissionSnapshot | None = None
    belief_heartbeat: HyperHeartbeatResult | None = None


@dataclass(frozen=True, slots=True)
class PlanningMissionRunResult:
    """Evidence returned by one planner-native, scene-backed Mission Run."""

    outcome: PlanningHeartbeatOutcome
    planner_choice: PlannerChoiceRecord | None = None
    attempt: PlannerGenerationAttempt | None = None
    context_snapshot: MissionSnapshot | None = None
    scene_graph: TransportEvent | None = None


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """Validated configuration and the selected transport adapter."""

    config: RuntimeConfig
    transport: FileTransport | InProcessTransport
    operational_log: OperationalLog | None = None
    lease: RuntimeLeaseStore | None = None

    def __post_init__(self) -> None:
        if self.lease is None:
            object.__setattr__(
                self,
                "lease",
                RuntimeLeaseStore(self.config.storage.root / "runtime"),
            )

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
        return cls(
            config=config,
            transport=transport,
            operational_log=FileOperationalLog(config.storage.root / "operational-log"),
        )

    @contextmanager
    def runtime_session(self):
        """Publish a read-only runtime lease for the duration of one run."""
        lease = self.lease
        if lease is None:  # guarded by __post_init__; keeps the optional injection type narrow.
            raise RuntimeError("runtime lease was not initialized")
        with lease.session():
            yield lease

    @contextmanager
    def mission_session(
        self,
        mission_id: str,
        *,
        summarizer: MissionLogSummarizer | None = None,
        model: Any | None = None,
    ):
        """Run one mission-scoped summary worker inside the runtime lease."""

        if (
            not isinstance(mission_id, str)
            or not mission_id.strip()
            or Path(mission_id).name != mission_id
            or mission_id in {".", ".."}
        ):
            raise ValueError("mission ID must be one path component")
        selected = summarizer
        stop = Event()

        def record_failure(exc: Exception) -> None:
            try:
                self._logger().emit(
                    mission_id,
                    "runtime",
                    "summary-unavailable",
                    "failed",
                    details={
                        "operation": "mission_summary",
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception:
                pass

        def summarize() -> None:
            nonlocal selected
            try:
                if selected is None:
                    selected = self.create_mission_log_summarizer(model=model)
                selected.heartbeat(mission_id)
            except SummarizationError as exc:
                record_failure(exc)
            except Exception as exc:
                record_failure(exc)

        def summary_worker() -> None:
            cadence = float(self.config.heartbeats.summary_seconds)
            while not stop.wait(cadence):
                summarize()
            summarize()

        worker = Thread(
            target=summary_worker,
            name=f"mission-summary-{mission_id}",
            daemon=False,
        )
        with self.runtime_session() as lease:
            worker.start()
            try:
                yield lease
            finally:
                stop.set()
                worker.join()

    def run_mission(
        self,
        mission_input: MissionInput,
        *,
        hyper_agent: HyperAgent,
        context_coordination: ContextCoordination,
        fsm_runner: FSMRunner,
        maneuver_control: ManeuverControl,
        environment_step: Callable[[], object],
        bayesian_belief_service: BayesianBeliefService | None = None,
        summarizer: MissionLogSummarizer | None = None,
        model: Any | None = None,
    ) -> RuntimeRunResult:
        """Run one mission while publishing a bounded, non-authoritative lease."""
        with self.mission_session(
            mission_input.mission_id,
            summarizer=summarizer,
            model=model,
        ):
            return self._run_mission(
                mission_input,
                hyper_agent=hyper_agent,
                context_coordination=context_coordination,
                fsm_runner=fsm_runner,
                maneuver_control=maneuver_control,
                environment_step=environment_step,
                bayesian_belief_service=bayesian_belief_service,
            )

    def _logger(self) -> OperationalLog:
        logger = self.operational_log
        if logger is None:
            logger = FileOperationalLog(self.config.storage.root / "operational-log")
        return logger

    def create_chat_model(
        self,
        *,
        mission_id: str | None = None,
        debug_scope: str = "runtime",
    ) -> ChatOpenAI:
        """Create the configured OpenAI-compatible chat model."""

        llm = self.config.llm
        options: dict[str, Any] = {}
        recorder: LLMResponseRecorder | None = None
        agent_recorder: AgentDebugRecorder | None = None
        if self.config.debug and mission_id:
            recorder = LLMResponseRecorder(
                self.config.storage.root.parent / "debug" / "llm",
                mission_id,
                role=debug_scope,
            )
            agent_recorder = AgentDebugRecorder(
                self.config.storage.root.parent / "debug" / "agent",
                mission_id,
                role=debug_scope,
            )
            options["http_client"] = recorder.http_client
        model = ChatOpenAI(
            base_url=llm.base_url,
            model=llm.model,
            api_key=cast(Any, llm.api_key),
            temperature=llm.temperature,
            timeout=120.0,
            max_retries=1,
            **options,
        )
        if recorder is not None:
            object.__setattr__(model, "_llm_response_recorder", recorder)
        if agent_recorder is not None:
            object.__setattr__(model, "_agent_debug_recorder", agent_recorder)
        return model

    def verify_llm_reachability(self, *, timeout: float = 5.0) -> None:
        """Verify the configured vLLM endpoint before composing live agents."""

        llm = self.config.llm
        probe_vllm_reachability(
            llm.base_url,
            llm.model,
            api_key=llm.api_key,
            temperature=llm.temperature,
            timeout=timeout,
        )

    def create_mission_log_summarizer(
        self,
        *,
        model: Any | None = None,
        operational_log: OperationalLog | None = None,
    ) -> MissionLogSummarizer:
        """Compose the independent heartbeat summarizer for configured storage."""

        return FileMissionLogSummarizer(
            operational_log or self._logger(),
            self.config.storage.root,
            model if model is not None else self.create_chat_model(),
        )

    def heartbeat(
        self,
        mission_id: str,
        *,
        summarizer: MissionLogSummarizer | None = None,
    ) -> SummaryArtifact | None:
        """Run one mission-log summarizer heartbeat."""

        selected = summarizer or self.create_mission_log_summarizer()
        return selected.heartbeat(mission_id)

    def create_planners(self, artifact_root: Path) -> dict[object, object]:
        """Compose both configured real planner facades with persistent artifacts."""

        root = Path(artifact_root).expanduser().resolve()
        return {
            "temporal": TemporalPlanning(
                MiniZincExecutor(
                    executable=self.config.planners.temporal.entrypoint,
                    artifact_root=root / "temporal",
                    timeout_seconds=self.config.planners.temporal.timeout_seconds,
                )
            ),
            "symbolic": SymbolicPlanning(
                FastDownwardExecutor(
                    executable=self.config.planners.symbolic.entrypoint,
                    artifact_root=root / "symbolic",
                    timeout_seconds=self.config.planners.symbolic.timeout_seconds,
                )
            ),
        }

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
            handler = PlanningCommandHandler(
                self.transport, planner, topic=topic, operational_log=self._logger()
            )
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
            operational_log=self._logger(),
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
            operational_log=self._logger(),
        )

    def create_bayesian_belief_service(
        self,
        *,
        mission_id: str,
        keys: Iterable[BeliefKey] | None = None,
        constraints: Iterable[ForbiddenBeliefCombination] | None = None,
        particle_count: int | None = None,
        transition_probability: float | None = None,
        seed: int | None = None,
        observation_topic: str = "belief-observations",
        context_topic: str = "normalized-plans",
        clock: Callable[[], str] | None = None,
    ) -> BayesianBeliefService:
        """Compose the durable event-driven Bayesian belief application service."""

        selected_keys = None if keys is None else tuple(keys)
        selected_constraints = None if constraints is None else tuple(constraints)
        store = FileBayesianBeliefStore(self.config.storage.root)
        checkpoint = store.load_checkpoint(mission_id)
        if checkpoint is None:
            if selected_keys is None:
                raise ValueError("belief keys are required when no checkpoint exists")
            manager = BayesianBeliefManager(
                mission_id,
                selected_keys,
                constraints=selected_constraints or (),
                particle_count=1024 if particle_count is None else particle_count,
                transition_probability=(
                    0.0 if transition_probability is None else transition_probability
                ),
                seed=0 if seed is None else seed,
            )
        else:
            manager = BayesianBeliefManager.from_checkpoint(checkpoint)
            if selected_keys is not None and tuple(sorted(selected_keys)) != manager.keys:
                raise ValueError("configured belief keys do not match the durable checkpoint")
            if selected_constraints is not None and tuple(
                sorted(selected_constraints, key=lambda item: item.constraint_id)
            ) != manager.constraints:
                raise ValueError("configured belief constraints do not match the durable checkpoint")
            if particle_count is not None and particle_count != manager.particle_count:
                raise ValueError("configured particle count does not match the durable checkpoint")
            if transition_probability is not None:
                if isinstance(transition_probability, bool) or not isinstance(
                    transition_probability, (int, float)
                ):
                    raise ValueError("configured transition probability must be numeric")
                if float(transition_probability) != manager.transition_probability:
                    raise ValueError(
                        "configured transition probability does not match the durable checkpoint"
                    )
            if seed is not None:
                raise ValueError("seed cannot be supplied when resuming a durable checkpoint")
        subscription = BayesianBeliefService.subscription_for(
            mission_id,
            observation_topic=observation_topic,
        )
        if subscription not in self.transport.subscriptions:
            self.transport.subscriptions = self.transport.subscriptions + (subscription,)
        return BayesianBeliefService(
            manager,
            store,
            self.transport,
            observation_topic=observation_topic,
            context_topic=context_topic,
            subscription=subscription,
            clock=clock,
        )

    def create_maneuver_control(
        self,
        adapter: ManeuverAdapter,
        decision_provider: object | None = None,
        *,
        target_service: str = "maneuver-adapter",
        model: Any | None = None,
        system_prompt: str | None = None,
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
                model = self.create_chat_model(
                    mission_id=mission_id,
                    debug_scope="maneuver-control",
                )
            if memory_store is None:
                memory_store = FileMissionMemoryStore(self.config.storage.root / "mission-memory")
            context_backend_root = backend_root
            if context_backend_root is None and skill_catalog is not None:
                context_backend_root = self.config.storage.root
            decision_provider = DeepAgentsDecisionProvider(
                create_deep_maneuver_control_agent(
                    model=model,
                    system_prompt=system_prompt,
                    mission_id=mission_id,
                    memory_store=memory_store,
                    skill_catalog=skill_catalog,
                    skill_version=skill_version,
                    backend_root=context_backend_root,
                ),
                max_retries=self.config.agents.maneuver_control.output_structure_retry.max_retries,
            )

        return ManeuverControl(
            cast(Any, self.transport),
            adapter,
            decision_provider,
            target_service=target_service,
            operational_log=self._logger(),
        )

    def create_hyper_agent(
        self,
        interpreter: object | None = None,
        planner: object | None = None,
        *,
        planning_intent_interpreter: object | None = None,
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
                model = self.create_chat_model(
                    mission_id=mission_id,
                    debug_scope="hyper-agent",
                )
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
                ),
                max_retries=self.config.agents.hyper_agent.output_structure_retry.max_retries,
            )
            if planning_intent_interpreter is None:
                planning_intent_interpreter = DeepAgentsPlanningIntentInterpreter(
                    create_planning_intent_agent(
                        model=model,
                        system_prompt=system_prompt,
                        mission_id=mission_id,
                        memory_store=memory_store,
                        skill_catalog=skill_catalog,
                        skill_version=skill_version,
                        backend_root=context_backend_root,
                    ),
                    max_retries=(
                        self.config.agents.hyper_agent.output_structure_retry.max_retries
                    ),
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
            planning_intent_interpreter=planning_intent_interpreter,
            planner=planner,
            planners=selected,
            transport=self.transport,
            mission_spec_topic=mission_spec_topic,
            normalized_plan_topic=normalized_plan_topic,
            replan_topic=replan_topic,
            operational_log=self._logger(),
        )

    def _run_mission(
        self,
        mission_input: MissionInput,
        *,
        hyper_agent: HyperAgent,
        context_coordination: ContextCoordination,
        fsm_runner: FSMRunner,
        maneuver_control: ManeuverControl,
        environment_step: Callable[[], object],
        bayesian_belief_service: BayesianBeliefService | None = None,
    ) -> RuntimeRunResult:
        """Run one deterministic MissionInput-to-authoritative-feedback seam."""

        if not isinstance(mission_input, MissionInput):
            raise TypeError("run_mission requires a MissionInput")
        if not callable(environment_step):
            raise TypeError("environment_step must be callable")
        mission_id = mission_input.mission_id
        logger = self._logger()
        logger.emit(
            mission_id,
            "runtime",
            "agent",
            "started",
            details={"operation": "run_mission"},
        )
        if context_coordination.subscription.mission_id != mission_id:
            raise ValueError("Context Coordination mission ID does not match MissionInput")
        fsm_subscription = fsm_runner.subscription or FSMRunner.subscription_for(
            mission_id,
            service_id=self.config.services.fsm_runner,
        )
        if fsm_subscription.mission_id != mission_id:
            raise ValueError("FSM Runner mission ID does not match MissionInput")
        if (
            bayesian_belief_service is not None
            and bayesian_belief_service.manager.mission_id != mission_id
        ):
            raise ValueError("Bayesian belief service mission ID does not match MissionInput")
        if (
            bayesian_belief_service is not None
            and bayesian_belief_service.context_topic != context_coordination.input_topic
        ):
            raise ValueError("Bayesian belief output topic must be Context Coordination input")

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
        if (
            bayesian_belief_service is not None
            and bayesian_belief_service.subscription not in self.transport.subscriptions
        ):
            self.transport.subscriptions = self.transport.subscriptions + (
                bayesian_belief_service.subscription,
            )

        def consume_event(consumer: Any, event_kind: str) -> TransportEvent:
            delivery = consumer.receive()
            if delivery is None or not isinstance(delivery.message, TransportEvent):
                if delivery is not None:
                    delivery.nack()
                logger.emit(
                    mission_id,
                    "runtime",
                    "error",
                    "failed",
                    details={"operation": "consume_transport_event", "error_type": "RuntimeError"},
                )
                raise RuntimeError(f"expected a {event_kind} transport event")
            if delivery.message.event_kind != event_kind:
                delivery.nack()
                logger.emit(
                    mission_id,
                    "runtime",
                    "error",
                    "failed",
                    details={"operation": "consume_transport_event", "error_type": "RuntimeError"},
                )
                raise RuntimeError(
                    f"expected a {event_kind} transport event, got {delivery.message.event_kind}"
                )
            event = delivery.message
            delivery.ack()
            logger.emit(
                event.mission_id,
                "runtime",
                "transport",
                "received",
                details={
                    "event_id": event.event_id,
                    "event_kind": event.event_kind,
                    "topic": event_kind,
                    "transport_sequence": event.sequence,
                },
            )
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
            belief_consumer = (
                consumers.enter_context(
                    self.transport.open_consumer(bayesian_belief_service.subscription)
                )
                if bayesian_belief_service is not None
                else None
            )

            authority = hyper_agent.freeze_mission(mission_input)
            if not isinstance(authority, FrozenMissionSpec) or authority.mission_id != mission_id:
                raise RuntimeError("Hyper Agent did not return frozen mission authority")
            if authority.content_hash != authority.sha256:
                raise RuntimeError("frozen mission authority hash is invalid")
            logger.emit(
                mission_id,
                "runtime",
                "agent",
                "completed",
                details={"operation": "freeze_mission", "revision": authority.revision},
            )

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
            logger.emit(
                mission_id,
                "runtime",
                "heartbeat",
                "completed",
                details={"operation": "hyper_heartbeat", "plan_revision": heartbeat.plan_revision},
            )
            plan = heartbeat.plan
            if (
                not isinstance(plan, NormalizedPlan)
                or plan.outcome is not PlanningOutcome.SOLVED
                or len(plan.maneuvers) != 1
                or plan.mission_spec != authority.mission_spec
            ):
                raise RuntimeError("initial Hyper heartbeat did not produce one solved maneuver")
            logger.emit(
                mission_id,
                "runtime",
                "planning",
                "solved",
                details={"operation": "initial_plan", "plan_revision": plan.plan_revision},
            )

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
            logger.emit(
                mission_id,
                "runtime",
                "fsm",
                "activated",
                details={"plan_revision": activated.plan_revision, "state": activated.active_state},
            )

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
            logger.emit(
                mission_id,
                "runtime",
                "control",
                "completed",
                details={
                    "command_id": command.command_id,
                    "maneuver_id": command.maneuver_id,
                    "plan_revision": command.plan_revision,
                },
            )

            try:
                environment_step()
            except Exception as exc:
                logger.emit(
                    mission_id,
                    "runtime",
                    "error",
                    "failed",
                    details={"operation": "environment_step", "error_type": type(exc).__name__},
                )
                raise
            logger.emit(
                mission_id,
                "runtime",
                "environment",
                "completed",
                details={"operation": "environment_step"},
            )
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
            logger.emit(
                mission_id,
                "runtime",
                "fsm",
                "transitioned",
                details={"state": final_status.active_state, "status": final_status.status},
            )

            belief_snapshot: BayesianBeliefSnapshot | None = None
            belief_context_snapshot: MissionSnapshot | None = None
            belief_heartbeat: HyperHeartbeatResult | None = None
            if bayesian_belief_service is not None:
                if belief_consumer is None:
                    raise RuntimeError("Bayesian belief consumer was not composed")
                belief_snapshot = bayesian_belief_service.run_once(belief_consumer)
                if belief_snapshot is None:
                    raise RuntimeError("Bayesian belief service did not consume an observation")
                belief_context_snapshot = context_coordination.run_once(context_consumer)
                if (
                    belief_context_snapshot is None
                    or belief_context_snapshot.source_revisions[
                        "bayesian_belief_snapshot"
                    ]
                    != belief_snapshot.belief_revision
                    or belief_context_snapshot.source_hashes[
                        "bayesian_belief_snapshot"
                    ]
                    != belief_snapshot.content_sha256
                ):
                    raise RuntimeError("Context Coordination did not authorize the belief artifact")
                reference = belief_context_snapshot.source_references[
                    "bayesian_belief_snapshot"
                ]
                content_hash = belief_context_snapshot.source_hashes[
                    "bayesian_belief_snapshot"
                ]
                if reference is None or content_hash is None:
                    raise RuntimeError("belief artifact provenance is incomplete")
                durable_belief = bayesian_belief_service.load_snapshot_reference(
                    reference, content_hash
                )
                if durable_belief != belief_snapshot:
                    raise RuntimeError("durable Bayesian belief artifact does not match the update")
                belief_heartbeat = hyper_agent.heartbeat(
                    belief_context_snapshot,
                    mission_id=mission_id,
                    snapshot_id=(
                        f"{mission_id}:snapshot:{belief_context_snapshot.version}"
                    ),
                    belief_snapshot=durable_belief,
                )
                if belief_heartbeat.plan_revision != plan.plan_revision + 1:
                    raise RuntimeError("belief-backed Hyper heartbeat did not replan normally")

            return RuntimeRunResult(
                authority=authority,
                plan=plan,
                context_snapshot=belief_context_snapshot or context_snapshot,
                status_before_feedback=status_before_feedback,
                decision=decision,
                command=command,
                scene_graph=scene_graph,
                feedback=feedback,
                final_status=final_status,
                belief_snapshot=belief_snapshot,
                belief_context_snapshot=belief_context_snapshot,
                belief_heartbeat=belief_heartbeat,
            )



    def run_planning_mission(
        self,
        mission_input: MissionInput,
        *,
        hyper_agent: HyperAgent,
        context_coordination: ContextCoordination,
        environment_heartbeat: Callable[[], object],
        generate: Callable[
            [PlannerChoiceRecord, MissionSnapshot, TransportEvent],
            PlannerGenerationAttempt,
        ],
        summarizer: MissionLogSummarizer | None = None,
        model: Any | None = None,
    ) -> PlanningMissionRunResult:
        """Run scene-backed planner selection/generation without a MissionSpec."""

        with self.mission_session(
            mission_input.mission_id,
            summarizer=summarizer,
            model=model,
        ):
            return self._run_planning_mission(
                mission_input,
                hyper_agent=hyper_agent,
                context_coordination=context_coordination,
                environment_heartbeat=environment_heartbeat,
                generate=generate,
            )

    def _run_planning_mission(
        self,
        mission_input: MissionInput,
        *,
        hyper_agent: HyperAgent,
        context_coordination: ContextCoordination,
        environment_heartbeat: Callable[[], object],
        generate: Callable[
            [PlannerChoiceRecord, MissionSnapshot, TransportEvent],
            PlannerGenerationAttempt,
        ],
    ) -> PlanningMissionRunResult:
        if not isinstance(mission_input, MissionInput):
            raise TypeError("run_planning_mission requires a MissionInput")
        if not callable(environment_heartbeat) or not callable(generate):
            raise TypeError("planning Mission Run requires environment and generation callables")
        mission_id = mission_input.mission_id
        if context_coordination.subscription.mission_id != mission_id:
            raise ValueError("Context Coordination mission ID does not match MissionInput")

        scene_subscription = Subscription(
            "runtime-planning-scene-observer",
            mission_id,
            "operational-scene-graph",
        )
        for subscription in (context_coordination.subscription, scene_subscription):
            if subscription not in self.transport.subscriptions:
                self.transport.subscriptions += (subscription,)

        logger = self._logger()
        logger.emit(
            mission_id,
            "runtime",
            "agent",
            "started",
            details={"operation": "run_planning_mission"},
        )
        with (
            self.transport.open_consumer(context_coordination.subscription) as context_consumer,
            self.transport.open_consumer(scene_subscription) as scene_consumer,
        ):
            environment_heartbeat()
            scene_delivery = scene_consumer.receive()
            if (
                scene_delivery is None
                or not isinstance(scene_delivery.message, TransportEvent)
                or scene_delivery.message.event_kind != "operational_scene_graph"
            ):
                if scene_delivery is not None:
                    scene_delivery.nack()
                logger.emit(
                    mission_id,
                    "runtime",
                    "planning-scene-evidence",
                    str(PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE),
                    details={"operation": "environment_heartbeat"},
                )
                return PlanningMissionRunResult(
                    outcome=PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE,
                )
            scene_graph = scene_delivery.message
            scene_delivery.ack()

            snapshot = context_coordination.run_once(context_consumer)
            if (
                snapshot is None
                or snapshot.operational_scene_graph != scene_graph.event_id
            ):
                logger.emit(
                    mission_id,
                    "runtime",
                    "planning-scene-evidence",
                    str(PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE),
                    details={"operation": "context_coordination"},
                )
                return PlanningMissionRunResult(
                    outcome=PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE,
                    context_snapshot=snapshot,
                    scene_graph=scene_graph,
                )
            heartbeat = hyper_agent.planning_heartbeat(
                mission_input,
                snapshot,
                scene_graph,
                generate,
            )
            if not isinstance(heartbeat, HyperPlanningHeartbeatResult):
                raise RuntimeError("Hyper Agent did not publish planning heartbeat evidence")
            if heartbeat.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE:
                logger.emit(
                    mission_id,
                    "runtime",
                    "heartbeat",
                    str(heartbeat.outcome),
                    details={"operation": "hyper_planning_heartbeat"},
                )
                return PlanningMissionRunResult(
                    outcome=heartbeat.outcome,
                    context_snapshot=snapshot,
                    scene_graph=scene_graph,
                )
            if heartbeat.attempt is None or heartbeat.planner_choice is None:
                raise RuntimeError("planning attempt evidence is incomplete")

            logger.emit(
                mission_id,
                "runtime",
                "heartbeat",
                "completed",
                details={
                    "operation": "hyper_planning_heartbeat",
                    "snapshot_id": heartbeat.mission_snapshot_id,
                    "attempt_id": heartbeat.attempt.attempt_id,
                    "decision_id": heartbeat.planner_choice.decision_id,
                },
            )
            return PlanningMissionRunResult(
                planner_choice=heartbeat.planner_choice,
                outcome=heartbeat.outcome,
                attempt=heartbeat.attempt,
                context_snapshot=snapshot,
                scene_graph=scene_graph,
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
