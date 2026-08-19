"""Installed deterministic demo integrations with no production authority."""

from onr.demo.fake_environment import (
    FakeEnvironment,
    FakeEnvironmentResult,
    SUPPORTED_LIFECYCLES,
)

__all__ = [
    "FakeEnvironment",
    "FakeEnvironmentResult",
    "SUPPORTED_LIFECYCLES",
]
