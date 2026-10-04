//! Run history browser (issue #76 U10).
//!
//! F3 opens the run history overlay from Launch or Run: one page of
//! `GET /api/v1/mission-runs` at a time, newest first, with cycling status and
//! preset filters over the loaded rows. Scrolling past the last loaded row
//! fetches the next older page through the Host's `before` cursor.
//!
//! Enter opens a run read-only in the normal seven tabs. The console then
//! holds two run contexts: the displayed historical one in the usual
//! [`App`] fields, and the current one (run record, view, terminal-refresh
//! bookkeeping and logical state) parked in [`HistoricalView`]. Host
//! responses for the current run are reduced into the parked context
//! ([`App::with_current_run`]), so the current run keeps being polled for
//! lifecycle changes and its attention events keep firing, while nothing about
//! the historical run reaches attention. The owner session, the activation
//! and the owned run id are never touched; mutating keys are disabled; Esc
//! restores the current run (or Launch).
//!
//! The footer notice belongs to its run context: a notice raised while a
//! historical run is displayed is dropped with it on Esc, and the current
//! run's notice comes back. Until the historical run's first Overview page
//! arrives the view is loading: the Host rebuilds a run it has not
//! served since it started from its Run Root on disk (ADR 0015), which can
//! outlast one request's time limit, so a timed-out read is retried under the
//! loading state instead of being reported as a failed poll.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent};

use super::run::RunView;
use super::{App, AppState, CancellationState, RefreshKey};
use crate::host::workers::Dispatch;
use crate::host::{
    HostCommand, HostError, HostMessage, MissionRunSummary, MissionRunsPage, OperatorSection,
    RunRecord,
};

/// Rows requested per run history page.
pub const HISTORY_PAGE_ROWS: u32 = 50;
/// Rows PgUp/PgDn move the history selection.
const HISTORY_PAGE_STEP: isize = 10;
/// Host API minor version that serves the run history route.
const HISTORY_API_MINOR: u32 = 5;

/// Cycling status filter (`s`) over the loaded history rows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum StatusFilter {
    #[default]
    All,
    Succeeded,
    Failed,
    Cancelled,
    /// Queued, running or awaiting a Human Decision.
    Active,
}

impl StatusFilter {
    const ORDER: [Self; 5] = [
        Self::All,
        Self::Succeeded,
        Self::Failed,
        Self::Cancelled,
        Self::Active,
    ];

    fn next(self) -> Self {
        let index = Self::ORDER
            .iter()
            .position(|item| *item == self)
            .unwrap_or(0);
        Self::ORDER[(index + 1) % Self::ORDER.len()]
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Active => "active",
        }
    }

    fn matches(self, status: &str) -> bool {
        match self {
            Self::All => true,
            Self::Succeeded => status == "succeeded",
            Self::Failed => status == "failed",
            Self::Cancelled => status == "cancelled",
            Self::Active => !crate::host::is_terminal_status(status),
        }
    }
}

/// The F3 overlay: loaded history pages, selection and filters.
#[derive(Debug, Default)]
pub struct HistoryOverlay {
    pub open: bool,
    /// Loaded rows, newest first, deduplicated by run id.
    pub rows: Vec<MissionRunSummary>,
    /// Selected run, by id, among the filtered rows.
    pub selected: Option<String>,
    /// First visible row of the list, kept across draws.
    pub offset: usize,
    /// Cursor for the next older page; `None` once the oldest page arrived.
    pub next_before: Option<String>,
    /// Whether the oldest page has arrived.
    pub complete: bool,
    /// The `before` of the page in flight (`Some(None)`: the newest page).
    loading: Option<Option<String>>,
    pub status_filter: StatusFilter,
    /// Preset filter (`p` cycles through the presets of the loaded rows).
    pub preset_filter: Option<String>,
    /// Why the list could not be loaded.
    pub error: Option<String>,
    /// Explicit answer to the last Enter (e.g. a missing Run Root).
    pub message: Option<String>,
}

