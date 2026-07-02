"""USAJOBS official API (US federal jobs).

Docs: https://developer.usajobs.gov/
Requires a free API key + the registered email in headers. Disabled unless
the user supplies credentials in config.yaml.
"""

from __future__ import annotations

from typing import Any

from jobpilot.discovery.base import client
from jobpilot.models import Job, Preferences

_API = "https://data.usajobs.gov/api/search"


def fetch(prefs: Preferences, cfg: dict[str, Any]) -> list[Job]:
    email = cfg.get("email", "")
    api_key = cfg.get("api_key", "")
    if not email or not api_key:
        return []

    out: list[Job] = []
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": api_key}
    with client() as http:
        for q in prefs.keywords or [""]:
            try:
                resp = http.get(
                    _API,
                    params={"Keyword": q, "ResultsPerPage": 50},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            for item in items:
                d = item.get("MatchedObjectDescriptor", {})
                pay = (d.get("PositionRemuneration") or [{}])[0]
                smin = _to_int(pay.get("MinimumRange"))
                smax = _to_int(pay.get("MaximumRange"))
                locs = d.get("PositionLocationDisplay", "")
                out.append(
                    Job(
                        source="usajobs",
                        ats="external",
                        company=d.get("OrganizationName", "US Government"),
                        title=d.get("PositionTitle", ""),
                        url=d.get("PositionURI", ""),
                        location=locs,
                        remote="remote" in locs.lower(),
                        description=(d.get("UserArea", {}).get("Details", {}) or {}).get(
                            "JobSummary", ""
                        ),
                        salary_min=smin,
                        salary_max=smax,
                        posted_at=d.get("PublicationStartDate"),
                        external_id=str(d.get("PositionID", "")),
                    )
                )
    return out


def _to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
