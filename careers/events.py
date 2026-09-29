"""Outbox handlers owned by careers (``@core.outbox.handler("<context>.<event>")``).

``careers.application_received`` → e-mail the hiring team when the company profile asks for it
(``notify_on_new_application`` + ``application_notification_emails``, the CMS ``SiteSettings`` fields). The mail
names the posting and the candidate only; the resume stays behind the Studio's signed download.
"""

from __future__ import annotations

from careers.models import JobApplication
from company.services.profile import get_profile
from core.notifications import queue_email
from core.outbox import Event, handler


@handler("careers.application_received")
def notify_hiring_team(event: Event) -> None:
    profile = get_profile()
    if profile is None or not profile.notify_on_new_application or not profile.application_notification_emails:
        return
    application = JobApplication.all_objects.filter(uid=event.payload.get("application_uid")).first()
    if application is None:
        return
    position = " ".join(application.display_position.split())  # one line, whatever the form sent
    subject = f"New job application: {position}"[:200]
    text = f"{application.name} applied for {position}.\n" f"Location: {application.location}\n\n" "Open Careers → Applications in the Studio to review the application and download the resume."
    queue_email(to=list(profile.application_notification_emails), subject=subject, text=text, category="careers.application_received")
