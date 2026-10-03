//! Stack tab: Environment Stack services and an auto-following tail of the
//! selected service's log, read through the Artifact content endpoint.

use std::collections::VecDeque;

use crossterm::event::{KeyCode, KeyEvent};

use super::run::CONTENT_PAGE_BYTES;
use super::{App, RefreshKey};
use crate::host::{
    ArtifactContentPage, ContentPurpose, HostCommand, HostError, OperatorStack, StackService,
};

/// Lines retained per service-log tail.
pub const MAX_TAIL_LINES: usize = 500;

/// Incremental tail of one service-log Artifact.
///
/// The first read probes the size; a log larger than one window jumps to
/// `byte_size - 4096` and drops the partial first line. Later reads continue
/// from the end of the previous page, jumping ahead again if the log grew by
/// more than a window between polls.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogTail {
    pub artifact_id: String,
    /// Complete lines, oldest first.
    pub lines: VecDeque<String>,
    /// Trailing text without a newline yet.
    pub partial: String,
    pub byte_size: Option<u64>,
    /// Bytes skipped by window jumps so far.
    pub skipped_bytes: u64,
    pub error: Option<String>,
    next_offset: Option<u64>,
    at_line_start: bool,
}

impl LogTail {
    pub fn new(artifact_id: impl Into<String>) -> Self {
        Self {
            artifact_id: artifact_id.into(),
            lines: VecDeque::new(),
            partial: String::new(),
            byte_size: None,
            skipped_bytes: 0,
            error: None,
            next_offset: None,
            at_line_start: true,
        }
    }

    /// Offset of the next read.
    pub fn next_offset(&self) -> u64 {
        self.next_offset.unwrap_or(0)
    }

    /// Visible lines, including an unterminated last line.
    pub fn visible_lines(&self) -> impl Iterator<Item = &str> {
        self.lines
            .iter()
            .map(String::as_str)
            .chain((!self.partial.is_empty()).then_some(self.partial.as_str()))
    }

    fn jump_to_window(&mut self, from: u64, byte_size: u64) {
        let target = byte_size.saturating_sub(CONTENT_PAGE_BYTES);
        self.skipped_bytes += target.saturating_sub(from);
        self.next_offset = Some(target);
        self.partial.clear();
        self.at_line_start = target == 0;
    }

    /// Apply the page read at `requested_offset`; stale pages are ignored.
    pub fn apply(&mut self, requested_offset: u64, result: Result<ArtifactContentPage, HostError>) {
        if requested_offset != self.next_offset() {
            return;
        }
        let page = match result {
            Ok(page) => page,
            Err(HostError::InvalidRequest { .. }) => {
                // The log shrank below our offset (rotated or truncated): re-probe.
                *self = Self::new(std::mem::take(&mut self.artifact_id));
                return;
            }
            Err(error) => {
                self.error = Some(error.to_string());
                return;
            }
        };
        self.error = None;
        self.byte_size = page.byte_size;
        let end = page.end_offset();
        let byte_size = page.byte_size.unwrap_or(end);
        if self.next_offset.is_none() && byte_size > CONTENT_PAGE_BYTES && !page.eof {
            // Probe of a large log: skip to the last window.
            self.jump_to_window(0, byte_size);
            return;
        }
        self.push_text(page.content.as_deref().unwrap_or(""));
        if !page.eof && byte_size.saturating_sub(end) > CONTENT_PAGE_BYTES {
            self.jump_to_window(end, byte_size);
            self.lines.push_back(format!(
                "… skipped {} bytes …",
                byte_size.saturating_sub(CONTENT_PAGE_BYTES) - end
            ));
            self.trim();
        } else {
            self.next_offset = Some(end);
        }
    }

