# Email Sending Agent

A local CLI that extracts email details, drafts a plain-text message, requires
explicit approval, and submits it over verified TLS to an SMTP server.

## Setup

Requires Python 3.14 or newer and uv. From this directory:

```powershell
uv sync --locked --inexact --package email-sender
uv run --locked --package email-sender email-sender
```

Add the configuration keys shown in `.env.example` to your existing `.env`, or
provide them through your deployment's secret manager. The existing Groq key
is not modified. Required keys are `GROQ_API_KEY`, `SENDER_EMAIL`, and
`SMTP_PASSWORD`. An empty `SMTP_USERNAME` uses `SENDER_EMAIL`.
`SENDER_PASSWORD` is supported as a legacy alias for `SMTP_PASSWORD`.

This is a member of the parent uv workspace. `--inexact` preserves packages used
by the other tutorials in its shared environment. The existing `venv` remains
usable; it is not replaced by these source changes. For an isolated installation,
set `UV_PROJECT_ENVIRONMENT` to a separate environment directory before uv commands.

**Revoke the formerly hardcoded Gmail app password before use.** Removing it
from source does not revoke it. Use a newly issued app password, not a normal
account password. Never commit secrets, databases, or notebook outputs containing
email content. Review existing Git history and notebook outputs for exposure.

The original `python agent.py` entry point and `from agent import final_graph,
EmailState` imports remain supported. `send_email(recipient, subject, body)`
still returns a string. Its success text now correctly reports SMTP acceptance,
not guaranteed inbox delivery. `send_email_result(...)` returns structured status.

## Approval and Resumption

The CLI displays the exact recipient, subject, and body. `yes`/`approve` approves;
`no`/`cancel` cancels; other nonempty text requests a revision. Blank input does
not send. At most two revisions are allowed. Missing or invalid recipients and
reasons require clarification. Subjects are limited to 30 words and bodies to
150 words; invalid model output ends safely without sending.

Each request prints a unique ID before contacting the model. State survives
restart in `EMAIL_DATABASE` (default `.email-sender/state.sqlite3`).

```powershell
uv run --locked --package email-sender email-sender --resume REQUEST_ID
uv run --locked --package email-sender email-sender --purge-request REQUEST_ID
```

Purging deletes completed request checkpoints containing email content, while
retaining the content-free send ledger and audit events. Choose a retention
period (for example seven days), and purge completed requests accordingly.
Pending or uncertain deliveries cannot be purged with this command. A pending
request can first be resumed and cancelled. SQLite secure-delete is enabled for
checkpoint deletion; encrypt the disk and backups because checkpoints are
otherwise stored in plaintext and historical backups may still contain content.

Notebook users can still invoke `final_graph.invoke(EmailState(question=...).model_dump(),
config)` and resume with `Command(resume="yes")`. A new request must use a fresh
thread ID; reusing a saved ID for a different question is deliberately rejected.
`final_graph.get_state(config)` and `final_graph.get_graph()` remain available.

## Delivery Safety

An atomic SQLite claim prevents a second SMTP submission for the same request.
The approved recipient and content are fingerprinted; changing them invalidates
approval. Reusing an accepted ID returns its prior result without resending.
The legacy three-argument send function generates a new ID each call; callers
requiring duplicate protection across retries must use `send_email_result` with
a stable `request_id` and verified `owner`.

Only transient failures before message submission receive bounded retries.
If a connection fails during submission, or a process crashes while sending,
the outcome is uncertain and automatic resend is blocked. Check provider logs
using the message ID before deciding whether to create a new request. SMTP
cannot guarantee exactly-once delivery. A stable Message-ID assists investigation
but is not itself a provider idempotency guarantee. Message-ID uses a SHA-256
hash of the request ID and the sender domain.

`ALLOWED_RECIPIENTS` and `ALLOWED_RECIPIENT_DOMAINS` are comma-separated
allowlists; when either is set, a recipient must match at least one. Empty lists
allow arbitrary valid addresses, preserving the local CLI behavior. Configure
an allowlist before operational use. `EMAIL_HOURLY_LIMIT` limits new requests per
local user, including cancelled/failed requests, to constrain SMTP and LLM usage.

## Deployment Boundary

This implementation is a **trusted local, single-user CLI**, not an authenticated
web service. Local username ownership checks prevent accidental cross-user resume,
but are not a security boundary against a user who can edit the database or
environment. Restrict the database directory and secrets with operating-system
permissions. Use one protected installation per OS account and persistent local
storage; do not put SQLite on a shared network filesystem.

Before exposing a shared service, add authenticated identity from your identity
provider, derive ownership server-side, authorize every start/read/resume/send,
add CSRF protection where applicable, and enforce per-tenant recipient policies
and rate limits. Do not accept `owner` or approval records directly from untrusted
clients. For distributed workers use a transactional shared store/checkpointer.
No unauthenticated HTTP endpoint is introduced here.

Email requests and draft content are sent to Groq for generation. Review provider
retention, region, and data-processing terms before sending sensitive information.
Disable LangSmith/provider tracing unless deliberately approved; tracing may
capture content. Human review remains necessary for hallucinations and prompt
injection: prompts alone are not a security control.

Logs contain request IDs, stages, outcomes, and elapsed times, not secrets, email
bodies, or recipient addresses. Audit events and send status are persisted in
SQLite. Monitor failed/unknown/blocked outcomes, latency, quota violations, and
disk space. Configure provider bounce handling, SPF/DKIM/DMARC, and delivery
monitoring separately; these require control of the email provider/domain.

## Verification

```powershell
uv run --locked --package email-sender python -m unittest discover -s tests -v
uv run --locked --package email-sender ruff check agent.py email_send.py src tests
uv build --package email-sender --no-sources
```

Tests mock SMTP and the model. They exercise approval, cancellation, validation,
draft integrity, checkpoint restart, quotas, ownership, retries, and duplicate
protection. The repository-level email-sender workflow runs these checks and
builds the package using the repository-root workspace lockfile (`../uv.lock`).
Live credentials and delivery
must be verified separately with a controlled recipient.
