#!/usr/bin/env python3
"""Decode a short clip, then exercise the actual Ratatui image widget in a TTY."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from onr.paths import repo_tmp_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", type=Path, help="video file; default: FFmpeg moving test pattern")
    parser.add_argument("--mode", choices=("direct", "console"), default="direct")
    parser.add_argument("--fps", type=float, default=15)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--width", type=int, default=640, help="decoded width in pixels; preserves aspect ratio")
    parser.add_argument("--image-protocol", choices=("auto", "kitty", "sixel", "iterm2", "halfblocks"), default="auto")
    parser.add_argument("--report", type=Path, help="JSON timing/empty-widget report, default under var/tui_test")
    args = parser.parse_args()
    if not 0 < args.fps <= 60 or not 0 < args.seconds <= 60 or not 16 <= args.width <= 1920:
        parser.error("use 0 < fps <= 60, 0 < seconds <= 60, and 16 <= width <= 1920")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("run this in an interactive terminal/tmux pane")
    root = Path(__file__).resolve().parents[2]
    video = args.video.resolve() if args.video else None
    if video is not None and not video.is_file():
        parser.error(f"video not found: {video}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        try:
            import imageio_ffmpeg
        except ImportError:
            parser.error("FFmpeg is required: install ffmpeg or imageio-ffmpeg in the onr environment")
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    report = args.report or root / "var/tui_test" / (
        f"video-{args.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    )
    report = report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cargo", "build", "--release", "-p", "operator-console", "--example", "video"], cwd=root, check=True)
    # Decode before the timed loop: no live decoder or model can mask widget gaps.
    # Only compressed frames are kept on disk; playback loads one JPEG at a time.
    with TemporaryDirectory(prefix="tui-video-", dir=repo_tmp_root()) as scratch:
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if video is None:
            command += ["-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={args.fps}"]
        else:
            command += ["-i", str(video)]
        command += [
            "-t", str(args.seconds), "-an", "-vf", f"fps={args.fps},scale={args.width}:-2",
            "-threads", "1", "-q:v", "2", str(Path(scratch) / "%06d.jpg"),
        ]
        subprocess.run(command, check=True)
        return subprocess.call([
            str(root / "target/release/examples/video"), scratch, str(args.fps), args.mode,
            args.image_protocol, str(report),
        ], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
