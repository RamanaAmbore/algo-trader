import socket
import smtplib
from datetime import date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr


class _IPv4SMTP(smtplib.SMTP):
    """Force IPv4 connections — server's outbound IPv6 hangs."""
    def _get_socket(self, host, port, timeout):
        for res in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            sock = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            sock.connect(sa)
            return sock
        raise OSError(f"Could not connect to {host}:{port} via IPv4")

from backend.shared.helpers.utils import secrets, is_enabled
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Brand-facing sender address used in every outbound alert / summary /
# contact / market email. Recipients see this in their inbox regardless
# of which mailbox actually authenticates against the SMTP server. Set
# secrets.mail_from to override (e.g. for staging or a different alias).
DEFAULT_MAIL_FROM = "rambo@ramboq.com"
DEFAULT_MAIL_FROM_NAME = "RamboQuant Analytics"


def _normalise_recipients(value) -> list[str]:
    """Accept a string (single address, or comma-separated list) OR an
    iterable of strings → return a clean, de-duped list of addresses.

    Strips whitespace, drops empties, lowercases for the dedup key but
    preserves the original casing of the first occurrence. Tolerates
    operator config quirks (a string instead of a yaml list; trailing
    commas; mixed-case duplicates) without raising."""
    if not value:
        return []
    if isinstance(value, str):
        raw = [s.strip() for s in value.split(",")]
    else:
        raw = []
        for item in value:
            if isinstance(item, str):
                raw.extend(s.strip() for s in item.split(","))
    out: list[str] = []
    seen: set[str] = set()
    for addr in raw:
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def _email_build_message(
    name: str, to_addrs: list[str], subject: str, html_body: str,
    mail_from: str, mail_from_name: str,
) -> MIMEMultipart:
    """Build the MIMEMultipart message object (without attachments)."""
    if len(to_addrs) == 1 and name:
        to_header = formataddr((name, to_addrs[0]))
    else:
        to_header = ", ".join(to_addrs)
    msg = MIMEMultipart()
    msg["From"] = formataddr((mail_from_name, mail_from))
    msg["To"] = to_header
    msg["Reply-To"] = msg["From"]
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    return msg


def _email_attach_files(
    msg: MIMEMultipart, attachments: list | None,
) -> None:
    """Attach binary files to a MIMEMultipart message in place."""
    for blob, filename, mime_type in (attachments or []):
        main, _, sub = mime_type.partition("/")
        part = MIMEBase(main or "application", sub or "octet-stream")
        part.set_payload(blob)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)


def _smtp_send(
    smtp_server: str, smtp_port: int, smtp_user: str, smtp_pass: str,
    envelope_to: list[str], msg: "MIMEMultipart",
) -> "tuple[bool, str]":
    """Open SMTP connection and send. Returns (success, message)."""
    try:
        if is_enabled('mail'):
            with _IPv4SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, envelope_to, msg.as_string())
            return True, '✅ Your message has been sent successfully!'
        else:
            logger.info(
                f"Email suppressed (mail capability off): "
                f"to={envelope_to} subject={msg.get('Subject')!r}"
            )
            return True, "Non-prod mode only"
    except smtplib.SMTPRecipientsRefused as e:
        refused = list(getattr(e, 'recipients', {}).keys()) or envelope_to
        err_msg = f"Email send error: recipients refused {refused}"
        logger.warning(err_msg)
        return False, err_msg
    except Exception as e:
        return False, f"Email send error: {e}"


def send_email(name, email_id, subject, html_body, attachments=None):
    """
    Email one or more recipients with the operator's brand address
    copied via Bcc (so the recipient never sees the operator inbox).

    - email_id: str (single address, or comma-separated list) OR
      iterable of strings. Normalised internally via
      `_normalise_recipients` so caller config quirks (yaml string
      vs list, trailing commas, dup case) are tolerated.
    - attachments: optional list of (data: bytes, filename: str,
      mime_type: str) tuples. Used today by the monthly investor
      statement task to attach the rendered PDF. Each becomes a
      MIMEBase part on the multipart envelope.

    The displayed "From" is `secrets.mail_from` (defaults to
    rambo@ramboq.com) — the operator-facing brand address. SMTP still
    authenticates with `smtp_user`; the SMTP server must allow sending
    "as" the brand address (alias on the auth mailbox). The brand
    address is added to the SMTP envelope as Bcc so it actually
    receives a copy; pre-fix the brand was in the Cc HEADER only and
    the envelope dropped it — Hostinger expanded the Cc-target group
    while the envelope went elsewhere, which some servers reject as
    a header/envelope mismatch. Use `secrets.mail_skip_bcc_brand: True`
    to suppress the Bcc when the operator doesn't want a copy.
    """
    # Dev-idle suppression — same contract as _send_telegram. When
    # dev's engine is idle, no operator-facing alerts fire. Contact-form
    # submissions DO still send (they go via send_email but only from
    # the public website route, not from dev's background tasks).
    try:
        from backend.shared.helpers.utils import is_engine_idle
        if is_engine_idle():
            return True, "Email skipped — engine idle (dev)"
    except Exception:
        pass

    smtp_server    = secrets['smtp_server']
    smtp_port      = secrets['smtp_port']
    smtp_user      = secrets['smtp_user']
    smtp_pass      = secrets['smtp_pass']
    smtp_user_name = secrets.get('smtp_user_name', '')

    mail_from      = secrets.get('mail_from') or DEFAULT_MAIL_FROM
    mail_from_name = secrets.get('mail_from_name') or smtp_user_name or DEFAULT_MAIL_FROM_NAME

    to_addrs = _normalise_recipients(email_id)
    if not to_addrs:
        return False, "Email send error: no recipient address"

    skip_bcc_brand = bool(secrets.get('mail_skip_bcc_brand', False))
    bcc_brand = "" if skip_bcc_brand else mail_from
    if bcc_brand and bcc_brand.lower() in {a.lower() for a in to_addrs}:
        bcc_brand = ""

    msg = _email_build_message(name, to_addrs, subject, html_body, mail_from, mail_from_name)
    _email_attach_files(msg, attachments)

    envelope_to = list(to_addrs)
    if bcc_brand:
        envelope_to.append(bcc_brand)

    return _smtp_send(smtp_server, smtp_port, smtp_user, smtp_pass, envelope_to, msg)


if __name__ == "__main__":
    # Test run — fires to the operator inbox configured in
    # secrets.alert_emails[0] (sanity check that SMTP credentials work).
    name = "Rambo"
    recipients = (secrets.get('alert_emails') or [secrets.get('smtp_user', '')])[0]
    query = "Testing single-recipient email functionality."

    success, msg = send_email(name, recipients, query)
    if success:
        print("✅ Email sent successfully!")
    else:
        print("❌ Failed to send email:", msg)