impl HistoryOverlay {
    /// Whether `row` passes both filters.
    pub fn matches(&self, row: &MissionRunSummary) -> bool {
        self.status_filter.matches(&row.mission_run.status)
            && self
                .preset_filter
                .as_deref()
                .is_none_or(|preset| preset_id(row) == Some(preset))
    }

    /// Loaded rows that pass both filters.
    pub fn visible(&self) -> Vec<&MissionRunSummary> {
        self.rows.iter().filter(|row| self.matches(row)).collect()
    }

    /// Index of the selection among [`Self::visible`].
    pub fn selected_index(&self) -> Option<usize> {
        let selected = self.selected.as_deref()?;
        self.visible()
            .iter()
            .position(|row| row.mission_run.mission_run_id == selected)
    }

    pub fn selected_row(&self) -> Option<&MissionRunSummary> {
        let selected = self.selected.as_deref()?;
        self.rows
            .iter()
            .find(|row| row.mission_run.mission_run_id == selected)
    }

    pub fn loading(&self) -> bool {
        self.loading.is_some()
    }

    /// Preset ids among the loaded rows, sorted.
    fn presets(&self) -> Vec<String> {
        let mut presets: Vec<String> = self
            .rows
            .iter()
            .filter_map(|row| preset_id(row).map(str::to_string))
            .collect();
        presets.sort();
        presets.dedup();
        presets
    }

    /// Keep the selection on a visible row: the first one when it was
    /// filtered out or never set.
    fn settle_selection(&mut self) {
        if self.selected_index().is_none() {
            self.selected = self
                .visible()
                .first()
                .map(|row| row.mission_run.mission_run_id.clone());
        }
    }

    fn step(&mut self, delta: isize) {
        let visible = self.visible();
        if visible.is_empty() {
            return;
        }
        let current = self.selected_index().unwrap_or(0);
        let last = visible.len() - 1;
        let next = current.saturating_add_signed(delta).min(last);
        self.selected = Some(visible[next].mission_run.mission_run_id.clone());
    }

    /// Whether the selection sits on the last loaded (filtered) row, or no
    /// row passes the filters: the next older page is wanted.
    fn wants_more(&self) -> bool {
        if self.complete || self.loading() {
            return false;
        }
        let len = self.visible().len();
        len == 0 || self.selected_index().is_some_and(|index| index + 1 >= len)
    }
}

fn preset_id(row: &MissionRunSummary) -> Option<&str> {
    row.mission_run
        .stack
        .as_ref()
        .map(|stack| stack.preset_id.as_str())
}

/// The current run's context, parked while a historical run is displayed.
#[derive(Debug)]
pub(crate) struct RunContext {
    state: AppState,
    run: Option<RunRecord>,
    view: RunView,
    final_refresh: Option<HashSet<RefreshKey>>,
    final_complete: HashSet<OperatorSection>,
    final_narrative_ready: bool,
    next_terminal_refresh: Option<Instant>,
    notice: Option<String>,
}

/// A historical run whose first Overview page has not arrived yet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HistoricalLoading {
    since: Instant,
    /// Reads that timed out (and are retried) while the Host loads it.
    pub timed_out: u32,
}

/// A run opened read-only from the history.
#[derive(Debug)]
pub struct HistoricalView {
    /// The history row it was opened from.
    pub row: MissionRunSummary,
    /// The Host reported its Run Root missing while it was viewed.
    pub unavailable: Option<String>,
    /// Set from opening until the first Overview page arrives.
    pub loading: Option<HistoricalLoading>,
    current: RunContext,
}

impl HistoricalView {
    /// Whether Esc returns to a Run screen (else to Launch).
    pub fn returns_to_run(&self) -> bool {
        matches!(self.current.state, AppState::Run)
    }
}

