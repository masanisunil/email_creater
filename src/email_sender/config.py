import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def positive_number(name: str, default: str, cast: type = int):
    value = cast(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    database: Path
    smtp_host: str
    smtp_port: int
    smtp_timeout: float
    smtp_attempts: int
    sender_email: str
    sender_name: str
    smtp_username: str
    smtp_password: str = field(repr=False)
    allowed_recipients: frozenset[str]
    allowed_domains: frozenset[str]
    hourly_limit: int
    model: str
    llm_timeout: float
    llm_retries: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        sender = os.environ.get("SENDER_EMAIL", "")
        retries = int(os.environ.get("LLM_MAX_RETRIES", "2"))
        if retries < 0 or retries > 5:
            raise ValueError("LLM_MAX_RETRIES must be between 0 and 5")
        attempts = positive_number("SMTP_MAX_ATTEMPTS", "3")
        port = positive_number("SMTP_PORT", "465")
        if attempts > 5 or port > 65535:
            raise ValueError("Invalid SMTP attempt limit or port")

        def values(name: str) -> frozenset[str]:
            return frozenset(item.strip().casefold() for item in os.environ.get(name, "").split(",") if item.strip())

        return cls(
            database=Path(os.environ.get("EMAIL_DATABASE", ".email-sender/state.sqlite3")),
            smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"), smtp_port=port,
            smtp_timeout=positive_number("SMTP_TIMEOUT", "20", float), smtp_attempts=attempts,
            sender_email=sender, sender_name=os.environ.get("SENDER_NAME", "").strip(),
            smtp_username=os.environ.get("SMTP_USERNAME") or sender,
            smtp_password=os.environ.get("SMTP_PASSWORD") or os.environ.get("SENDER_PASSWORD", ""),
            allowed_recipients=values("ALLOWED_RECIPIENTS"), allowed_domains=values("ALLOWED_RECIPIENT_DOMAINS"),
            hourly_limit=positive_number("EMAIL_HOURLY_LIMIT", "20"),
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
            llm_timeout=positive_number("LLM_TIMEOUT", "30", float), llm_retries=retries,
        )

    def validate_smtp(self) -> None:
        if not self.sender_email or not self.smtp_username or not self.smtp_password:
            raise ValueError("Set SENDER_EMAIL, SMTP_USERNAME (optional), and SMTP_PASSWORD")
        if not self.smtp_host or any(character.isspace() for character in self.smtp_host):
            raise ValueError("Invalid SMTP_HOST")

    def check_recipient(self, recipient: str) -> None:
        if self.allowed_recipients or self.allowed_domains:
            if recipient.casefold() not in self.allowed_recipients and recipient.rsplit("@", 1)[-1].casefold() not in self.allowed_domains:
                raise ValueError("Recipient is not permitted by the configured allowlist")