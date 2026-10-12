"""
Moderated chat UI.

Run:  streamlit run app.py
Needs moderation.py in the same folder and GEMINI_API_KEY in your .env
pip install streamlit
"""
import html
from datetime import datetime

import streamlit as st

st.set_page_config(page_title="Moderated Chat", page_icon="💬")

try:
    from moderation import moderate_text
except Exception as e:  # e.g. missing API key
    st.error(f"Could not load moderation module: {e}")
    st.stop()

# ---------- Styling ----------
st.markdown(
    """
    <style>
    .row { display: flex; justify-content: flex-end; margin: 6px 0; }
    .bubble {
        background: #dcf8c6; color: #111;
        padding: 8px 12px; max-width: 75%;
        border-radius: 12px 12px 0 12px;
        word-wrap: break-word; overflow-wrap: anywhere;
    }
    .bubble.rejected { background: #fde8e8; border: 1px solid #e57373; color: #7a1c1c; }
    .bubble.error    { background: #fff4e5; border: 1px solid #ffb74d; color: #7a4a00; }
    .meta { font-size: 11px; opacity: .65; text-align: right; margin-top: 3px; }
    .detail { font-size: 13px; margin-top: 4px; }
    .spinner {
        width: 14px; height: 14px; margin-right: 8px;
        border: 2px solid #bbb; border-top-color: #25d366;
        border-radius: 50%; display: inline-block; vertical-align: middle;
        animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- Helpers ----------
def esc(text: str) -> str:
    """Escape user/model text so it can't inject HTML, keep line breaks."""
    return html.escape(text).replace("\n", "<br>")


def build_message(prompt: str, outcome: dict) -> dict:
    now = datetime.now().strftime("%H:%M")
    status = outcome.get("status")

    if status == "ok":
        r = outcome["result"]
        labels = [
            name
            for name, flag in (
                ("harmful", r["is_harmful"]),
                ("illegal", r["is_illegal"]),
                ("abusive", r["is_abusive"]),
            )
            if flag
        ]
        if not labels:
            return {"kind": "ok", "text": prompt, "time": now}
        return {
            "kind": "rejected",
            "labels": labels,
            "categories": r.get("categories", []),
            "severity": r.get("severity", ""),
            "reason": r.get("reason", ""),
            "time": now,
        }

    if status == "blocked":
        return {
            "kind": "rejected",
            "labels": ["blocked by safety filters"],
            "categories": [],
            "severity": "",
            "reason": "",
            "time": now,
        }

    # status == "error": fail closed, message is NOT shown
    return {"kind": "error", "time": now}


def render(msg: dict) -> None:
    kind = msg["kind"]

    if kind == "ok":
        body = esc(msg["text"])
        cls = "bubble"
    elif kind == "rejected":
        parts = [f"<b>Message not sent.</b> It was flagged as <b>{esc(', '.join(msg['labels']))}</b>."]
        if msg.get("categories"):
            parts.append(f"<div class='detail'>Type: {esc(', '.join(msg['categories']))}</div>")
        if msg.get("severity") and msg["severity"] != "none":
            parts.append(f"<div class='detail'>Severity: {esc(msg['severity'])}</div>")
        if msg.get("reason"):
            parts.append(f"<div class='detail'>{esc(msg['reason'])}</div>")
        body = "".join(parts)
        cls = "bubble rejected"
    else:
        body = "<b>Message not sent.</b> We couldn't check it right now. Please try again."
        cls = "bubble error"

    st.markdown(
        f"<div class='row'><div class='{cls}'>{body}"
        f"<div class='meta'>{msg['time']}</div></div></div>",
        unsafe_allow_html=True,
    )


SPINNER_HTML = (
    "<div class='row'><div class='meta' style='font-size:13px;'>"
    "<span class='spinner'></span>Checking your message…</div></div>"
)

# ---------- App ----------
st.title("💬 Moderated Chat")

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()

for m in st.session_state.messages:
    render(m)

prompt = st.chat_input("Type a message", max_chars=2000)

if prompt and prompt.strip():
    # Spinner sits right above the input box, on the right (like "typing..." in WhatsApp)
    status_slot = st.empty()
    status_slot.markdown(SPINNER_HTML, unsafe_allow_html=True)

    outcome = moderate_text(prompt)

    status_slot.empty()
    msg = build_message(prompt, outcome)
    st.session_state.messages.append(msg)
    render(msg)