"""POST /api/talk: the user's words in, Mom's reply out as text + speech.

Request:  {"history": [{"role": "user"|"assistant", "content": "..."}, ...], "passcode": "..."}
          (the last item must be the new user message)
Response: {"reply": "...", "audio": "<base64 mp3>"}

Environment variables:
  ANTHROPIC_API_KEY   Claude API key (required)
  CARTESIA_API_KEY    Cartesia API key (required)
  CARTESIA_VOICE_ID   the cloned voice id (required)
  DEMO_PASSCODE       optional; if set, requests must include it (stops strangers
                      from spending your API credits)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
from _persona import GREETING, SYSTEM_PROMPT  # noqa: E402

MODEL = "claude-opus-5-5"
MAX_TURNS = 20          # history sent to Claude
MAX_CHARS = 1000        # per message
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2025-04-16"

_client: anthropic.Anthropic | None = None


class BadRequest(Exception):
    pass


def clean_history(raw) -> list[dict]:
    """Keep only well-formed, alternating user/assistant text turns."""
    if not isinstance(raw, list) or not raw:
        raise BadRequest("history must be a non-empty list")
    turns = []
    for item in raw[-MAX_TURNS:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        text = str(item.get("content", "")).strip()[:MAX_CHARS]
        if not text:
            continue
        if turns and turns[-1]["role"] == item["role"]:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": item["role"], "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if not turns or turns[-1]["role"] != "user":
        raise BadRequest("the last message must be from the user")
    return turns


def ask_mom(history: list[dict]) -> str:
    global _client
    _client = _client or anthropic.Anthropic()
    response = _client.beta.messages.create(
        model=MODEL,
        # Thinking counts toward max_tokens, so leave plenty of room at high effort.
        max_tokens=16000,
        # Highest practical effort: the most considered replies, a few seconds slower.
        # Drop to "medium" or "low" if replies feel too slow in conversation.
        output_config={"effort": "xhigh"},
        # If a safety classifier declines, retry on a fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=history,
    )
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "refusal" or not text:
        return "I'm right here with you, sweetheart. Tell me a little more?"
    return text


def speak(text: str) -> bytes:
    body = {
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": os.environ["CARTESIA_VOICE_ID"]},
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
        "language": "en",
    }
    key = os.environ["CARTESIA_API_KEY"]
    req = urllib.request.Request(
        CARTESIA_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "X-API-Key": key,
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def handle(payload: dict) -> tuple[int, dict]:
    """Core logic, shared by the Vercel handler and the local dev server."""
    passcode = os.environ.get("DEMO_PASSCODE")
    if passcode and payload.get("passcode") != passcode:
        return 401, {"error": "passcode"}

    try:
        if payload.get("greeting"):
            reply = GREETING
        else:
            history = clean_history(payload.get("history"))
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return 500, {"error": "ANTHROPIC_API_KEY is not set on the server."}
            reply = ask_mom(history)
    except BadRequest as e:
        return 400, {"error": str(e)}
    except anthropic.RateLimitError:
        return 429, {"error": "Too many requests, give it a few seconds."}
    except anthropic.AuthenticationError:
        return 500, {"error": "ANTHROPIC_API_KEY is missing or invalid."}
    except anthropic.APIStatusError as e:
        return 502, {"error": f"Claude API error ({e.status_code})."}
    except anthropic.APIConnectionError:
        return 502, {"error": "Couldn't reach the Claude API."}

    try:
        audio = base64.b64encode(speak(reply)).decode()
    except (urllib.error.URLError, KeyError) as e:
        # Still return the text so the conversation keeps going without audio.
        return 200, {"reply": reply, "audio": None, "warning": f"voice unavailable: {e}"}
    return 200, {"reply": reply, "audio": audio}


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime entry point."""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(min(length, 200_000)) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            status, body = 400, {"error": "invalid JSON"}
        else:
            try:
                status, body = handle(payload)
            except Exception as e:  # never drop the connection without an answer
                status, body = 500, {"error": f"Server error: {type(e).__name__}"}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
