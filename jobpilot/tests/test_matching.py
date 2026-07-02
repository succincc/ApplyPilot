from jobpilot.matching import evaluate
from jobpilot.models import Job, Preferences


def prefs(**over):
    base = dict(
        keywords=["python", "backend"],
        exclude_keywords=["clearance"],
        preferred_locations=["New York, NY", "Remote"],
        location_anchor="New York, NY",
        radius_miles=0,
        allow_remote=True,
        allow_unspecified=True,
        min_salary=90000,
        target_salary=130000,
        currency="USD",
        drop_if_no_salary=False,
        employment_types=["full_time"],
        max_age_days=30,
        min_score=55,
        auto_approve_score=75,
        auto_submit=False,
        headless=False,
        max_per_run=25,
        delay_between_seconds=20,
        human_solve_timeout=180,
        sources={},
    )
    base.update(over)
    return Preferences(**base)


def test_strong_match_scores_high():
    job = Job(source="greenhouse", company="Acme", title="Python Backend Engineer",
              url="http://x", location="Remote", remote=True,
              description="We need a python backend engineer.",
              employment_type="full_time", salary_max=140000, posted_at=None)
    score, reasons, passed = evaluate(job, prefs())
    assert passed
    assert score >= 75


def test_excluded_keyword_rejected():
    job = Job(source="x", company="Gov", title="Python Engineer (clearance required)",
              url="http://x", description="clearance needed", employment_type="full_time",
              location="Remote", remote=True)
    score, reasons, passed = evaluate(job, prefs())
    assert not passed
    assert score == 0


def test_salary_floor_rejects_low_pay():
    job = Job(source="x", company="Acme", title="Python Backend", url="http://x",
              location="Remote", remote=True, employment_type="full_time",
              salary_min=50000, salary_max=60000)
    score, reasons, passed = evaluate(job, prefs())
    assert not passed
    assert any("salary" in r for r in reasons)


def test_location_not_in_list_rejected_when_not_remote():
    job = Job(source="x", company="Acme", title="Python Backend", url="http://x",
              location="Boise, ID", remote=False, employment_type="full_time",
              salary_max=120000)
    score, reasons, passed = evaluate(job, prefs(allow_unspecified=False))
    assert not passed


def test_wrong_employment_type_rejected():
    job = Job(source="x", company="Acme", title="Python Backend", url="http://x",
              location="Remote", remote=True, employment_type="internship",
              salary_max=120000)
    score, reasons, passed = evaluate(job, prefs())
    assert not passed


def test_no_salary_kept_when_allowed():
    job = Job(source="x", company="Acme", title="Python Backend Engineer", url="http://x",
              location="Remote", remote=True, employment_type="full_time")
    score, reasons, passed = evaluate(job, prefs())
    assert passed