    fn push_text(&mut self, content: &str) {
        let mut text = std::mem::take(&mut self.partial);
        text.push_str(content);
        let mut rest = text.as_str();
        if !self.at_line_start {
            match rest.split_once('\n') {
                Some((_, after)) => {
                    rest = after;
                    self.at_line_start = true;
                }
                None => return,
            }
        }
        let mut parts = rest.split('\n').peekable();
        while let Some(line) = parts.next() {
            if parts.peek().is_some() {
                self.lines
                    .push_back(line.trim_end_matches('\r').to_string());
            } else {
                self.partial = line.to_string();
            }
        }
        self.trim();
    }

    fn trim(&mut self) {
        while self.lines.len() > MAX_TAIL_LINES {
            self.lines.pop_front();
        }
    }
}

/// Stack tab state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StackView {
    pub stack: Option<OperatorStack>,
    /// Selected service, by name.
    pub selected: Option<String>,
    pub tail: Option<LogTail>,
    /// Whether the log view sticks to the newest line.
    pub follow: bool,
    /// Lines scrolled up from the bottom while not following.
    pub scroll_back: u16,
}

impl Default for StackView {
    fn default() -> Self {
        Self {
            stack: None,
            selected: None,
            tail: None,
            follow: true,
            scroll_back: 0,
        }
    }
}

impl StackView {
    pub fn selected_service(&self) -> Option<(usize, &StackService)> {
        let selected = self.selected.as_deref()?;
        self.stack
            .as_ref()?
            .services
            .iter()
            .enumerate()
            .find(|(_, service)| service.name == selected)
    }

    /// Keep the tail bound to the selected service's log Artifact.
    fn sync_tail(&mut self) {
        let wanted = self
            .selected_service()
            .and_then(|(_, service)| service.log_artifact_id.clone());
        let current = self.tail.as_ref().map(|tail| tail.artifact_id.as_str());
        if wanted.as_deref() != current {
            self.tail = wanted.map(LogTail::new);
            self.scroll_back = 0;
        }
    }

    fn move_selection(&mut self, delta: isize) {
        let Some(services) = self.stack.as_ref().map(|stack| &stack.services) else {
            return;
        };
        if services.is_empty() {
            return;
        }
        let current = self.selected_service().map_or(0, |(index, _)| index) as isize;
        let next = (current + delta).clamp(0, services.len() as isize - 1) as usize;
        self.selected = Some(services[next].name.clone());
        self.sync_tail();
    }
}

impl App {
    pub(crate) fn apply_stack(&mut self, stack: OperatorStack) {
        let view = &mut self.view.stack;
        let keep = view
            .selected
            .as_deref()
            .is_some_and(|name| stack.services.iter().any(|service| service.name == name));
        if !keep {
            view.selected = stack.services.first().map(|service| service.name.clone());
        }
        view.stack = Some(stack);
        view.sync_tail();
    }

    pub(crate) fn request_service_log(&mut self) {
        let Some(run) = self.run.as_ref() else {
            return;
        };
        let Some(tail) = self.view.stack.tail.as_ref() else {
            return;
        };
        let command = HostCommand::FetchArtifactContent {
            purpose: ContentPurpose::ServiceLog,
            mission_run_id: run.mission_run_id.clone(),
            artifact_id: tail.artifact_id.clone(),
            offset: tail.next_offset(),
            limit: CONTENT_PAGE_BYTES,
        };
        // A probe that jumps to the tail window needs one more read, so the
        // final refresh is keyed by the offset rather than once per log.
        let key = RefreshKey::ServiceLog(format!("{}@{}", tail.artifact_id, tail.next_offset()));
        if self.claim_refresh(key) {
            self.outbox.push(command);
        }
    }

    pub(crate) fn apply_service_log(
        &mut self,
        artifact_id: &str,
        requested_offset: u64,
        result: Result<ArtifactContentPage, HostError>,
    ) {
        if self
            .view
            .stack
            .tail
            .as_ref()
            .is_none_or(|tail| tail.artifact_id != artifact_id)
        {
            return;
        }
        if result.is_err() {
            self.release_refresh(&RefreshKey::ServiceLog(format!(
                "{artifact_id}@{requested_offset}"
            )));
        }
        if let Some(tail) = self.view.stack.tail.as_mut() {
            tail.apply(requested_offset, result);
        }
    }

