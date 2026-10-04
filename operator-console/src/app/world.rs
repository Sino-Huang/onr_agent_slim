//! World-frame decoding and image encoding never run on the terminal thread.

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::time::Duration;

use parking_lot::Mutex;
use ratatui::{Frame, layout::Rect};
use ratatui_image::{
    Resize, StatefulImage,
    picker::{Picker, ProtocolType},
    protocol::StatefulProtocol,
    thread::{ResizeRequest, ResizeResponse, ThreadProtocol},
};

use crate::host::{FrameSource, OperatorWorld, WorldFrame};

pub const FRAME_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ImageProtocol {
    #[default]
    Auto,
    Kitty,
    Sixel,
    Iterm2,
    Halfblocks,
    Off,
}

impl std::str::FromStr for ImageProtocol {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "auto" => Ok(Self::Auto),
            "kitty" => Ok(Self::Kitty),
            "sixel" => Ok(Self::Sixel),
            "iterm2" => Ok(Self::Iterm2),
            "halfblocks" => Ok(Self::Halfblocks),
            "off" => Ok(Self::Off),
            _ => Err(format!(
                "invalid image protocol {value:?}; expected auto|kitty|sixel|iterm2|halfblocks|off"
            )),
        }
    }
}

impl ImageProtocol {
    /// Call only after entering the alternate screen, before reading events.
    /// Explicit protocols never consume terminal input for capability queries.
    pub fn picker(self) -> Option<Picker> {
        if self == Self::Off {
            return None;
        }
        if self == Self::Auto {
            return Some(Picker::from_query_stdio().unwrap_or_else(|_| Picker::halfblocks()));
        }
        let mut picker = Picker::halfblocks();
        picker.set_protocol_type(match self {
            Self::Kitty => ProtocolType::Kitty,
            Self::Sixel => ProtocolType::Sixel,
            Self::Iterm2 => ProtocolType::Iterm2,
            _ => ProtocolType::Halfblocks,
        });
        Some(picker)
    }
}

/// Only sources advertised by the Host are selectable. An unavailable viewer
/// may advertise no frames yet; keep the selection until real sources arrive.
pub fn cycle_source(current: FrameSource, world: Option<&OperatorWorld>) -> FrameSource {
    let Some(world) = world else {
        return current;
    };
    let available = |source: FrameSource| {
        world
            .frames
            .iter()
            .any(|frame| frame.source == source.as_str())
    };
    let index = FrameSource::ALL
        .iter()
        .position(|source| *source == current)
        .unwrap_or(0);
    (1..=FrameSource::ALL.len())
        .map(|offset| FrameSource::ALL[(index + offset) % FrameSource::ALL.len()])
        .find(|source| available(*source))
        .unwrap_or(current)
}

struct DecodeJob {
    generation: u64,
    frame: WorldFrame,
}

#[derive(Default)]
struct Mailbox {
    // Bounded latest-wins slots. A slow codec cannot grow a frame backlog.
    decode: Option<DecodeJob>,
    decoded: Option<(u64, Result<StatefulProtocol, String>)>,
    resized: Option<Result<ResizeResponse, String>>,
}

struct ImageWorker {
    mailbox: Arc<Mutex<Mailbox>>,
    stopped: Arc<AtomicBool>,
    protocol: ThreadProtocol,
}

impl ImageWorker {
    fn spawn(picker: Picker) -> Self {
        let mailbox = Arc::new(Mutex::new(Mailbox::default()));
        let stopped = Arc::new(AtomicBool::new(false));
        let (tx, rx) = mpsc::channel::<ResizeRequest>();
        let worker_mailbox = Arc::clone(&mailbox);
        let worker_stopped = Arc::clone(&stopped);
        std::thread::Builder::new()
            .name("console-image".into())
            .spawn(move || {
                while !worker_stopped.load(Ordering::Relaxed) {
                    let job = worker_mailbox.lock().decode.take();
                    if let Some(job) = job {
                        let result = image::load_from_memory(&job.frame.bytes)
                            .map(|image| picker.new_resize_protocol(image))
                            .map_err(|error| {
                                format!(
                                    "Cannot decode {} frame: {error}",
                                    job.frame.source.as_str()
                                )
                            });
                        worker_mailbox.lock().decoded = Some((job.generation, result));
                    }
                    match rx.recv_timeout(Duration::from_millis(10)) {
                        Ok(mut request) => {
                            // ThreadProtocol has at most one outstanding resize per
                            // image. Drop superseded requests before expensive work.
                            while let Ok(newer) = rx.try_recv() {
                                request = newer;
                            }
                            let result = request
                                .resize_encode()
                                .map_err(|error| format!("Cannot encode frame: {error}"));
                            worker_mailbox.lock().resized = Some(result);
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => {}
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    }
                }
            })
            .expect("spawn image worker");
        Self {
            mailbox,
            stopped,
            protocol: ThreadProtocol::new(tx, None),
        }
    }
}

impl Drop for ImageWorker {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
    }
}

