"""Generate a test voice pack of short beeps (one pitch per scenario), so the
pipeline can be tested before the real Cartesia clips are ready.

    python tools/make_placeholder_pack.py            -> voice_packs/placeholder_pack.zip
"""

from __future__ import annotations

import argparse
import io
import math
import struct
import sys
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vision_backend.config import load_config  # noqa: E402


def tone_wav(freq: float, beeps: int, rate: int = 22050) -> bytes:
    """`beeps` short sine beeps at `freq` Hz, as an in-memory 16-bit mono WAV."""
    samples = []
    for _ in range(beeps):
        n = int(rate * 0.18)
        for i in range(n):
            fade = min(1.0, i / 400, (n - i) / 400)  # avoid clicks
            samples.append(int(12000 * fade * math.sin(2 * math.pi * freq * i / rate)))
        samples.extend([0] * int(rate * 0.12))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--out", help="output zip (default: <voice_packs_dir>/placeholder_pack.zip)")
    args = p.parse_args()

    cfg = load_config(args.config)
    out = Path(args.out) if args.out else cfg.resolve(cfg.audio.voice_packs_dir) / "placeholder_pack.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.tmp")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, rule in enumerate(cfg.scenarios):
            # Scenario 1 = 1 high beep, scenario 2 = 2 mid beeps, and so on.
            zf.writestr(f"{rule.audio_key}/beep.wav", tone_wav(880 / (1 + 0.25 * i), beeps=i + 1))
    tmp.replace(out)  # atomic, so the backend never sees a half-written zip
    print(f"Wrote {out} with clips for: {[r.audio_key for r in cfg.scenarios]}")


if __name__ == "__main__":
    main()
