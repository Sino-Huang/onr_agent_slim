//! Incremental progress hierarchy and operator navigation. Node identifiers,
//! rather than cursor positions or parent paths, preserve state across summaries.

use std::collections::BTreeMap;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::text::{Line, Span, Text};
use tui_tree_widget::{TreeItem, TreeState};

use crate::host::{OperatorProgress, ProgressNarrative, ProgressNode};
use crate::ui::theme::{Badge, Importance, Theme};

const ROOT: &str = "root";
const LIVE: &str = "live";

#[derive(Debug)]
pub struct ProgressView {
    pub nodes: BTreeMap<String, ProgressNode>,
    pub narrative: Option<ProgressNarrative>,
    pub tree_state: TreeState<String>,
    pub follow_newest: bool,
    pub minimum: Importance,
    pub search: String,
    pub search_editing: bool,
    /// Zero-based position in search matches, not in the tree's visible rows.
    pub match_position: Option<usize>,
    pub detail_scroll: u16,
    pub(crate) items: Vec<TreeItem<'static, String>>,
    pub(crate) detail: String,
    pub(crate) detail_ai: bool,
    pub(crate) preview_summaries: Vec<(Importance, String)>,
    order: Vec<String>,
    paths: BTreeMap<String, Vec<String>>,
    matches: Vec<String>,
    details_json: BTreeMap<String, String>,
    search_text: BTreeMap<String, String>,
    cached_theme: Theme,
    detail_selection: Option<String>,
}

impl Default for ProgressView {
    fn default() -> Self {
        let mut view = Self {
            nodes: BTreeMap::new(),
            narrative: None,
            tree_state: TreeState::default(),
            follow_newest: true,
            minimum: Importance::Routine,
            search: String::new(),
            search_editing: false,
            match_position: None,
            detail_scroll: 0,
            items: Vec::new(),
            detail: String::new(),
            detail_ai: false,
            preview_summaries: Vec::new(),
            order: Vec::new(),
            paths: BTreeMap::new(),
            matches: Vec::new(),
            details_json: BTreeMap::new(),
            search_text: BTreeMap::new(),
            cached_theme: Theme::new(false),
            detail_selection: None,
        };
        view.tree_state.open(vec![ROOT.to_string()]);
        view.tree_state
            .open(vec![ROOT.to_string(), LIVE.to_string()]);
        view.rebuild();
        view
    }
}

impl ProgressView {
    /// Pages are deltas. Stable source sequences keep navigation chronological
    /// even when recovered history arrives newest-first.
    pub fn ingest(&mut self, page: OperatorProgress) {
        let mut changed = self.narrative.as_ref() != Some(&page.narrative);
        let mut added = false;
        self.narrative = Some(page.narrative);
        for node in page.nodes {
            if self.nodes.get(&node.node_id) == Some(&node) {
                continue;
            }
            changed = true;
            if !self.nodes.contains_key(&node.node_id) {
                self.order.push(node.node_id.clone());
                added = true;
            }
            let json = if node.details.is_null() {
                String::new()
            } else {
                serde_json::to_string_pretty(&node.details).expect("JSON values serialize")
            };
            self.search_text.insert(
                node.node_id.clone(),
                format!(
                    "{} {} {} {} {} {}",
                    node.title,
                    node.text.as_deref().unwrap_or(""),
                    node.source,
                    node.event_kind.as_deref().unwrap_or(""),
                    node.outcome.as_deref().unwrap_or(""),
                    json
                )
                .to_lowercase(),
            );
            self.details_json.insert(node.node_id.clone(), json);
            self.nodes.insert(node.node_id.clone(), node);
        }
        if added {
            self.order
                .sort_unstable_by(|a, b| node_order(a).cmp(&node_order(b)));
        }
        if changed {
            self.rebuild();
            if self.follow_newest {
                self.follow_latest();
            }
            self.detail_selection = None;
            self.sync_detail();
        }
    }

    pub fn selected_id(&self) -> Option<&str> {
        self.tree_state.selected().last().map(String::as_str)
    }

    pub fn selected_node(&self) -> Option<&ProgressNode> {
        self.selected_id().and_then(|id| self.nodes.get(id))
    }

    /// Includes retained, filtered ancestors as well as records; independent of
    /// whether the operator has collapsed their parents.
    pub fn visible_path(&self, node_id: &str) -> Option<&[String]> {
        self.paths.get(node_id).map(Vec::as_slice)
    }

    pub fn select_node(&mut self, node_id: &str) -> bool {
        let Some(path) = self.paths.get(node_id).cloned() else {
            return false;
        };
        for end in 1..path.len() {
            self.tree_state.open(path[..end].to_vec());
        }
        self.tree_state.select(path);
        self.sync_detail();
        true
    }

