"""Render a voice pack with the Cartesia TTS API and drop it into voice_packs/.

Reads CARTESIA_API_KEY (and optionally CARTESIA_VOICE_ID) from the environment
or from .env in the project root. Your teammate's agent can produce packs in
any way it likes; this script is a reference for the expected layout.

    python tools/make_cartesia_pack.py                     # uses tools/voice_lines.yaml
    python tools/make_cartesia_pack.py --lines my.yaml --name v2
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vision_backend.config import load_config  # noqa: E402

API = "https://api.cartesia.ai"
API_VERSION = "2025-04-16"
MODEL_ID = "sonic-2"


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines); real env vars take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request(method: str, path: str, api_key: str, body: dict | None = None) -> bytes:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "Cartesia-Version": API_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Cartesia API error {e.code} on {path}: {e.read().decode(errors='replace')}")


def pick_voice(api_key: str) -> str:
    data = json.loads(request("GET", "/voices?limit=20", api_key))
    voices = data.get("data", data) if isinstance(data, dict) else data
    if not voices:
        raise SystemExit("No voices returned by the Cartesia API; set CARTESIA_VOICE_ID in .env")
    english = [v for v in voices if v.get("language", "en") == "en"] or voices
    print(f"Using voice: {english[0].get('name')} ({english[0]['id']})")
    return english[0]["id"]


def tts(api_key: str, voice_id: str, text: str) -> bytes:
    return request("POST", "/tts/bytes", api_key, {
        "model_id": MODEL_ID,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 44100},
        "language": "en",
    })


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--lines", default=str(ROOT / "tools" / "voice_lines.yaml"))
    p.add_argument("--name", default="cartesia_pack", help="zip file name (without .zip)")
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("CARTESIA_API_KEY")
    if not api_key:
        raise SystemExit("CARTESIA_API_KEY is not set (put it in .env)")
    voice_id = os.environ.get("CARTESIA_VOICE_ID") or pick_voice(api_key)

    cfg = load_config(args.config)
    lines: dict[str, list[str]] = yaml.safe_load(Path(args.lines).read_text(encoding="utf-8"))
    keys = [r.audio_key for r in cfg.scenarios]
    unknown = set(lines) - set(keys)
    if unknown:
        print(f"Warning: lines for unknown audio keys {sorted(unknown)} (config has {keys})")

    out = cfg.resolve(cfg.audio.voice_packs_dir) / f"{args.name}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.tmp")
    manifest = {"voice_id": voice_id, "model_id": MODEL_ID, "transcripts": {}}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, texts in lines.items():
            texts = [texts] if isinstance(texts, str) else texts
            for i, text in enumerate(texts, 1):
                name = f"{key}/{key}_{i:02d}.wav"
                print(f"Rendering {name}: {text!r}")
                zf.writestr(name, tts(api_key, voice_id, text))
                manifest["transcripts"][name] = text
        zf.writestr("pack_info.json", json.dumps(manifest, indent=2))
    tmp.write_bytes(buf.getvalue())
    tmp.replace(out)  # atomic, so the backend never picks up a half-written zip
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
