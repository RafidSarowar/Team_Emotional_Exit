"""Clone a voice with Cartesia from a short recording, and print its voice id.

Only clone a voice with that person's clear permission.

Recording tips: 10-30 seconds of them talking naturally, one speaker, a quiet room,
no music. A phone voice memo is fine (.wav, .mp3, .m4a, .webm).

    python tools/clone_voice.py mom.m4a --name "Mom"

Then use the printed id as that person's "voice" in web/api/_people.py, or paste it
into "Have a cloned voice?" when adding someone in the app.

You can also clone in the browser at https://play.cartesia.ai (Voices -> Clone).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.cartesia.ai"
API_VERSION = "2025-04-16"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        + file_path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def clone(api_key: str, clip: Path, name: str, language: str) -> dict:
    body, content_type = multipart(
        {"name": name, "language": language, "mode": "similarity", "enhance": "true",
         "description": "Cloned with permission for the Nearby demo"},
        "clip", clip,
    )
    req = urllib.request.Request(
        f"{API}/voices/clone", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "Cartesia-Version": API_VERSION,
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Cartesia clone failed ({e.code}): {e.read().decode(errors='replace')}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("clip", type=Path, help="audio recording of the voice to clone")
    p.add_argument("--name", default="Mom")
    p.add_argument("--language", default="en")
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("CARTESIA_API_KEY")
    if not api_key:
        raise SystemExit("CARTESIA_API_KEY is not set (put it in .env)")
    if not args.clip.is_file():
        raise SystemExit(f"File not found: {args.clip}")

    voice = clone(api_key, args.clip, args.name, args.language)
    voice_id = voice.get("id")
    if not voice_id:
        raise SystemExit(f"Unexpected response: {voice}")
    print(f"Cloned voice '{voice.get('name', args.name)}': {voice_id}")
    print("Use it as the person's \"voice\" in web/api/_people.py, or paste it into")
    print('"Have a cloned voice?" when adding someone in the app.')


if __name__ == "__main__":
    sys.exit(main())
