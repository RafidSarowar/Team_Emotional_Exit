"""Voice pack management: discovers ZIPs/folders dropped into the voice pack
directory, extracts them safely, and resolves a scenario to an audio file.

Pack layout (inside a .zip or a plain folder):

    <pack>/
      manifest.json            optional, see below
      approaching/             one folder per scenario `audio` key
        hello_there.wav
        come_closer.mp3
      lingering/
        ...
      unattended_motion/
        ...

A single wrapper folder at the top of the zip (e.g. from zipping a folder) is fine;
scenario folders are found at any depth.

manifest.json (optional) overrides folder discovery for the scenarios it lists:

    {"scenarios": {"approaching": ["clips/a.wav", "clips/b.wav"]}}

Paths are relative to the manifest. When several packs provide the same
scenario, the most recently modified pack wins, so dropping in a new zip
updates the voice without restarting the backend.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".flac"}
EXTRACT_DIRNAME = ".extracted"
IGNORED_SUFFIXES = {".tmp", ".part", ".partial", ".crdownload"}


@dataclass
class Clip:
    path: Path
    pack: str


class VoicePackManager:
    def __init__(self, root: Path, audio_keys: list[str], selection: str = "round_robin"):
        if selection not in ("round_robin", "random", "first"):
            raise ValueError(f"Unknown clip selection mode: {selection}")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.extract_root = self.root / EXTRACT_DIRNAME
        self.audio_keys = list(dict.fromkeys(audio_keys))
        self.selection = selection
        self.index: dict[str, list[Clip]] = {}
        self._fingerprint: str | None = None
        self._rr: dict[str, int] = {}
        self._last_scan = float("-inf")

    # -- discovery ---------------------------------------------------------

    def _sources(self) -> list[Path]:
        """Zips and folders in the root, newest first."""
        out = []
        for p in self.root.iterdir():
            if p.name.startswith(".") or p.suffix.lower() in IGNORED_SUFFIXES:
                continue
            if p.is_dir() or (p.is_file() and p.suffix.lower() == ".zip"):
                out.append(p)
        return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)

    @staticmethod
    def _stamp(p: Path) -> str:
        if p.is_file():
            st = p.stat()
            return f"{p.name}:{st.st_size}:{st.st_mtime_ns}"
        # For folders, include every file so edits inside are noticed.
        parts = [p.name]
        for f in sorted(p.rglob("*")):
            if f.is_file():
                st = f.stat()
                parts.append(f"{f.relative_to(p)}:{st.st_size}:{st.st_mtime_ns}")
        return "|".join(parts)

    def maybe_rescan(self, now: float, every: float) -> bool:
        """Rescan at most every `every` seconds. Returns True if the index changed."""
        if now - self._last_scan < every:
            return False
        self._last_scan = now
        return self.rescan()

    def rescan(self) -> bool:
        sources = self._sources()
        fingerprint = hashlib.sha1("\n".join(self._stamp(s) for s in sources).encode()).hexdigest()
        if fingerprint == self._fingerprint:
            return False

        index: dict[str, list[Clip]] = {}
        live_dirs: set[Path] = set()
        complete = True
        for src in sources:
            if src.is_dir():
                pack_dir = src
            else:
                try:
                    pack_dir = self._extract(src)
                except (zipfile.BadZipFile, OSError) as e:
                    # Usually a zip that is still being written; retry next scan.
                    log.warning("Skipping voice pack %s: %s", src.name, e)
                    complete = False
                    continue
                live_dirs.add(pack_dir)
            for key, paths in self._scan_pack(pack_dir).items():
                if key not in index and paths:  # newest pack wins per scenario
                    index[key] = [Clip(p, src.name) for p in paths]

        self._cleanup_extracted(live_dirs)
        self.index = index
        self._rr = {}
        # Leaving the fingerprint unset forces a retry for half-written zips.
        self._fingerprint = fingerprint if complete else None
        missing = [k for k in self.audio_keys if k not in index]
        log.info("Voice packs loaded: %s", {k: len(v) for k, v in index.items()})
        if missing:
            log.warning("No clips for scenario(s): %s", missing)
        return True

    def _extract(self, zip_path: Path) -> Path:
        """Extract into .extracted/<stem>-<hash>/. A new zip version gets a new
        folder, so a clip that is currently playing (and locked on Windows) is
        never overwritten."""
        st = zip_path.stat()
        tag = hashlib.sha1(f"{zip_path.name}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:10]
        dest = self.extract_root / f"{zip_path.stem}-{tag}"
        if dest.is_dir():
            return dest

        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                base = tmp.resolve()
                for member in zf.infolist():
                    target = (tmp / member.filename).resolve()
                    # Reject path traversal ("zip slip") entries.
                    if base != target and base not in target.parents:
                        raise zipfile.BadZipFile(f"unsafe path in zip: {member.filename}")
                zf.extractall(tmp)
            tmp.rename(dest)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        log.info("Extracted %s -> %s", zip_path.name, dest.name)
        return dest

    def _cleanup_extracted(self, keep: set[Path]) -> None:
        if not self.extract_root.is_dir():
            return
        for d in self.extract_root.iterdir():
            if d.is_dir() and d not in keep:
                # Fails harmlessly on Windows if a clip inside is still open.
                shutil.rmtree(d, ignore_errors=True)

    def _scan_pack(self, pack_dir: Path) -> dict[str, list[Path]]:
        found: dict[str, list[Path]] = {}
        for d in [pack_dir, *sorted(p for p in pack_dir.rglob("*") if p.is_dir())]:
            if d.name in self.audio_keys and d.name not in found:
                clips = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
                if clips:
                    found[d.name] = clips

        for manifest in sorted(pack_dir.rglob("manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                for key, rels in (data.get("scenarios") or {}).items():
                    rels = [rels] if isinstance(rels, str) else rels
                    paths = [(manifest.parent / r).resolve() for r in rels]
                    paths = [p for p in paths if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
                    if paths:
                        found[key] = paths
            except (OSError, ValueError, AttributeError) as e:
                log.warning("Bad manifest %s: %s", manifest, e)
        return found

    # -- lookup --------------------------------------------------------------

    def pick(self, audio_key: str) -> Clip | None:
        clips = self.index.get(audio_key)
        if not clips:
            return None
        if self.selection == "first":
            return clips[0]
        if self.selection == "random":
            return random.choice(clips)
        i = self._rr.get(audio_key, 0)
        self._rr[audio_key] = i + 1
        return clips[i % len(clips)]

    def summary(self) -> dict[str, list[str]]:
        return {k: [f"{c.pack}/{c.path.name}" for c in v] for k, v in self.index.items()}
