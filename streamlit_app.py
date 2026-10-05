import hmac
import os
import time
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.types import Command

from src.email_sender.agent import final_graph
from src.email_sender.models import EmailState

load_dotenv()

st.set_page_config(page_title="Email Agent", page_icon="✉️", layout="centered")

st.markdown(
    """
    <style>
    @keyframes fadeIn { from {opacity:0; transform: translateY(8px);} to {opacity:1; transform: translateY(0);} }
    .block-container { animation: fadeIn 0.5s ease-out; }
    .status-pill {
        display:inline-block; padding:4px 14px; border-radius:999px;
        font-size:0.85rem; font-weight:600; margin-bottom:10px;
    }
    .pill-pending { background:#2d4a6b; color:#9fd3ff; }
    .pill-accepted { background:#1f4d2e; color:#8fe3a3; }
    .pill-cancelled { background:#5a2d2d; color:#ffb3b3; }
    .pill-failed { background:#5a2d2d; color:#ffb3b3; }
    div.stButton > button {
        border-radius: 10px; font-weight:600;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    div.stButton > button:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.3); }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    "<h1 style='font-size:3rem; margin-bottom:0;'>📧</h1>"
    "<h1 style='margin-top:-10px;'>Email Agent</h1>",
    unsafe_allow_html=True,
)
st.caption("Human-approved email drafting and sending")
st.divider()

if "config" not in st.session_state:
    st.session_state.config = None
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False


def check_password() -> bool:
    required = os.environ.get("APP_PASSWORD", "")
    if not required:
        return True  # no password configured: local/dev mode, gate disabled
    if st.session_state.authenticated:
        return True
    with st.container(border=True):
        st.markdown("### \U0001F512 Enter password to continue")
        entered = st.text_input("Password", type="password", key="password_input")
        if st.button("Unlock", type="primary"):
            if hmac.compare_digest(entered, required):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()


def pill(status: str) -> str:
    cls = {"accepted": "pill-accepted", "cancelled": "pill-cancelled",
           "failed": "pill-failed", "declined": "pill-cancelled",
           "blocked": "pill-failed"}.get(status, "pill-pending")
    return f'<span class="status-pill {cls}">{status.upper()}</span>'


def reset():
    st.session_state.config = None
    st.session_state.thread_id = None


if st.session_state.config is None:
    with st.container(border=True):
        query = st.text_area("What email should I send?",
                              placeholder="Email jane@example.com about rescheduling our meeting",
                              height=100)
        col1, col2 = st.columns([1, 3])
        with col1:
            start = st.button("🚀 Send Request", type="primary", use_container_width=True)

    if start:
        if not query.strip():
            st.warning("Please describe the email you want to send before submitting.")
        else:
            thread_id = str(uuid.uuid4())
            state = EmailState(question=query.strip(), owner="streamlit-user", request_id=thread_id)
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
            with st.spinner("Thinking..."):
                final_graph.invoke(state.model_dump(), config)
            st.session_state.config = config
            st.session_state.thread_id = thread_id
            st.rerun()

else:
    config = st.session_state.config
    snapshot = final_graph.get_state(config)
    # st.markdown(f"`Request ID: {st.session_state.thread_id}`")

    if not snapshot.next and not snapshot.interrupts:
        status = snapshot.values.get("status", "done")
        with st.container(border=True):
            st.markdown(pill(status), unsafe_allow_html=True)
            if status == "accepted":
                st.success("✅ Your email was sent successfully!")
            elif status in {"cancelled", "declined"}:
                st.warning("Email sending was cancelled.")
            elif status == "blocked":
                st.error("This request couldn't be completed. Please check the recipient and try again.")
            else:
                st.error("Something went wrong while processing your request. Please try again.")
        if st.button("↩️ New request"):
            reset()
            st.rerun()

    elif not snapshot.interrupts:
        with st.spinner("Processing..."):
            final_graph.invoke(None, config)
        time.sleep(0.3)
        st.rerun()

    else:
        payload = snapshot.interrupts[0].value
        with st.container(border=True):
            st.markdown(pill("pending"), unsafe_allow_html=True)

            if payload["kind"] == "details":
                st.markdown(f"**{payload['message']}**")
                with st.form("details_form"):
                    recipient = st.text_input("Recipient email")
                    name = st.text_input("Recipient name (optional)")
                    reason = st.text_area("Email reason", height=80)
                    c1, c2 = st.columns(2)
                    submit = c1.form_submit_button("✅ Continue", use_container_width=True)
                    cancel = c2.form_submit_button("❌ Cancel", use_container_width=True)
                if submit:
                    if not recipient.strip():
                        st.warning("Recipient email is required.")
                    else:
                        with st.spinner("Drafting email..."):
                            final_graph.invoke(Command(resume={
                                "receipt_email": recipient.strip(), "receipt_name": name.strip(),
                                "mail_reason": reason.strip(),
                            }), config)
                        st.rerun()
                if cancel:
                    with st.spinner("Cancelling..."):
                        final_graph.invoke(Command(resume="cancel"), config)
                    st.rerun()

            else:
                st.markdown(f"**To:** {payload['to']}")
                st.markdown(f"**Subject:** {payload['subject']}")
                st.text_area("Body", value=payload["body"], height=180, disabled=True)
                with st.form("review_form", clear_on_submit=True):
                    feedback = st.text_area("Provide revision feedback (optional)", height=80,
                                            placeholder="e.g. Make it more formal and add a closing date")
                    c1, c2, c3 = st.columns(3)
                    approve = c1.form_submit_button("✅ Approve & Send", type="primary", use_container_width=True)
                    revise = c2.form_submit_button("✏️ Submit Feedback", use_container_width=True)
                    cancel = c3.form_submit_button("❌ Cancel", use_container_width=True)
                if approve:
                    with st.spinner("Sending email..."):
                        final_graph.invoke(Command(resume={"action": "approve", "digest": payload["digest"]}), config)
                    st.rerun()
                elif cancel:
                    with st.spinner("Cancelling..."):
                        final_graph.invoke(Command(resume={"action": "cancel", "digest": payload["digest"]}), config)
                    st.rerun()
                elif revise:
                    if not feedback.strip():
                        st.warning("Please enter feedback before submitting a revision.")
                    else:
                        with st.spinner("Revising draft..."):
                            final_graph.invoke(Command(resume={
                                "action": "revise", "feedback": feedback.strip(), "digest": payload["digest"],
                            }), config)
                        st.rerun()
