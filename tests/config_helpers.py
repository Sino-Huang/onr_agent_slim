from pathlib import Path


def write_environment_profile(tmp_path: Path) -> Path:
    scenario = tmp_path / "environment-scenario.json"
    scenario.write_text("[]\n", encoding="utf-8")
    profile = tmp_path / "environment-profile.yaml"
    profile.write_text(
        f"""adapter_kind: fake
protocols:
  maneuver_command: 1
  maneuver_feedback: 1
  environment_data: 1
  perception: 1
updates:
  ownership: coordinator_driven
  cadence_seconds: 0.5
topics:
  command_target: maneuver-adapter
  command: maneuver
  feedback: maneuver-feedback
  perception: environment-perceptions
  environment_data: environment-data
  context: planning-evidence
supported_actions: [navigate, takeoff, land, search_area, pursue, investigate]
fake:
  scenario_path: {scenario}
  initial_position: [0, 0, -250]
  max_velocity: 20
  sensing_radius: 30
  max_retries: 3
  artifact_root: {tmp_path / "environment"}
""",
        encoding="utf-8",
    )
    return profile
