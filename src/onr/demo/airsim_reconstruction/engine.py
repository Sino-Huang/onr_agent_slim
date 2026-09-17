"""Isolated Harbor/AirSim engine configuration and launch helpers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace *path* atomically while retaining its existing mode when possible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def preflight_ports_free(
    ports: Iterable[int], host: str = "127.0.0.1", timeout_s: float = 0.2
) -> None:
    """Raise when any requested TCP port already accepts connections."""
    occupied: list[int] = []
    for port in dict.fromkeys(int(value) for value in ports):
        try:
            connection = socket.create_connection((host, port), timeout=timeout_s)
        except OSError:
            continue
        connection.close()
        occupied.append(port)
    if occupied:
        listed = ", ".join(str(port) for port in occupied)
        raise RuntimeError(
            f"Port {occupied[0]} occupied; certification requires fresh ports "
            f"(occupied: {listed})"
        )


class EngineConfigSwap:
    """Temporarily install scenario and instance-segmentation engine config."""

    def __init__(
        self,
        engine_config_path: str | Path,
        object_ids_path: str | Path,
        instance_object_ids_path: str | Path,
        scenario_dir: str | Path,
        status_dir: str | Path,
        *,
        warmup_s: float = 30.0,
        cooldown_s: float = 2.0,
        evidence_dir: str | Path,
    ) -> None:
        self.engine_config_path = Path(engine_config_path)
        self.object_ids_path = Path(object_ids_path)
        self.instance_object_ids_path = Path(instance_object_ids_path)
        self.scenario_dir = Path(scenario_dir)
        self.status_dir = Path(status_dir)
        self.warmup_s = float(warmup_s)
        self.cooldown_s = float(cooldown_s)
        self.evidence_dir = Path(evidence_dir)
        self.configuration_restored = False
        self.original_sha256: dict[str, str] = {}
        self._original_environment: bytes | None = None
        self._original_object_ids: bytes | None = None

    def __enter__(self) -> EngineConfigSwap:  # noqa: PYI034
        self.configuration_restored = False
        try:
            self._original_environment = self.engine_config_path.read_bytes()
            self._original_object_ids = self.object_ids_path.read_bytes()
            self.original_sha256 = {
                "environment": _sha256(self._original_environment),
                "object_ids": _sha256(self._original_object_ids),
            }

            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            (self.evidence_dir / "environment.before.json").write_bytes(
                self._original_environment
            )
            (self.evidence_dir / "object_ids.before.txt").write_bytes(
                self._original_object_ids
            )

            config = json.loads(self._original_environment)
            config.update(
                ScenarioDir=str(self.scenario_dir),
                StatusDir=str(self.status_dir),
                WarmUpTime=self.warmup_s,
                CoolDownTime=self.cooldown_s,
            )
            test_environment = (json.dumps(config, indent=2) + "\n").encode()
            test_object_ids = self.instance_object_ids_path.read_bytes()
            (self.evidence_dir / "environment.test.json").write_bytes(
                test_environment
            )
            (self.evidence_dir / "object_ids.test.txt").write_bytes(
                test_object_ids
            )

            _atomic_write(self.engine_config_path, test_environment)
            _atomic_write(self.object_ids_path, test_object_ids)
        except BaseException:
            self._restore()
            raise
        return self

    def _restore(self) -> None:
        errors: list[BaseException] = []
        for path, original in (
            (self.engine_config_path, self._original_environment),
            (self.object_ids_path, self._original_object_ids),
        ):
            if original is None:
                continue
            try:
                _atomic_write(path, original)
            except OSError as exc:  # preserve an attempt on the other file
                errors.append(exc)

        restored = (
            self._original_environment is not None
            and self._original_object_ids is not None
            and self.engine_config_path.read_bytes() == self._original_environment
            and self.object_ids_path.read_bytes() == self._original_object_ids
        )
        self.configuration_restored = bool(restored)
        if errors or not restored:
            detail = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            raise RuntimeError(
                "Engine configuration restoration failed"
                + (f": {detail}" if detail else "")
            ) from (errors[0] if errors else None)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._restore()


def launch_engine(
    engine_executable: str | Path,
    settings_path: str | Path,
    log_path: str | Path,
    *,
    cuda_device: int = 2,
    extra_args: Iterable[str] = (),
) -> subprocess.Popen[bytes]:
    """Launch the isolated offscreen engine; the caller owns its lifecycle."""
    executable = Path(engine_executable)
    settings = Path(settings_path)
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        "-vulkan",
        "-RenderOffscreen",
        "-ResX=640",
        "-ResY=480",
        "-windowed",
        *[str(argument) for argument in extra_args],
        f"-settings={settings}",
    ]
    environment = {
        **os.environ,
        "SDL_VIDEODRIVER_VALUE": "offscreen",
        "SDL_HINT_CUDA_DEVICE": str(cuda_device),
    }
    stream = log.open("wb")
    try:
        return subprocess.Popen(
            command,
            cwd=executable.parent,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        stream.close()


def build_engine_settings(
    source_settings_path: str | Path,
    out_path: str | Path,
    *,
    api_port: int = 41461,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Copy AirSim settings with an isolated port and camera dimensions."""
    source = Path(source_settings_path)
    output = Path(out_path)
    settings = json.loads(source.read_text())
    settings["ApiServerPort"] = int(api_port)
    vehicles = settings.get("Vehicles", {})
    for vehicle in vehicles.values():
        for camera in vehicle.get("Cameras", {}).values():
            for capture in camera.get("CaptureSettings", []):
                capture["Width"] = int(width)
                capture["Height"] = int(height)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(settings, indent=2) + "\n")
    return output
