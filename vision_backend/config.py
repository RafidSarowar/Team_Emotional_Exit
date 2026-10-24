"""Typed configuration loaded from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


@dataclass
class VideoConfig:
    source: int | str = 0
    width: int = 1280
    height: int = 720
    show_window: bool = True
    loop_file: bool = False


@dataclass
class YoloConfig:
    model: str = "yolov8n.pt"
    confidence: float = 0.45
    device: str | None = None
    imgsz: int = 640
    classes: list[str] = field(default_factory=list)


@dataclass
class MotionConfig:
    method: str = "hybrid"
    process_width: int = 320
    mog2_history: int = 300
    mog2_var_threshold: float = 25
    diff_threshold: int = 25
    min_blob_area: int = 150
    warmup_frames: int = 30


@dataclass
class EngineConfig:
    release_seconds: float = 1.5


@dataclass
class ScenarioRule:
    id: str
    priority: int = 100
    description: str = ""
    require_any: list[str] = field(default_factory=list)
    require_all: list[str] = field(default_factory=list)
    forbid_any: list[str] = field(default_factory=list)
    min_count: int = 1
    motion_scope: str = "global"
    motion_min: float | None = None
    motion_max: float | None = None
    hold_seconds: float = 0.5
    cooldown_seconds: float = 10.0
    audio: str | None = None

    @property
    def audio_key(self) -> str:
        return self.audio or self.id


@dataclass
class AudioConfig:
    enabled: bool = True
    backend: str = "auto"
    voice_packs_dir: str = "voice_packs"
    rescan_seconds: float = 5.0
    selection: str = "round_robin"
    interrupt: bool = False


@dataclass
class StateConfig:
    dir: str = "runtime"
    heartbeat_seconds: float = 0.5


@dataclass
class AppConfig:
    video: VideoConfig
    yolo: YoloConfig
    motion: MotionConfig
    engine: EngineConfig
    scenarios: list[ScenarioRule]
    audio: AudioConfig
    state: StateConfig
    base_dir: Path = Path(".")

    def resolve(self, relative: str) -> Path:
        """Resolve a config path relative to the config file's directory."""
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p).resolve()


def _build(cls, data: dict[str, Any] | None):
    """Instantiate a dataclass, rejecting unknown keys so typos fail loudly."""
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path).resolve()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    scenarios = [_build(ScenarioRule, s) for s in raw.get("scenarios", [])]
    if not scenarios:
        raise ValueError("config.yaml must define at least one scenario")
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Scenario ids must be unique, got {ids}")
    for s in scenarios:
        if s.motion_scope not in ("global", "target"):
            raise ValueError(f"Scenario {s.id}: motion_scope must be 'global' or 'target'")
        if s.motion_scope == "target" and not (s.require_any or s.require_all):
            raise ValueError(f"Scenario {s.id}: motion_scope 'target' needs require_any/require_all")

    return AppConfig(
        video=_build(VideoConfig, raw.get("video")),
        yolo=_build(YoloConfig, raw.get("yolo")),
        motion=_build(MotionConfig, raw.get("motion")),
        engine=_build(EngineConfig, raw.get("engine")),
        scenarios=sorted(scenarios, key=lambda s: s.priority),
        audio=_build(AudioConfig, raw.get("audio")),
        state=_build(StateConfig, raw.get("state")),
        base_dir=path.parent,
    )
