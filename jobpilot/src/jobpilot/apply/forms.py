"""Generic, no-AI form-filling engine.

Given a Playwright page (or a scoped container), find inputs, work out the
question each one is asking (its label / placeholder / aria-label / name),
and fill it from the profile or the deterministic answer engine.

This is intentionally heuristic and conservative: when it isn't confident, it
leaves the field blank and records it under `flagged` so the job is routed to
manual review instead of being submitted with a wrong answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jobpilot.answers import AnswerEngine
from jobpilot.models import Job

log = logging.getLogger(__name__)

# JS that returns the best human-readable label for a form control.
_LABEL_JS = """
(el) => {
  const byAria = el.getAttribute('aria-label');
  if (byAria) return byAria;
  if (el.id) {
    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lab && lab.innerText.trim()) return lab.innerText;
  }
  let p = el.closest('label');
  if (p && p.innerText.trim()) return p.innerText;
  // Nearest preceding label-ish text within the same field wrapper.
  let wrap = el.closest('div,fieldset,li,p');
  if (wrap) {
    const lab = wrap.querySelector('label,legend');
    if (lab && lab.innerText.trim()) return lab.innerText;
  }
  return el.getAttribute('placeholder') || el.getAttribute('name') || '';
}
"""

# (needles, profile lookup) — first matching needle in the label wins.
_DIRECT_FIELDS: list[tuple[list[str], list[str]]] = [
    (["first name", "given name"], ["personal", "first_name"]),
    (["last name", "family name", "surname"], ["personal", "last_name"]),
    (["full name", "your name", "name"], ["personal", "full_name"]),
    (["email"], ["personal", "email"]),
    (["phone", "mobile", "telephone"], ["personal", "phone"]),
    (["address", "street"], ["personal", "address_line1"]),
    (["city", "town"], ["personal", "city"]),
    (["state", "province", "region"], ["personal", "state"]),
    (["zip", "postal"], ["personal", "postal_code"]),
    (["country"], ["personal", "country"]),
    (["linkedin"], ["links", "linkedin"]),
    (["github"], ["links", "github"]),
    (["portfolio", "personal website", "website"], ["links", "portfolio"]),
    (["current company", "employer"], ["experience", "current_company"]),
    (["current title", "job title", "current role"], ["experience", "current_title"]),
]

# EEO / authorization selects handled as choices.
_CHOICE_FIELDS: list[tuple[list[str], list[str]]] = [
    (["authorized to work", "legally authorized", "work authorization"],
     ["work_authorization", "authorized_to_work"]),
    (["sponsorship", "require sponsor", "visa"], ["work_authorization", "requires_sponsorship"]),
    (["gender"], ["eeo", "gender"]),
    (["race", "ethnicity"], ["eeo", "race_ethnicity"]),
    (["veteran"], ["eeo", "veteran_status"]),
    (["disability"], ["eeo", "disability_status"]),
    (["hispanic", "latino"], ["eeo", "hispanic_latino"]),
]


@dataclass
class FillReport:
    filled: int = 0
    uploaded: int = 0
    answered: int = 0
    flagged: list[str] = field(default_factory=list)  # questions we couldn't answer

    @property
    def needs_review(self) -> bool:
        return bool(self.flagged)


def _dig(profile: dict[str, Any], path: list[str]) -> str:
    node: Any = profile
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return "" if node is None else str(node)


def _first_needle(label: str, table) -> list[str] | None:
    low = label.lower()
    for needles, path in table:
        if any(n in low for n in needles):
            return path
    return None


class FormFiller:
    def __init__(self, profile: dict[str, Any], engine: AnswerEngine, job: Job) -> None:
        self.profile = profile
        self.engine = engine
        self.job = job

    def _label(self, handle) -> str:
        try:
            return (handle.evaluate(_LABEL_JS) or "").strip()
        except Exception:
            return ""

    def fill(self, page) -> FillReport:
        report = FillReport()
        self._fill_files(page, report)
        self._fill_text_inputs(page, report)
        self._fill_selects(page, report)
        return report

    # -- file uploads --------------------------------------------------------
    def _fill_files(self, page, report: FillReport) -> None:
        docs = self.profile.get("documents", {})
        resume = docs.get("resume", "")
        cover = docs.get("cover_letter", "")
        try:
            file_inputs = page.locator("input[type='file']")
            count = file_inputs.count()
        except Exception:
            return
        for i in range(count):
            el = file_inputs.nth(i)
            label = self._label(el.element_handle()) if el.element_handle() else ""
            path = cover if ("cover" in label.lower() and cover) else resume
            if not path:
                continue
            try:
                el.set_input_files(path)
                report.uploaded += 1
            except Exception as exc:
                log.debug("file upload failed for '%s': %s", label, exc)

    # -- text / email / tel / textarea --------------------------------------
    def _fill_text_inputs(self, page, report: FillReport) -> None:
        selector = (
            "input[type='text'], input[type='email'], input[type='tel'], "
            "input[type='url'], input:not([type]), textarea"
        )
        try:
            inputs = page.locator(selector)
            count = inputs.count()
        except Exception:
            return
        for i in range(count):
            el = inputs.nth(i)
            try:
                if not el.is_visible() or not el.is_editable():
                    continue
                if (el.input_value() or "").strip():
                    continue  # already filled
            except Exception:
                continue
            handle = el.element_handle()
            label = self._label(handle) if handle else ""
            if not label:
                continue

            path = _first_needle(label, _DIRECT_FIELDS)
            if path:
                value = _dig(self.profile, path)
                if value:
                    self._type(el, value, report)
                continue

            # Not a known profile field — treat as a free-text question.
            ans = self.engine.answer(label, self.job)
            if ans.needs_review or not ans.text:
                report.flagged.append(label[:120])
            else:
                self._type(el, ans.text, report)
                report.answered += 1

    def _type(self, el, value: str, report: FillReport) -> None:
        try:
            el.fill(value)
            report.filled += 1
        except Exception as exc:
            log.debug("fill failed: %s", exc)

    # -- selects / dropdowns -------------------------------------------------
    def _fill_selects(self, page, report: FillReport) -> None:
        try:
            selects = page.locator("select")
            count = selects.count()
        except Exception:
            return
        for i in range(count):
            el = selects.nth(i)
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            handle = el.element_handle()
            label = self._label(handle) if handle else ""
            if not label:
                continue

            path = _first_needle(label, _CHOICE_FIELDS)
            desired = _dig(self.profile, path) if path else ""
            if not desired:
                ans = self.engine.answer(label, self.job)
                if ans.kind == "choice" and ans.text:
                    desired = ans.text
            if not desired:
                continue
            self._select_best(el, desired, report, label)

    def _select_best(self, el, desired: str, report: FillReport, label: str) -> None:
        """Pick the option whose text best matches `desired`."""
        try:
            options = el.locator("option")
            n = options.count()
        except Exception:
            return
        desired_low = desired.lower()
        best_idx = None
        for i in range(n):
            txt = (options.nth(i).inner_text() or "").strip().lower()
            if not txt:
                continue
            if txt == desired_low or desired_low in txt or txt in desired_low:
                best_idx = i
                break
        if best_idx is None:
            report.flagged.append(f"{label[:80]} (no matching option for '{desired}')")
            return
        try:
            el.select_option(index=best_idx)
            report.answered += 1
        except Exception as exc:
            log.debug("select failed for '%s': %s", label, exc)
