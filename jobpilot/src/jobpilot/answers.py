"""Deterministic answer engine for application questions.

No AI. Given a question string, walk the user's rule list (answers.yaml) and
return the first rule whose `match_any` substrings appear in the question.
Placeholders in the answer are filled from the profile and the job.

Returns an `Answer` with a `.needs_review` flag when nothing matched, so the
apply runner can leave the field blank and flag the job instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from jobpilot.models import Job

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass
class Answer:
    text: str
    kind: str = "text"          # "text" or "choice"
    matched_rule: Optional[str] = None
    needs_review: bool = False


class AnswerEngine:
    def __init__(self, answers_cfg: dict[str, Any], profile: dict[str, Any]) -> None:
        self.rules: list[dict[str, Any]] = answers_cfg.get("rules", []) or []
        self.default: str = str(answers_cfg.get("default", "") or "")
        self.profile = profile

    def _context(self, job: Optional[Job]) -> dict[str, str]:
        p = self.profile
        personal = p.get("personal", {})
        links = p.get("links", {})
        exp = p.get("experience", {})
        # Prefer job-specific salary target if present in profile compensation.
        salary = str(p.get("compensation", {}).get("desired_salary", "")) or "negotiable"
        ctx = {
            "company": job.company if job else "",
            "role": job.title if job else "",
            "location": (job.location if job else "") or "",
            "first_name": personal.get("first_name", ""),
            "last_name": personal.get("last_name", ""),
            "full_name": personal.get("full_name", ""),
            "email": personal.get("email", ""),
            "phone": personal.get("phone", ""),
            "years_experience": str(exp.get("years_experience", "")),
            "current_title": exp.get("current_title", ""),
            "current_company": exp.get("current_company", ""),
            "desired_title": exp.get("desired_title", ""),
            "salary": salary,
            "linkedin": links.get("linkedin", ""),
            "github": links.get("github", ""),
            "portfolio": links.get("portfolio", ""),
        }
        return ctx

    def _fill(self, template: str, ctx: dict[str, str]) -> str:
        def repl(m: re.Match) -> str:
            return ctx.get(m.group(1), m.group(0))

        # Collapse the YAML block-scalar newlines/indentation into clean prose.
        text = _PLACEHOLDER.sub(repl, template)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def answer(self, question: str, job: Optional[Job] = None) -> Answer:
        q = (question or "").lower()
        ctx = self._context(job)
        for rule in self.rules:
            needles = [str(n).lower() for n in rule.get("match_any", [])]
            if any(n in q for n in needles):
                kind = rule.get("type", "text")
                text = self._fill(str(rule.get("answer", "")), ctx)
                return Answer(text=text, kind=kind, matched_rule=needles[0] if needles else None)
        # Nothing matched.
        if self.default:
            return Answer(text=self._fill(self.default, ctx), needs_review=True)
        return Answer(text="", needs_review=True)


def build_engine() -> AnswerEngine:
    """Convenience factory that loads config from disk."""
    from jobpilot.config import load_answers, load_profile
    return AnswerEngine(load_answers(), load_profile())
