"""Bounded, RAM-only task context with *freshly resolved* UI references.

The language model must never be allowed to reuse a DOM/UIA element ID from
an earlier observation. A spoken "それ" may refer to a recent element only
when an exact, unique match exists in the CURRENT screen and document/window.
Raw input text and field values are not stored by this module.
"""
from __future__ import annotations

import time
from pathlib import Path

_MAX_INSTRUCTIONS = 4
_MAX_STEPS = 6
_REFERENCE_TTL = 600.0  # 10 minutes; expiry affects only deictic references
_BROWSER_ACTIONS = frozenset({"browser_click", "browser_fill"})
_WINDOWS_ACTIONS = frozenset({"windows_click", "windows_fill"})


def remember_instruction(session: dict, instruction: str) -> None:
    recent = session.setdefault("recent_instructions", [])
    recent.append(instruction[:500])
    del recent[:-_MAX_INSTRUCTIONS]


def _label_for(action: str, args: dict, observed: dict) -> str:
    if action.startswith("browser_") and args.get("id"):
        elements = (observed.get("browser") or {}).get("elements", [])
        target = next((e for e in elements if e.get("id") == args["id"]), {})
        return str(target.get("label", ""))[:90]
    if action.startswith("windows_") and args.get("id"):
        elements = (observed.get("windows") or {}).get("controls", [])
        target = next((e for e in elements if e.get("id") == args["id"]), {})
        return str(target.get("name", ""))[:90]
    if args.get("title"):
        return str(args["title"])[:90]
    if args.get("path"):
        return Path(args["path"]).name[:90]
    if args.get("folder"):
        return str(args["folder"])[:90]
    if args.get("app"):
        return str(args["app"])[:90]
    if args.get("target"):
        return str(args["target"])[:90]
    if args.get("query"):
        return str(args["query"])[:90]
    return ""


def _norm(text: object) -> str:
    return " ".join(str(text or "").casefold().split())


def _reference_from_observation(action: str, args: dict, observed: dict) -> dict | None:
    source = "browser" if action in _BROWSER_ACTIONS else (
        "windows" if action in _WINDOWS_ACTIONS else None)
    if source is None:
        return None
    snapshot = observed.get(source) or {}
    if not isinstance(snapshot, dict) or not isinstance(args.get("id"), str):
        return None
    scope = snapshot.get("document_ref" if source == "browser" else "window_ref")
    if not scope:
        return None  # Never remember a target whose screen cannot be identified.
    elements = snapshot.get("elements" if source == "browser" else "controls", [])
    item = next((e for e in elements if e.get("id") == args["id"]), None)
    if not item:
        return None
    label = str(item.get("label" if source == "browser" else "name") or "")[:90]
    if not label.strip():
        return None  # Unlabelled targets are not reliable deictic references.
    fields = ("tag", "role", "type", "in_form", "in_dialog",
              "link_key", "external_origin") if source == "browser" else ("type",)
    return {"source": source, "scope": str(scope), "label": label,
            "signature": tuple(str(item.get(f) or "") for f in fields),
            "at": time.monotonic()}


def remember_step(session: dict, action: str, args: dict,
                  observed: dict, confidence: str) -> None:
    """Keep only bounded descriptions, never typed content or old element IDs."""
    target = _label_for(action, args, observed)
    active_title = str((observed.get("windows") or {}).get("active_title", ""))[:90]
    step = {"action": action, "target": target, "app": active_title,
            "confirmation": confidence}
    recent = session.setdefault("recent_steps", [])
    recent.append(step)
    del recent[:-_MAX_STEPS]

    # "unverified" means an effect may have happened, NOT that the observed
    # control was imaginary. Its identity is a useful referent only if it can
    # be uniquely re-found on the *same* screen. Never claim it succeeded.
    if action in _BROWSER_ACTIONS | _WINDOWS_ACTIONS:
        if target and confidence in {"verified", "unverified"}:
            session["last_target"] = {"label": target, "app": active_title,
                                      "source": "browser" if action.startswith("browser_") else "windows",
                                      "confirmation": confidence}
            ref = _reference_from_observation(action, args, observed)
            if ref:
                session["last_reference"] = ref
            else:
                session.pop("last_reference", None)
        else:
            session.pop("last_reference", None)
            session.pop("last_target", None)
    elif confidence == "verified" and target:
        session["last_target"] = {"label": target, "app": active_title,
                                  "source": "browser" if action.startswith("browser_") else "windows"}
        # Another explicit target supersedes the previous element reference.
        session.pop("last_reference", None)


def _resolve_live(ref: dict, browser: dict | None, windows: dict | None) -> tuple[str, dict | None]:
    if time.monotonic() - float(ref.get("at", 0)) > _REFERENCE_TTL:
        return "expired", None
    source = ref.get("source")
    screen = browser if source == "browser" else windows if source == "windows" else None
    if not isinstance(screen, dict):
        return "unavailable", None
    key = "document_ref" if source == "browser" else "window_ref"
    if not screen.get(key) or str(screen[key]) != ref.get("scope"):
        return "changed_screen", None
    items = screen.get("elements" if source == "browser" else "controls", [])
    fields = ("tag", "role", "type", "in_form", "in_dialog",
              "link_key", "external_origin") if source == "browser" else ("type",)
    name_key = "label" if source == "browser" else "name"
    candidates = [item for item in items
                  if isinstance(item, dict) and item.get("id") is not None
                  and _norm(item.get(name_key)) == _norm(ref.get("label"))
                  and tuple(str(item.get(f) or "") for f in fields) == tuple(ref.get("signature", ()))
                  and (source != "browser" or item.get("in_view", True))]
    if len(candidates) > 1:
        return "ambiguous", None
    if not candidates:
        return "not_found", None
    item = candidates[0]
    # IDs come ONLY from a current observation; the old ID is never retained.
    return "matched", {"source": source, "id": item["id"],
                       "label": str(item[name_key])[:90],
                       **({"pos": item.get("pos")} if source == "browser" and item.get("pos") else {})}


def task_context(session: dict, active_title: str, *,
                 browser: dict | None = None, windows: dict | None = None) -> dict:
    last = session.get("last_target")
    last = dict(last) if isinstance(last, dict) else None
    has_live_snapshot = browser is not None or windows is not None
    result = {"last_target": last,
              "recent_steps": list(session.get("recent_steps", [])[-_MAX_STEPS:])}
    if last is None:
        return result
    last["possibly_stale"] = bool(last.get("app") and active_title and
                                   last["app"].casefold() != active_title.casefold())
    ref = session.get("last_reference")
    if not has_live_snapshot or not isinstance(ref, dict):
        if has_live_snapshot:
            last["possibly_stale"] = True  # historical labels aren't clickable IDs
        return result
    status, resolved = _resolve_live(ref, browser, windows)
    last["possibly_stale"] = status != "matched"
    result["reference_status"] = status
    if resolved is not None:
        result["resolved_reference"] = resolved
    return result