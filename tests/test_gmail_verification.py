import base64
from unittest.mock import MagicMock

from src.integrations.gmail_verification import GmailVerificationClient


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _message(
    message_id: str,
    *,
    received_ms: int,
    subject: str,
    body: str,
    sender: str = "notifications@example.invalid",
) -> dict:
    return {
        "id": message_id,
        "internalDate": str(received_ms),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": _encoded(body)},
        },
    }


def _client_with_messages(messages: list[dict]) -> GmailVerificationClient:
    client = GmailVerificationClient("unused.json", "unused-token.json")
    service = MagicMock()
    messages_api = service.users.return_value.messages.return_value
    messages_api.list.return_value.execute.return_value = {
        "messages": [{"id": message["id"]} for message in messages]
    }
    by_id = {message["id"]: message for message in messages}
    messages_api.get.side_effect = lambda **kwargs: MagicMock(execute=lambda: by_id[kwargs["id"]])
    client.service = service
    return client


def test_classifies_otp_activation_and_receipt_without_exposing_values():
    client = GmailVerificationClient("unused.json", "unused-token.json")
    otp = client._message_to_relevant(
        _message(
            "otp",
            received_ms=2_000_000,
            subject="Acme verification code",
            body="Your verification code is 123456",
        ),
        company="Acme",
    )
    activation = client._message_to_relevant(
        _message(
            "activation",
            received_ms=2_000_000,
            subject="Activate your Acme account",
            body="Confirm account: https://jobs.example.invalid/activate?token=secret",
        ),
        company="Acme",
    )
    receipt = client._message_to_relevant(
        _message(
            "receipt",
            received_ms=2_000_000,
            subject="Acme application received",
            body="Thank you for applying for Support Technician.",
        ),
        company="Acme",
        job_title="Support Technician",
    )

    assert otp.message_kind == "otp_code"
    assert activation.message_kind == "account_activation"
    assert receipt.message_kind == "application_receipt"


def test_search_rejects_stale_and_wrong_company_messages():
    since_epoch = 2_000.0
    client = _client_with_messages(
        [
            _message(
                "stale",
                received_ms=1_999_000,
                subject="Acme verification code",
                body="Verification code 111111",
            ),
            _message(
                "wrong-company",
                received_ms=2_001_000,
                subject="Other Corp verification code",
                body="Verification code 222222",
            ),
            _message(
                "matching",
                received_ms=2_002_000,
                subject="Acme verification code",
                body="Verification code 333333",
            ),
        ]
    )

    results = client.search_relevant(since_epoch=since_epoch, company="Acme")

    assert [message.message_id for message in results] == ["matching"]


def test_unrelated_receipt_cannot_confirm_submission():
    client = _client_with_messages(
        [
            _message(
                "other-receipt",
                received_ms=2_001_000,
                subject="Other Corp application received",
                body="Thank you for applying.",
            )
        ]
    )

    assert client.search_relevant(since_epoch=2_000.0, company="Acme") == []


def test_activation_selection_ignores_security_and_unrelated_messages():
    client = GmailVerificationClient("unused.json", "unused-token.json")
    activation = client._message_to_relevant(
        _message(
            "activation",
            received_ms=2_001_000,
            subject="Activate your Acme account",
            body="Confirm account: https://jobs.example.invalid/activate?token=secret",
            sender="no-reply@jobs.example.invalid",
        ),
        company="Acme",
    )
    otp = client._message_to_relevant(
        _message(
            "otp",
            received_ms=2_002_000,
            subject="Acme verification code",
            body="Your verification code is 123456",
            sender="no-reply@jobs.example.invalid",
        ),
        company="Acme",
    )
    selected = client._select_activation([activation, otp], expected_host="jobs.example.invalid")
    assert selected.message_id == "activation"
    assert client.activation_url(
        activation.verification_links[0], expected_host="jobs.example.invalid"
    )
    assert (
        client.activation_url(
            "https://unrelated.example.invalid/activate?token=secret",
            expected_host="jobs.example.invalid",
        )
        is None
    )


def test_activation_selection_ambiguous_fails_closed():
    client = GmailVerificationClient("unused.json", "unused-token.json")
    messages = [
        client._message_to_relevant(
            _message(
                item,
                received_ms=2_001_000,
                subject="Activate your Acme account",
                body="Confirm account: https://jobs.example.invalid/activate?token=secret",
                sender="no-reply@jobs.example.invalid",
            ),
            company="Acme",
        )
        for item in ("one", "two")
    ]
    import pytest
    from src.integrations.gmail_verification import GmailVerificationAmbiguous

    with pytest.raises(GmailVerificationAmbiguous):
        client._select_activation(messages, expected_host="jobs.example.invalid")


def test_application_verification_selects_correlated_otp_without_using_magic_link():
    client = GmailVerificationClient("unused.json", "unused-token.json")
    otp = client._message_to_relevant(
        _message(
            "otp",
            received_ms=2_001_000,
            subject="Acme verification code",
            body="Your verification code is 123456",
            sender="no-reply@jobs.example.invalid",
        ),
        company="Acme",
        job_title="Support Technician",
    )
    selected = client._select_application_verification(
        [otp], expected_host="jobs.example.invalid"
    )
    assert selected.message_id == "otp"
    assert selected.codes == ["123456"]


def test_application_verification_rejects_equal_candidates_as_ambiguous():
    client = GmailVerificationClient("unused.json", "unused-token.json")
    messages = [
        client._message_to_relevant(
            _message(
                item,
                received_ms=2_001_000,
                subject="Acme verification code",
                body="Your verification code is 123456",
                sender="no-reply@jobs.example.invalid",
            ),
            company="Acme",
            job_title="Support Technician",
        )
        for item in ("one", "two")
    ]
    import pytest
    from src.integrations.gmail_verification import GmailVerificationAmbiguous

    with pytest.raises(GmailVerificationAmbiguous):
        client._select_application_verification(messages, expected_host="jobs.example.invalid")
