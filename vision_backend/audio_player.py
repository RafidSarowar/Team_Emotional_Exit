"""Non-blocking audio playback with pygame (preferred) or sounddevice."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class _PygameBackend:
    name = "pygame"

    def __init__(self):
        import os
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame

        pygame.mixer.init()
        self._music = pygame.mixer.music

    def play(self, path: Path) -> None:
        self._music.load(str(path))
        self._music.play()

    def busy(self) -> bool:
        return self._music.get_busy()

    def stop(self) -> None:
        self._music.stop()
        self._music.unload()


class _SounddeviceBackend:
    name = "sounddevice"

    def __init__(self):
        import sounddevice as sd
        import soundfile as sf

        self._sd, self._sf = sd, sf
        sd.query_devices(kind="output")  # fail fast if there is no output device

    def play(self, path: Path) -> None:
        data, rate = self._sf.read(str(path), dtype="float32")
        self._sd.play(data, rate)

    def busy(self) -> bool:
        stream = self._sd.get_stream()
        return bool(stream and stream.active)

    def stop(self) -> None:
        self._sd.stop()


class AudioPlayer:
    """Plays one clip at a time without blocking the vision loop."""

    def __init__(self, backend: str = "auto"):
        backends = {"pygame": _PygameBackend, "sounddevice": _SounddeviceBackend}
        order = {"auto": ["pygame", "sounddevice"], "none": []}.get(backend, [backend])
        if any(name not in backends for name in order):
            raise ValueError(f"Unknown audio backend: {backend}")
        self._backend = None
        for name in order:
            try:
                self._backend = backends[name]()
                break
            except Exception as e:
                log.warning("Audio backend %s unavailable: %s", name, e)
        if self._backend is None and order:
            log.warning("No audio backend available; voice clips will only be logged")
        self.muted = False

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else "none"

    def busy(self) -> bool:
        return bool(self._backend and self._backend.busy())

    def play(self, path: Path, interrupt: bool = False) -> str:
        """Returns 'played', 'skipped_busy', 'muted', 'no_backend', or 'error'."""
        if self.muted:
            return "muted"
        if self._backend is None:
            return "no_backend"
        if self.busy():
            if not interrupt:
                return "skipped_busy"
            self._backend.stop()
        try:
            self._backend.play(path)
            return "played"
        except Exception as e:
            log.error("Failed to play %s: %s", path, e)
            return "error"

    def stop(self) -> None:
        if self._backend:
            try:
                self._backend.stop()
            except Exception:
                pass
