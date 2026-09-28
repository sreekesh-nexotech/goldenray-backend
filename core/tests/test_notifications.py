"""core.notifications: validated immediately, delivered after commit, synchronous fallback, Celery retry policy."""

from smtplib import SMTPException
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import transaction

from core import notifications, tasks

pytestmark = pytest.mark.django_db


def test_queued_mail_is_sent_only_after_commit(django_capture_on_commit_callbacks, settings):
    settings.DEFAULT_FROM_EMAIL = "Flarize <no-reply@flarize.com>"
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        notifications.queue_email(to="a@example.com", subject="Hello", text="Body", html="<p>Body</p>", category="tests")
        assert mail.outbox == []
    assert len(callbacks) == 1
    message = mail.outbox[0]
    assert message.to == ["a@example.com"] and message.subject == "Hello" and message.from_email == "Flarize <no-reply@flarize.com>"
    assert message.alternatives[0].content == "<p>Body</p>"


def test_rolled_back_transaction_sends_nothing(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                notifications.queue_email(to="a@example.com", subject="Hello", text="Body")
                raise RuntimeError
    assert mail.outbox == []


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"to": "not-an-email", "subject": "s"}, ValidationError),
        ({"to": [], "subject": "s"}, ValueError),
        ({"to": [f"u{n}@example.com" for n in range(51)], "subject": "s"}, ValueError),
        ({"to": "a@example.com", "subject": "two\nlines"}, ValueError),
        ({"to": "a@example.com", "subject": "  "}, ValueError),
    ],
)
def test_invalid_messages_fail_fast(kwargs, error):
    with pytest.raises(error):
        notifications.queue_email(text="Body", **kwargs)


def test_broker_outage_falls_back_to_synchronous_delivery(django_capture_on_commit_callbacks):
    with mock.patch.object(tasks.send_email, "delay", side_effect=ConnectionError("broker down")):
        with django_capture_on_commit_callbacks(execute=True):
            notifications.queue_email(to="a@example.com", subject="Reset", text="Body")
    assert [message.subject for message in mail.outbox] == ["Reset"]


def test_fallback_failure_never_reaches_the_caller(django_capture_on_commit_callbacks):
    with mock.patch.object(tasks.send_email, "delay", side_effect=ConnectionError), mock.patch("core.notifications.EmailMultiAlternatives.send", side_effect=SMTPException):
        with django_capture_on_commit_callbacks(execute=True):
            notifications.queue_email(to="a@example.com", subject="Reset", text="Body")
    assert mail.outbox == []


def test_task_delivers_and_retries_on_smtp_errors():
    assert tasks.send_email.run(to=["a@example.com"], subject="S", text="T") == 1
    assert SMTPException in tasks.send_email.autoretry_for and tasks.send_email.max_retries == 5
