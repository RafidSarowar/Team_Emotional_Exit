"""YOLO object detection, run on a background thread so motion detection and the
display loop never wait on inference."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .config import YoloConfig
from .datatypes import Detection

log = logging.getLogger(__name__)


class YoloDetector:
    """Thin wrapper over an Ultralytics YOLO model.

    If your existing script uses a different model or post-processing, keep
    this class's `detect(frame) -> list[Detection]` signature and swap the body.
    """

    def __init__(self, cfg: YoloConfig):
        from ultralytics import YOLO  # imported lazily so tests don't need torch

        self.cfg = cfg
        self.model = YOLO(cfg.model)
        names: dict[int, str] = self.model.names
        name_to_id = {v: k for k, v in names.items()}
        missing = [c for c in cfg.classes if c not in name_to_id]
        if missing:
            raise ValueError(f"Classes not in {cfg.model}: {missing}. Available: {sorted(name_to_id)}")
        self._class_ids = [name_to_id[c] for c in cfg.classes] or None
        self._names = names

    def detect(self, frame: np.ndarray) -> list[Detection]:
        result = self.model.predict(
            frame,
            conf=self.cfg.confidence,
            classes=self._class_ids,
            imgsz=self.cfg.imgsz,
            device=self.cfg.device,
            verbose=False,
        )[0]
        detections = []
        for xyxy, conf, cls in zip(
            result.boxes.xyxy.cpu().numpy(),
            result.boxes.conf.cpu().numpy(),
            result.boxes.cls.cpu().numpy(),
        ):
            x1, y1, x2, y2 = (int(v) for v in xyxy)
            detections.append(Detection(self._names[int(cls)], float(conf), (x1, y1, x2, y2)))
        return detections


class AsyncDetector:
    """Runs any `detect(frame)` callable on a worker thread.

    The main loop calls `submit()` with every frame; the worker always grabs the
    newest one and drops anything older, so detections stay as fresh as the
    model's throughput allows instead of building a backlog.
    """

    def __init__(self, detector):
        self._detector = detector
        self._lock = threading.Lock()
        self._new_frame = threading.Event()
        self._pending: np.ndarray | None = None
        self._latest: list[Detection] = []
        self._latest_at = 0.0
        self.inference_ms = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="yolo-worker", daemon=True)
        self._thread.start()

    def submit(self, frame: np.ndarray) -> None:
        with self._lock:
            self._pending = frame
        self._new_frame.set()

    def latest(self) -> tuple[list[Detection], float]:
        """Most recent detections and the time they were produced."""
        with self._lock:
            return [Detection(d.label, d.confidence, d.box) for d in self._latest], self._latest_at

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._new_frame.wait(timeout=0.1):
                continue
            with self._lock:
                frame, self._pending = self._pending, None
                self._new_frame.clear()
            if frame is None:
                continue
            start = time.perf_counter()
            try:
                dets = self._detector.detect(frame)
            except Exception:
                log.exception("YOLO inference failed")
                continue
            self.inference_ms = (time.perf_counter() - start) * 1000
            with self._lock:
                self._latest, self._latest_at = dets, time.time()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
