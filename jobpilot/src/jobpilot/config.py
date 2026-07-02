"""Configuration loading for JobPilot.

Resolves and parses the three user files (config.yaml, profile.yaml,
answers.yaml). Falls back to the bundled *.example.yaml so the tool runs
out of the box for a dry-run, but real use requires the user's own copies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from jobpilot.models import Preferences

# Where JobPilot keeps its database, logs, and browser profile.
APP_DIR = Path(os.environ.get("JOBPILOT_HOME", Path.home() / ".jobpilot"))
DB_PATH = APP_DIR / "jobpilot.db"
LOG_DIR = APP_DIR / "logs"
BROWSER_PROFILE_DIR = APP_DIR / "browser-profile"

# Project root (the directory holding config.yaml). Search order:
#   1. $JOBPILOT_CONFIG_DIR
#   2. current working directory
#   3. the packaged project directory (for the bundled examples)
_PKG_ROOT = Path(__file__).resolve().parents[2]


def ensure_dirs() -> None:
    for d in (APP_DIR, LOG_DIR, BROWSER_PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("JOBPILOT_CONFIG_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path.cwd())
    dirs.append(_PKG_ROOT)
    return dirs


def _find_file(name: str, example: str) -> Path:
    """Return the first existing `name`, else the first existing `example`."""
    for d in _search_dirs():
        p = d / name
        if p.exists():
            return p
    for d in _search_dirs():
        p = d / example
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Could not find {name} or {example} in {[str(d) for d in _search_dirs()]}"
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_raw_config() -> dict[str, Any]:
    return _load_yaml(_find_file("config.yaml", "config.example.yaml"))


def load_profile() -> dict[str, Any]:
    return _load_yaml(_find_file("profile.yaml", "profile.example.yaml"))


def load_answers() -> dict[str, Any]:
    return _load_yaml(_find_file("answers.yaml", "answers.example.yaml"))


def load_preferences() -> Preferences:
    """Parse config.yaml into a validated Preferences object."""
    c = load_raw_config()
    search = c.get("search", {})
    location = c.get("location", {})
    comp = c.get("compensation", {})
    filters = c.get("filters", {})
    matching = c.get("matching", {})
    apply_cfg = c.get("apply", {})
    discovery = c.get("discovery", {})

    return Preferences(
        keywords=[str(k) for k in search.get("keywords", [])],
        exclude_keywords=[str(k) for k in search.get("exclude_keywords", [])],
        preferred_locations=[str(k) for k in location.get("preferred", [])],
        location_anchor=str(location.get("anchor", "")),
        radius_miles=int(location.get("radius_miles", 0) or 0),
        allow_remote=bool(location.get("allow_remote", True)),
        allow_unspecified=bool(location.get("allow_unspecified", True)),
        min_salary=_opt_int(comp.get("min_salary")),
        target_salary=_opt_int(comp.get("target_salary")),
        currency=str(comp.get("currency", "USD")),
        drop_if_no_salary=bool(comp.get("drop_if_no_salary", False)),
        employment_types=[str(t) for t in filters.get("employment_types", [])],
        max_age_days=int(filters.get("max_age_days", 0) or 0),
        min_score=int(matching.get("min_score", 50)),
        auto_approve_score=int(matching.get("auto_approve_score", 75)),
        auto_submit=bool(apply_cfg.get("auto_submit", False)),
        headless=bool(apply_cfg.get("headless", False)),
        max_per_run=int(apply_cfg.get("max_per_run", 25)),
        delay_between_seconds=int(apply_cfg.get("delay_between_seconds", 20)),
        human_solve_timeout=int(apply_cfg.get("human_solve_timeout", 180)),
        sources=discovery.get("sources", {}),
    )


def _opt_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def using_example_config() -> bool:
    """True if the user hasn't created their own config.yaml yet."""
    for d in _search_dirs():
        if (d / "config.yaml").exists():
            return False
    return True
