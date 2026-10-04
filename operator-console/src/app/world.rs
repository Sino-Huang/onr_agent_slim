//! World-frame decoding and image encoding never run on the terminal thread.

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::time::Duration;

use image::DynamicImage;
use parking_lot::Mutex;
use ratatui::{
    Frame,
    layout::{Rect, Size},
};
use ratatui_image::{
    Image, Resize,
    picker::{Picker, ProtocolType},
    protocol::Protocol,
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
    /// Query cell geometry even for a forced protocol: character cells are not
    /// necessarily twice as tall as they are wide.
    pub fn picker(self) -> Option<Picker> {
        if self == Self::Off {
            return None;
        }
        let mut picker = Picker::from_query_stdio().unwrap_or_else(|_| Picker::halfblocks());
        if self != Self::Auto {
            picker.set_protocol_type(match self {
                Self::Kitty => ProtocolType::Kitty,
                Self::Sixel => ProtocolType::Sixel,
                Self::Iterm2 => ProtocolType::Iterm2,
                _ => ProtocolType::Halfblocks,
            });
        }
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

#[derive(Clone, Copy, PartialEq, Eq)]
struct EncodeKey {
    generation: u64,
    size: Size,
    scaled: bool,
}

struct EncodeJob {
    key: EncodeKey,
    image: Arc<DynamicImage>,
}

#[derive(Default)]
struct Mailbox {
    // Bounded latest-wins slots. A slow codec cannot grow a frame backlog.
    decode: Option<DecodeJob>,
    decoded: Option<(u64, Result<Arc<DynamicImage>, String>)>,
    encode: Option<EncodeJob>,
    encoded: Option<(EncodeKey, Result<Protocol, String>)>,
}

struct ImageWorker {
    mailbox: Arc<Mutex<Mailbox>>,
    stopped: Arc<AtomicBool>,
    wake: mpsc::SyncSender<()>,
    source: Option<(u64, Arc<DynamicImage>)>,
    requested: Option<EncodeKey>,
    displayed: Option<Protocol>,
}

impl ImageWorker {
    fn spawn(picker: Picker) -> Self {
        let mailbox = Arc::new(Mutex::new(Mailbox::default()));
        let stopped = Arc::new(AtomicBool::new(false));
        let (wake, rx) = mpsc::sync_channel(1);
        let worker_mailbox = Arc::clone(&mailbox);
        let worker_stopped = Arc::clone(&stopped);
        std::thread::Builder::new()
            .name("console-image".into())
            .spawn(move || {
                while !worker_stopped.load(Ordering::Relaxed) {
                    if rx.recv().is_err() || worker_stopped.load(Ordering::Relaxed) {
                        break;
                    }
                    let job = worker_mailbox.lock().decode.take();
                    if let Some(job) = job {
                        let result = image::load_from_memory(&job.frame.bytes)
                            .map(Arc::new)
                            .map_err(|error| {
                                format!(
                                    "Cannot decode {} frame: {error}",
                                    job.frame.source.as_str()
                                )
                            });
                        worker_mailbox.lock().decoded = Some((job.generation, result));
                    }
                    let job = worker_mailbox.lock().encode.take();
                    if let Some(job) = job {
                        let resize = if job.key.scaled {
                            Resize::Scale(None)
                        } else {
                            Resize::Fit(None)
                        };
                        let size = resize.size_for(&job.image, picker.font_size(), job.key.size);
                        let image = resize.resize(&job.image, picker.font_size(), size, None);
                        // Already cell-aligned: new_protocol consumes these pixels
                        // without another resize or a copy of the original image.
                        let result = picker
                            .new_protocol(image, size, Resize::Fit(None))
                            .map_err(|error| format!("Cannot encode frame: {error}"));
                        worker_mailbox.lock().encoded = Some((job.key, result));
                    }
                }
            })
            .expect("spawn image worker");
        Self {
            mailbox,
            stopped,
            wake,
            source: None,
            requested: None,
            displayed: None,
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
            worker.source = None;
            worker.requested = None;
            worker.displayed = None;
            let mut mailbox = worker.mailbox.lock();
            mailbox.decode = None;
            mailbox.decoded = None;
            mailbox.encode = None;
            mailbox.encoded = None;
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
            let _ = worker.wake.try_send(());
        }
    }

    /// Integrate completed work without waiting. Decode errors preserve the
    /// last good image; source changes explicitly clear it.
    pub fn poll(&mut self) -> Option<Result<(), String>> {
        let worker = self.worker.as_mut()?;
        let (decoded, encoded) = {
            let mut mailbox = worker.mailbox.lock();
            (mailbox.decoded.take(), mailbox.encoded.take())
        };
        let mut completion = None;
        if let Some((key, result)) = encoded
            && worker.requested == Some(key)
        {
            match result {
                Ok(protocol) => {
                    worker.displayed = Some(protocol);
                    self.ready = true;
                    completion = Some(Ok(()));
                }
                Err(error) => completion = Some(Err(error)),
            }
        }
        if let Some((generation, result)) = decoded
            && generation == self.generation
        {
            match result {
                Ok(image) => {
                    worker.source = Some((generation, image));
                    worker.requested = None;
                    completion = Some(Ok(()));
                }
                Err(error) => completion = Some(Err(error)),
            }
        }
        completion
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect) {
        self.render_with(frame, area, false);
    }

    /// The presentation layout: scaled to fill `area`, also upwards.
    pub fn render_scaled(&mut self, frame: &mut Frame, area: Rect) {
        self.render_with(frame, area, true);
    }

    fn render_with(&mut self, frame: &mut Frame, area: Rect, scaled: bool) {
        if area.is_empty() {
            return;
        }
        if let Some(worker) = self.worker.as_mut() {
            if let Some((generation, image)) = worker.source.as_ref() {
                let key = EncodeKey {
                    generation: *generation,
                    size: area.into(),
                    scaled,
                };
                if worker.requested != Some(key) {
                    worker.mailbox.lock().encode = Some(EncodeJob {
                        key,
                        image: Arc::clone(image),
                    });
                    worker.requested = Some(key);
                    let _ = worker.wake.try_send(());
                }
            }
            // Never render the pending encoder state. Retain the last completed
            // image until poll swaps in its replacement, including on resize.
            if let Some(protocol) = worker.displayed.as_ref() {
                frame.render_widget(Image::new(protocol).allow_clipping(true), area);
            }
        }
    }
}