/// How much one poll of a run context asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PollScope {
    /// The displayed current run: `/current`, the visible tab and hydration.
    Full,
    /// The displayed historical run: its sections, never `/current`.
    Historical,
    /// The parked current run: `/current` plus Overview and Stack, enough
    /// for lifecycle changes and attention edges.
    Lifecycle,
}

/// Mission Run a command reads, if it is run-scoped.
fn command_run_id(command: &HostCommand) -> Option<&str> {
    match command {
        HostCommand::FetchOperatorView { mission_run_id, .. }
        | HostCommand::FetchArtifactContent { mission_run_id, .. }
        | HostCommand::FetchConversationEntries { mission_run_id, .. }
        | HostCommand::FetchWorldFrame { mission_run_id, .. }
        | HostCommand::FetchIntent { mission_run_id, .. }
        | HostCommand::Cancel { mission_run_id, .. }
        | HostCommand::ExportReceipt { mission_run_id, .. } => Some(mission_run_id),
        _ => None,
    }
}

impl App {
    /// The historical run on display, if any.
    pub fn historical(&self) -> Option<&HistoricalView> {
        self.historical.as_ref()
    }

    pub fn viewing_history(&self) -> bool {
        self.historical.is_some()
    }

    /// The current Mission Run record, parked or displayed.
    pub fn current_run(&self) -> Option<&RunRecord> {
        match self.historical.as_ref() {
            Some(historical) => historical.current.run.as_ref(),
            None => self.run.as_ref(),
        }
    }

    /// The current run's view, parked or displayed.
    pub(crate) fn current_view(&self) -> &RunView {
        match self.historical.as_ref() {
            Some(historical) => &historical.current.view,
            None => &self.view,
        }
    }

    fn swap_context(&mut self, context: &mut RunContext) {
        std::mem::swap(self.logical_state_mut(), &mut context.state);
        std::mem::swap(&mut self.run, &mut context.run);
        std::mem::swap(&mut self.view, &mut context.view);
        std::mem::swap(&mut self.final_refresh, &mut context.final_refresh);
        std::mem::swap(&mut self.final_complete, &mut context.final_complete);
        std::mem::swap(
            &mut self.final_narrative_ready,
            &mut context.final_narrative_ready,
        );
        std::mem::swap(
            &mut self.next_terminal_refresh,
            &mut context.next_terminal_refresh,
        );
        std::mem::swap(&mut self.notice, &mut context.notice);
    }

    /// Run `f` against the current run's context. While a historical run is
    /// displayed the parked context is swapped in for `f`, with
    /// `historical` cleared so attention observes the current run.
    pub(crate) fn with_current_run<R>(&mut self, f: impl FnOnce(&mut Self) -> R) -> R {
        let Some(mut historical) = self.historical.take() else {
            return f(self);
        };
        self.swap_context(&mut historical.current);
        let result = f(self);
        self.swap_context(&mut historical.current);
        self.historical = Some(historical);
        result
    }

    /// Whether a response or dispatch for `mission_run_id` belongs to the
    /// parked current run.
    fn parked_run(&self, mission_run_id: &str) -> bool {
        self.historical.as_ref().is_some_and(|historical| {
            historical
                .current
                .run
                .as_ref()
                .is_some_and(|run| run.mission_run_id == mission_run_id)
        })
    }

    /// Whether a Host message is about the current run while a historical
    /// run is displayed.
    pub(crate) fn message_for_parked_run(&self, message: &HostMessage) -> bool {
        if self.historical.is_none() {
            return false;
        }
        match message {
            HostMessage::Current(_)
            | HostMessage::Intent(_)
            | HostMessage::Cancelled(_)
            | HostMessage::ReceiptExported(_) => true,
            HostMessage::OperatorView { mission_run_id, .. }
            | HostMessage::ArtifactContent { mission_run_id, .. }
            | HostMessage::ConversationEntries { mission_run_id, .. }
            | HostMessage::WorldFrame { mission_run_id, .. } => self.parked_run(mission_run_id),
            _ => false,
        }
    }

