import base64
from unittest.mock import MagicMock

from src.integrations.gmail_verification import (
    GmailVerificationClient,
    classify_employer_email,
    match_employer_email_to_jobs,
)


def _encoded(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _message(message_id, subject, body, sender="recruiting@acme.example"):
    return {
        "id": message_id,
        "internalDate": "1798891200000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "body": {"data": _encoded(body)},
        },
    }


def test_rejection_and_positive_semantics_have_distinct_results():
    assert classify_employer_email("Application update", "We will not be moving forward with your application") == ("REJECTED", 0.94)
    assert classify_employer_email("Interview request", "We want to speak with you about next steps") == ("INTERVIEW_REQUESTED", 0.95)
    assert classify_employer_email("Update", "We have not rejected your application") == ("", 0.0)


def test_unrelated_email_is_ignored_and_ambiguous_match_fails_closed():
    client = GmailVerificationClient("unused", "unused")
    relevant = client._message_to_relevant(
        _message("m1", "Application update", "We will not be moving forward with your application"),
    )
    assert match_employer_email_to_jobs(relevant, [{"job_key": "a", "company_name": "Other", "job_title": "Role"}]).job_key is None
    jobs = [
        {"job_key": "a", "company_name": "Acme", "job_title": "Platform Engineer", "external_url": "https://jobs.acme.example/a", "submitted_at": "2026-09-01T00:00:00+00:00"},
        {"job_key": "b", "company_name": "Acme", "job_title": "Platform Engineer", "external_url": "https://jobs.acme.example/b", "submitted_at": "2026-09-01T00:00:00+00:00"},
    ]
    ambiguous = client._message_to_relevant(
        _message("m2", "Acme application update", "We will not be moving forward with your Platform Engineer application"),
        company="Acme",
        job_title="Platform Engineer",
    )
    assert match_employer_email_to_jobs(ambiguous, jobs).ambiguous is True


def test_status_search_is_bounded_to_recent_messages_and_no_body_persistence():
    client = GmailVerificationClient("unused", "unused")
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}]
    }
    messages = {
        "m1": _message("m1", "Acme interview request", "We want to speak with you"),
        "m2": _message("m2", "Newsletter", "We will not be moving forward with unrelated content"),
    }
    service.users.return_value.messages.return_value.get.side_effect = lambda **kwargs: MagicMock(execute=lambda: messages[kwargs["id"]])
    client.service = service
    result = client.search_status_messages(since_epoch=1798890000)
    assert [item.message_id for item in result] == ["m1", "m2"]
    assert all(not hasattr(item, "safe_body") for item in result)
