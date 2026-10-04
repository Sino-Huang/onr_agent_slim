//! Isolate ratatui-image from the Host/mission. Use scripts/tui/test_video.py.
use std::{
    fs, io,
    path::PathBuf,
    time::{Duration, Instant},
};

use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use operator_console::{
    app::world::{ImageProtocol, WorldMedia},
    host::{FrameSource, WorldFrame},
    terminal::{TerminalGuard, install_panic_hook},
};
use ratatui::{
    layout::{Constraint, Layout},
    style::Color,
    widgets::Paragraph,
};
use ratatui_image::{Resize, StatefulImage};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 6 {
        return Err(
            "usage: video FRAME_DIRECTORY FPS direct|console IMAGE_PROTOCOL REPORT_JSON".into(),
        );
    }
    let fps: f64 = args[2].parse()?;
    if !fps.is_finite() || fps <= 0.0 || fps > 60.0 {
        return Err("FPS must be in (0, 60]".into());
    }
    let console = match args[3].as_str() {
        "direct" => false,
        "console" => true,
        _ => return Err("mode must be direct or console".into()),
    };
    let requested: ImageProtocol = args[4].parse()?;
    if requested == ImageProtocol::Off {
        return Err("video playback requires an image protocol".into());
    }
    let mut paths: Vec<PathBuf> = fs::read_dir(&args[1])?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<io::Result<_>>()?;
    paths.retain(|path| path.extension().is_some_and(|ext| ext == "jpg"));
    paths.sort();
    if paths.is_empty() {
        return Err("no JPEG frames in input directory".into());
    }

    install_panic_hook();
    let mut terminal = TerminalGuard::new()?;
    let picker = requested.picker().expect("images enabled");
    let protocol = format!("{:?}", picker.protocol_type());
    let font = picker.font_size();
    let mut media = WorldMedia::default();
    if console {
        media.configure(Some(picker.clone()));
    }
    let mut direct = None;
    let started = Instant::now();
    let duration = Duration::from_secs_f64(paths.len() as f64 / fps);
    let mut next_frame = 0;
    let mut submitted = 0;
    let mut skipped = 0;
    let mut draws = 0;
    let mut blank_draws = 0;
    let mut blank_transitions = 0;
    let mut previously_blank = false;
    let mut seen_image = false;
    let mut draw_ms = Vec::new();
    let mut preparation_ms = Vec::new();
    let mut quit = false;
    while started.elapsed() < duration && !quit {
        let tick = Instant::now();
        let index = ((started.elapsed().as_secs_f64() * fps) as usize).min(paths.len() - 1);
        if index >= next_frame {
            skipped += index - next_frame;
            let prep = Instant::now();
            let bytes = fs::read(&paths[index])?;
            if console {
                media.submit(WorldFrame {
                    source: FrameSource::World,
                    media_type: "image/jpeg".into(),
                    etag: None,
                    sequence: Some(index as u64),
                    mission_time: None,
                    bytes,
                });
            } else {
                direct = Some(picker.new_resize_protocol(image::load_from_memory(&bytes)?));
            }
            preparation_ms.push(prep.elapsed().as_secs_f64() * 1000.0);
            submitted += 1;
            next_frame = index + 1;
        }
        if console && let Some(result) = media.poll() {
            result?;
        }
        let draw_started = Instant::now();
        terminal.terminal().draw(|frame| {
            let [header, image_area, footer] = Layout::vertical([
                Constraint::Length(2), Constraint::Min(1), Constraint::Length(2),
            ]).areas(frame.area());
            frame.render_widget(Paragraph::new(format!(
                "Ratatui video | {} | {protocol} | cell {}x{} px\nTarget {fps:.1} fps | submitted {submitted} | skipped {skipped} | frame {index}/{}",
                args[3], font.width, font.height, paths.len()
            )), header);
            if console { media.render(frame, image_area); }
            else if let Some(image) = direct.as_mut() {
                frame.render_stateful_widget(StatefulImage::default().resize(Resize::Fit(None)), image_area, image);
            }
            // Inspect the image region only, not labels. This detects empty widget
            // draws, not blank source pixels or desktop/herdr compositing flashes.
            let buffer = frame.buffer_mut();
            let populated = (image_area.y..image_area.bottom()).any(|y| {
                (image_area.x..image_area.right()).any(|x| {
                    let cell = &buffer[(x, y)];
                    cell.symbol() != " " || cell.bg != Color::Reset
                })
            });
            if populated { seen_image = true; }
            else if seen_image {
                blank_draws += 1;
                if !previously_blank { blank_transitions += 1; }
            }
            previously_blank = !populated;
            frame.render_widget(Paragraph::new(format!(
                "Empty image draws after first image: {blank_draws} | gaps: {blank_transitions}\nq / Esc / Ctrl+C quit | counts are application output, not desktop display FPS"
            )), footer);
        })?;
        draw_ms.push(draw_started.elapsed().as_secs_f64() * 1000.0);
        draws += 1;
        if let Some(image) = direct.as_mut()
            && let Some(result) = image.last_encoding_result()
        {
            result?;
        }
        let wait = Duration::from_secs_f64(1.0 / 60.0).saturating_sub(tick.elapsed());
        if event::poll(wait)?
            && let Event::Key(key) = event::read()?
            && key.kind != KeyEventKind::Release
        {
            quit = matches!(key.code, KeyCode::Char('q') | KeyCode::Esc)
                || (key.code == KeyCode::Char('c')
                    && key.modifiers.contains(KeyModifiers::CONTROL));
        }
    }
    let elapsed = started.elapsed().as_secs_f64();
    drop(terminal);
    draw_ms.sort_by(f64::total_cmp);
    preparation_ms.sort_by(f64::total_cmp);
    let percentile = |samples: &[f64]| {
        samples
            .get(samples.len().saturating_sub(1) * 95 / 100)
            .copied()
            .unwrap_or(0.0)
    };
    let report = serde_json::json!({
        "mode": args[3], "protocol": protocol, "cell_pixels": [font.width, font.height],
        "target_fps": fps, "elapsed_seconds": elapsed, "source_frames": paths.len(),
        "submitted_frames": submitted, "skipped_source_frames": skipped,
        "submitted_fps": submitted as f64 / elapsed, "draws": draws,
        "empty_image_draws_after_first": blank_draws, "empty_image_transitions": blank_transitions,
        "saw_image": seen_image, "draw_p95_ms": percentile(&draw_ms),
        "prepare_p95_ms": percentile(&preparation_ms), "quit_early": quit,
        "measurement": "application output only; not terminal presentation FPS"
    });
    let json = serde_json::to_string_pretty(&report)?;
    fs::write(&args[5], &json)?;
    println!("{json}\nReport: {}", args[5]);
    if !seen_image {
        return Err("no image was rendered".into());
    }
    Ok(())
}
