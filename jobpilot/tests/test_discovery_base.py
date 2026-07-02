from jobpilot.discovery.base import guess_remote, parse_salary


def test_parse_salary_range_with_k():
    lo, hi = parse_salary("Compensation: $120K - $150K per year")
    assert lo == 120000
    assert hi == 150000


def test_parse_salary_single_full_number():
    lo, hi = parse_salary("Salary is 130,000 USD annually")
    assert lo == 130000
    assert hi == 130000


def test_parse_salary_ignores_tiny_numbers():
    lo, hi = parse_salary("Apply within 30 days, ref 12")
    assert lo is None and hi is None


def test_guess_remote():
    assert guess_remote("Remote - US") is True
    assert guess_remote("New York, NY") is False
    assert guess_remote("", "You can work from home") is True


def test_parse_salary_ignores_retirement_plans():
    assert parse_salary("Benefits include a 401k plan") == (None, None)
    assert parse_salary("401(k) matching and health insurance") == (None, None)
    assert parse_salary("403(b) available") == (None, None)
    # Real salary next to a 401k mention still parses.
    lo, hi = parse_salary("Salary $95,000 - $120,000 plus 401k")
    assert (lo, hi) == (95000, 120000)
