"""Minimal runtime composition without mission-authority state."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.file_transport import FileTransport
from onr.adapters.fsm_store import JsonFSMStateStore
from onr.adapters.human_decisions import FileHumanDecisionStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.minizinc import MiniZincExecutor
from onr.adapters.mission_log_summarizer import (
    FileMissionLogSummarizer,
    SummarizationError,
)
from onr.adapters.mission_memory import FileMissionMemoryStore
from onr.adapters.operational_log import FileOperationalLog
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.adapters.val import VALPlanValidator
from onr.adapters.vllm_reachability import probe_vllm_reachability
from onr.agents.hyper_agent import (
    DeepAgentsHyperHeartbeatProvider,
    DeepAgentsPlanningIntentInterpreter,
    create_hyper_heartbeat_agent,
    create_planning_intent_agent,
)
from onr.agents.hyper_workflow import (
    DeepAgentsHyperWorkflow,
    HyperWorkflowContext,
    create_hyper_workflow_agent,
)
from onr.agents.maneuver_control import (
    DeepAgentsDecisionProvider,  # noqa: F401 - retained public composition seam
    DeepAgentsManeuverProvider,
)
from onr.agents.maneuver_control import (
    create_maneuver_control_agent as create_deep_maneuver_control_agent,
)
from onr.application.bayesian_belief import (
    BayesianBeliefManager,
    BayesianBeliefService,
)
from onr.application.communication import TransportCommunicationPort
from onr.application.context_coordination import ContextCoordination
from onr.application.fsm import FSMRunner
from onr.application.human_decisions import HumanDecisionCoordinator
from onr.application.hyper_agent import (
    HyperAgent,
    HyperPlanningHeartbeatResult,
    PlanningHeartbeatOutcome,
)
from onr.application.hyper_supervisor import HyperSupervisor
from onr.application.maneuver_control import ManeuverControl
from onr.contracts.bayesian_belief import (
    BayesianBeliefSnapshot,
    BeliefKey,
    ForbiddenBeliefCombination,
)
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverFeedback
from onr.contracts.human_decision import (
    HumanDecision,
    HumanDecisionDisposition,
    HumanDecisionRequest,
    HumanDecisionResolution,
    RunCheckpoint,
)
from onr.contracts.hyper_agent import MissionInput, ReplanRequest
from onr.contracts.maneuver_control import (
    ManeuverCommand,
    ManeuverControlDecision,
)
from onr.contracts.planner_translation import (
    PlanningTranslationOutcome,
    PlanningTranslationResult,
)
from onr.contracts.planning import NormalizedPlan
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
)
from onr.contracts.transport import (
    TransportEvent,
)
from onr.ports.maneuver import ManeuverAdapter
from onr.ports.mission_log_summarizer import MissionLogSummarizer, SummaryArtifact
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Subscription
from onr.runtime.agent_debug import AgentDebugRecorder
from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.runtime.lease import RuntimeLeaseStore
from onr.runtime.llm_debug import LLMResponseRecorder


@dataclass(frozen=True, slots=True)
class RuntimeRunResult:
    """Evidence returned by one synchronous, file-backed runtime run."""

    plan: NormalizedPlan
    context_snapshot: MissionSnapshot
    status_before_feedback: FSMStatus
    decision: ManeuverControlDecision
    command: ManeuverCommand
    environment_event: TransportEvent
    feedback: ManeuverFeedback
    final_status: FSMStatus


@dataclass(frozen=True, slots=True)
class PlanningMissionRunResult:
    """Evidence returned by one planner-native, environment-backed Mission Run."""

    outcome: PlanningHeartbeatOutcome
    planner_choice: PlannerChoiceRecord | None = None
    attempt: PlannerGenerationAttempt | None = None
    generation_attempts: tuple[PlannerGenerationAttempt, ...] = ()
    context_snapshot: MissionSnapshot | None = None
    environment_event: TransportEvent | None = None
    translation: PlanningTranslationResult | None = None
    human_decision_request: HumanDecisionRequest | None = None
    execution: RuntimeRunResult | None = None


@dataclass(frozen=True, slots=True)
class PlanningMissionDecisionResult:
    """Recorded operator resolution and any deterministically resumed run."""

    resolution: HumanDecisionResolution
    resumed_run: PlanningMissionRunResult | None = None


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
    ) -> RuntimeComposition:
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
        if (
            lease is None
        ):  # guarded by __post_init__; keeps the optional injection type narrow.
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
        plan: NormalizedPlan,
        context_coordination: ContextCoordination,
        fsm_runner: FSMRunner,
        maneuver_control: ManeuverControl,
        environment_step: Callable[[], object],
        summarizer: MissionLogSummarizer | None = None,
        model: Any | None = None,
    ) -> RuntimeRunResult:
        """Reject the retired direct NormalizedPlan execution path."""

        raise RuntimeError(
            "direct NormalizedPlan execution is retired; activate a Statechart "
            "through the Hyper workflow"
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
            top_p=0.95,
            presence_penalty=0.0,
            reasoning_effort="medium",
            streaming=True,
            timeout=800.0,
            max_retries=0,
            extra_body={
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.0,
            },
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

    def create_human_decision_coordinator(self) -> HumanDecisionCoordinator:
        """Compose durable Human Decision pause and resume coordination."""

        return HumanDecisionCoordinator(
            FileHumanDecisionStore(self.config.storage.root / "human-decisions")
        )

    def create_fsm_runner(
        self,
        *,
        mission_id: str,
    ) -> FSMRunner:
        """Compose the pure FSM service with the selected transport and JSON store."""

        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission ID must be a non-empty string")
        subscription = FSMRunner.subscription_for(
            mission_id,
            service_id=self.config.services.fsm_runner,
        )
        if subscription not in self.transport.subscriptions:
            self.transport.subscriptions = (*self.transport.subscriptions, subscription)
        return FSMRunner(
            self.transport,
            store=JsonFSMStateStore(self.config.storage.root / "fsm" / mission_id),
            subscription=subscription,
            operational_log=self._logger(),
            machine_factory=PythonStateMachineFactory(),
        )

    def create_context_coordination(
        self,
        *,
        mission_id: str,
        clock: Callable[[], str] | None = None,
        input_topic: str = "normalized-plans",
        snapshot_topic: str = "mission-snapshots",
        environment: object | None = None,
        fsm_runner: object | None = None,
        maneuver_control: object | None = None,
        hyper_supervisor: object | None = None,
        belief_service: object | None = None,
        replan_workflow: Callable[..., object] | None = None,
        maneuver_seconds: float = 5,
        hyper_seconds: float = 10,
        simulation_limit_seconds: float = 600,
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
            self.transport.subscriptions = (*self.transport.subscriptions, subscription)
        return ContextCoordination(
            cast(Any, self.transport),
            mission_id,
            input_topic=input_topic,
            snapshot_topic=snapshot_topic,
            service_id=self.config.services.context_coordination,
            clock=clock,
            subscription=subscription,
            operational_log=self._logger(),
            environment=environment,
            fsm_runner=fsm_runner,
            maneuver_control=maneuver_control,
            hyper_supervisor=hyper_supervisor,
            belief_service=belief_service,
            replan_workflow=cast(Any, replan_workflow),
            maneuver_seconds=maneuver_seconds,
            hyper_seconds=hyper_seconds,
            simulation_limit_seconds=simulation_limit_seconds,
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
            if (
                selected_keys is not None
                and tuple(sorted(selected_keys)) != manager.keys
            ):
                raise ValueError(
                    "configured belief keys do not match the durable checkpoint"
                )
            if (
                selected_constraints is not None
                and tuple(
                    sorted(selected_constraints, key=lambda item: item.constraint_id)
                )
                != manager.constraints
            ):
                raise ValueError(
                    "configured belief constraints do not match the durable checkpoint"
                )
            if particle_count is not None and particle_count != manager.particle_count:
                raise ValueError(
                    "configured particle count does not match the durable checkpoint"
                )
            if transition_probability is not None:
                if isinstance(transition_probability, bool) or not isinstance(
                    transition_probability, (int, float)
                ):
                    raise ValueError(
                        "configured transition probability must be numeric"
                    )
                if float(transition_probability) != manager.transition_probability:
                    raise ValueError(
                        "configured transition probability does not match the "
                        "durable checkpoint"
                    )
            if seed is not None:
                raise ValueError(
                    "seed cannot be supplied when resuming a durable checkpoint"
                )
        subscription = BayesianBeliefService.subscription_for(
            mission_id,
            observation_topic=observation_topic,
        )
        if subscription not in self.transport.subscriptions:
            self.transport.subscriptions = (*self.transport.subscriptions, subscription)
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
        fsm_runner: FSMRunner | None = None,
        environment_authority: object | None = None,
        belief_service: BayesianBeliefService | None = None,
        communication_port: object | None = None,
    ) -> ManeuverControl:
        """Compose tool-driven Maneuver Control with opaque live dependencies."""

        if decision_provider is None:
            if mission_id is None:
                raise ValueError(
                    "create_maneuver_control requires a provider or model and "
                    "Mission ID"
                )
            if model is None:
                model = self.create_chat_model(
                    mission_id=mission_id,
                    debug_scope="maneuver-control",
                )
            if memory_store is None:
                memory_store = FileMissionMemoryStore(
                    self.config.storage.root / "mission-memory"
                )
            context_backend_root = backend_root
            if context_backend_root is None and skill_catalog is not None:
                context_backend_root = self.config.storage.root
            decision_provider = DeepAgentsManeuverProvider(
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
            fsm_runner=fsm_runner,
            environment_authority=environment_authority,
            belief_service=belief_service,
            communication_port=communication_port,
        )

    def create_communication_port(
        self,
        *,
        hyper_handler: Callable[[Any], object] | None = None,
    ) -> TransportCommunicationPort:
        """Compose the shared correlated agent-message registry."""

        port = TransportCommunicationPort(cast(Any, self.transport))

        def handle_hyper_message(message: Any) -> Mapping[str, object]:
            if message.recipient != "hyper-agent":
                raise ValueError("Hyper communication recipient does not match")
            if str(message.kind) == "replan":
                raw = message.payload.get("replan_request")
                if not isinstance(raw, Mapping):
                    raise ValueError("Hyper replan message lacks ReplanRequest")
                request = ReplanRequest.from_dict(raw)
                return {
                    "status": "received",
                    "disposition": "no_change",
                    "replan_request": request.to_dict(),
                }
            return {
                "status": "received",
                "kind": str(message.kind),
                "message": message.payload.get("message"),
            }

        port.register("hyper-agent", hyper_handler or handle_hyper_message)
        return port

    def create_hyper_agent(
        self,
        interpreter: object | None = None,
        *,
        model: Any | None = None,
        system_prompt: str | None = None,
        mission_id: str | None = None,
        memory_store: object | None = None,
        skill_catalog: object | None = None,
        skill_version: str | None = None,
        backend_root: Path | None = None,
        planning_evidence_topic: str = "planning-evidence",
    ) -> HyperAgent:
        """Compose Hyper Agent with a PlanningIntent interpreter."""

        if interpreter is None:
            if model is None:
                model = self.create_chat_model(
                    mission_id=mission_id,
                    debug_scope="hyper-agent",
                )
            if mission_id is not None and memory_store is None:
                memory_store = FileMissionMemoryStore(
                    self.config.storage.root / "mission-memory"
                )
            context_backend_root = backend_root
            if context_backend_root is None and skill_catalog is not None:
                context_backend_root = self.config.storage.root
            interpreter = DeepAgentsPlanningIntentInterpreter(
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
        return HyperAgent(
            interpreter,
            transport=self.transport,
            planning_evidence_topic=planning_evidence_topic,
            operational_log=self._logger(),
        )

    def create_hyper_supervisor(
        self,
        *,
        model: Any | None = None,
        system_prompt: str,
        mission_id: str,
        memory_store: object | None = None,
        skill_catalog: object | None = None,
        skill_version: str | None = None,
        backend_root: Path | None = None,
    ) -> HyperSupervisor:
        """Compose independent Hyper evaluation episodes with Mission Memory only."""

        if model is None:
            model = self.create_chat_model(
                mission_id=mission_id,
                debug_scope="hyper-agent-supervisor",
            )
        if memory_store is None:
            memory_store = FileMissionMemoryStore(
                self.config.storage.root / "mission-memory"
            )
        agent = create_hyper_heartbeat_agent(
            model=model,
            system_prompt=system_prompt,
            mission_id=mission_id,
            memory_store=memory_store,
            skill_catalog=skill_catalog,
            skill_version=skill_version,
            backend_root=backend_root,
        )
        provider = DeepAgentsHyperHeartbeatProvider(
            agent,
            max_retries=(
                self.config.agents.hyper_agent.output_structure_retry.max_retries
            ),
        )
        return HyperSupervisor(
            provider,
            transport=self.transport,
            operational_log=self._logger(),
        )

    def create_hyper_workflow(
        self,
        *,
        model: Any | None = None,
        system_prompt: str,
        mission_id: str,
        memory_store: object | None = None,
        skill_catalog: object | None = None,
        skill_version: str | None = None,
        backend_root: Path | None = None,
        checkpointer: object | None = None,
        artifact_root: Path | None = None,
    ) -> DeepAgentsHyperWorkflow:
        """Compose one checkpointed Deep Agent for the complete Hyper workflow."""

        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("Hyper workflow requires a Mission ID")
        if model is None:
            model = self.create_chat_model(
                mission_id=mission_id,
                debug_scope="hyper-agent",
            )
        if memory_store is None:
            memory_store = FileMissionMemoryStore(
                self.config.storage.root / "mission-memory"
            )
        context_backend_root = backend_root
        if context_backend_root is None and skill_catalog is not None:
            context_backend_root = self.config.storage.root
        planner_workspace_location = None
        if artifact_root is not None:
            if context_backend_root is None:
                raise ValueError(
                    "Hyper workflow planner workspace requires a backend root"
                )
            try:
                relative_workspace = (
                    Path(artifact_root).resolve() / "workspace"
                ).relative_to(Path(context_backend_root).resolve())
            except ValueError as exc:
                raise ValueError(
                    "Hyper workflow planner workspace is outside the backend root"
                ) from exc
            planner_workspace_location = "/" + relative_workspace.as_posix()
        graph = create_hyper_workflow_agent(
            model=model,
            system_prompt=system_prompt,
            mission_id=mission_id,
            memory_store=memory_store,
            skill_catalog=skill_catalog,
            skill_version=skill_version,
            backend_root=context_backend_root,
            checkpointer=(InMemorySaver() if checkpointer is None else checkpointer),
            planner_workspace_location=planner_workspace_location,
        )
        return DeepAgentsHyperWorkflow(graph)

    def create_hyper_workflow_context(
        self,
        mission_input: MissionInput,
        mission_snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        environment_file: Path,
        *,
        artifact_root: Path,
        belief_snapshot: BayesianBeliefSnapshot | None = None,
        backend_root: Path | None = None,
        fsm_runner: FSMRunner | None = None,
        environment_authority: object | None = None,
        belief_service: BayesianBeliefService | None = None,
        communication_port: object | None = None,
    ) -> HyperWorkflowContext:
        """Bind one Mission Run's authorized evidence to workflow planner tools."""

        validated_belief = HyperAgent.validate_belief_provenance(
            mission_snapshot, belief_snapshot
        )
        validator_entrypoint = self.config.planners.symbolic.validator_entrypoint
        if validator_entrypoint is None:
            raise ValueError("symbolic planner VAL entrypoint is not configured")
        planner_workspace_location = None
        if backend_root is not None:
            try:
                relative_workspace = (
                    Path(artifact_root).resolve() / "workspace"
                ).relative_to(Path(backend_root).resolve())
            except ValueError as exc:
                raise ValueError(
                    "Hyper workflow planner workspace is outside the backend root"
                ) from exc
            planner_workspace_location = "/" + relative_workspace.as_posix()
        return HyperWorkflowContext(
            mission_input=mission_input,
            mission_snapshot=mission_snapshot,
            environment_event=environment_event,
            environment_file=environment_file,
            belief_snapshot=validated_belief,
            artifact_root=artifact_root,
            backend_root=backend_root,
            planner_workspace_location=planner_workspace_location,
            minizinc_planner=MiniZincExecutor(
                executable=self.config.planners.temporal.entrypoint,
                artifact_root=artifact_root / "minizinc-runs",
                timeout_seconds=self.config.planners.temporal.timeout_seconds,
            ),
            fast_downward_planner=FastDownwardExecutor(
                executable=self.config.planners.symbolic.entrypoint,
                artifact_root=artifact_root / "fast-downward-runs",
                timeout_seconds=self.config.planners.symbolic.timeout_seconds,
            ),
            val_validator=VALPlanValidator(
                executable=validator_entrypoint,
                timeout_seconds=self.config.planners.symbolic.timeout_seconds,
            ),
            max_planner_attempts=(
                self.config.agents.hyper_agent.output_structure_retry.max_retries + 1
            ),
            max_statechart_attempts=(
                self.config.agents.hyper_agent.output_structure_retry.max_retries + 1
            ),
            state_machine_factory=PythonStateMachineFactory(),
            operational_log=self._logger(),
            fsm_runner=fsm_runner,
            environment_authority=environment_authority,
            belief_service=belief_service,
            communication_port=communication_port,
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
        translator: object,
        asset_generator: object,
        human_decision_coordinator: HumanDecisionCoordinator,
        fsm_runner: FSMRunner,
        maneuver_control: ManeuverControl,
        environment_step: Callable[[], object],
        bayesian_belief_service: BayesianBeliefService | None = None,
        summarizer: MissionLogSummarizer | None = None,
        model: Any | None = None,
    ) -> PlanningMissionRunResult:
        """Run environment-backed planner selection and planner-native generation."""

        with self.mission_session(
            mission_input.mission_id,
            summarizer=summarizer,
            model=model,
        ):
            preparation = self._prepare_planning_mission(
                mission_input,
                hyper_agent=hyper_agent,
                context_coordination=context_coordination,
                environment_heartbeat=environment_heartbeat,
                generate=generate,
                bayesian_belief_service=bayesian_belief_service,
            )

            return self._complete_planning_mission(
                mission_input,
                preparation=preparation,
                hyper_agent=hyper_agent,
                translator=translator,
                asset_generator=asset_generator,
                human_decision_coordinator=human_decision_coordinator,
                context_coordination=context_coordination,
                fsm_runner=fsm_runner,
                maneuver_control=maneuver_control,
                environment_step=environment_step,
            )

    def _complete_planning_mission(
        self,
        mission_input: MissionInput,
        *,
        preparation: PlanningMissionRunResult,
        hyper_agent: HyperAgent,
        translator: object,
        asset_generator: object,
        human_decision_coordinator: HumanDecisionCoordinator,
        context_coordination: ContextCoordination,
        fsm_runner: FSMRunner,
        maneuver_control: ManeuverControl,
        environment_step: Callable[[], object],
    ) -> PlanningMissionRunResult:
        previous_revision = (
            preparation.context_snapshot.plan_revision
            if preparation.context_snapshot is not None
            else None
        )
        plan_revision = (previous_revision or 0) + 1
        if not isinstance(human_decision_coordinator, HumanDecisionCoordinator):
            raise TypeError("planning Mission Run requires Human Decision coordination")

        mission_id = mission_input.mission_id
        checkpoint = RunCheckpoint(
            checkpoint_id=f"planning-checkpoint:{mission_id}:{plan_revision}",
            mission_id=mission_id,
            mission_run_id=f"planning-run:{mission_id}:{plan_revision}",
            continuation=f"translate-and-execute:{plan_revision}",
        )

        def paused(
            outcome: object,
            evidence_references: tuple[str, ...],
            translation: PlanningTranslationResult | None = None,
        ) -> PlanningMissionRunResult:
            translated_attempts = (
                translation.generation_attempts if translation is not None else ()
            )
            generation_attempts = preparation.generation_attempts + translated_attempts
            request = human_decision_coordinator.pause_for_outcome(
                outcome,
                checkpoint,
                correlation_id=f"planning:{mission_id}:{plan_revision}",
                evidence_references=evidence_references,
            )
            return PlanningMissionRunResult(
                outcome=preparation.outcome,
                planner_choice=preparation.planner_choice,
                attempt=(generation_attempts[-1] if generation_attempts else None),
                generation_attempts=generation_attempts,
                context_snapshot=preparation.context_snapshot,
                environment_event=preparation.environment_event,
                translation=translation,
                human_decision_request=request,
            )

        if (
            preparation.outcome
            is PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA
        ):
            environment_reference = (
                preparation.environment_event.event_id
                if preparation.environment_event is not None
                else f"environment-data:{mission_id}:missing"
            )
            return paused(preparation.outcome, (environment_reference,))
        if (
            preparation.planner_choice is None
            or preparation.attempt is None
            or preparation.context_snapshot is None
            or preparation.environment_event is None
        ):
            raise RuntimeError("planning preparation evidence is incomplete")

        plan_method = getattr(translator, "plan", None)
        if not callable(plan_method):
            raise TypeError("planner translator must expose plan")
        translation = plan_method(
            mission_input,
            preparation.planner_choice,
            preparation.context_snapshot,
            preparation.environment_event,
            asset_generator,
            plan_revision=plan_revision,
        )
        if not isinstance(translation, PlanningTranslationResult):
            raise TypeError("planner translator returned an invalid result")
        published_attempts = tuple(
            hyper_agent.publish_generation_attempt(
                attempt,
                preparation.planner_choice,
            )
            for attempt in translation.generation_attempts
        )
        if published_attempts != translation.generation_attempts:
            raise RuntimeError(
                "published correction attempts do not match translation evidence"
            )
        latest_attempt = published_attempts[-1]
        if translation.outcome is not PlanningTranslationOutcome.VERIFIED:
            references = list(latest_attempt.asset_references.values())
            if translation.evidence is not None:
                references.extend(
                    str(path)
                    for path in (
                        *translation.evidence.artifact_paths,
                        translation.evidence.stdout_path,
                        translation.evidence.stderr_path,
                    )
                )
            if not references:
                references.append(latest_attempt.attempt_id)
            return paused(
                translation.outcome,
                tuple(sorted(set(references))),
                translation,
            )

        plan = translation.normalized_plan
        if plan is None:
            raise RuntimeError("verified translation did not provide a plan")
        raise RuntimeError(
            "translation-driven NormalizedPlan execution is retired; use the "
            "Hyper planner-artifact to Statechart workflow"
        )

    def _prepare_planning_mission(
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
        bayesian_belief_service: BayesianBeliefService | None = None,
    ) -> PlanningMissionRunResult:
        if not isinstance(mission_input, MissionInput):
            raise TypeError("run_planning_mission requires a MissionInput")
        if not callable(environment_heartbeat) or not callable(generate):
            raise TypeError(
                "planning Mission Run requires environment and generation callables"
            )
        mission_id = mission_input.mission_id
        if context_coordination.subscription.mission_id != mission_id:
            raise ValueError(
                "Context Coordination mission ID does not match MissionInput"
            )

        environment_subscription = Subscription(
            "runtime-environment-observer",
            mission_id,
            "environment-data",
        )
        for subscription in (
            context_coordination.subscription,
            environment_subscription,
        ):
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
            self.transport.open_consumer(
                context_coordination.subscription
            ) as context_consumer,
            self.transport.open_consumer(
                environment_subscription
            ) as environment_consumer,
        ):
            environment_heartbeat()
            environment_delivery = environment_consumer.receive()
            if (
                environment_delivery is None
                or not isinstance(environment_delivery.message, TransportEvent)
                or environment_delivery.message.event_kind != "environment_data"
            ):
                if environment_delivery is not None:
                    environment_delivery.nack()
                logger.emit(
                    mission_id,
                    "runtime",
                    "planning-environment-data",
                    str(PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA),
                    details={"operation": "environment_heartbeat"},
                )
                return PlanningMissionRunResult(
                    outcome=PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA,
                )
            environment_event = environment_delivery.message
            environment_delivery.ack()

            snapshot = context_coordination.run_once(context_consumer)
            if (
                snapshot is None
                or snapshot.environment_data != environment_event.event_id
            ):
                logger.emit(
                    mission_id,
                    "runtime",
                    "planning-environment-data",
                    str(PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA),
                    details={"operation": "context_coordination"},
                )
                return PlanningMissionRunResult(
                    outcome=PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA,
                    context_snapshot=snapshot,
                    environment_event=environment_event,
                )
            belief_source = "bayesian_belief_snapshot"
            belief_revision = snapshot.source_revisions[belief_source]
            belief_reference = snapshot.source_references[belief_source]
            belief_snapshot = None
            if belief_revision is not None or belief_reference is not None:
                if belief_revision is None or belief_reference is None:
                    raise ValueError("MissionSnapshot belief provenance is incomplete")
                if bayesian_belief_service is None:
                    raise ValueError(
                        "planning requires the durable Bayesian belief service"
                    )
                belief_snapshot = bayesian_belief_service.load_snapshot_reference(
                    belief_reference,
                )
            heartbeat = hyper_agent.planning_heartbeat(
                mission_input,
                snapshot,
                environment_event,
                generate,
                belief_snapshot,
            )
            if not isinstance(heartbeat, HyperPlanningHeartbeatResult):
                raise RuntimeError(
                    "Hyper Agent did not publish planning heartbeat evidence"
                )
            if (
                heartbeat.outcome
                is PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA
            ):
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
                    environment_event=environment_event,
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
                generation_attempts=(heartbeat.attempt,),
                context_snapshot=snapshot,
                environment_event=environment_event,
            )

    def resolve_planning_mission(
        self,
        decision: HumanDecision,
        *,
        human_decision_coordinator: HumanDecisionCoordinator,
        resume: Callable[[RunCheckpoint], PlanningMissionRunResult],
    ) -> PlanningMissionDecisionResult:
        """Apply an operator decision, resuming only from its persisted checkpoint."""

        if not isinstance(human_decision_coordinator, HumanDecisionCoordinator):
            raise TypeError("planning Mission Run requires Human Decision coordination")
        if not callable(resume):
            raise TypeError("planning Mission Run resume callback must be callable")
        resolution = human_decision_coordinator.record(decision)
        if resolution.disposition is HumanDecisionDisposition.END:
            return PlanningMissionDecisionResult(resolution)
        checkpoint = resolution.checkpoint
        if checkpoint is None:
            raise RuntimeError("resume resolution did not provide a Run Checkpoint")
        with human_decision_coordinator.resume_claim(resolution) as claimed:
            if not claimed:
                return PlanningMissionDecisionResult(resolution)
            resumed_run = resume(checkpoint)
            if not isinstance(resumed_run, PlanningMissionRunResult):
                raise TypeError(
                    "planning Mission Run resume callback returned an invalid result"
                )
            return PlanningMissionDecisionResult(resolution, resumed_run)


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