    pub(crate) fn handle_stack_key(&mut self, key: KeyEvent) {
        let view = &mut self.view.stack;
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => view.move_selection(-1),
            KeyCode::Down | KeyCode::Char('j') => view.move_selection(1),
            KeyCode::PageUp => {
                view.follow = false;
                view.scroll_back = view.scroll_back.saturating_add(10);
            }
            KeyCode::PageDown => {
                view.scroll_back = view.scroll_back.saturating_sub(10);
                if view.scroll_back == 0 {
                    view.follow = true;
                }
            }
            KeyCode::Char('f') => {
                view.follow = !view.follow;
                if view.follow {
                    view.scroll_back = 0;
                }
            }
            _ => return,
        }
        self.request_service_log();
    }
}

#[cfg(test)]
mod tests {
    use super::LogTail;
    use crate::host::{ArtifactContentPage, HostError};

    fn page(offset: u64, content: &str, byte_size: u64) -> ArtifactContentPage {
        let end = offset + content.len() as u64;
        let eof = end >= byte_size;
        ArtifactContentPage {
            schema_version: 1,
            mission_id: "mission".to_string(),
            mission_run_id: "run".to_string(),
            artifact_id: "service-log".to_string(),
            classification: "service_log".to_string(),
            media_type: "text/plain".to_string(),
            byte_size: Some(byte_size),
            offset,
            next_offset: (!eof).then_some(end),
            eof,
            truncated: false,
            content: Some(content.to_string()),
        }
    }

    #[test]
    fn small_log_is_read_whole_then_followed_from_its_end() {
        let mut tail = LogTail::new("service-log");
        tail.apply(0, Ok(page(0, "one\ntwo\nthr", 11)));
        assert_eq!(
            tail.visible_lines().collect::<Vec<_>>(),
            ["one", "two", "thr"]
        );
        assert_eq!(tail.next_offset(), 11);
        tail.apply(11, Ok(page(11, "ee\nfour\n", 19)));
        assert_eq!(
            tail.visible_lines().collect::<Vec<_>>(),
            ["one", "two", "three", "four"]
        );
        assert_eq!(tail.next_offset(), 19);
        // Nothing new: an empty eof page leaves the tail unchanged.
        tail.apply(19, Ok(page(19, "", 19)));
        assert_eq!(tail.lines.len(), 4);
    }

    #[test]
    fn large_log_tails_from_the_last_window_and_drops_the_cut_line() {
        let mut tail = LogTail::new("service-log");
        tail.apply(0, Ok(page(0, &"x".repeat(4096), 20_480)));
        assert!(tail.lines.is_empty());
        assert_eq!(tail.next_offset(), 20_480 - 4096);
        tail.apply(16_384, Ok(page(16_384, "cut line\nwhole\nlast\n", 16_405)));
        assert_eq!(tail.visible_lines().collect::<Vec<_>>(), ["whole", "last"]);
        // A stale page for an older offset is ignored.
        tail.apply(0, Ok(page(0, "stale\n", 16_405)));
        assert_eq!(tail.lines.len(), 2);
    }

    #[test]
    fn shrunk_log_reprobes_and_transport_errors_keep_lines() {
        let mut tail = LogTail::new("service-log");
        tail.apply(0, Ok(page(0, "kept\n", 5)));
        tail.apply(5, Err(HostError::Transport("down".to_string())));
        assert_eq!(tail.lines.len(), 1);
        assert!(tail.error.is_some());
        tail.apply(
            5,
            Err(HostError::InvalidRequest {
                code: "invalid_request".to_string(),
                message: "offset exceeds content size".to_string(),
            }),
        );
        assert_eq!(tail.next_offset(), 0);
        assert!(tail.lines.is_empty());
    }
}