    /// Route dispatch backpressure to the context that issued the command.
    pub(crate) fn dispatch_for_parked_run(&self, dispatch: &Dispatch) -> bool {
        let Some(historical) = self.historical.as_ref() else {
            return false;
        };
        let command = match dispatch {
            Dispatch::Dropped(command) | Dispatch::Coalesced { command, .. } => command,
            _ => return false,
        };
        match command_run_id(command) {
            Some(mission_run_id) => self.parked_run(mission_run_id),
            // With Launch parked only the displayed run polls `/current`.
            None => {
                matches!(command, HostCommand::PollCurrent { .. }) && historical.returns_to_run()
            }
        }
    }

    /// A `/current` answer while a historical run is displayed. When the
    /// displayed run is the Host's current one (a run this console does not
    /// own, opened from Launch), its record is refreshed from the answer.
    /// Returns whether the answer is consumed here: with Launch parked there
    /// is no current run context to reduce it into.
    pub(crate) fn sync_displayed_current(&mut self, message: &HostMessage) -> bool {
        let HostMessage::Current(result) = message else {
            return false;
        };
        let Some(historical) = self.historical.as_ref() else {
            return false;
        };
        let parked_run_screen = historical.returns_to_run();
        match result {
            Ok(current) => {
                if let Some(record) = current.mission_run.as_ref()
                    && self
                        .run
                        .as_ref()
                        .is_some_and(|run| run.mission_run_id == record.mission_run_id)
                {
                    self.run = Some(record.clone());
                    if record.is_terminal() && self.final_refresh.is_none() {
                        self.final_refresh = Some(HashSet::from([RefreshKey::Current]));
                        self.next_terminal_refresh = Some(self.clock.now());
                    }
                }
            }
            Err(_) if !parked_run_screen => self.release_refresh(&RefreshKey::Current),
            Err(_) => {}
        }
        !parked_run_screen
    }

    /// F3: open the overlay and load the newest page.
    pub(crate) fn open_history(&mut self) {
        if self.cancellation == CancellationState::Confirming {
            return;
        }
        let history = &mut self.history;
        history.open = true;
        history.message = None;
        history.error = None;
        history.rows.clear();
        history.offset = 0;
        history.next_before = None;
        history.complete = false;
        history.loading = None;
        if self
            .health
            .as_ref()
            .is_some_and(|health| health.api_version.minor < HISTORY_API_MINOR)
        {
            let minor = self.health.as_ref().map_or(0, |h| h.api_version.minor);
            self.history.error = Some(format!(
                "Run history needs Runtime Host API v1.{HISTORY_API_MINOR}; this Host reports v1.{minor}. Restart the Host from this checkout."
            ));
            self.history.complete = true;
            return;
        }
        self.fetch_history(None);
    }

    fn fetch_history(&mut self, before: Option<String>) {
        self.history.loading = Some(before.clone());
        self.outbox.push(HostCommand::FetchRunHistory {
            before,
            limit: HISTORY_PAGE_ROWS,
        });
    }

    fn fetch_more_history_if_wanted(&mut self) {
        if self.history.open && self.history.wants_more() {
            let before = self.history.next_before.clone();
            self.fetch_history(before);
        }
    }

    pub(crate) fn on_run_history(
        &mut self,
        before: Option<String>,
        result: Result<MissionRunsPage, HostError>,
    ) {
        if !self.history.open || self.history.loading.as_ref() != Some(&before) {
            return;
        }
        self.history.loading = None;
        match result {
            Ok(page) => {
                let history = &mut self.history;
                if before.is_none() {
                    history.rows.clear();
                }
                for row in page.mission_runs {
                    if !history.rows.iter().any(|known| {
                        known.mission_run.mission_run_id == row.mission_run.mission_run_id
                    }) {
                        history.rows.push(row);
                    }
                }
                history.complete = page.next_before.is_none();
                history.next_before = page.next_before;
                history.error = None;
                history.settle_selection();
                self.fetch_more_history_if_wanted();
            }
            Err(error) => {
                self.history.error = Some(format!("Run history failed: {error}"));
            }
        }
    }

