//! Actual halfblocks rendering, asynchronous decode failures, retained final
//! frames, and visible/paused polling boundaries.

mod common;

use image::{DynamicImage, ImageFormat, Rgba, RgbaImage};
use operator_console::{
    app::world::{WorldMedia, cycle_source},
    host::{
        Fetched, FrameSource, HostMessage, OperatorWorld, WorldFrame, WorldFrameInfo, WorldViewer,
    },
};
use ratatui::{Terminal, backend::TestBackend, style::Color};
use ratatui_image::picker::Picker;
use std::{
    io::Cursor,
    time::{Duration, Instant},
};

fn encoded(source: FrameSource, color: [u8; 4], format: ImageFormat) -> WorldFrame {
    let image = DynamicImage::ImageRgba8(RgbaImage::from_pixel(320, 240, Rgba(color)));
    let mut bytes = Cursor::new(Vec::new());
    // JPEG has no alpha channel.
    let image = if format == ImageFormat::Jpeg {
        DynamicImage::ImageRgb8(image.to_rgb8())
    } else {
        image
    };
    image.write_to(&mut bytes, format).unwrap();
    WorldFrame {
        source,
        media_type: if format == ImageFormat::Jpeg {
            "image/jpeg"
        } else {
            "image/png"
        }
        .into(),
        etag: Some("frame:9".into()),
        sequence: Some(9),
        mission_time: Some("14.5".into()),
        bytes: bytes.into_inner(),
    }
}

fn terminal() -> Terminal<TestBackend> {
    Terminal::new(TestBackend::new(24, 12)).unwrap()
}

fn complete(media: &mut WorldMedia, terminal: &mut Terminal<TestBackend>) {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(result) = media.poll() {
            result.unwrap();
        }
        terminal
            .draw(|frame| media.render(frame, frame.area()))
            .unwrap();
        if media.is_ready() {
            return;
        }
        assert!(Instant::now() < deadline, "image worker failed to finish");
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn png_and_jpeg_preserve_frame_colors_across_terminal_resize() {
    for format in [ImageFormat::Png, ImageFormat::Jpeg] {
        let mut media = WorldMedia::default();
        media.configure(Some(Picker::halfblocks()));
        media.submit(encoded(FrameSource::World, [220, 30, 50, 255], format));
        let mut terminal = terminal();
        complete(&mut media, &mut terminal);
        let assert_frame_color = |terminal: &Terminal<TestBackend>, x, y| {
            let pixel = &terminal.backend().buffer()[(x, y)];
            assert!(
                matches!(pixel.bg, Color::Rgb(r, g, b) if r > 210 && g < 40 && b < 60),
                "frame colour missing at ({x}, {y}): {pixel:?}"
            );
        };
        assert_frame_color(&terminal, 2, 1);
        terminal.backend_mut().resize(10, 6);
        terminal.autoresize().unwrap();
        terminal
            .draw(|frame| media.render(frame, frame.area()))
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let completed = media.poll();
            terminal
                .draw(|frame| media.render(frame, frame.area()))
                .unwrap();
            if matches!(completed, Some(Ok(()))) {
                break;
            }
            assert!(Instant::now() < deadline, "resized frame failed to finish");
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_frame_color(&terminal, 2, 1);
    }
}

#[test]
fn invalid_frame_reports_error_without_discarding_last_good_image() {
    let mut media = WorldMedia::default();
    media.configure(Some(Picker::halfblocks()));
    media.submit(encoded(
        FrameSource::World,
        [20, 170, 70, 255],
        ImageFormat::Png,
    ));
    let mut terminal = terminal();
    complete(&mut media, &mut terminal);
    let before = terminal.backend().buffer().clone();
    let mut invalid = encoded(FrameSource::World, [0, 0, 0, 255], ImageFormat::Png);
    invalid.bytes = b"not an image".to_vec();
    media.submit(invalid);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(result) = media.poll() {
            assert!(result.unwrap_err().contains("Cannot decode world frame"));
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(5));
    }
    terminal
        .draw(|frame| media.render(frame, frame.area()))
        .unwrap();
    assert_eq!(terminal.backend().buffer(), &before);
    assert!(media.is_ready());
}

