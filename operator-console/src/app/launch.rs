//! Launch screen: Stack Preset and toggles, live preflight, Mission Intent
//! editor, demo prompts, review, and activation with `stack` (§4.1).

use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use unicode_width::UnicodeWidthChar;

use super::{App, AppState, SOURCE_AUTHORITY};
use crate::host::{
    ActivationRequest, HostCommand, HostError, PreflightQuery, StackPreflight, StackPreset,
    StackPresets, StackSelection, StackToggles,
};

/// Preflight re-runs this long after the last toggle change.
pub const PREFLIGHT_DEBOUNCE: Duration = Duration::from_millis(300);
/// Every perception mode the console knows, in display order.
pub const PERCEPTION_MODES: [&str; 3] = ["off", "ideal", "yolo"];
/// Environment update ownership modes, in display order.
pub const UPDATE_OWNERSHIP_MODES: [&str; 2] = ["coordinator_driven", "environment_driven"];
const SIM_LIMIT_STEP: u64 = 30;
const SIM_LIMIT_MIN: u64 = 30;
const SIM_LIMIT_MAX: u64 = 3600;

/// Rejection prompts from the #66 battery, offered by the F2 picker.
pub const REJECTION_PROMPTS: [(&str, &str); 3] = [
    ("Rejection · personal errand", "buy me a coffee"),
    ("Rejection · trivia", "What is the capital of France?"),
    (
        "Rejection · gibberish",
        "qzx vlorp blenk 7#k frabble wug snee",
    ),
];

/// Focusable Launch screen fields, in Tab order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LaunchField {
    Preset,
    Airsim,
    Perception,
    Updates,
    SimLimit,
    #[default]
    Intent,
}

impl LaunchField {
    pub const ALL: [Self; 6] = [
        Self::Preset,
        Self::Airsim,
        Self::Perception,
        Self::Updates,
        Self::SimLimit,
        Self::Intent,
    ];

    fn offset(self, delta: isize) -> Self {
        let index = Self::ALL
            .iter()
            .position(|field| *field == self)
            .unwrap_or(0) as isize;
        let len = Self::ALL.len() as isize;
        Self::ALL[(index + delta).rem_euclid(len) as usize]
    }
}

/// Mission Intent editor buffer with a character-index cursor.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IntentEditor {
    text: String,
    /// Cursor position as a character index into `text`.
    cursor: usize,
}

/// The editor laid out into visual rows for a given width.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WrappedIntent {
    pub lines: Vec<String>,
    /// Cursor as (visual row, display column).
    pub cursor: (usize, usize),
}

impl IntentEditor {
    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn cursor(&self) -> usize {
        self.cursor
    }

    /// Replace the buffer and put the cursor at the end.
    pub fn set_text(&mut self, text: impl Into<String>) {
        self.text = text.into();
        self.cursor = self.text.chars().count();
    }

    /// Cursor position as (logical line, column) character offsets.
    pub fn cursor_line_col(&self) -> (usize, usize) {
        let before: Vec<&str> = self.text[..self.byte_cursor()].split('\n').collect();
        (
            before.len() - 1,
            before.last().map_or(0, |line| line.chars().count()),
        )
    }

    fn byte_cursor(&self) -> usize {
        self.text
            .char_indices()
            .nth(self.cursor)
            .map_or(self.text.len(), |(index, _)| index)
    }

    fn char_count(&self) -> usize {
        self.text.chars().count()
    }

    fn line_len(&self, line: usize) -> usize {
        self.text
            .split('\n')
            .nth(line)
            .map_or(0, |text| text.chars().count())
    }

    fn line_col_to_cursor(&self, line: usize, col: usize) -> usize {
        let mut cursor = 0;
        for (index, text) in self.text.split('\n').enumerate() {
            if index == line {
                return cursor + col.min(text.chars().count());
            }
            cursor += text.chars().count() + 1;
        }
        cursor
    }

    pub fn insert(&mut self, c: char) {
        let at = self.byte_cursor();
        self.text.insert(at, c);
        self.cursor += 1;
    }

    pub fn backspace(&mut self) {
        if self.cursor > 0 {
            self.cursor -= 1;
            let at = self.byte_cursor();
            self.text.remove(at);
        }
    }