    /// Keys while the overlay is open; it owns every key but Ctrl+C/Ctrl+Q
    /// and F1.
    pub(crate) fn handle_history_key(&mut self, key: KeyEvent) {
        let history = &mut self.history;
        match key.code {
            KeyCode::Esc | KeyCode::F(3) => {
                history.open = false;
                return;
            }
            KeyCode::Up | KeyCode::Char('k') => history.step(-1),
            KeyCode::Down | KeyCode::Char('j') => history.step(1),
            KeyCode::PageUp => history.step(-HISTORY_PAGE_STEP),
            KeyCode::PageDown => history.step(HISTORY_PAGE_STEP),
            KeyCode::Home => history.step(isize::MIN),
            KeyCode::End => history.step(isize::MAX),
            KeyCode::Char('s') => {
                history.status_filter = history.status_filter.next();
                history.settle_selection();
            }
            KeyCode::Char('p') => {
                let presets = history.presets();
                history.preset_filter = match history.preset_filter.as_deref() {
                    None => presets.first().cloned(),
                    Some(current) => presets
                        .iter()
                        .position(|preset| preset == current)
                        .and_then(|index| presets.get(index + 1))
                        .cloned(),
                };
                history.settle_selection();
            }
            KeyCode::Char('r') => {
                self.open_history();
                return;
            }
            KeyCode::Enter => {
                self.open_selected_history_row();
                return;
            }
            _ => {}
        }
        self.history.message = None;
        self.fetch_more_history_if_wanted();
    }

    fn open_selected_history_row(&mut self) {
        let Some(row) = self.history.selected_row().cloned() else {
            return;
        };
        let run_id = row.mission_run.mission_run_id.clone();
        if !row.run_root_available {
            self.history.message = Some(format!(
                "Run Root of {run_id} is missing on this Host: its evidence was removed, so it cannot be opened."
            ));
            return;
        }
        let current_id = self.current_run().map(|run| run.mission_run_id.clone());
        let current_on_screen = self
            .historical
            .as_ref()
            .map_or(self.logical_state_name() == "Run", |historical| {
                historical.returns_to_run()
            });
        if current_on_screen && current_id.as_deref() == Some(run_id.as_str()) {
            // The current run is not history: show it live.
            self.history.open = false;
            self.close_historical_view();
            self.hint = Some(format!("{run_id} is the current run"));
            return;
        }
        if self
            .run
            .as_ref()
            .is_some_and(|run| run.mission_run_id == run_id)
            && self.historical.is_some()
        {
            self.history.open = false;
            return;
        }
        self.open_historical(row);
    }

    /// Display `row` read-only; the current context is parked (or stays
    /// parked when another historical run was on display).
    fn open_historical(&mut self, row: MissionRunSummary) {
        let record = row.mission_run.clone();
        let mut view = RunView::default();
        view.media.configure(self.image_picker.clone());
        let terminal = record.is_terminal();
        let mut context = RunContext {
            state: AppState::Run,
            run: Some(record),
            view,
            final_refresh: terminal.then(|| HashSet::from([RefreshKey::Current])),
            final_complete: HashSet::new(),
            final_narrative_ready: false,
            next_terminal_refresh: terminal.then(|| self.clock.now()),
            notice: None,
        };
        // The displayed fields swap with `context`: they hold the current
        // run (parked into the new view) or, when another historical run is
        // on display, that run (dropped with `context`).
        self.swap_context(&mut context);
        let loading = Some(HistoricalLoading {
            since: self.clock.now(),
            timed_out: 0,
        });
        match self.historical.as_mut() {
            Some(historical) => {
                historical.row = row;
                historical.unavailable = None;
                historical.loading = loading;
            }
            None => {
                self.historical = Some(HistoricalView {
                    row,
                    unavailable: None,
                    loading,
                    current: context,
                });
            }
        }
        self.history.open = false;
        self.hint = None;
        self.request_poll();
    }