#[test]
fn switching_source_clears_old_pixels_and_rejects_late_previous_source_frame() {
    let mut media = WorldMedia::default();
    media.configure(Some(Picker::halfblocks()));
    media.submit(encoded(
        FrameSource::World,
        [220, 30, 50, 255],
        ImageFormat::Png,
    ));
    let mut terminal = terminal();
    complete(&mut media, &mut terminal);
    media.set_source(FrameSource::CameraFront);
    media.submit(encoded(
        FrameSource::World,
        [220, 30, 50, 255],
        ImageFormat::Png,
    ));
    terminal
        .draw(|frame| media.render(frame, frame.area()))
        .unwrap();
    assert!(!media.is_ready());
    assert!(
        !terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|cell| cell.symbol() == "▀")
    );
    media.submit(encoded(
        FrameSource::CameraFront,
        [20, 170, 70, 255],
        ImageFormat::Png,
    ));
    complete(&mut media, &mut terminal);
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|cell| matches!(cell.fg, Color::Rgb(r, g, b) if r == 20 && g == 170 && b == 70))
    );
}

#[test]
fn only_visible_unpaused_images_poll_at_two_hertz_and_switches_are_immediate() {
    let mut media = WorldMedia::default();
    assert!(!media.should_request(true, FrameSource::World, Duration::ZERO));
    media.configure(Some(Picker::halfblocks()));
    assert!(!media.should_request(false, FrameSource::World, Duration::ZERO));
    assert!(media.should_request(true, FrameSource::World, Duration::ZERO));
    assert!(!media.should_request(true, FrameSource::World, Duration::from_millis(499)));
    assert!(media.should_request(true, FrameSource::World, Duration::from_millis(500)));
    media.paused = true;
    assert!(!media.should_request(true, FrameSource::World, Duration::from_secs(2)));
    media.paused = false;
    assert!(media.should_request(true, FrameSource::World, Duration::from_secs(2)));
    assert!(media.should_request(true, FrameSource::CameraFront, Duration::from_secs(2)));
}

#[test]
fn source_cycle_uses_only_advertised_frames() {
    let world = |sources: &[FrameSource]| OperatorWorld {
        viewer: WorldViewer {
            available: true,
            reason: None,
        },
        state: None,
        frames: sources
            .iter()
            .map(|source| WorldFrameInfo {
                source: source.as_str().into(),
                sequence: None,
                media_type: "image/png".into(),
            })
            .collect(),
        airsim: None,
    };
    let all = world(&[
        FrameSource::CameraThirdPerson,
        FrameSource::CameraFront,
        FrameSource::CameraFrontAnnotated,
        FrameSource::World,
    ]);
    let mut source = FrameSource::World;
    let mut order = Vec::new();
    for _ in 0..4 {
        source = cycle_source(source, Some(&all));
        order.push(source);
    }
    assert_eq!(
        order,
        [
            FrameSource::CameraFrontAnnotated,
            FrameSource::CameraFront,
            FrameSource::CameraThirdPerson,
            FrameSource::World,
        ],
        "cycle order is fixed, not the Host's advertisement order"
    );

    let partial = world(&[FrameSource::World, FrameSource::CameraThirdPerson]);
    assert_eq!(
        cycle_source(FrameSource::World, Some(&partial)),
        FrameSource::CameraThirdPerson
    );
    assert_eq!(
        cycle_source(FrameSource::CameraThirdPerson, Some(&partial)),
        FrameSource::World
    );
    assert_eq!(cycle_source(FrameSource::World, None), FrameSource::World);
}

#[test]
fn not_modified_keeps_final_decoded_image_and_metadata() {
    let (mut app, _) = common::run_app("world-final-304");
    app.configure_images(Some(Picker::halfblocks()));
    let image = encoded(FrameSource::World, [20, 170, 70, 255], ImageFormat::Png);
    app.handle_host_message(HostMessage::WorldFrame {
        mission_run_id: common::RUN_ID.into(),
        source: FrameSource::World,
        result: Ok(Fetched::Fresh {
            value: image,
            etag: Some("frame:9".into()),
        }),
    });
    let mut terminal = terminal();
    complete(&mut app.view.media, &mut terminal);
    let before = terminal.backend().buffer().clone();
    app.run.as_mut().unwrap().status = "completed".into();
    app.handle_host_message(HostMessage::WorldFrame {
        mission_run_id: common::RUN_ID.into(),
        source: FrameSource::World,
        result: Ok(Fetched::NotModified),
    });
    terminal
        .draw(|frame| app.view.media.render(frame, frame.area()))
        .unwrap();
    assert_eq!(terminal.backend().buffer(), &before);
    assert_eq!(app.view.frame.as_ref().unwrap().sequence, Some(9));
    assert_eq!(
        app.view.frame.as_ref().unwrap().etag.as_deref(),
        Some("frame:9")
    );
    assert!(app.view.media.is_ready());
}
