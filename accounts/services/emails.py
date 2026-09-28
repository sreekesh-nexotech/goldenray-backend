"""Account e-mails (invite, password reset, password changed), delivered after commit via ``core.notifications``."""

from __future__ import annotations

from datetime import timedelta

from core.notifications import queue_email

PRODUCT_NAME = "Flarize Studio"


def _hours(ttl: timedelta) -> str:
    hours = max(1, round(ttl.total_seconds() / 3600))
    return "1 hour" if hours == 1 else f"{hours} hours"


def _greeting(user) -> str:
    return f"Hello {user.first_name}," if user.first_name else "Hello,"


def send_invite(user, link: str, ttl: timedelta) -> None:
    text = (
        f"{_greeting(user)}\n\n"
        f"An account has been created for you on {PRODUCT_NAME}. Set your password to sign in:\n\n"
        f"{link}\n\n"
        f'This link can be used once and expires in {_hours(ttl)}. If it expires, use "Forgot password" on the sign-in page.\n'
    )
    queue_email(to=user.email, subject=f"Your {PRODUCT_NAME} account", text=text, category="accounts.invite")


def send_password_reset(user, link: str, ttl: timedelta) -> None:
    text = (
        f"{_greeting(user)}\n\n"
        f"We received a request to reset the password of your {PRODUCT_NAME} account. Choose a new password here:\n\n"
        f"{link}\n\n"
        f"This link can be used once and expires in {_hours(ttl)}. If you did not ask for this, you can ignore this e-mail; "
        f"your password stays unchanged.\n"
    )
    queue_email(to=user.email, subject=f"Reset your {PRODUCT_NAME} password", text=text, category="accounts.password_reset")


def send_password_changed(user) -> None:
    text = (
        f"{_greeting(user)}\n\n"
        f"The password of your {PRODUCT_NAME} account was just changed and your other sessions were signed out.\n\n"
        f"If this was not you, reset your password immediately and tell your administrator.\n"
    )
    queue_email(to=user.email, subject=f"Your {PRODUCT_NAME} password was changed", text=text, category="accounts.password_changed")
