"""
Contact form endpoint.

POST /api/contact/  — sends an email via mail_utils and returns success/error
"""

import time

import msgspec
from litestar import Controller, post
from litestar.exceptions import HTTPException

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Simple in-process cooldown: one submission per email per 5 minutes
_cooldown: dict[str, float] = {}
_COOLDOWN_SECONDS = 300


class ContactRequest(msgspec.Struct):
    name: str
    email: str
    message: str


class ContactResponse(msgspec.Struct):
    detail: str


class ContactController(Controller):
    path = "/api/contact"

    @post("/")
    async def submit(self, data: ContactRequest) -> ContactResponse:
        if not data.name.strip() or not data.email.strip() or not data.message.strip():
            raise HTTPException(status_code=422, detail="All fields are required")

        now = time.monotonic()
        last = _cooldown.get(data.email, 0)
        if now - last < _COOLDOWN_SECONDS:
            raise HTTPException(status_code=429, detail="Please wait before submitting again")

        try:
            from backend.shared.helpers.alert_utils import get_market_recipients
            from backend.shared.helpers.mail_utils import send_email

            # Public-website inbound mail routes to `market_emails` in
            # secrets.yaml (e.g. website.ramboquant@gmail.com,
            # afridihajayt@gmail.com). Kept separate from the operator
            # alert inbox so trading-ops notifications and inbound leads
            # don't bleed into the same thread.
            recipients = get_market_recipients()
            if not recipients:
                logger.error("Contact form: no market_emails recipients configured")
                raise HTTPException(status_code=500, detail="Failed to send message")

            subject = f"RamboQuant Contact: {data.name}"
            html_body = (
                f"<p><strong>Name:</strong> {data.name}</p>"
                f"<p><strong>Email:</strong> {data.email}</p>"
                f"<p><strong>Message:</strong></p>"
                f"<p>{data.message.replace(chr(10), '<br>')}</p>"
            )
            # send_email takes one recipient at a time; loop so every
            # configured market address gets a copy. Any individual
            # failure is logged but doesn't block the rest from going
            # through (partial-success path beats all-or-nothing).
            sent = 0
            last_err = ''
            for to_email in recipients:
                success, msg = send_email(data.name, to_email, subject, html_body)
                if success:
                    sent += 1
                else:
                    last_err = msg
                    logger.error(f"Contact form email failed for {to_email}: {msg}")
            if sent == 0:
                raise HTTPException(status_code=500, detail="Failed to send message")

            _cooldown[data.email] = now
            logger.info(
                f"Contact form submitted by {data.email!r} — sent to "
                f"{sent}/{len(recipients)} market recipients"
            )
            return ContactResponse(detail="Your message has been sent. We will get back to you shortly.")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Contact form error: {e}")
            raise HTTPException(status_code=500, detail="Failed to send message")
