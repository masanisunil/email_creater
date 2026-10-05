import getpass
import hashlib
import smtplib
import sqlite3
import ssl
import time
from email.message import EmailMessage
from uuid import uuid4

from pydantic import TypeAdapter, EmailStr

from .config import Settings
from .models import DeliveryResult, DraftEmail, email_digest
from .storage import Store


def send_email_result(receipt_email: str, subject: str, body: str, *,
                      request_id: str | None = None, owner: str | None = None,
                      settings: Settings | None = None) -> DeliveryResult:
    request_id = request_id or str(uuid4())
    owner = owner or getpass.getuser()
    try:
        settings = settings or Settings.from_env()
        settings.validate_smtp()
        recipient = str(TypeAdapter(EmailStr).validate_python(receipt_email))
        sender = str(TypeAdapter(EmailStr).validate_python(settings.sender_email))
        draft = DraftEmail(subject=subject, body=body)
        settings.check_recipient(recipient)
        store = Store(settings.database)
        store.register(request_id, owner, settings.hourly_limit)
        digest = email_digest(recipient, draft.subject, draft.body)
        claim = store.claim(request_id, owner, digest)
    except (ValueError, PermissionError, OSError, sqlite3.Error):
        return DeliveryResult(status="blocked", message="Email blocked: check configuration, recipient, content, quota, or ownership.", request_id=request_id)
    if claim != "claimed":
        if claim == "accepted":
            return DeliveryResult(status="accepted", message="SMTP already accepted this request; no duplicate sent.", request_id=request_id)
        return DeliveryResult(status="blocked", message="Request already processed or delivery is uncertain; investigate before creating another request.", request_id=request_id)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = draft.subject
    message_id = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    message["Message-ID"] = f"<{message_id}@{sender.rsplit('@', 1)[1]}>"
    message.set_content(draft.body)
    started = time.monotonic()
    status = "failed"
    retryable = False
    attempts = 0
    for attempts in range(1, settings.smtp_attempts + 1):
        server = None
        sending = False
        try:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      context=ssl.create_default_context(), timeout=settings.smtp_timeout)
            server.login(settings.smtp_username, settings.smtp_password)
            sending = True
            refused = server.send_message(message, from_addr=sender, to_addrs=[recipient])
            status = "failed" if refused else "accepted"
            break
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            status = "failed"
            break
        except smtplib.SMTPAuthenticationError:
            status = "failed"
            break
        except (smtplib.SMTPException, OSError) as error:
            if sending:
                status = "unknown"
                break
            retryable = not isinstance(error, ssl.SSLCertVerificationError) and (
                isinstance(error, (OSError, smtplib.SMTPServerDisconnected)) or
                isinstance(error, smtplib.SMTPResponseException) and 400 <= error.smtp_code < 500
            )
            if not retryable or attempts == settings.smtp_attempts:
                break
            time.sleep(min(2 ** (attempts - 1), 4))
        except Exception:
            status = "unknown" if sending else "failed"
            break
        finally:
            if server is not None:
                try:
                    server.close()
                except OSError:
                    pass
    try:
        store.finish(request_id, status, attempts)
        store.audit(request_id, f"smtp_elapsed_ms_{int((time.monotonic() - started) * 1000)}", status)
    except (sqlite3.Error, OSError):
        status = "unknown"
    messages = {
        "accepted": "Email sent: accepted by the recipient's mail server for delivery. "
                   "(SMTP cannot confirm inbox placement from here; check spam/junk if the recipient doesn't see it.)",
        "unknown": "Delivery outcome unknown; automatic resend blocked. Check provider logs.",
        "failed": "Email was not accepted. Check SMTP configuration or provider status.",
    }
    return DeliveryResult(status=status, message=messages[status], request_id=request_id,
                          attempts=attempts, retryable=retryable if status == "failed" else False)


def send_email(receipt_email: str, subject: str, body: str) -> str:
    return send_email_result(receipt_email, subject, body).message