    /// Returns whether the local editor/navigation consumed the key. The caller
    /// must offer search editing before global tab/cancel/quit shortcuts.
    pub fn handle_key(&mut self, key: KeyEvent) -> bool {
        if key.modifiers.contains(KeyModifiers::CONTROL) {
            if matches!(key.code, KeyCode::Char('c' | 'q')) {
                return false;
            }
            if self.search_editing && key.code == KeyCode::Char('u') {
                self.search.clear();
                self.refresh_matches();
            }
            return self.search_editing;
        }
        if self.search_editing {
            match key.code {
                KeyCode::Esc | KeyCode::Enter => self.search_editing = false,
                KeyCode::Backspace => {
                    self.search.pop();
                    self.refresh_matches();
                }
                KeyCode::Char(character) => {
                    self.search.push(character);
                    self.refresh_matches();
                    self.jump_match(false);
                }
                _ => {}
            }
            return true;
        }
        match key.code {
            KeyCode::Char('/') => {
                self.search_editing = true;
                self.follow_newest = false;
            }
            KeyCode::Char('n') => self.jump_match(false),
            KeyCode::Char('N') => self.jump_match(true),
            KeyCode::Char('f') => {
                self.follow_newest = true;
                self.follow_latest();
            }
            KeyCode::Char('i') => {
                self.minimum = match self.minimum {
                    Importance::Routine => Importance::Debug,
                    Importance::Debug => Importance::Critical,
                    Importance::Critical => Importance::Warning,
                    Importance::Warning => Importance::Notable,
                    Importance::Notable => Importance::Routine,
                };
                self.rebuild();
                if self.follow_newest {
                    self.follow_latest();
                }
            }
            KeyCode::Up | KeyCode::Down | KeyCode::Home | KeyCode::End => {
                self.follow_newest = false;
                self.navigate(key.code);
            }
            KeyCode::Left | KeyCode::Right | KeyCode::Enter | KeyCode::Char(' ') => {
                self.follow_newest = false;
                let selected = self.tree_state.selected().to_vec();
                match key.code {
                    KeyCode::Left => {
                        if !self.tree_state.close(&selected) && selected.len() > 1 {
                            self.tree_state
                                .select(selected[..selected.len() - 1].to_vec());
                        }
                    }
                    KeyCode::Right => {
                        self.tree_state.open(selected);
                    }
                    _ => {
                        self.tree_state.toggle_selected();
                    }
                }
            }
            KeyCode::PageUp => {
                self.follow_newest = false;
                self.detail_scroll = self.detail_scroll.saturating_sub(8);
            }
            KeyCode::PageDown => {
                self.follow_newest = false;
                self.detail_scroll = self.detail_scroll.saturating_add(8);
            }
            _ => return false,
        }
        self.sync_detail();
        true
    }

    pub(crate) fn ensure_theme(&mut self, theme: Theme) {
        if self.cached_theme != theme {
            self.cached_theme = theme;
            self.rebuild();
        }
        self.sync_detail();
    }

    fn navigate(&mut self, direction: KeyCode) {
        let visible: Vec<Vec<String>> = self
            .tree_state
            .flatten(&self.items)
            .into_iter()
            .map(|row| row.identifier)
            .collect();
        if visible.is_empty() {
            return;
        }
        let current = visible
            .iter()
            .position(|path| path == self.tree_state.selected())
            .unwrap_or(0);
        let index = match direction {
            KeyCode::Up => current.saturating_sub(1),
            KeyCode::Down => (current + 1).min(visible.len() - 1),
            KeyCode::Home => 0,
            KeyCode::End => visible.len() - 1,
            _ => unreachable!(),
        };
        self.tree_state.select(visible[index].clone());
    }

    fn follow_latest(&mut self) {
        // Log IDs carry the operational-log sequence. Time breaks ties for
        // streams with other IDs; insertion order is the final tie-breaker.
        let newest = self
            .order
            .iter()
            .enumerate()
            .filter_map(|(index, id)| {
                let node = self.nodes.get(id)?;
                (node.level == "record" && self.paths.contains_key(id)).then_some((index, node))
            })
            .max_by(|(ai, a), (bi, b)| {
                let sequence = |node: &ProgressNode| {
                    node.node_id
                        .strip_prefix("log:")
                        .and_then(|value| value.parse::<u64>().ok())
                };
                sequence(a)
                    .cmp(&sequence(b))
                    .then_with(|| a.time_start.cmp(&b.time_start))
                    .then_with(|| ai.cmp(bi))
            })
            .map(|(_, node)| node.node_id.clone());
        if let Some(id) = newest {
            self.select_node(&id);
        }
    }