    pub fn delete(&mut self) {
        if self.cursor < self.char_count() {
            let at = self.byte_cursor();
            self.text.remove(at);
        }
    }

    /// Apply one editing key; returns whether the key was consumed.
    pub fn handle_key(&mut self, key: KeyEvent) -> bool {
        let bare = key.modifiers.is_empty();
        let shift = key.modifiers == KeyModifiers::SHIFT;
        match key.code {
            KeyCode::Enter if bare => self.insert('\n'),
            KeyCode::Char(c) if bare || shift => self.insert(c),
            KeyCode::Backspace if bare => self.backspace(),
            KeyCode::Delete if bare => self.delete(),
            KeyCode::Left if bare => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right if bare => self.cursor = (self.cursor + 1).min(self.char_count()),
            KeyCode::Home if bare => {
                let (line, _) = self.cursor_line_col();
                self.cursor = self.line_col_to_cursor(line, 0);
            }
            KeyCode::End if bare => {
                let (line, _) = self.cursor_line_col();
                self.cursor = self.line_col_to_cursor(line, self.line_len(line));
            }
            KeyCode::Up if bare => {
                let (line, col) = self.cursor_line_col();
                if line > 0 {
                    self.cursor =
                        self.line_col_to_cursor(line - 1, col.min(self.line_len(line - 1)));
                }
            }
            KeyCode::Down if bare => {
                let (line, col) = self.cursor_line_col();
                if line + 1 < self.text.split('\n').count() {
                    self.cursor =
                        self.line_col_to_cursor(line + 1, col.min(self.line_len(line + 1)));
                }
            }
            _ => return false,
        }
        true
    }

    /// Word-wrap the buffer to `width` display columns and locate the cursor
    /// in the wrapped rows. Rendering uses these exact rows, so the cursor
    /// always lands on the character it precedes, including after wraps.
    pub fn wrap(&self, width: usize) -> WrappedIntent {
        let width = width.max(1);
        let mut lines = Vec::new();
        let mut cursor = (0, 0);
        let mut remaining = self.cursor;
        for logical in self.text.split('\n') {
            let chars: Vec<char> = logical.chars().collect();
            let widths: Vec<usize> = chars.iter().map(|c| c.width().unwrap_or(0)).collect();
            let segments = wrap_segments(&chars, &widths, width);
            let cursor_here = remaining <= chars.len();
            for (index, &(start, end)) in segments.iter().enumerate() {
                let last = index + 1 == segments.len();
                if cursor_here && remaining >= start && (remaining < end || last) {
                    let col: usize = widths[start..remaining].iter().sum();
                    cursor = if col >= width && last {
                        (lines.len() + 1, 0)
                    } else {
                        (lines.len(), col)
                    };
                }
                lines.push(chars[start..end].iter().collect());
            }
            if cursor_here {
                remaining = usize::MAX;
            } else {
                remaining -= chars.len() + 1;
            }
        }
        if cursor.0 >= lines.len() {
            lines.push(String::new());
        }
        WrappedIntent { lines, cursor }
    }
}

/// Word-wrap one line of text to `width` display columns, the same way the
/// intent editor wraps (newlines start new rows).
pub fn wrap_line(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut rows = Vec::new();
    for logical in text.split('\n') {
        let chars: Vec<char> = logical.chars().collect();
        let widths: Vec<usize> = chars.iter().map(|c| c.width().unwrap_or(0)).collect();
        for (start, end) in wrap_segments(&chars, &widths, width) {
            rows.push(
                chars[start..end]
                    .iter()
                    .collect::<String>()
                    .trim_end()
                    .to_string(),
            );
        }
    }
    rows
}

/// Greedy word wrap of one logical line into `(start, end)` char ranges.
fn wrap_segments(chars: &[char], widths: &[usize], width: usize) -> Vec<(usize, usize)> {
    let mut segments = Vec::new();
    let mut start = 0;
    let mut col = 0;
    let mut last_break: Option<usize> = None;
    let mut index = 0;
    while index < chars.len() {
        let char_width = widths[index];
        if col + char_width > width && index > start {
            let end = last_break.filter(|&at| at > start).unwrap_or(index);
            segments.push((start, end));
            start = end;
            col = widths[start..index].iter().sum();
            last_break = None;
            continue;
        }
        col += char_width;
        if chars[index] == ' ' {
            last_break = Some(index + 1);
        }
        index += 1;
    }
    segments.push((start, chars.len()));
    segments
}

