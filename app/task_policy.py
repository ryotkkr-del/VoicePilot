"""Bounded, opt-in desktop task policy. No persistence or raw text logging.

The AI is not granted new OS capabilities in long-task mode. This layer
only makes existing step execution safer to run for longer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Reject ambiguity for anything that can change the PC or external services.
EFFECTFUL_ACTIONS = frozenset({
    "browser_open", "browser_new_tab", "browser_click", "browser_fill",
    "browser_back", "browser_forward", "browser_switch_tab",
    "windows_focus", "windows_arrange", "windows_minimize", "windows_maximize",
    "windows_launch", "windows_click", "windows_fill", "windows_type",
    "windows_hotkey", "screen_click", "screen_double_click", "screen_drag",
    "files_open", "files_move", "files_rename", "files_show_folder",
})

# Especially important on uncertain results: no blind continuation.
NON_RETRYABLE = EFFECTFUL_ACTIONS


@dataclass
class TaskProgress:
    """Temporary action *categories*, not arguments, window titles or user text."""
    total_attempts: int = 0
    verified: int = 0
    last_status: str = "idle"
    actions: list[str] = field(default_factory=list)

    def record(self, action: str, verified: bool) -> None:
        self.total_attempts += 1
        self.verified += int(verified)
        self.last_status = "verified" if verified else "unverified"
        self.actions.append(action)
        if len(self.actions) > 24:
            del self.actions[:-24]


@dataclass(frozen=True)
class LongTaskLimits:
    max_steps: int = 28
    max_effects: int = 12
    max_seconds: int = 240
    checkpoint_every: int = 6
    max_confirmations: int = 10

    def __post_init__(self):
        if not (1 <= self.max_effects <= self.max_steps <= 32):
            raise ValueError("Invalid task step limits")
        if not 10 <= self.max_seconds <= 300:
            raise ValueError("Invalid task time limit")
        if not 1 <= self.checkpoint_every <= self.max_effects:
            raise ValueError("Invalid checkpoint interval")
        if not 1 <= self.max_confirmations <= 20:
            raise ValueError("Invalid confirmation budget")


def requires_stop_for_ambiguity(action: str, status: str) -> bool:
    return action in NON_RETRYABLE and status != "verified"