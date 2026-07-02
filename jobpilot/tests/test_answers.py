from jobpilot.answers import AnswerEngine
from jobpilot.models import Job

PROFILE = {
    "personal": {"first_name": "Jane", "last_name": "Doe", "full_name": "Jane Doe",
                 "email": "jane@example.com", "phone": "555"},
    "links": {"linkedin": "https://linkedin.com/in/jane", "github": "https://github.com/jane"},
    "experience": {"years_experience": 4, "current_title": "Software Engineer",
                   "current_company": "Acme", "desired_title": "Engineer"},
    "compensation": {"desired_salary": "120000"},
}

ANSWERS = {
    "default": "",
    "rules": [
        {"match_any": ["desired salary", "salary expectation"], "answer": "{salary}"},
        {"match_any": ["authorized to work"], "type": "choice", "answer": "Yes"},
        {"match_any": ["why do you want", "why are you interested"],
         "answer": "I want to work at {company} as a {role} because I have {years_experience} years."},
    ],
}

JOB = Job(source="greenhouse", company="Stripe", title="Backend Engineer", url="http://x")


def engine():
    return AnswerEngine(ANSWERS, PROFILE)


def test_salary_placeholder_filled():
    a = engine().answer("What is your desired salary?", JOB)
    assert a.text == "120000"


def test_choice_authorization():
    a = engine().answer("Are you legally authorized to work in the US?", JOB)
    assert a.kind == "choice"
    assert a.text == "Yes"


def test_why_question_fills_company_and_role():
    a = engine().answer("Why do you want to work here?", JOB)
    assert "Stripe" in a.text
    assert "Backend Engineer" in a.text
    assert "4 years" in a.text


def test_unmatched_question_flagged():
    a = engine().answer("Describe a time you resolved a conflict.", JOB)
    assert a.needs_review is True
    assert a.text == ""


def test_first_matching_rule_wins():
    # "salary expectation" should hit the salary rule, not fall through.
    a = engine().answer("Please state your salary expectation.", JOB)
    assert a.text == "120000"
