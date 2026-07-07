"""Shared plumbing for personal agents: one summarizer call, one Telegram send.

Imported by every agent (morning briefing, GitHub radar, housekeeper) so the
delivery/summarize code exists exactly once. Agents still run on independent
schedules and fail independently.
"""

import os

import requests

TELEGRAM_CHUNK = 4000  # Telegram's hard limit is 4096 chars per message


def ask_llm(prompt, model="claude-haiku-4-5", max_tokens=2000):
    """Single-turn model call; returns the text response."""
    from anthropic import Anthropic  # deferred: send-only agents skip the dep

    client = Anthropic()  # ANTHROPIC_API_KEY from environment
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b.text for b in response.content if b.type == "text")


def send_telegram(text):
    """Send to Telegram via bot API, split into <=4000-char chunks."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), TELEGRAM_CHUNK):
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text[i : i + TELEGRAM_CHUNK]},
            timeout=30,
        )
        resp.raise_for_status()