    fn rebuild(&mut self) {
        let selected = self.selected_id().map(str::to_string);
        let opened: Vec<String> = self
            .tree_state
            .opened()
            .iter()
            .filter_map(|path| path.last().cloned())
            .collect();
        let mut children: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for id in &self.order {
            let node = &self.nodes[id];
            if id == ROOT || id == LIVE {
                continue;
            }
            let parent = node.parent_id.as_deref().unwrap_or(ROOT);
            let parent = if parent == ROOT || parent == LIVE || self.nodes.contains_key(parent) {
                parent
            } else {
                // A paginated record can arrive before its summary.
                ROOT
            };
            children
                .entry(parent.to_string())
                .or_default()
                .push(id.clone());
        }
        // The live container exists even before the Host emits it and survives
        // pages that only contain records. Do not duplicate a Host live row.
        let mut root_children = children.remove(ROOT).unwrap_or_default();
        if self.nodes.contains_key(LIVE) || children.contains_key(LIVE) {
            root_children.push(LIVE.to_string());
        }
        self.paths.clear();
        self.paths.insert(ROOT.to_string(), vec![ROOT.to_string()]);
        let path = vec![ROOT.to_string()];
        let mut items = Vec::new();
        for id in root_children {
            if let Some(item) = self.build_item(&id, &path, &children) {
                items.push(item);
            }
        }
        let theme = self.cached_theme;
        let mut title = vec![
            Span::styled("◆ Run Narrative · ", theme.title()),
            theme.ai_badge(),
        ];
        if let Some(narrative) = &self.narrative {
            title.push(Span::styled(
                format!(" · {}", narrative.status),
                theme.dim(),
            ));
        }
        let text = self
            .narrative
            .as_ref()
            .and_then(|n| n.text.as_deref())
            .unwrap_or("Waiting for the Run Narrative from the Host");
        let root = TreeItem::new(
            ROOT.to_string(),
            Text::from(vec![
                Line::from(title),
                Line::from(Span::styled(text.to_string(), theme.ai())),
            ]),
            items,
        )
        .expect("progress IDs are unique");
        self.items = vec![root];
        let mut summaries: Vec<_> = self
            .nodes
            .values()
            .filter(|node| node.level == "summary")
            .collect();
        summaries.sort_by(|a, b| {
            b.time_end
                .cmp(&a.time_end)
                .then_with(|| b.node_id.cmp(&a.node_id))
        });
        self.preview_summaries = summaries
            .into_iter()
            .take(3)
            .map(|node| {
                (
                    Importance::parse(&node.importance).unwrap_or(Importance::Routine),
                    format!(" {} ({} records)", node.title, node.child_count),
                )
            })
            .collect();
        self.tree_state.close_all();
        for id in opened {
            if let Some(path) = self.paths.get(&id) {
                self.tree_state.open(path.clone());
            }
        }
        let selected_path = selected
            .as_ref()
            .and_then(|id| self.paths.get(id))
            .cloned()
            .unwrap_or_else(|| vec![ROOT.to_string()]);
        for end in 1..selected_path.len() {
            self.tree_state.open(selected_path[..end].to_vec());
        }
        self.tree_state.select(selected_path);
        self.refresh_matches();
    }