/// Non-cloneable state: the full decoded image and encoded protocol stay owned
/// by a single worker/render pipeline, rather than copied each draw.
#[derive(Default)]
pub struct WorldMedia {
    worker: Option<ImageWorker>,
    generation: u64,
    source: Option<FrameSource>,
    last_request: Option<Duration>,
    pub paused: bool,
    ready: bool,
}

impl std::fmt::Debug for WorldMedia {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("WorldMedia")
            .field("enabled", &self.is_enabled())
            .field("source", &self.source)
            .field("paused", &self.paused)
            .field("ready", &self.ready)
            .finish()
    }
}

impl WorldMedia {
    pub fn configure(&mut self, picker: Option<Picker>) {
        self.worker = picker.map(ImageWorker::spawn);
        self.last_request = None;
        self.ready = false;
        self.generation = self.generation.wrapping_add(1);
    }

    pub fn is_enabled(&self) -> bool {
        self.worker.is_some()
    }
    pub fn is_ready(&self) -> bool {
        self.ready
    }

    pub fn set_source(&mut self, source: FrameSource) {
        if self.source == Some(source) {
            return;
        }
        self.source = Some(source);
        self.generation = self.generation.wrapping_add(1);
        self.last_request = None;
        self.ready = false;
        if let Some(worker) = self.worker.as_mut() {
            worker.protocol.empty_protocol();
            let mut mailbox = worker.mailbox.lock();
            mailbox.decode = None;
            mailbox.decoded = None;
            mailbox.resized = None;
        }
    }

    /// Returns a polling opportunity. The App's final-refresh bookkeeping owns
    /// terminal once-only semantics, including its final-summary rearm.
    pub fn should_request(&mut self, visible: bool, source: FrameSource, now: Duration) -> bool {
        if !visible || self.paused || !self.is_enabled() {
            return false;
        }
        self.set_source(source);
        if self
            .last_request
            .is_some_and(|last| now.saturating_sub(last) < FRAME_INTERVAL)
        {
            return false;
        }
        self.last_request = Some(now);
        true
    }

    pub fn submit(&mut self, frame: WorldFrame) {
        if self.source.is_some_and(|source| source != frame.source) {
            return;
        }
        self.source = Some(frame.source);
        self.generation = self.generation.wrapping_add(1);
        if let Some(worker) = self.worker.as_mut() {
            worker.mailbox.lock().decode = Some(DecodeJob {
                generation: self.generation,
                frame,
            });
        }
    }

    /// Integrate completed work without waiting. Decode errors preserve the
    /// last good image; source changes explicitly clear it.
    pub fn poll(&mut self) -> Option<Result<(), String>> {
        let worker = self.worker.as_mut()?;
        let (decoded, resized) = {
            let mut mailbox = worker.mailbox.lock();
            (mailbox.decoded.take(), mailbox.resized.take())
        };
        let mut completion = None;
        if let Some(result) = resized {
            match result {
                Ok(response) => {
                    if worker.protocol.update_resized_protocol(response) {
                        self.ready = true;
                        completion = Some(Ok(()));
                    }
                }
                Err(error) => completion = Some(Err(error)),
            }
        }
        if let Some((generation, result)) = decoded
            && generation == self.generation
        {
            match result {
                Ok(protocol) => {
                    worker.protocol.replace_protocol(protocol);
                    self.ready = false;
                    completion = Some(Ok(()));
                }
                Err(error) => completion = Some(Err(error)),
            }
        }
        completion
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect) {
        self.render_with(frame, area, Resize::Fit(None));
    }

    /// The presentation layout: scaled to fill `area`, also upwards.
    pub fn render_scaled(&mut self, frame: &mut Frame, area: Rect) {
        self.render_with(frame, area, Resize::Scale(None));
    }

    fn render_with(&mut self, frame: &mut Frame, area: Rect, resize: Resize) {
        if let Some(worker) = self.worker.as_mut() {
            frame.render_stateful_widget(
                StatefulImage::<ThreadProtocol>::default().resize(resize),
                area,
                &mut worker.protocol,
            );
        }
    }
}
