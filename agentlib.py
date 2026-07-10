"""Shared plumbing for personal agents: one summarizer call, one Telegram send.

Imported by every agent (morning briefing, GitHub radar, housekeeper) so the
delivery/summarize code exists exactly once. Agents still run on independent
schedules and fail independently.
"""

import os
import time

import requests

TELEGRAM_CHUNK = 4000  # Telegram's hard limit is 4096 chars per message
SEND_ATTEMPTS = 2  # one retry on transient failures — see send_telegram


def ask_llm(prompt, model="claude-haiku-4-5", max_tokens=2000):
    """Single-turn model call; returns the text response.

    No retry loop here on purpose: the Anthropic SDK already retries
    connection errors, 429s and 5xx internally (max_retries=2 default).

    Thinking is disabled: every caller is a one-shot summarizer/classifier
    /writer that wants the whole token budget spent on the answer, not on a
    chain of thought. This also sidesteps a Sonnet-5-class failure — those
    models default to adaptive thinking when `thinking` is omitted, and
    max_tokens caps thinking+answer together, so a tight budget can come
    back as thinking-only with no text block. We still guard for an empty
    response and fail loudly with the stop_reason rather than a bare
    StopIteration, so any no-text reply (e.g. a refusal) is diagnosable."""
    from anthropic import Anthropic  # deferred: send-only agents skip the dep

    client = Anthropic()  # ANTHROPIC_API_KEY from environment
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    if not text:
        raise RuntimeError(
            f"model returned no text (stop_reason={response.stop_reason})"
        )
    return text


def send_telegram(text):
    """Send to Telegram via bot API, split into <=4000-char chunks.

    Delivery is the last step of every agent — a transient network blip
    here throws away an already-built message, so connection-class
    failures (incl. SSL hiccups) get one retry after a short pause.
    HTTP errors (bad token, bad chat id) still raise immediately: a
    second identical request would fail identically."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), TELEGRAM_CHUNK):
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": text[i : i + TELEGRAM_CHUNK]},
                    timeout=30,
                )
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == SEND_ATTEMPTS:
                    raise
                time.sleep(3)
        resp.raise_for_status()
