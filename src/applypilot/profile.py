"""Profile normalization: make any profile.json safe to put on a real form.

profile.json reaches the engine from three places — the setup wizard, the
control panel, and hand editing — and each produces slightly different shapes.
Small differences matter here because this data is typed onto legal
application forms:

  * The wizard asks work authorization with a yes/no confirm, which lands in
    JSON as `true`/`false`. Rendered straight into the apply prompt that
    becomes "Work Auth: True", and an agent can type "True" into "Are you
    legally authorized to work in the United States?" That is the single most
    important question on an application.
  * Key names drifted between the wizard, the example file, and the readers:
    current_title vs current_job_title, programming_languages vs languages.
    A mismatch silently blanks the field rather than erroring.
  * Missing sections cause KeyError deep inside prompt building, long after
    the run started.

normalize() is applied on every load so the rest of the engine can assume one
canonical shape. It never invents facts — it only renames, coerces types, and
fills structural gaps with explicitly safe defaults.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Canonical section -> {canonical_key: (alias, alias, ...)}
KEY_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "personal": {
        "full_name": ("name", "legal_name"),
        "preferred_name": ("nickname", "preferred"),
        "email": ("email_address", "contact_email"),
        "phone": ("phone_number", "telephone"),
    },
    "experience": {
        "current_job_title": ("current_title", "job_title", "title"),
        "years_of_experience_total": ("years_of_experience", "years_experience"),
        "current_company": ("company", "employer"),
    },
    "skills_boundary": {
        "languages": ("programming_languages", "langs"),
        "tools": ("tooling",),
    },
}

# Fields that must read as "Yes"/"No" on a form, never True/False.
YES_NO_FIELDS: dict[str, tuple[str, ...]] = {
    "work_authorization": ("legally_authorized_to_work", "require_sponsorship"),
    "availability": ("available_for_full_time", "available_for_contract",
                     "willing_to_relocate"),
}

# Structural defaults. Chosen to be safe if a user never sets them: decline
# EEO questions, assume no sponsorship need, available immediately.
SECTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "personal": {
        "full_name": "", "preferred_name": "", "email": "", "phone": "",
        "address": "", "city": "", "province_state": "", "country": "",
        "postal_code": "", "linkedin_url": "", "github_url": "",
        "portfolio_url": "", "website_url": "", "password": "",
    },
    "work_authorization": {
        "legally_authorized_to_work": "Yes",
        "require_sponsorship": "No",
        "work_permit_type": "",
    },
    "availability": {
        "earliest_start_date": "Immediately",
        "available_for_full_time": "Yes",
        "available_for_contract": "No",
    },
    "compensation": {
        "salary_expectation": "", "salary_currency": "USD",
        "salary_range_min": "", "salary_range_max": "",
        "currency_conversion_note": "",
    },
    "experience": {
        "years_of_experience_total": "", "education_level": "",
        "current_job_title": "", "current_company": "", "target_role": "",
    },
    "skills_boundary": {
        "languages": [], "frameworks": [], "devops": [],
        "databases": [], "tools": [],
    },
    "resume_facts": {
        "preserved_companies": [], "preserved_projects": [],
        "preserved_school": "", "real_metrics": [],
    },
    "eeo_voluntary": {
        "gender": "Decline to self-identify",
        "race_ethnicity": "Decline to self-identify",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "I do not wish to answer",
    },
}

_TRUTHY = {"true", "yes", "y", "1", "authorized", "citizen"}
_FALSY = {"false", "no", "n", "0", "none", "not authorized"}


def to_yes_no(value: Any, default: str = "") -> str:
    """Coerce a value into the literal string "Yes" or "No".

    Application forms never accept a Python boolean. Anything unrecognized is
    passed through unchanged so real free-text answers (e.g. "Green Card")
    are not destroyed.
    """
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or value == "":
        return default
    text = str(value).strip()
    lowered = text.lower()
    if lowered in _TRUTHY:
        return "Yes"
    if lowered in _FALSY:
        return "No"
    return text


def _apply_aliases(section: dict, aliases: dict[str, tuple[str, ...]]) -> list[str]:
    """Rename known alias keys to their canonical names. Returns what moved."""
    moved = []
    for canonical, alts in aliases.items():
        if section.get(canonical):
            continue
        for alt in alts:
            if section.get(alt):
                section[canonical] = section[alt]
                moved.append(f"{alt} -> {canonical}")
                break
    return moved


def normalize(profile: dict) -> tuple[dict, list[str]]:
    """Return (canonical_profile, notes). Never raises on a malformed profile."""
    if not isinstance(profile, dict):
        return ({k: dict(v) for k, v in SECTION_DEFAULTS.items()},
                ["profile.json was not an object; using defaults"])

    result: dict[str, Any] = {}
    notes: list[str] = []

    for section, defaults in SECTION_DEFAULTS.items():
        current = profile.get(section)
        if not isinstance(current, dict):
            if current is not None:
                notes.append(f"section '{section}' was not an object; replaced with defaults")
            current = {}
        merged = dict(defaults)
        merged.update({k: v for k, v in current.items() if v is not None})
        result[section] = merged

    # Alias renames (current_title -> current_job_title, etc.)
    for section, aliases in KEY_ALIASES.items():
        moved = _apply_aliases(result[section], aliases)
        # Aliases can also appear at the top level of a flat profile
        top_moved = _apply_aliases_from_top(profile, result[section], aliases)
        notes.extend(f"{section}: {m}" for m in moved + top_moved)

    # Booleans -> Yes/No on everything a form will ask
    for section, fields in YES_NO_FIELDS.items():
        for field in fields:
            before = result[section].get(field)
            after = to_yes_no(before, default=SECTION_DEFAULTS[section].get(field, ""))
            if before != after:
                notes.append(f"{section}.{field}: {before!r} -> {after!r}")
            result[section][field] = after

    # Skills must be lists; a comma string is a common hand-edit
    for field, value in list(result["skills_boundary"].items()):
        if isinstance(value, str):
            result["skills_boundary"][field] = [
                s.strip() for s in value.split(",") if s.strip()]
            notes.append(f"skills_boundary.{field}: string split into a list")
        elif not isinstance(value, list):
            result["skills_boundary"][field] = []

    for field in ("preserved_companies", "preserved_projects", "real_metrics"):
        value = result["resume_facts"].get(field)
        if isinstance(value, str):
            result["resume_facts"][field] = [
                s.strip() for s in value.split(",") if s.strip()]
        elif not isinstance(value, list):
            result["resume_facts"][field] = []

    # Salary fields must be digit strings for the apply prompt's arithmetic
    comp = result["compensation"]
    for field in ("salary_expectation", "salary_range_min", "salary_range_max"):
        raw = str(comp.get(field) or "").strip()
        digits = "".join(c for c in raw if c.isdigit())
        if raw and digits != raw:
            comp[field] = digits
            notes.append(f"compensation.{field}: {raw!r} -> {digits!r}")

    # A range typed into one box ("60000-85000") splits across min/max
    expectation = str(comp.get("salary_expectation") or "")
    if not expectation and comp.get("salary_range_min"):
        comp["salary_expectation"] = comp["salary_range_min"]
        notes.append("compensation.salary_expectation filled from salary_range_min")

    # Preserve any sections we don't know about rather than dropping them
    for key, value in profile.items():
        if key not in result:
            result[key] = value

    return result, notes


def _apply_aliases_from_top(profile: dict, section: dict,
                            aliases: dict[str, tuple[str, ...]]) -> list[str]:
    """Pull canonical values from a flat top-level profile shape."""
    moved = []
    for canonical, alts in aliases.items():
        if section.get(canonical):
            continue
        for candidate in (canonical, *alts):
            value = profile.get(candidate)
            if value not in (None, "", [], {}):
                section[canonical] = value
                moved.append(f"top-level {candidate} -> {canonical}")
                break
    return moved


def validate(profile: dict) -> list[str]:
    """Report what would make a real application fail or look wrong.

    Returns human-readable problems, most important first. Empty means the
    profile is safe to submit with.
    """
    problems: list[str] = []
    personal = profile.get("personal") or {}
    comp = profile.get("compensation") or {}
    exp = profile.get("experience") or {}
    auth = profile.get("work_authorization") or {}

    if not str(personal.get("full_name") or "").strip():
        problems.append("No full name — every application form requires it.")
    email = str(personal.get("email") or "").strip()
    if not email or "@" not in email:
        problems.append("No valid email — you would never receive a reply.")
    if "example.com" in email.lower():
        problems.append("Email is still a placeholder (example.com).")
    if not str(personal.get("phone") or "").strip():
        problems.append("No phone number — most forms require one.")
    if not str(personal.get("city") or "").strip():
        problems.append("No city — location questions cannot be answered.")

    for field in ("legally_authorized_to_work", "require_sponsorship"):
        value = str(auth.get(field) or "")
        if value in ("True", "False", "true", "false"):
            problems.append(
                f"work_authorization.{field} is the boolean {value!r} rather than "
                f"Yes/No. This is a legal question on every application."
            )

    if not str(comp.get("salary_expectation") or "").strip():
        problems.append("No salary expectation — many forms make this required.")
    if not str(exp.get("target_role") or "").strip():
        problems.append("No target role — scoring and tailoring depend on it.")

    return problems
