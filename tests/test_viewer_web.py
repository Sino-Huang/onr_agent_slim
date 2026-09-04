from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import quote

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    expect,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError

from onr.adapters.file_transport import FileTransport
from onr.adapters.operational_log import FileOperationalLog
from onr.contracts.fsm import FSMExecutionRecord, Statechart
from onr.contracts.transport import TransportEvent
from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.runtime.lease import RuntimeLeaseStore
from onr.viewer.server import ViewerHTTPServer, create_server
from tests.config_helpers import write_environment_profile

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WEB_ROOT = _REPO_ROOT / "src" / "onr" / "viewer" / "web"
_TEST_PORT = 8799
_LIVE_MISSION = "mission:live"
_SELECTED = re.compile(r"\bselected\b")


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        try:
            yield browser
        finally:
            browser.close()


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _static_server() -> Iterator[str]:
    handler = partial(_QuietStaticHandler, directory=str(_WEB_ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", _TEST_PORT), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, name="viewer-static-test", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{_TEST_PORT}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        assert not thread.is_alive()


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    planner = tmp_path / "planner"
    planner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    planner.chmod(0o755)
    storage = tmp_path / "var" / "storage"
    transport = tmp_path / "var" / "transport"
    environment_profile = write_environment_profile(tmp_path)
    config = tmp_path / "viewer.yaml"
    config.write_text(
        "\n".join(
            (
                "agent_name: test-agent",
                "debug: true",
                f"environment_profile: {environment_profile}",
                "llm:",
                "  provider: openai",
                "  base_url: http://127.0.0.1:1/v1",
                "  model: offline",
                "  api_key: browser-test-private-key",
                "  temperature: 0",
                "planners:",
                "  temporal:",
                f"    entrypoint: {planner}",
                "    timeout_seconds: 1",
                "  symbolic:",
                f"    entrypoint: {planner}",
                "    timeout_seconds: 1",
                "heartbeats:",
                "  hyper_seconds: 1",
                "  maneuver_seconds: 1",
                "  summary_seconds: 30",
                "transport:",
                "  backend: file",
                f"  root: {transport}",
                "storage:",
                f"  root: {storage}",
                f"  planner_artifacts: {tmp_path / 'configured-planner-artifacts'}",
                "services:",
                "  hyper_agent: hyper-agent",
                "  maneuver_control: maneuver-control",
                "  context_coordination: context-coordination",
                "  fsm_runner: fsm-runner",
                "  planner: planner",
                "agents:",
                "  hyper_agent:",
                "    output_structure_retry:",
                "      max_retries: 2",
                "  maneuver_control:",
                "    output_structure_retry:",
                "      max_retries: 1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return config, storage, transport


@contextmanager
def _viewer_server(
    tmp_path: Path, *, world_model_feed: Any | None = None
) -> Iterator[tuple[str, Path, Path]]:
    config, storage, transport = _config(tmp_path)
    lease = RuntimeLeaseStore(storage / "runtime")
    lease.start(session_id="browser-test-session")
    server: ViewerHTTPServer = create_server(
        host="127.0.0.1",
        port=_TEST_PORT,
        repo_root=tmp_path,
        config_path=config,
        world_model_feed=world_model_feed,
    )
    thread = Thread(target=server.serve_forever, name="viewer-web-test", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", storage, transport
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        lease.stop()
        assert not thread.is_alive()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _agent_invocation(
    *,
    sequence: int,
    invocation_id: str,
    parent_id: str | None,
    kind: str,
    name: str,
    input_value: dict[str, object],
    output: object,
    started_at: str,
    finished_at: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": sequence,
        "invocation_id": invocation_id,
        "parent_id": parent_id,
        "agent_role": "hyper-agent",
        "kind": kind,
        "name": name,
        "input": input_value,
        "output": output,
        "error": None,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _seed_live_artifacts(storage: Path, transport_root: Path, repo_root: Path) -> None:
    mission_name = quote(_LIVE_MISSION, safe="._-")
    agent_root = storage.parent / "debug" / "agent" / "hyper-agent" / mission_name
    llm_root = storage.parent / "debug" / "llm" / "hyper-agent" / mission_name

    llm_invocation = _agent_invocation(
        sequence=1,
        invocation_id="llm-live-1",
        parent_id=None,
        kind="llm",
        name="planner_executor",
        input_value={"correlation_id": "corr-live", "attempt": 2},
        output=None,
        started_at="2026-08-21T10:00:00+00:00",
        finished_at="2026-08-21T10:00:02+00:00",
    )
    tool_invocation = _agent_invocation(
        sequence=2,
        invocation_id="tool-live-1",
        parent_id="llm-live-1",
        kind="tool",
        name="run_minizinc",
        input_value={"attempt": 2, "model": "workspace/002/model.mzn"},
        output={"status": "accepted", "objective": 9.5},
        started_at="2026-08-21T10:00:00.500000+00:00",
        finished_at="2026-08-21T10:00:01.500000+00:00",
    )
    mission_invocation = _agent_invocation(
        sequence=3,
        invocation_id="mission-live-1",
        parent_id=None,
        kind="tool",
        name="record_planning_intent",
        input_value={
            "title": "Account for reported events",
            "objective": "Patrol sector seven and account for every reported event.",
            "sector": "sector-7",
            "constraints": ["Remain within the patrol boundary."],
            "issued_at": "2026-08-21T09:59:00+00:00",
            "source_authority": "operator",
            "details": {
                "mission_pattern": "report_event_accounting_patrol",
                "capture_rule": "Observe each event from within sensor range.",
            },
        },
        output={"status": "recorded"},
        started_at="2026-08-21T09:59:58+00:00",
        finished_at="2026-08-21T09:59:59+00:00",
    )
    for sequence, invocation in enumerate(
        (llm_invocation, tool_invocation, mission_invocation), 1
    ):
        _write_json(agent_root / f"{sequence:020d}.json", invocation)

    _write_json(
        llm_root / "00000000000000000001.json",
        {
            "schema_version": 1,
            "request": {"messages": [{"role": "user", "content": "private"}]},
            "response_id": "response-live-1",
            "model": "reasoning-model",
            "status_code": 200,
            "finish_reason": "tool_calls",
            "content": "Planner attempt two was accepted.",
            "function_call": None,
            "reasoning": "The second planner attempt satisfies every constraint.",
            "reasoning_content": "The objective and feasibility checks passed.",
            "reasoning_details": [{"type": "summary", "text": "Validated."}],
            "tool_calls": [
                {
                    "name": "run_minizinc",
                    "args": {"attempt": 2, "model": "workspace/002/model.mzn"},
                    "result": {"status": "accepted", "objective": 9.5},
                    "error": None,
                    "duration_ms": 1000,
                }
            ],
        },
    )

    FileOperationalLog(storage / "operational-log").emit(
        _LIVE_MISSION,
        "hyper-agent",
        "planner-execution",
        "completed",
        details={"attempt": 2, "correlation_id": "corr-live"},
    )
    FileTransport(transport_root).publish_event(
        "maneuver-feedback",
        TransportEvent(
            1,
            "feedback-live",
            _LIVE_MISSION,
            0,
            "maneuver-feedback",
            {
                "schema_version": 1,
                "feedback_id": "feedback-live",
                "mission_id": _LIVE_MISSION,
                "maneuver_id": "maneuver-live",
                "lifecycle": "completed",
                "payload": {
                    "correlation_id": "corr-live",
                    "result": "accepted",
                },
            },
        ),
    )

    summary = SummaryArtifact.create(
        _LIVE_MISSION,
        1,
        1,
        3,
        (),
        "Planner attempt two completed and feedback was observed.",
        created_at="2026-08-21T10:00:03+00:00",
    )
    _write_json(
        storage / "summaries" / _LIVE_MISSION / "00000000000000000001.json",
        summary.to_dict(),
    )

    chart = Statechart(
        mission_id=_LIVE_MISSION,
        plan_revision=2,
        mission_snapshot_id="snapshot-live",
        planning_profile="temporal",
        entry_state="state-0",
        terminal_states=("state-0",),
        states=("state-0",),
        state_context={"state-0": {}},
        transitions=(),
    )
    execution = FSMExecutionRecord(
        mission_id=_LIVE_MISSION,
        plan_revision=2,
        statechart_revision=2,
        active_state="state-0",
    )
    fsm_root = storage / "fsm" / _LIVE_MISSION
    fsm_root.mkdir(parents=True, exist_ok=True)
    (fsm_root / "statechart.json").write_text(
        chart.to_canonical_json(), encoding="utf-8"
    )
    (fsm_root / "execution-record.json").write_text(
        execution.to_canonical_json(), encoding="utf-8"
    )

    model = (
        repo_root / "configured-planner-artifacts" / "workspace" / "002" / "model.mzn"
    )
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("solve satisfy;\n", encoding="utf-8")
    accepted_chart = (
        repo_root
        / "configured-planner-artifacts"
        / "statechart-attempts"
        / "001"
        / "accepted-statechart.json"
    )
    accepted_chart.parent.mkdir(parents=True, exist_ok=True)
    accepted_chart.write_text('{"accepted":true}\n', encoding="utf-8")


@contextmanager
def _diagnostic(page: Page, tmp_path: Path, name: str) -> Iterator[None]:
    try:
        yield
    except BaseException:
        page.screenshot(path=str(tmp_path / f"{name}.png"), full_page=True)
        raise


def _page(
    browser: Browser, *, width: int = 1440, height: int = 1000
) -> tuple[BrowserContext, Page]:
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.set_default_timeout(10_000)
    return context, page


def _browser_errors(page: Page) -> list[str]:
    errors: list[str] = []

    def record_console(message: object) -> None:
        if getattr(message, "type", None) == "error":
            errors.append(str(getattr(message, "text", message)))

    page.on("console", record_console)
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    return errors


def _assert_no_browser_errors(
    errors: list[str], *, allow_mock_endpoint_404s: bool = False
) -> None:
    unexpected = errors
    if allow_mock_endpoint_404s:
        unexpected = [
            error
            for error in errors
            if not (error.startswith("Failed to load resource:") and "404" in error)
        ]
    assert unexpected == []


def _open_mock(page: Page, url: str, fragment: str = "") -> None:
    page.goto(f"{url}/{fragment}", wait_until="networkidle")
    expect(page.get_by_test_id("mock-badge")).to_have_text("demo data")
    expect(page.get_by_test_id("step-row").first).to_be_visible()


def _mock_planner_attempt(page: Page) -> Locator:
    return page.get_by_test_id("step-row").filter(
        has_text="Planner execution (attempt 1)"
    )


def test_mock_page_loads_with_header_aggregates_and_status(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "mock-header-failure"):
                _open_mock(page, url)
                expect(page.get_by_test_id("mission-title")).to_have_text(
                    "Patrol sector, investigate contacts"
                )
                expect(page.get_by_test_id("run-status-pill")).to_have_text("complete")
                expect(page.get_by_test_id("runtime-status")).to_have_text(
                    "Runtime live"
                )
                expect(page.get_by_test_id("mission-picker")).to_have_value(
                    "mission:demo"
                )
                for test_id in (
                    "badge-steps",
                    "badge-llm-calls",
                    "badge-tool-calls",
                    "badge-errors",
                    "badge-duration",
                    "badge-planner-attempts",
                    "badge-statechart-attempts",
                ):
                    expect(
                        page.get_by_test_id(test_id).locator(".badge-value")
                    ).not_to_have_text("—")
                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_trajectory_selection_populates_all_five_detail_tabs(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "trajectory-details-failure"):
                _open_mock(page, url)
                row = _mock_planner_attempt(page)
                expect(row).to_have_count(1)
                row.click()
                expect(row).to_have_class(_SELECTED)
                expect(page.get_by_test_id("detail-panel")).to_contain_text(
                    "Planner execution (attempt 1)"
                )
                expect(page.get_by_test_id("detail-status")).to_have_text("ok")

                tab_expectations = {
                    "reasoning": "Assets persisted",
                    "decision": "checks_failed",
                    "tools": "planner_executor",
                    "feedback": "planner-repair",
                    "raw": "step_id",
                }
                for tab_name, content in tab_expectations.items():
                    tab = page.get_by_test_id(f"detail-tab-{tab_name}")
                    tab.click()
                    expect(tab).to_have_attribute("aria-selected", "true")
                    expect(page.get_by_test_id("detail-body")).to_contain_text(content)

                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_tool_call_cards_expand_and_show_arguments_and_result(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "tool-card-failure"):
                _open_mock(page, url)
                row = _mock_planner_attempt(page)
                inline = (
                    row.locator(
                        "xpath=following-sibling::div[contains(@class, 'step-sub')][1]"
                    )
                    .locator(".tool-inline")
                    .first
                )
                inline_head = inline.locator(".tool-inline-head")
                inline_body = inline.locator(".tool-inline-body")
                expect(inline_head).to_have_attribute("aria-expanded", "false")
                expect(inline_body).to_be_hidden()
                inline_head.click()
                expect(inline_head).to_have_attribute("aria-expanded", "true")
                expect(inline_body).to_be_visible()
                expect(inline_body).to_contain_text("Arguments")
                expect(inline_body).to_contain_text("attempt")
                expect(inline_body).to_contain_text("Result")
                expect(inline_body).to_contain_text("rejected")
                inline_head.click()
                expect(inline_body).to_be_hidden()

                row.click()
                page.get_by_test_id("detail-tab-tools").click()
                tool_card = page.get_by_test_id("tool-card")
                expect(tool_card).to_have_count(1)
                expect(tool_card).to_contain_text("planner_executor")
                expect(tool_card).to_contain_text("Arguments")
                expect(tool_card).to_contain_text("Result")
                expect(tool_card).to_contain_text("rejected")
                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_json_view_formatted_raw_toggle_flips_content(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "json-toggle-failure"):
                _open_mock(page, url)
                _mock_planner_attempt(page).click()
                page.get_by_test_id("detail-tab-raw").click()
                json_view = page.get_by_test_id("detail-body").locator(".jsonview")
                formatted = json_view.get_by_role("button", name="Formatted")
                raw = json_view.get_by_role("button", name="Raw", exact=True)

                expect(formatted).to_have_attribute("aria-pressed", "true")
                expect(json_view.locator(".jv-block").first).to_be_visible()
                expect(json_view.locator(".jv-raw")).to_have_count(0)
                raw.click()
                expect(json_view.locator(".jv-block")).to_have_count(0)
                expect(json_view.locator(".jv-raw")).to_contain_text('"step_id"')
                page.get_by_test_id("detail-tab-decision").click()
                page.get_by_test_id("detail-tab-raw").click()
                json_view = page.get_by_test_id("detail-body").locator(".jsonview")
                formatted = json_view.get_by_role("button", name="Formatted")
                raw = json_view.get_by_role("button", name="Raw", exact=True)
                expect(raw).to_have_attribute("aria-pressed", "true")
                formatted.click()
                expect(json_view.locator(".jv-block").first).to_be_visible()
                expect(json_view.locator(".jv-raw")).to_have_count(0)
                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_tree_timeline_and_overview_views_render_without_errors(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "alternate-views-failure"):
                _open_mock(page, url)
                page.get_by_test_id("tab-tree").click()
                expect(page.get_by_test_id("view-tree")).to_be_visible()
                expect(page.get_by_test_id("tree-node").first).to_be_visible()

                page.get_by_test_id("tab-timeline").click()
                expect(page.get_by_test_id("view-timeline")).to_be_visible()
                expect(page.get_by_test_id("timeline-bar").first).to_be_visible()

                page.get_by_test_id("tab-overview").click()
                expect(page.get_by_test_id("view-overview")).to_be_visible()
                expect(page.get_by_test_id("ov-mission")).to_contain_text(
                    "Patrol sector, investigate contacts"
                )
                expect(page.get_by_test_id("ov-fsm")).to_contain_text("Statechart")
                expect(page.get_by_test_id("fsm-transitions")).to_be_visible()
                expect(page.get_by_test_id("artifact-list")).to_be_visible()
                expect(page.get_by_test_id("artifact-row").first).to_be_visible()
                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_world_model_view_renders_live_socket_frame_and_state(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    frame = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    class Feed:
        closed = False

        def start(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def payload(self) -> dict[str, object]:
            return {
                "available": True,
                "connected": True,
                "status": "live",
                "sequence": 12,
                "generation_timestamp_s": 123.5,
                "state": {
                    "mission_id": "mission:live",
                    "mission_time_seconds": 8.5,
                    "state_version": 17,
                },
            }

        def frame(self) -> bytes:
            return frame

    feed = Feed()
    context = chromium_browser.new_context()
    page = context.new_page()
    try:
        with _viewer_server(tmp_path, world_model_feed=feed) as (base_url, _, _):
            page.goto(base_url)
            page.get_by_test_id("tab-world-model").click()
            expect(page.get_by_test_id("view-world-model")).to_be_visible()
            expect(page.get_by_test_id("world-model-status")).to_have_text("live")
            expect(page.get_by_test_id("world-model-frame")).to_be_visible()
            expect(page.get_by_text("mission:live", exact=True)).to_be_visible()
    finally:
        context.close()

    assert feed.closed is True


def test_deep_link_selects_step_and_reload_restores_it(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "deep-link-failure"):
                fragment = "#mission=mission:demo&view=trajectory&step=5"
                _open_mock(page, url, fragment)
                selected = page.locator('[data-testid="step-row"][data-seq="5"]')
                expect(selected).to_have_class(_SELECTED)
                expect(page.get_by_test_id("detail-panel")).to_contain_text(
                    "Record the planning intent"
                )
                page.reload(wait_until="networkidle")
                expect(page.get_by_test_id("mock-badge")).to_be_visible()
                expect(selected).to_have_class(_SELECTED)
                expect(page.get_by_test_id("detail-panel")).to_contain_text(
                    "Record the planning intent"
                )
                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_keyboard_arrows_move_step_selection(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _static_server() as url:
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "keyboard-navigation-failure"):
                _open_mock(page, url)
                rows = page.get_by_test_id("step-row")
                first = rows.nth(0)
                second = rows.nth(1)
                first.click()
                expect(first).to_have_class(_SELECTED)
                page.keyboard.press("ArrowDown")
                expect(second).to_have_class(_SELECTED)
                expect(first).not_to_have_class(_SELECTED)
                page.keyboard.press("ArrowUp")
                expect(first).to_have_class(_SELECTED)
                expect(second).not_to_have_class(_SELECTED)
                _assert_no_browser_errors(errors, allow_mock_endpoint_404s=True)
        finally:
            context.close()


def test_live_backend_uses_real_var_data_without_mock_fallback(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path) as (url, storage, transport):
        _seed_live_artifacts(storage, transport, tmp_path)
        context, page = _page(chromium_browser)
        errors = _browser_errors(page)
        try:
            with _diagnostic(page, tmp_path, "live-backend-failure"):
                page.goto(url, wait_until="networkidle")
                expect(page.get_by_test_id("mission-title")).to_have_text(
                    "Account for reported events"
                )
                expect(page.get_by_test_id("mock-badge")).to_have_count(0)
                expect(page.get_by_test_id("runtime-status")).to_have_text(
                    "Runtime live"
                )
                expect(page.get_by_test_id("run-status-pill")).to_have_text("running")
                expect(page.get_by_test_id("mission-picker")).to_have_value(
                    _LIVE_MISSION
                )
                for test_id in (
                    "badge-steps",
                    "badge-llm-calls",
                    "badge-tool-calls",
                    "badge-errors",
                    "badge-duration",
                    "badge-planner-attempts",
                    "badge-statechart-attempts",
                ):
                    expect(
                        page.get_by_test_id(test_id).locator(".badge-value")
                    ).not_to_have_text("—")

                step = page.get_by_test_id("step-row").filter(
                    has_text="Planner execution"
                )
                expect(step).to_have_count(1)
                step.click()
                expect(page.get_by_test_id("detail-body")).to_contain_text(
                    "second planner attempt"
                )
                page.get_by_test_id("detail-tab-decision").click()
                expect(page.get_by_test_id("detail-body")).to_contain_text(
                    "planner-execution"
                )
                page.get_by_test_id("detail-tab-tools").click()
                expect(page.get_by_test_id("tool-card")).to_contain_text("run_minizinc")
                page.get_by_test_id("detail-tab-feedback").click()
                expect(page.get_by_test_id("detail-body")).to_contain_text(
                    "maneuver-feedback"
                )

                page.get_by_test_id("tab-overview").click()
                expect(page.get_by_test_id("ov-mission")).to_contain_text(
                    "Account for reported events"
                )
                expect(page.get_by_test_id("ov-fsm")).to_contain_text("state-0")
                expect(page.get_by_test_id("artifact-list")).to_be_visible()
                expect(page.get_by_test_id("artifact-row")).to_have_count(2)
                _assert_no_browser_errors(errors)
        finally:
            context.close()


def test_polling_renders_live_revisions_without_losing_selection_focus_or_scroll(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path) as (url, storage, transport):
        _seed_live_artifacts(storage, transport, tmp_path)
        mission_name = quote(_LIVE_MISSION, safe="._-")
        artifact_path = (
            storage.parent
            / "debug"
            / "llm"
            / "hyper-agent"
            / mission_name
            / "00000000000000000001.json"
        )

        def live_record(
            revision: int,
            completion_state: str,
            marker: str,
        ) -> dict[str, object]:
            terminal = completion_state != "live"
            return {
                "schema_version": 2,
                "sequence": 1,
                "invocation_id": "llm-live-1",
                "request": {"model": "reasoning-model", "stream": True},
                "response_id": "response-live-1",
                "model": "reasoning-model",
                "status_code": 200,
                "finish_reason": None,
                "content": ("growing response " * 300) + marker,
                "function_call": None,
                "reasoning": "The model is still evaluating the planner.",
                "reasoning_content": "",
                "reasoning_details": None,
                "tool_calls": [
                    {
                        "index": 0,
                        "type": "function",
                        "function": {
                            "name": "run_minizinc",
                            "arguments": '{"attempt":',
                        },
                    }
                ],
                "error": (
                    {"type": "StreamError", "message": "stream stopped"}
                    if completion_state == "error"
                    else None
                ),
                "started_at": "2026-08-21T10:00:00+00:00",
                "updated_at": f"2026-08-21T10:00:0{revision}+00:00",
                "finished_at": (
                    f"2026-08-21T10:00:0{revision}+00:00" if terminal else None
                ),
                "completion_state": completion_state,
                "revision": revision,
            }

        _write_json(artifact_path, live_record(1, "live", "revision one"))
        context, page = _page(chromium_browser, height=520)
        errors = _browser_errors(page)
        try:
            page.goto(url, wait_until="networkidle")
            step = page.get_by_test_id("step-row").filter(has_text="Planner execution")
            step.click()
            expect(page.get_by_test_id("completion-state")).to_have_text("live")
            expect(page.get_by_test_id("detail-body")).to_contain_text("revision one")
            page.get_by_test_id("detail-tab-tools").click()
            expect(page.get_by_test_id("draft-tool-arguments")).to_contain_text(
                '{"attempt":'
            )
            page.get_by_test_id("detail-tab-reasoning").click()

            search = page.get_by_test_id("step-search")
            search.fill("Planner")
            detail = page.get_by_test_id("detail-panel")
            detail.evaluate("element => { element.scrollTop = 90; }")
            before_scroll = detail.evaluate("element => element.scrollTop")
            assert before_scroll > 0

            _write_json(artifact_path, live_record(2, "partial", "revision two"))
            expect(page.get_by_test_id("completion-state")).to_have_text("partial")
            expect(page.get_by_test_id("detail-body")).to_contain_text("revision two")
            expect(step).to_have_class(_SELECTED)
            expect(search).to_be_focused()
            assert detail.evaluate("element => element.scrollTop") == before_scroll

            _write_json(artifact_path, live_record(3, "error", "revision tri"))
            expect(page.get_by_test_id("completion-state")).to_have_text("error")
            expect(page.get_by_test_id("detail-body")).to_contain_text("revision tri")
            expect(step).to_have_class(_SELECTED)
            expect(search).to_be_focused()
            assert detail.evaluate("element => element.scrollTop") == before_scroll
            _assert_no_browser_errors(errors)
        finally:
            context.close()