/// What a review is bound to: changing either issues a new Activation
/// Request ID; an unchanged retry replays the same one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReviewSnapshot {
    intent: String,
    stack: Option<StackSelection>,
}

/// Launch screen state. Retained across runs so `e` from a finished run
/// returns with the same preset and toggles.
#[derive(Debug, Clone, Default)]
pub struct LaunchState {
    pub presets: Option<StackPresets>,
    pub presets_error: Option<String>,
    pub preset_index: usize,
    pub airsim: bool,
    pub perception: String,
    pub update_ownership: String,
    pub simulation_limit_seconds: u64,
    pub focus: LaunchField,
    pub editor: IntentEditor,
    /// Latest preflight answer (possibly for an older toggle combination).
    pub preflight: Option<StackPreflight>,
    pub preflight_error: Option<String>,
    /// F2 demo prompt picker selection, when open.
    pub demo_picker: Option<usize>,
    preflight_due: Option<Instant>,
    preflight_request_id: u64,
    preflight_response_id: u64,
    preflight_in_flight: Option<PreflightQuery>,
}

impl LaunchState {
    pub fn preset(&self) -> Option<&StackPreset> {
        self.presets.as_ref()?.presets.get(self.preset_index)
    }

    pub fn toggles(&self) -> StackToggles {
        StackToggles {
            airsim: self.airsim,
            perception: self.perception.clone(),
            update_ownership: self.update_ownership.clone(),
        }
    }

    /// The `stack` sent with the activation, once presets are loaded.
    pub fn selection(&self) -> Option<StackSelection> {
        let preset = self.preset()?;
        Some(StackSelection {
            preset_id: preset.preset_id.clone(),
            airsim: self.airsim,
            perception: self.perception.clone(),
            update_ownership: self.update_ownership.clone(),
            simulation_limit_seconds: self.simulation_limit_seconds,
        })
    }

    fn query(&self) -> Option<PreflightQuery> {
        Some(PreflightQuery {
            preset_id: self.preset()?.preset_id.clone(),
            toggles: self.toggles(),
        })
    }

    /// The preflight answer for the current selection, if it has arrived.
    pub fn current_preflight(&self) -> Option<&StackPreflight> {
        let query = self.query()?;
        self.preflight
            .as_ref()
            .filter(|preflight| preflight.preset_id == query.preset_id)
            .filter(|preflight| preflight.toggles == query.toggles)
    }

    /// Whether a preflight for the current selection is scheduled or running.
    pub fn preflight_pending(&self) -> bool {
        self.preflight_due.is_some() || self.preflight_in_flight.is_some()
    }

    /// Supported AirSim values for the selected preset.
    pub fn airsim_options(&self) -> Vec<bool> {
        let Some(preset) = self.preset() else {
            return Vec::new();
        };
        let mut options = Vec::new();
        for value in [false, true] {
            if preset.supports.airsim.contains(&value)
                && !self.perception_options_for(value).is_empty()
            {
                options.push(value);
            }
        }
        options
    }

    /// Supported perception modes given the AirSim toggle: perception other
    /// than `off` requires AirSim.
    pub fn perception_options(&self) -> Vec<String> {
        self.perception_options_for(self.airsim)
    }

    fn perception_options_for(&self, airsim: bool) -> Vec<String> {
        let Some(preset) = self.preset() else {
            return Vec::new();
        };
        PERCEPTION_MODES
            .iter()
            .filter(|mode| {
                preset
                    .supports
                    .perception
                    .iter()
                    .any(|value| value == *mode)
            })
            .filter(|mode| airsim || **mode == "off")
            .map(|mode| (*mode).to_string())
            .collect()
    }

