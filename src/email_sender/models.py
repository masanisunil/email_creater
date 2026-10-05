import getpass
import hashlib
import json
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z .'-]*$")


class UserDetails(BaseModel):
    is_email_request: bool = Field(default=True, description="False if the message is not a request to compose/send an email, e.g. greetings, general questions, or unrelated chat")
    mail_reason: str = Field(default="", max_length=4000, description="Reason, or empty if missing")
    receipt_name: str = Field(default="", max_length=200, description="Recipient name, or empty if missing")
    receipt_email: str = Field(default="", max_length=320, description="Explicit recipient email, or empty if missing; never invent")


class RecipientDetails(BaseModel):
    receipt_email: EmailStr
    mail_reason: str = Field(min_length=1, max_length=4000)
    receipt_name: str = Field(default="", max_length=200)

    @field_validator("mail_reason")
    @classmethod
    def reason_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Email reason is required")
        return value.strip()

    @field_validator("receipt_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        # Reject stray single letters/junk so a bad name never reaches the greeting as-is.
        if len(value) < 2 or not _NAME_PATTERN.match(value):
            return ""
        return value.title()


class DraftEmail(BaseModel):
    subject: str = Field(min_length=1, max_length=300, description="Subject, at most 30 words")
    body: str = Field(min_length=1, max_length=10000, description="Plain-text email, at most 150 words")

    @field_validator("subject", "body")
    @classmethod
    def validate_content(cls, value: str, info) -> str:
        value = value.strip()
        limit = 30 if info.field_name == "subject" else 150
        if not value or len(value.split()) > limit:
            raise ValueError(f"{info.field_name} must contain 1 to {limit} words")
        if info.field_name == "subject" and ("\r" in value or "\n" in value):
            raise ValueError("Subject cannot contain line breaks")
        return value


class EmailState(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    question: str = Field(default="", max_length=4000)
    mail_reason: str = ""
    receipt_name: str = ""
    receipt_email: str = ""
    subject: str = ""
    body: str = ""
    feedback: str = Field(default="", max_length=2000)
    feedback_count: int = Field(default=0, ge=0)
    response: str = ""
    approved: bool = False
    cancelled: bool = False
    approval_digest: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    owner: str = Field(default_factory=getpass.getuser)
    status: str = "pending"


class DeliveryResult(BaseModel):
    status: Literal["accepted", "failed", "unknown", "blocked"]
    message: str
    request_id: str
    attempts: int = 0
    retryable: bool = False


def email_digest(recipient: str, subject: str, body: str) -> str:
    payload = json.dumps([recipient, subject, body], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()