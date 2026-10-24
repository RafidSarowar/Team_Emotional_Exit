"""POST /api/clone: turn a short recording of a loved one into a Cartesia voice.

Request:  the recording as the raw body, a 16-bit PCM WAV (the page converts whatever
          was recorded or uploaded), with ?name=<their name>&consent=yes, and the demo
          passcode, if one is set, in the X-Demo-Passcode header.
Response: {"voice": "<voice id>"}

Only copy a voice with that person's permission; the page asks for it before sending.
The recording goes straight to Cartesia and isn't stored here.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from talk import CARTESIA_VERSION, send_json  # noqa: E402

CLONE_URL = "https://api.cartesia.ai/voices/clone"
MIN_SECONDS = 8      # Cartesia needs about 10 s of speech; a little slack for silence trimming
MAX_SECONDS = 65     # the page sends at most 60 s, which is what Sonic 3.6 can learn from
MAX_BYTES = 4_000_000  # Vercel rejects bodies over 4.5 MB; 60 s of 24 kHz mono WAV is ~2.9 MB


class BadRequest(Exception):
    pass


def wav_seconds(data: bytes) -> float:
    """Length of a PCM WAV in seconds; raises BadRequest if it isn't one."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise BadRequest("The recording must be a WAV file.")
    pos, byte_rate = 12, 0
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        if chunk == b"fmt ":
            byte_rate = int.from_bytes(data[pos + 16:pos + 20], "little")
        elif chunk == b"data" and byte_rate:
            return min(size, len(data) - pos - 8) / byte_rate
        pos += 8 + size + (size & 1)
    raise BadRequest("The recording must be a WAV file.")


def clone(name: str, wav: bytes) -> str:
    """Create a private Cartesia voice from the recording and return its id."""
    boundary = uuid.uuid4().hex
    fields = {
        "name": f"HearU · {name}",
        "language": "en",
        "description": "Copied in HearU with the speaker's permission",
    }
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        for key, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="clip"; filename="recording.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    key = os.environ["CARTESIA_API_KEY"]
    req = urllib.request.Request(
        CLONE_URL,
        data=b"".join(parts),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "X-API-Key": key,
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=50) as resp:
        return json.loads(resp.read())["id"]


def handle(query: dict, passcode: str, wav: bytes) -> tuple[int, dict]:
    """Core logic, shared by the Vercel handler and the local dev server."""
    expected = os.environ.get("DEMO_PASSCODE")
    if expected and passcode != expected:
        return 401, {"error": "passcode"}
    if query.get("consent") != "yes":
        return 400, {"error": "Please confirm they said it's okay to copy their voice."}
    name = " ".join(str(query.get("name") or "").split())[:30]
    if not name:
        return 400, {"error": "Add their name first."}
    try:
        seconds = wav_seconds(wav)
    except BadRequest as e:
        return 400, {"error": str(e)}
    if seconds < MIN_SECONDS:
        return 400, {"error": f"That's only {seconds:.0f} seconds. Record at least 10 seconds of them talking."}
    if seconds > MAX_SECONDS:
        return 400, {"error": "Keep the recording under a minute."}
    if not os.environ.get("CARTESIA_API_KEY"):
        return 500, {"error": "CARTESIA_API_KEY is not set on the server."}

    try:
        return 200, {"voice": clone(name, wav)}
    except urllib.error.HTTPError as e:
        if e.code == 402:
            return 402, {"error": "Copying voices needs a paid Cartesia plan."}
        if e.code == 429:
            return 429, {"error": "Too many requests, give it a few seconds."}
        return 502, {"error": f"Cartesia couldn't use that recording ({e.code}). Try a clearer one."}
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return 502, {"error": "Couldn't reach Cartesia. Try again in a moment."}


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime entry point."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BYTES:
            # Read and drop it, so the browser gets this answer instead of a reset connection.
            remaining = min(length, 20_000_000)
            while remaining > 0 and (chunk := self.rfile.read(min(remaining, 65536))):
                remaining -= len(chunk)
            status, body = 413, {"error": "Keep the recording under a minute."}
        else:
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            wav = self.rfile.read(length)
            try:
                status, body = handle(query, self.headers.get("X-Demo-Passcode") or "", wav)
            except Exception as e:  # never drop the connection without an answer
                status, body = 500, {"error": f"Server error: {type(e).__name__}"}
        send_json(self, status, body)