    /// Select preset `index` and apply its defaults. The intent follows the
    /// preset's default text unless the operator edited it.
    pub fn select_preset(&mut self, index: usize, now: Instant) {
        let previous_default = self
            .preset()
            .map(|preset| preset.default_mission_text.clone());
        let Some(preset) = self
            .presets
            .as_ref()
            .and_then(|presets| presets.presets.get(index))
            .cloned()
        else {
            return;
        };
        self.preset_index = index;
        self.airsim = preset.defaults.airsim;
        self.perception = preset.defaults.perception.clone();
        self.update_ownership = preset.defaults.update_ownership.clone();
        self.simulation_limit_seconds = preset.defaults.simulation_limit_seconds;
        let untouched = self.editor.text().trim().is_empty()
            || previous_default.as_deref() == Some(self.editor.text());
        if untouched {
            self.editor.set_text(preset.default_mission_text);
        }
        self.schedule_preflight(now, PREFLIGHT_DEBOUNCE);
    }

    /// Ask for a preflight `delay` from `now`, replacing any pending one.
    pub fn schedule_preflight(&mut self, now: Instant, delay: Duration) {
        if self.presets.is_some() {
            self.preflight_due = Some(now + delay);
        }
    }

    /// The preflight command once its debounce has elapsed.
    pub(crate) fn take_due_preflight(&mut self, now: Instant) -> Option<HostCommand> {
        if self.preflight_due.is_none_or(|due| now < due) {
            return None;
        }
        self.preflight_due = None;
        let query = self.query()?;
        self.preflight_request_id += 1;
        self.preflight_response_id = self.preflight_request_id;
        self.preflight_in_flight = Some(query.clone());
        Some(HostCommand::Preflight {
            request_id: self.preflight_request_id,
            query,
        })
    }

    pub(crate) fn retry_dropped_preflight(&mut self, request_id: u64, now: Instant) {
        if request_id == self.preflight_response_id {
            self.preflight_in_flight = None;
            self.schedule_preflight(now, PREFLIGHT_DEBOUNCE);
        }
    }

    pub(crate) fn adopt_preflight_request(&mut self, request_id: u64, original: u64) {
        if request_id == self.preflight_response_id {
            self.preflight_response_id = original;
        }
    }

    pub(crate) fn apply_preflight(
        &mut self,
        request_id: u64,
        result: Result<StackPreflight, HostError>,
    ) {
        if request_id != self.preflight_response_id {
            return;
        }
        self.preflight_in_flight = None;
        match result {
            Ok(preflight) => {
                self.preflight = Some(preflight);
                self.preflight_error = None;
            }
            Err(error) => {
                self.preflight = None;
                self.preflight_error = Some(error.to_string());
            }
        }
    }

    /// Why launching is not possible right now, if it is not.
    pub fn launch_blocker(&self) -> Option<String> {
        if self.editor.text().trim().is_empty() {
            return Some("Mission Intent is empty".to_string());
        }
        if self.preset().is_none() {
            return Some(match self.presets_error.as_deref() {
                Some(error) => format!("Stack presets unavailable: {error}"),
                None => "Waiting for Stack presets".to_string(),
            });
        }
        if let Some(error) = self.preflight_error.as_deref()
            && !self.preflight_pending()
        {
            return Some(format!("Preflight unavailable: {error}"));
        }
        let Some(preflight) = self
            .current_preflight()
            .filter(|_| !self.preflight_pending())
        else {
            return Some("Preflight running".to_string());
        };
        if preflight.allows_launch() {
            return None;
        }
        let failed: Vec<&str> = preflight
            .checks
            .iter()
            .filter(|check| check.status == "fail")
            .map(|check| check.label.as_str())
            .collect();
        Some(if failed.is_empty() {
            "Launch disabled: Host reports the stack is not launchable".to_string()
        } else {
            format!("Launch disabled: {}", failed.join(", "))
        })
    }

    /// F2 demo prompts: the preset's mission, then the rejection battery.
    pub fn demo_prompts(&self) -> Vec<(String, String)> {
        let mut prompts = Vec::new();
        if let Some(preset) = self.preset() {
            prompts.push((
                format!("Preset mission · {}", preset.title),
                preset.default_mission_text.clone(),
            ));
        }
        prompts.extend(
            REJECTION_PROMPTS
                .iter()
                .map(|(label, text)| ((*label).to_string(), (*text).to_string())),
        );
        prompts
    }

