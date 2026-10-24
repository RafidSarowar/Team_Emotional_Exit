import json
import zipfile
from pathlib import Path

import numpy as np

from vision_backend.app import VisionApp
from vision_backend.config import MotionConfig, load_config
from vision_backend.datatypes import Detection
from vision_backend.motion import MotionDetector

ROOT = Path(__file__).resolve().parent.parent


def frame_with_square(x: int, size: int = 80) -> np.ndarray:
    f = np.full((360, 640, 3), 40, dtype=np.uint8)
    f[140:140 + size, x:x + size] = 230
    return f


def test_moving_square_registers_motion_static_does_not():
    det = MotionDetector(MotionConfig(warmup_frames=5))
    for _ in range(40):  # learn a static scene
        ratio, _ = det.process(frame_with_square(100))
    assert ratio == 0.0

    ratios = [det.process(frame_with_square(100 + 15 * i))[0] for i in range(1, 15)]
    assert max(ratios) > 0.005
    assert det.region_ratio((100, 100, 500, 300)) > det.region_ratio((0, 300, 50, 360))


class FakeDetector:
    def __init__(self):
        self.detections = []

    def submit(self, frame):
        pass

    def latest(self):
        return [Detection(d.label, d.confidence, d.box) for d in self.detections], 1e12


class FakePlayer:
    backend_name = "fake"
    muted = False

    def __init__(self):
        self.played = []

    def busy(self):
        return False

    def play(self, path, interrupt=False):
        if self.muted:
            return "muted"
        self.played.append(Path(path).name)
        return "played"

    def stop(self):
        pass


def test_end_to_end_unattended_motion_plays_clip_and_logs(tmp_path):
    cfg = load_config(ROOT / "config.yaml")
    cfg.audio.voice_packs_dir = str(tmp_path / "packs")
    cfg.state.dir = str(tmp_path / "runtime")
    cfg.motion.warmup_frames = 5
    (tmp_path / "packs").mkdir()
    with zipfile.ZipFile(tmp_path / "packs" / "p.zip", "w") as zf:
        zf.writestr("unattended_motion/who_is_there.wav", b"RIFF")

    player = FakePlayer()
    app = VisionApp(cfg, FakeDetector(), player=player)
    app.packs.rescan()

    t = 1000.0
    for _ in range(30):
        app.step(frame_with_square(100, size=140), t)
        t += 0.1
    for i in range(50):  # 5s of a moving blob with no person detected
        app.step(frame_with_square(20 + (20 * i) % 420, size=140), t)
        t += 0.1
    app.shutdown()

    assert player.played == ["who_is_there.wav"]
    events = [json.loads(line) for line in (tmp_path / "runtime" / "events.jsonl").read_text().splitlines()]
    entered = [e for e in events if e["event"] == "scenario_entered"]
    assert entered[0]["scenario"] == "unattended_motion"
    assert entered[0]["playback"] == "played"
    state = json.loads((tmp_path / "runtime" / "state.json").read_text())
    assert state["running"] is False and "unattended_motion" in state["rules"]


def test_control_file_mutes_player(tmp_path):
    cfg = load_config(ROOT / "config.yaml")
    cfg.audio.voice_packs_dir = str(tmp_path / "packs")
    cfg.state.dir = str(tmp_path / "runtime")
    app = VisionApp(cfg, FakeDetector(), player=FakePlayer())
    (tmp_path / "runtime" / "control.json").write_text('{"muted": true}')
    app.step(frame_with_square(100), 1.0)
    assert app.player.muted is True
    app.shutdown()
