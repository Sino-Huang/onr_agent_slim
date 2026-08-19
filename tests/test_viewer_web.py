from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
from threading import Thread
from typing import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright

from onr.adapters.file_transport import FileTransport
from onr.adapters.operational_log import FileOperationalLog
from onr.contracts.fsm import ManeuverFeedback
from onr.contracts.hyper_agent import ReplanRequest
from onr.contracts.transport import Command, CommandOutcome, TransportEvent
from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.runtime.lease import RuntimeLeaseStore
from onr.viewer.server import ViewerHTTPServer, create_server


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    planner = tmp_path / "planner"
    planner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    planner.chmod(0o755)
    storage = tmp_path / "storage"
    transport = tmp_path / "transport"
    config = tmp_path / "viewer.yaml"
    config.write_text(
        "\n".join(
            (
                "debug: true",
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
    tmp_path: Path, *, active: bool
) -> Iterator[tuple[str, Path, Path]]:
    config, storage, transport = _config(tmp_path)
    lease = RuntimeLeaseStore(storage / "runtime")
    if active:
        lease.start(session_id="browser-test-session")
    server: ViewerHTTPServer = create_server(
        host="127.0.0.1",
        port=0,
        repo_root=tmp_path,
        config_path=config,
    )
    thread = Thread(target=server.serve_forever, name="viewer-web-test", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", storage, transport
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        if active:
            lease.stop()
        assert not thread.is_alive()


def _write_summary(storage: Path, artifact: SummaryArtifact) -> None:
    directory = storage / "summaries" / artifact.mission_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{artifact.sequence:020d}.json").write_text(
        json.dumps(artifact.to_dict(), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _seed_active_artifacts(storage: Path, transport_root: Path) -> None:
    mission = "mission-alpha"
    other = "mission-beta"
    log = FileOperationalLog(storage / "operational-log")
    log.emit(
        mission,
        "runtime",
        "heartbeat",
        "completed",
        details={"status": "ready", "api_key": "raw-browser-secret"},
    )
    log.emit(other, "runtime", "heartbeat", "completed", details={"status": "ready"})

    transport = FileTransport(transport_root)
    command = Command(
        1,
        "command-alpha",
        "correlation-alpha",
        mission,
        "maneuver-adapter",
        "maneuver",
        {"action": "navigate", "analysis": "private command reasoning"},
    )
    transport.send_command(command)
    transport.publish_outcome(
        CommandOutcome(
            1,
            command.command_id,
            command.correlation_id,
            mission,
            "completed",
            {"status": "ready", "result": "accepted", "token": "raw-outcome-secret"},
        )
    )
    feedback = ManeuverFeedback(
        "feedback-alpha",
        mission,
        "maneuver-alpha",
        "completed",
        {
            "command_id": command.command_id,
            "correlation_id": command.correlation_id,
            "source": "environment",
            "plan_revision": 2,
            "snapshot_id": "snapshot-alpha",
            "analysis": "private feedback reasoning",
        },
    )
    transport.publish_event(
        "maneuver-feedback",
        TransportEvent(
            1,
            feedback.feedback_id,
            mission,
            0,
            feedback.event_kind,
            feedback.to_dict(),
        ),
    )
    replan = ReplanRequest(
        "replan-alpha",
        mission,
        "scene revision changed",
        "maneuver-control",
        2,
        {"scene": 4},
    )
    transport.publish_event(
        "replan-requests",
        TransportEvent(
            1,
            "replan-wire-alpha",
            mission,
            0,
            "replan-request",
            replan.to_dict(),
        ),
    )
    transport.publish_event(
        "advisory",
        TransportEvent(
            1,
            "advisory-alpha",
            mission,
            0,
            "role-skills-advisory",
            {
                "role_skills": ["navigation"],
                "operation": "public advisory",
                "authorization": "Bearer raw-advisory-secret",
            },
        ),
    )

    first = SummaryArtifact.create(
        mission,
        1,
        1,
        2,
        (),
        "Alpha established a public operating picture.",
        created_at="2026-08-19T01:00:00+00:00",
    )
    second = SummaryArtifact.create(
        mission,
        2,
        3,
        6,
        (first.summary_id,),
        "Alpha completed maneuver feedback and requested a bounded replan.",
        created_at="2026-08-19T01:01:00+00:00",
    )
    beta = SummaryArtifact.create(
        other,
        1,
        1,
        1,
        (),
        "Beta remains scoped to its own observation stream.",
        created_at="2026-08-19T01:02:00+00:00",
    )
    for artifact in (first, second, beta):
        _write_summary(storage, artifact)


@contextmanager
def _diagnostic(page: Page, tmp_path: Path, name: str) -> Iterator[None]:
    try:
        yield
    except BaseException:
        page.screenshot(path=str(tmp_path / f"{name}.png"), full_page=True)
        raise


def _page(
    browser: Browser, *, width: int, height: int
) -> tuple[BrowserContext, Page]:
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.set_default_timeout(7000)
    return context, page


def _assert_no_global_overflow(page: Page) -> None:
    dimensions = page.evaluate(
        """() => ({
          viewport: window.innerWidth,
          document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth
        })"""
    )
    assert dimensions["document"] <= dimensions["viewport"] + 1
    assert dimensions["body"] <= dimensions["viewport"] + 1


def _pause_and_show_all(page: Page) -> None:
    play = page.locator("#playPause")
    if play.text_content() == "Pause":
        play.click()
    page.locator("#scrubber").evaluate(
        """element => {
          element.value = element.max;
          element.dispatchEvent(new Event('input', { bubbles: true }));
        }"""
    )


def _set_replay_cursor(page: Page, value: int) -> None:
    page.locator("#scrubber").evaluate(
        """(element, cursor) => {
          element.value = String(cursor);
          element.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
        value,
    )


def test_desktop_idle_has_architecture_only(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path, active=False) as (url, _, _):
        context, page = _page(chromium_browser, width=1440, height=1000)
        try:
            with _diagnostic(page, tmp_path, "desktop-idle-failure"):
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#runtimeLabel")).to_have_text("Runtime idle")
                nodes = page.locator(".node")
                expect(nodes).to_have_count(8)
                for index in range(8):
                    expect(nodes.nth(index)).to_be_visible()
                expect(page.locator("#idleNote")).to_be_visible()
                mission_picker = page.locator("#missionPicker")
                expect(mission_picker).to_be_hidden()
                expect(page.locator("#missionSelect")).to_be_hidden()
                expect(page.locator("#missionSelect option")).to_have_count(0)
                expect(page.locator("#traceStrip")).to_be_hidden()
                replay = page.locator("#replayControls")
                expect(replay).to_be_hidden()
                assert not replay.evaluate(
                    "section => section.contains(document.activeElement)"
                )
                assert replay.locator("button, input, select").evaluate_all(
                    "elements => elements.every(element => element.getClientRects().length === 0)"
                )
                expect(page.locator("#scrubber")).to_have_attribute("max", "0")
                expect(page.locator("#scrubberEnd")).to_have_text("0 observed")
                expect(page.locator("#summaries")).to_be_hidden()
                expect(page.locator(".event")).to_have_count(0)
                expect(page.locator(".edge.active-flow")).to_have_count(0)
                expect(page.locator(".flow-chip")).to_have_count(0)
                assert page.evaluate(
                    "() => document.getAnimations().filter(a => a.playState === 'running').length"
                ) == 0
                _assert_no_global_overflow(page)
        finally:
            context.close()


def test_active_mission_shows_safe_flow_and_summary(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path, active=True) as (url, storage, transport):
        _seed_active_artifacts(storage, transport)
        context, page = _page(chromium_browser, width=1440, height=1000)
        try:
            with _diagnostic(page, tmp_path, "desktop-active-failure"):
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#runtimeLabel")).to_have_text("Runtime active")
                expect(page.locator("#missionSelect")).to_have_value("mission-alpha")
                expect(page.locator("#observationCount")).to_contain_text("observed")
                _pause_and_show_all(page)
                expect(page.locator("#summaries")).to_be_visible()
                expect(page.locator("#summaryStatus")).to_have_text("2 observed")
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "Alpha completed maneuver feedback"
                )
                expect(page.locator(".summary-item")).to_have_count(1)
                expect(page.locator(".edge.active-flow").first).to_be_visible()
                expect(page.locator(".node.focused").first).to_be_visible()

                page.locator(".tab[data-view='feedback']").click()
                feedback_event = page.locator(".event", has_text="maneuver-feedback")
                expect(feedback_event).to_have_count(1)
                feedback_event.click()
                expect(page.locator("#inspectorTitle")).to_have_text("maneuver-feedback")
                expect(page.locator("#detailList")).to_contain_text("command-alpha")
                expect(page.locator("#detailList")).to_contain_text("correlation-alpha")
                expect(page.locator("#detailList")).to_contain_text("completed")
                body = page.locator("body").inner_text().lower()
                for forbidden in (
                    "raw-browser-secret",
                    "private command reasoning",
                    "raw-outcome-secret",
                    "private feedback reasoning",
                    "raw-advisory-secret",
                    "browser-test-private-key",
                ):
                    assert forbidden not in body
                _assert_no_global_overflow(page)
        finally:
            context.close()


def test_available_completed_mission_keeps_replay_and_debug_activity(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    """The UI must retain a completed mission without scheduling live polling."""
    with _viewer_server(tmp_path, active=False) as (url, _, _):
        context, page = _page(chromium_browser, width=1280, height=900)
        try:
            page.route(
                "**/api/runtime",
                lambda route: route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "available": True,
                            "active": False,
                            "mission_ids": ["mission-complete"],
                        }
                    ),
                ),
            )
            page.route(
                "**/api/trace?mission_id=mission-complete",
                lambda route: route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "items": [
                                {
                                    "mission_id": "mission-complete",
                                    "trace_id": "trace-complete",
                                    "event_id": "event-complete",
                                    "component": "hyper-agent",
                                    "authority": "observed",
                                    "event_kind": "mission-completed",
                                    "occurred_at": "2026-08-19T00:00:00Z",
                                    "observation_sequence": 1,
                                    "payload": {"status": "completed"},
                                }
                            ]
                        }
                    ),
                ),
            )
            page.route(
                "**/api/debug?mission_id=mission-complete",
                lambda route: route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "enabled": True,
                            "profiles": [
                                {
                                    "agent_role": "navigator",
                                    "skills": [
                                        {
                                            "name": "route-planning",
                                            "version": "2.1",
                                            "path": "/skills/route",
                                        }
                                    ],
                                    "tools": ["map.lookup"],
                                }
                            ],
                            "invocations": [
                                {
                                    "sequence": 4,
                                    "agent_role": "navigator",
                                    "kind": "tool",
                                    "name": "map.lookup",
                                    "input": {"zone": "A"},
                                    "output": {"route": "clear"},
                                    "started_at": "2026-08-19T00:00:01Z",
                                    "finished_at": "2026-08-19T00:00:02Z",
                                    "invocation_id": "tool-4",
                                }
                            ],
                        }
                    ),
                ),
            )
            page.goto(url, wait_until="networkidle")
            expect(page.locator("#runtimeLabel")).to_have_text("Runtime available")
            expect(page.locator("#missionSelect")).to_have_value("mission-complete")
            expect(page.locator("#replayControls")).to_be_visible()
            expect(page.locator("#traceStrip")).to_be_visible()
            expect(page.locator(".event", has_text="mission-completed")).to_have_count(1)

            page.locator(".tab[data-view='debug']").click()
            expect(page.locator("#debugProfiles")).to_be_visible()
            expect(page.locator("#debugProfileList")).to_contain_text("route-planning 2.1")
            expect(page.locator("#debugProfileList")).to_contain_text("map.lookup")
            invocation = page.locator("#debugEventList .event", has_text="map.lookup")
            expect(invocation).to_have_count(1)
            invocation.click()
            expect(page.locator("#inspectorTitle")).to_have_text("map.lookup")
            expect(page.locator("#detailList")).to_contain_text('"route": "clear"')
            assert page.evaluate("() => document.querySelectorAll('#debugEventList .event').length") == 1
        finally:
            context.close()


def test_summary_panel_is_bounded_by_replay_cursor(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path, active=True) as (url, storage, transport):
        _seed_active_artifacts(storage, transport)
        context, page = _page(chromium_browser, width=1280, height=900)
        try:
            with _diagnostic(page, tmp_path, "summary-replay-failure"):
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#runtimeLabel")).to_have_text("Runtime active")
                _pause_and_show_all(page)
                kinds = page.locator(".event strong").all_text_contents()
                summary_positions = [
                    index for index, kind in enumerate(kinds) if kind == "summary"
                ]
                assert len(summary_positions) == 2
                first, second = summary_positions
                assert first > 0 and second > first

                _set_replay_cursor(page, first - 1)
                expect(page.locator("#summaryStatus")).to_have_text("Unavailable")
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "No public summary has been observed"
                )
                expect(page.locator(".summary-item")).to_have_count(0)

                _set_replay_cursor(page, first)
                expect(page.locator("#summaryStatus")).to_have_text("1 observed")
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "Alpha established a public operating picture"
                )
                expect(page.locator("#summaryLatest")).not_to_contain_text(
                    "Alpha completed maneuver feedback"
                )
                expect(page.locator(".summary-item")).to_have_count(0)

                _set_replay_cursor(page, second)
                expect(page.locator("#summaryStatus")).to_have_text("2 observed")
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "Alpha completed maneuver feedback"
                )
                expect(page.locator(".summary-item")).to_have_count(1)
        finally:
            context.close()


def test_mission_replay_selection_and_drill_down_stay_synchronized(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path, active=True) as (url, storage, transport):
        _seed_active_artifacts(storage, transport)
        context, page = _page(chromium_browser, width=1280, height=900)
        try:
            with _diagnostic(page, tmp_path, "interaction-failure"):
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#runtimeLabel")).to_have_text("Runtime active")
                _pause_and_show_all(page)

                page.locator(".tab[data-view='feedback']").click()
                expect(page.locator("#viewTitle")).to_have_text("Feedback paths")
                feedback_event = page.locator(".event", has_text="maneuver-feedback")
                feedback_event.click()
                expect(feedback_event).to_have_class(re.compile(r"\bselected\b"))
                expect(page.locator("#inspectorCopy")).to_have_text(
                    "feedback reported this observation."
                )
                assert int(page.locator("#scrubber").input_value()) > 0

                page.locator(".tab[data-view='hyper']").click()
                expect(page.locator("#viewTitle")).to_have_text("Hyper Agent")
                expect(page.locator(".event.selected")).to_have_count(0)
                replan_event = page.locator(".event", has_text="replan-request")
                expect(replan_event).to_have_count(1)
                replan_event.click()
                expect(page.locator("#inspectorCopy")).to_have_text(
                    "hyper-agent reported this observation."
                )
                expect(page.locator("#detailList")).to_contain_text("replan-alpha")
                expect(page.locator("#clearSelection")).to_be_visible()

                page.locator("#restart").click()
                page.locator("#playPause").click()
                expect(page.locator("#scrubber")).to_have_value("0")
                expect(page.locator("#inspectorTitle")).to_have_text("Hyper Agent")

                page.locator(".tab[data-view='overview']").click()
                page.locator("#missionSelect").select_option("mission-beta")
                expect(page.locator("#missionSelect")).to_have_value("mission-beta")
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "Beta remains scoped"
                )
                expect(page.locator("#summaryLatest")).not_to_contain_text("Alpha")
                beta_event = page.locator(".event", has_text="heartbeat")
                expect(beta_event).to_have_count(1)
                beta_event.click()
                expect(page.locator("#detailList")).to_contain_text("mission-beta")

                page.locator("#missionSelect").select_option("mission-alpha")
                expect(page.locator("#missionSelect")).to_have_value("mission-alpha")
                _pause_and_show_all(page)
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "Alpha completed maneuver feedback"
                )
                expect(page.locator("#summaryLatest")).not_to_contain_text("Beta")
        finally:
            context.close()


def test_mobile_active_layout_flow_and_summary_history_are_usable(
    chromium_browser: Browser, tmp_path: Path
) -> None:
    with _viewer_server(tmp_path, active=True) as (url, storage, transport):
        _seed_active_artifacts(storage, transport)
        context, page = _page(chromium_browser, width=390, height=844)
        try:
            with _diagnostic(page, tmp_path, "mobile-active-failure"):
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#runtimeLabel")).to_have_text("Runtime active")
                _pause_and_show_all(page)
                expect(page.locator(".flow-chip").first).to_be_visible()
                expect(page.locator("#replayControls")).to_be_visible()
                expect(page.locator("#summaries")).to_be_visible()
                expect(page.locator("#summaryLatest")).to_contain_text(
                    "Alpha completed maneuver feedback"
                )
                history = page.locator(".summary-item")
                expect(history).to_have_count(1)
                expect(history).to_be_visible()
                history.click()
                expect(history).to_have_class("summary-item selected")

                _assert_no_global_overflow(page)
                selectors = (
                    ".brand",
                    "#runtimeLabel",
                    ".mission-select span",
                    ".section-heading h1",
                    "#playPause",
                    ".speed-label",
                )
                fits = page.evaluate(
                    """selectors => selectors.every(selector => {
                      const element = document.querySelector(selector);
                      if (!element) return false;
                      const rect = element.getBoundingClientRect();
                      return rect.left >= -1 && rect.right <= window.innerWidth + 1
                        && element.scrollWidth <= element.clientWidth + 1;
                    })""",
                    selectors,
                )
                assert fits
        finally:
            context.close()