    /// Esc on a historical run: restore the current run (or Launch).
    pub(crate) fn close_historical_view(&mut self) {
        let Some(mut historical) = self.historical.take() else {
            return;
        };
        self.swap_context(&mut historical.current);
        if self.logical_state_name() == "Run" {
            self.request_poll();
        }
    }

    /// Keys a historical run refuses: everything that mutates or forgets the
    /// owner session. Returns whether the key was handled here.
    pub(crate) fn handle_historical_key(&mut self, key: KeyEvent) -> bool {
        if self.historical.is_none() {
            return false;
        }
        match key.code {
            KeyCode::Esc => {
                self.close_historical_view();
                true
            }
            KeyCode::Char('c' | 'q' | 'e' | 'x') => {
                self.hint = Some(format!(
                    "Historical run: read-only (no cancel, export or new intent) · Esc returns to the {}",
                    self.history_return_label()
                ));
                true
            }
            _ => false,
        }
    }

    /// Ctrl+C on a historical run leaves the history first, then acts on the
    /// current run as usual.
    pub(crate) fn leave_history_for_interrupt(&mut self) {
        self.history.open = false;
        self.close_historical_view();
    }

    /// The displayed historical run's Host reported its Run Root missing.
    pub(crate) fn mark_historical_unavailable(&mut self, message: &str) {
        if let Some(historical) = self.historical.as_mut() {
            historical.unavailable = Some(message.to_string());
            historical.loading = None;
        }
    }

    /// An operator-view page of the displayed run arrived. A historical run
    /// has loaded with its first Overview page, the run summary behind the
    /// header and the first tab: it is read first, so it is the read that
    /// waits for the Host's rebuild, while sections read meanwhile can
    /// answer before the rebuild ends.
    pub(crate) fn historical_page_arrived(&mut self, section: OperatorSection) {
        if section != OperatorSection::Overview {
            return;
        }
        if let Some(historical) = self.historical.as_mut() {
            historical.loading = None;
        }
    }

    /// A read of the displayed run timed out. Returns whether that is a
    /// historical run still loading from disk, where the timeout is counted
    /// and retried rather than reported as a failed poll.
    pub(crate) fn historical_read_timed_out(&mut self) -> bool {
        match self
            .historical
            .as_mut()
            .and_then(|historical| historical.loading.as_mut())
        {
            Some(loading) => {
                loading.timed_out += 1;
                true
            }
            None => false,
        }
    }

    /// How long the displayed historical run has been loading, and how many
    /// of its reads timed out meanwhile; `None` once it loaded.
    pub fn historical_loading(&self) -> Option<(Duration, u32)> {
        let loading = self.historical.as_ref()?.loading?;
        Some((
            self.clock.now().saturating_duration_since(loading.since),
            loading.timed_out,
        ))
    }

    /// Tab the historical view returns to on Esc, for key hints.
    pub fn history_return_label(&self) -> &'static str {
        match self.historical.as_ref() {
            Some(historical) if historical.returns_to_run() => "current run",
            Some(_) => "Launch",
            None => "",
        }
    }

    /// Whether the run on screen is the one this console launched (or
    /// recovered), for the history list's `owned` mark.
    pub fn owned_run_id(&self) -> Option<&str> {
        self.activation
            .as_ref()
            .map(|activation| activation.mission_run_id.as_str())
            .or_else(|| {
                self.recovered_state
                    .as_ref()
                    .map(|owner| owner.mission_run_id.as_str())
            })
    }
}