    fn build_item(
        &mut self,
        id: &str,
        parent: &[String],
        children: &BTreeMap<String, Vec<String>>,
    ) -> Option<TreeItem<'static, String>> {
        let mut path = parent.to_vec();
        path.push(id.to_string());
        let mut items = Vec::new();
        if let Some(ids) = children.get(id) {
            for child in ids {
                if let Some(item) = self.build_item(child, &path, children) {
                    items.push(item);
                }
            }
        }
        let node = self.nodes.get(id);
        let importance = node
            .and_then(|n| Importance::parse(&n.importance))
            .unwrap_or(Importance::Routine);
        let eligible = importance <= self.minimum;
        if !eligible && items.is_empty() {
            return None;
        }
        self.paths.insert(id.to_string(), path);
        let theme = self.cached_theme;
        let mut spans = vec![theme.importance_mark(importance), Span::raw(" ")];
        if let Some(node) = node {
            if let Some(time) = node.time_start.as_deref() {
                let clock = time.split('T').nth(1).unwrap_or(time).trim_end_matches('Z');
                spans.push(Span::styled(format!("{clock} "), theme.dim()));
            }
            if node.level == "record" {
                spans
                    .push(theme.badge(Badge::for_source(&node.source, node.event_kind.as_deref())));
                spans.push(Span::raw(" "));
            }
            if !node.authoritative {
                spans.push(theme.ai_badge());
                spans.push(Span::raw(" "));
            }
            spans.push(Span::styled(
                node.title.clone(),
                if !eligible {
                    theme.dim()
                } else if !node.authoritative {
                    theme.importance(importance).patch(theme.ai())
                } else {
                    theme.importance(importance)
                },
            ));
            if node.level != "record" {
                spans.push(Span::styled(
                    format!(" ({} records)", node.child_count),
                    theme.dim(),
                ));
            }
        } else {
            spans.push(Span::raw("Live — not yet summarized"));
            spans.push(Span::styled(
                format!(" ({} records)", items.len()),
                theme.dim(),
            ));
        }
        if !eligible {
            spans.push(Span::styled(" [filtered parent]", theme.dim()));
        }
        Some(
            TreeItem::new(id.to_string(), Line::from(spans), items)
                .expect("progress IDs are unique"),
        )
    }

    fn refresh_matches(&mut self) {
        let old_match = self
            .match_position
            .and_then(|index| self.matches.get(index))
            .cloned();
        self.matches.clear();
        if !self.search.is_empty() {
            let term = self.search.to_lowercase();
            if self
                .narrative
                .as_ref()
                .and_then(|n| n.text.as_ref())
                .is_some_and(|text| text.to_lowercase().contains(&term))
            {
                self.matches.push(ROOT.to_string());
            }
            for id in &self.order {
                if self.paths.contains_key(id)
                    && self
                        .search_text
                        .get(id)
                        .is_some_and(|text| text.contains(&term))
                {
                    self.matches.push(id.clone());
                }
            }
        }
        self.match_position =
            old_match.and_then(|id| self.matches.iter().position(|matched| *matched == id));
    }

    fn jump_match(&mut self, previous: bool) {
        self.follow_newest = false;
        if self.matches.is_empty() {
            return;
        }
        let index = match self.match_position {
            Some(0) if previous => self.matches.len() - 1,
            Some(index) if previous => index - 1,
            Some(index) => (index + 1) % self.matches.len(),
            None if previous => self.matches.len() - 1,
            None => 0,
        };
        self.match_position = Some(index);
        let id = self.matches[index].clone();
        self.select_node(&id);
    }

    pub fn match_count(&self) -> usize {
        self.matches.len()
    }

    pub(crate) fn sync_detail(&mut self) {
        self.match_position = self
            .matches
            .iter()
            .position(|id| Some(id.as_str()) == self.selected_id());
        if self.detail_selection.as_deref() == self.selected_id() {
            return;
        }
        self.detail_selection = self.selected_id().map(str::to_owned);
        self.detail_scroll = 0;
        self.detail.clear();
        self.detail_ai = false;
        match self.detail_selection.as_deref() {
            Some(ROOT) => {
                self.detail_ai = true;
                if let Some(narrative) = &self.narrative {
                    self.detail = format!(
                        "Run Narrative · AI · non-authoritative\nStatus: {} · source watermark {}\nGenerated: {}\n\n{}",
                        narrative.status,
                        narrative.source_watermark,
                        narrative.generated_at.as_deref().unwrap_or("—"),
                        narrative
                            .text
                            .as_deref()
                            .unwrap_or("Narrative is not available yet.")
                    );
                } else {
                    self.detail.push_str("Run Narrative · AI · non-authoritative\n\nWaiting for progress from the Host.");
                }
            }
            Some(id) => {
                if let Some(node) = self.nodes.get(id) {
                    self.detail_ai = !node.authoritative;
                    self.detail = format!(
                        "{}\n{} · {} · {}{}\nTime: {}{}\n\n{}",
                        node.title,
                        node.source,
                        node.importance,
                        node.level,
                        if node.authoritative {
                            ""
                        } else {
                            " · AI · non-authoritative"
                        },
                        node.time_start.as_deref().unwrap_or("—"),
                        node.time_end
                            .as_deref()
                            .map(|end| format!(" – {end}"))
                            .unwrap_or_default(),
                        node.text.as_deref().unwrap_or("")
                    );
                    if let Some(json) = self.details_json.get(id).filter(|json| !json.is_empty()) {
                        self.detail.push_str("\nDetails:\n");
                        self.detail.push_str(json);
                    }
                } else if id == LIVE {
                    self.detail.push_str("Live — not yet summarized\n\nThese records have not been covered by a Mission Log Summary yet.");
                }
            }
            None => {}
        }
    }
}

fn node_order(id: &str) -> (&str, u64, &str) {
    let (kind, sequence) = id.split_once(':').unwrap_or((id, ""));
    (kind, sequence.parse().unwrap_or(u64::MAX), id)
}
