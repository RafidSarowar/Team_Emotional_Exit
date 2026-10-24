"""Play every voice clip through the same pipeline the backend uses, one at a
time, so you can check the audio by ear without a camera.

    python tools/test_voice.py                    # all scenarios, all clips
    python tools/test_voice.py lingering          # one scenario
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vision_backend.audio_player import AudioPlayer  # noqa: E402
from vision_backend.config import load_config  # noqa: E402
from vision_backend.voice_packs import VoicePackManager  # noqa: E402


def transcripts(packs_dir: Path) -> dict[str, str]:
    """Spoken text per clip, from pack_info.json written by make_cartesia_pack.py."""
    out = {}
    for z in packs_dir.glob("*.zip"):
        try:
            with zipfile.ZipFile(z) as zf:
                info = json.loads(zf.read("pack_info.json"))
            out.update({Path(k).name: v for k, v in info.get("transcripts", {}).items()})
        except (KeyError, ValueError, zipfile.BadZipFile):
            pass
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenarios", nargs="*", help="scenario ids to play (default: all)")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = p.parse_args()

    cfg = load_config(args.config)
    packs_dir = cfg.resolve(cfg.audio.voice_packs_dir)
    packs = VoicePackManager(packs_dir, [r.audio_key for r in cfg.scenarios])
    packs.rescan()
    player = AudioPlayer(cfg.audio.backend)
    print(f"Audio backend: {player.backend_name}")
    if player.backend_name == "none":
        raise SystemExit("No audio backend available, so nothing can be played.")

    texts = transcripts(packs_dir)
    rules = [r for r in cfg.scenarios if not args.scenarios or r.id in args.scenarios]
    for rule in rules:
        clips = packs.index.get(rule.audio_key, [])
        print(f"\n== {rule.id} ({len(clips)} clip{'s' if len(clips) != 1 else ''}) ==")
        if not clips:
            print("   (no clips found)")
        for clip in clips:
            print(f"   playing {clip.pack}/{clip.path.name}")
            if clip.path.name in texts:
                print(f'   should say: "{texts[clip.path.name]}"')
            result = player.play(clip.path)
            if result != "played":
                print(f"   -> {result}")
                continue
            while player.busy():
                time.sleep(0.05)
            time.sleep(0.4)
    player.stop()
    print("\nDone.")


if __name__ == "__main__":
    main()
