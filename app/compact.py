"""Reduce language-model input without removing underlying UI capabilities.

The full DOM/UIA observation is retained for action validation. Only the
serialized context presented to the language model is reduced, prioritizing
visible controls and text that overlaps the spoken instruction.
"""
from __future__ import annotations


def _score(item: dict, spoken_pairs: set[str]) -> int:
    label = str(item.get("label") or item.get("name") or "").casefold()
    pairs = {label[i:i + 2] for i in range(max(0, len(label) - 1))}
    overlap = len(pairs & spoken_pairs)
    return 4 * overlap + 2 * bool(item.get("in_view")) + bool(label)


def _shortlist(items: list[dict], utterance: str, limit: int,
               must_include: set[str] | None = None) -> list[dict]:
    if len(items) <= limit:
        return items
    spoken = utterance.casefold()
    pairs = {spoken[i:i + 2] for i in range(max(0, len(spoken) - 1))}
    # Keep a balanced set of initially visible candidates and the best
    # instruction-related candidates, preserving source order for readability.
    baseline = max(12, limit // 3)
    kept = set(range(baseline))
    # The fresh deictic target must not disappear from a long DOM/UIA list.
    # This ID comes from the *current* screen, never from history.
    for idx, item in enumerate(items):
        if str(item.get("id")) in (must_include or set()):
            kept.add(idx)
    ranked = sorted(range(len(items)), key=lambda i: (-_score(items[i], pairs), i))
    for idx in ranked:
        kept.add(idx)
        if len(kept) >= limit:
            break
    return [item for idx, item in enumerate(items) if idx in kept]


def compact_for_model(context: dict, instruction: str, *, cloud: bool = False) -> dict:
    """Keep full local safety evidence, but transmit only task-relevant cloud context.

    Consent for the current screen/instruction isn't consent to send previous
    commands, unrelated file paths, or background window titles.
    """
    compact = dict(context)
    reference = (context.get("task_memory") or {}).get("resolved_reference") or {}
    if cloud:
        compact.pop("recent_instructions", None)
        compact.pop("recent_files", None)
        # Historical target details can reveal earlier private documents.
        # Only a freshly re-resolved reference remains available.
        compact["task_memory"] = (
            {"resolved_reference": dict(reference)} if isinstance(reference, dict) and reference else {}
        )
    browser = context.get("browser")
    if isinstance(browser, dict):
        compact["browser"] = dict(browser)
        compact["browser"].pop("document_ref", None)  # Keep page fingerprints away from models.
        required = {str(reference["id"])} if reference.get("source") == "browser" else set()
        compact["browser"]["elements"] = _shortlist(browser.get("elements", []), instruction, 70, required)
        if cloud:
            # A generic request to switch tabs is NOT consent to disclose
            # titles of every other tab (e.g., private email subjects).
            # The user may still name the desired tab in the instruction.
            compact["browser"].pop("tabs", None)
    windows = context.get("windows")
    if isinstance(windows, dict):
        compact["windows"] = dict(windows)
        compact["windows"].pop("window_ref", None)
        required = {str(reference["id"])} if reference.get("source") == "windows" else set()
        compact["windows"]["controls"] = _shortlist(windows.get("controls", []), instruction, 45, required)
        if cloud:
            # Background app titles may contain document names/credentials.
            # Never enumerate them in a cloud prompt, including on switches.
            compact["windows"].pop("windows", None)
    return compact