from jobpilot.apply.forms import _first_needle, _DIRECT_FIELDS


def test_ambiguous_name_labels_not_claimed():
    for label in ["Company Name", "School Name", "Name of referrer",
                  "University name", "Hiring manager name", "Middle name"]:
        assert _first_needle(label, _DIRECT_FIELDS) is None, label


def test_real_name_labels_still_routed():
    assert _first_needle("Your Name", _DIRECT_FIELDS) == ["personal", "full_name"]
    assert _first_needle("Full name *", _DIRECT_FIELDS) == ["personal", "full_name"]
    assert _first_needle("Preferred first name", _DIRECT_FIELDS) == ["personal", "first_name"]


def test_estate_and_statement_not_state():
    assert _first_needle("Real Estate License Number", _DIRECT_FIELDS) is None
    assert _first_needle("Personal statement", _DIRECT_FIELDS) is None
    assert _first_needle("State/Region", _DIRECT_FIELDS) == ["personal", "state"]


def test_current_company_routed_before_generic_name():
    assert _first_needle("Current company", _DIRECT_FIELDS) == ["experience", "current_company"]
    assert _first_needle("Email Address", _DIRECT_FIELDS) == ["personal", "email"]
