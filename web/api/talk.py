"""POST /api/talk: the user's words in, their loved one's reply out as text + speech.

Request:  {"person": "mom",                      a preset id from _people.PEOPLE, or
           "custom": {"name", "relationship",     someone added in the browser
                      "calls_you", "details", "voice"},
           "history": [{"role": "user"|"assistant", "content": "..."}, ...],
           "image": "<base64 jpeg>",              optional camera still for the newest message
           "greeting": true,                      (instead of history) start the call
           "tzOffset": 420,                       the browser's getTimezoneOffset(), in minutes,
                                                  so they know what time it is for the user
           "passcode": "..."}
          (the last history item must be the new user message)
Response: {"reply": "...", "audio": "<base64 mp3>"}

Environment variables:
  ANTHROPIC_API_KEY   Claude API key (required)
  CARTESIA_API_KEY    Cartesia API key (required)
  DEMO_PASSCODE       optional; if set, requests must include it (stops strangers
                      from spending your API credits)
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
from _people import PEOPLE, BadPerson, custom_person, greeting, system_prompt  # noqa: E402

MODEL = "claude-opus-5-5"
MAX_TURNS = 20          # history sent to Claude
MAX_CHARS = 1000        # per message
MAX_REPLY_CHARS = 600   # spoken replies are short; this caps Cartesia credits per reply
MAX_IMAGE_B64 = 700_000  # camera stills are ~50 KB; this rejects anything unreasonable
MAX_BODY = 1_000_000
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2026-08-14"
CARTESIA_MODEL = "sonic-3.6"   # about twice as fast as sonic-2 here, and supports per-request speed

_client: anthropic.Anthropic | None = None
_greeting_audio: dict[tuple[str, float, str], str] = {}  # (voice, speed, text) -> base64 mp3


class BadRequest(Exception):
    pass


def local_now(tz_offset) -> datetime | None:
    """The user's local time: our clock, shifted by the browser's UTC offset.

    JavaScript reports the offset as UTC minus local time, in minutes (420 in California
    in summer, -180 in Istanbul). Anything else is ignored rather than refused.
    """
    if isinstance(tz_offset, bool) or not isinstance(tz_offset, (int, float)):
        return None
    if not -840 <= tz_offset <= 840:
        return None
    return datetime.now(timezone.utc) - timedelta(minutes=tz_offset)


def resolve_person(payload: dict) -> dict:
    if payload.get("custom") is not None:
        try:
            return custom_person(payload["custom"])
        except BadPerson as e:
            raise BadRequest(str(e)) from None
    person = PEOPLE.get(payload.get("person") or "mom")
    if person is None:
        raise BadRequest("unknown person")
    return person


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


def clean_image(raw) -> str | None:
    """Accept an optional camera still: a base64 JPEG. It's sent to Claude, never stored."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or len(raw) > MAX_IMAGE_B64:
        raise BadRequest("image must be a base64 JPEG under 500 KB")
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        raise BadRequest("image must be a base64 JPEG") from None
    if not data.startswith(b"\xff\xd8\xff"):
        raise BadRequest("image must be a JPEG")
    return raw


def shorten(text: str) -> str:
    """Cut an unusually long reply at the last full sentence that fits."""
    if len(text) <= MAX_REPLY_CHARS:
        return text
    cut = text[:MAX_REPLY_CHARS]
    ends = [m.end() for m in re.finditer(r"[.!?](\s|$)", cut)]
    return cut[:ends[-1]].strip() if ends else cut.rstrip() + "…"


def ask(person: dict, history: list[dict], image: str | None = None, now: datetime | None = None) -> str:
    global _client
    _client = _client or anthropic.Anthropic()
    messages = history
    if image:
        # The photo goes with the newest message only, like a glance during a video call.
        newest = history[-1]["content"]
        messages = history[:-1] + [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
            {"type": "text", "text": newest},
        ]}]
    response = _client.beta.messages.create(
        model=MODEL,
        # Thinking counts toward max_tokens, so leave room for it.
        max_tokens=16000,
        # Medium keeps the pause before they answer short. Higher effort was slower
        # and didn't noticeably improve these short, spoken replies.
        output_config={"effort": "medium"},
        # If a safety classifier declines, retry on a fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system_prompt(person, now),
        messages=messages,
    )
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "refusal" or not text:
        name = f", {person['calls_you']}" if person["calls_you"] else ""
        return f"I'm right here with you{name}. Tell me a little more?"
    return shorten(text)


def speak(text: str, voice: str, speed: float = 1.0) -> bytes:
    body = {
        "model_id": CARTESIA_MODEL,
        "transcript": text,
        "voice": {"id": voice},
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
        "language": "en",
        # Each person's own pace (Grandma is slower). Emotion is left for the model
        # to infer from the words, so comfort sounds gentle and a joke sounds light.
        "generation_config": {"speed": speed},
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

    now = local_now(payload.get("tzOffset"))
    try:
        person = resolve_person(payload)
        if payload.get("greeting"):
            reply = greeting(person, now)
        else:
            history = clean_history(payload.get("history"))
            image = clean_image(payload.get("image"))
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return 500, {"error": "ANTHROPIC_API_KEY is not set on the server."}
            reply = ask(person, history, image, now)
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
        # Greetings repeat on every call, so reuse their audio instead of paying for it again.
        speed = person.get("speed", 1.0)
        cache_key = (person["voice"], speed, reply)
        if payload.get("greeting") and cache_key in _greeting_audio:
            audio = _greeting_audio[cache_key]
        else:
            audio = base64.b64encode(speak(reply, person["voice"], speed)).decode()
            if payload.get("greeting"):
                _greeting_audio[cache_key] = audio
    except (urllib.error.URLError, KeyError) as e:
        # Still return the text so the conversation keeps going without audio.
        return 200, {"reply": reply, "audio": None, "warning": f"voice unavailable: {e}"}
    return 200, {"reply": reply, "audio": audio}


def send_json(req: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    req.send_response(status)
    req.send_header("Content-Type", "application/json")
    req.send_header("Content-Length", str(len(data)))
    req.end_headers()
    req.wfile.write(data)


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime entry point."""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            status, body = 400, {"error": "invalid JSON"}
        else:
            try:
                status, body = handle(payload)
            except Exception as e:  # never drop the connection without an answer
                status, body = 500, {"error": f"Server error: {type(e).__name__}"}
        send_json(self, status, body)