    /// Change the focused toggle by `delta` steps. Returns whether the
    /// preflight-relevant selection changed.
    fn change_value(&mut self, delta: isize, now: Instant) -> bool {
        match self.focus {
            LaunchField::Preset => {
                let Some(count) = self.presets.as_ref().map(|presets| presets.presets.len()) else {
                    return false;
                };
                if count == 0 {
                    return false;
                }
                let next = (self.preset_index as isize + delta).rem_euclid(count as isize);
                self.select_preset(next as usize, now);
                false
            }
            LaunchField::Airsim => {
                let options = self.airsim_options();
                let Some(next) = cycle(&options, &self.airsim, delta) else {
                    return false;
                };
                self.airsim = next;
                let perception = self.perception_options();
                if !perception.contains(&self.perception)
                    && let Some(first) = perception.first()
                {
                    self.perception = first.clone();
                }
                true
            }
            LaunchField::Perception => {
                let options = self.perception_options();
                let Some(next) = cycle(&options, &self.perception, delta) else {
                    return false;
                };
                self.perception = next;
                true
            }
            LaunchField::Updates => {
                let options: Vec<String> = UPDATE_OWNERSHIP_MODES
                    .iter()
                    .map(|mode| (*mode).to_string())
                    .collect();
                let Some(next) = cycle(&options, &self.update_ownership, delta) else {
                    return false;
                };
                self.update_ownership = next;
                true
            }
            LaunchField::SimLimit => {
                let step = SIM_LIMIT_STEP as i64 * delta as i64;
                self.simulation_limit_seconds = (self.simulation_limit_seconds as i64 + step)
                    .clamp(SIM_LIMIT_MIN as i64, SIM_LIMIT_MAX as i64)
                    as u64;
                false
            }
            LaunchField::Intent => false,
        }
    }
}

/// Step through `options` from `current`; a value missing from `options`
/// snaps to the first option.
fn cycle<T: Clone + PartialEq>(options: &[T], current: &T, delta: isize) -> Option<T> {
    if options.is_empty() {
        return None;
    }
    let next = match options.iter().position(|option| option == current) {
        Some(index) => (index as isize + delta).rem_euclid(options.len() as isize) as usize,
        None => 0,
    };
    (options[next] != *current).then(|| options[next].clone())
}

impl App {
    pub(crate) fn on_presets(&mut self, result: Result<StackPresets, HostError>) {
        match result {
            Ok(presets) => {
                let now = self.clock.now();
                let index = presets
                    .presets
                    .iter()
                    .position(|preset| preset.preset_id == presets.default_preset_id)
                    .unwrap_or(0);
                self.launch.presets = Some(presets);
                self.launch.presets_error = None;
                self.launch.select_preset(index, now);
                self.launch.schedule_preflight(now, Duration::ZERO);
            }
            Err(error) => self.launch.presets_error = Some(error.to_string()),
        }
    }

    pub(crate) fn handle_launch_key(&mut self, key: KeyEvent) {
        self.hint = None;
        let now = self.clock.now();
        if let Some(selected) = self.launch.demo_picker {
            let count = self.launch.demo_prompts().len();
            match key.code {
                KeyCode::Esc | KeyCode::F(2) => self.launch.demo_picker = None,
                KeyCode::Up | KeyCode::Char('k') => {
                    self.launch.demo_picker = Some(selected.saturating_sub(1));
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    self.launch.demo_picker = Some((selected + 1).min(count.saturating_sub(1)));
                }
                KeyCode::Enter => {
                    if let Some((_, text)) = self.launch.demo_prompts().into_iter().nth(selected) {
                        self.launch.editor.set_text(text);
                    }
                    self.launch.demo_picker = None;
                    self.launch.focus = LaunchField::Intent;
                }
                _ => {}
            }
            return;
        }
        let review = key.code == KeyCode::Enter
            && (key.modifiers.contains(KeyModifiers::ALT)
                || key.modifiers.contains(KeyModifiers::CONTROL));
        if review {
            self.open_review();
            return;
        }
        match key.code {
            KeyCode::F(2) => {
                self.launch.demo_picker = Some(0);
                return;
            }
            KeyCode::Tab => {
                self.launch.focus = self.launch.focus.offset(1);
                return;
            }
            KeyCode::BackTab => {
                self.launch.focus = self.launch.focus.offset(-1);
                return;
            }
            _ => {}
        }
        if self.launch.focus == LaunchField::Intent {
            self.launch.editor.handle_key(key);
            return;
        }
        match key.code {
            KeyCode::Left | KeyCode::Char('h') => {
                if self.launch.change_value(-1, now) {
                    self.launch.schedule_preflight(now, PREFLIGHT_DEBOUNCE);
                }
            }
            KeyCode::Right | KeyCode::Char('l') | KeyCode::Char(' ') => {
                if self.launch.change_value(1, now) {
                    self.launch.schedule_preflight(now, PREFLIGHT_DEBOUNCE);
                }
            }
            KeyCode::Up | KeyCode::Char('k') => self.launch.focus = self.launch.focus.offset(-1),
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Enter => {
                self.launch.focus = self.launch.focus.offset(1);
            }
            KeyCode::Char('r') => self.launch.schedule_preflight(now, Duration::ZERO),
            KeyCode::Char('?') => self.help_open = true,
            KeyCode::Char('q') => self.should_quit = true,
            _ => {}
        }
    }

    fn open_review(&mut self) {
        if let Some(blocker) = self.launch.launch_blocker() {
            self.hint = Some(format!("{blocker} - cannot review activation"));
            return;
        }
        let snapshot = ReviewSnapshot {
            intent: self.launch.editor.text().to_string(),
            stack: self.launch.selection(),
        };
        if self.review_snapshot.as_ref() != Some(&snapshot) {
            self.review_request_id = Some(uuid::Uuid::new_v4().to_string());
            self.review_snapshot = Some(snapshot);
        }
        self.submitted = false;
        self.state = AppState::ReviewActivation;
    }

    pub(crate) fn handle_review_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => self.state = AppState::Launch,
            KeyCode::Enter => self.confirm_submit(),
            _ => {}
        }
    }

    /// Confirm the review exactly once and submit the activation.
    fn confirm_submit(&mut self) {
        if self.submitted {
            return;
        }
        self.submitted = true;
        let request = ActivationRequest {
            activation_request_id: self
                .review_request_id
                .clone()
                .expect("review always assigns a request id"),
            console_session_id: self.session.session_id.clone(),
            mission_intent: self.launch.editor.text().to_string(),
            source_authority: SOURCE_AUTHORITY.to_string(),
            stack: self.launch.selection(),
        };
        self.state = AppState::Submitting;
        self.outbox.push(HostCommand::Submit {
            request: Box::new(request),
            credential: self.session.credential.clone(),
        });
    }
}

#[cfg(test)]
mod tests {
    use super::IntentEditor;

    fn editor(text: &str, cursor: usize) -> IntentEditor {
        let mut editor = IntentEditor::default();
        editor.set_text(text);
        editor.cursor = cursor;
        editor
    }

    #[test]
    fn wrapped_cursor_follows_soft_wraps_instead_of_clamping_to_the_edge() {
        // "alpha beta gamma" in 11 columns wraps after "beta ".
        let wrapped = editor("alpha beta gamma", 16).wrap(11);
        assert_eq!(wrapped.lines, ["alpha beta ", "gamma"]);
        assert_eq!(wrapped.cursor, (1, 5));
        // The first character of the second row is "g".
        assert_eq!(editor("alpha beta gamma", 11).wrap(11).cursor, (1, 0));
        assert_eq!(editor("alpha beta gamma", 10).wrap(11).cursor, (0, 10));
    }

    #[test]
    fn long_words_hard_wrap_and_a_full_last_row_moves_the_cursor_down() {
        let wrapped = editor("abcdefgh", 8).wrap(4);
        assert_eq!(wrapped.lines, ["abcd", "efgh", ""]);
        assert_eq!(wrapped.cursor, (2, 0));
    }

    #[test]
    fn newlines_start_new_rows_and_wide_chars_count_two_columns() {
        let wrapped = editor("ab\n漢字x", 4).wrap(4);
        assert_eq!(wrapped.lines, ["ab", "漢字", "x"]);
        assert_eq!(wrapped.cursor, (1, 2));
        assert_eq!(editor("ab\n漢字x", 5).wrap(4).cursor, (2, 0));
        assert_eq!(editor("ab\n", 3).wrap(4).cursor, (1, 0));
    }
}
