"""Motion detection with background subtraction (MOG2), frame differencing, or both."""

from __future__ import annotations

import cv2
import numpy as np

from .config import MotionConfig
from .datatypes import Box


class MotionDetector:
    """Produces a binary motion mask per frame plus summary stats.

    Methods:
      mog2:   adaptive background model. Catches anything that differs from the
              learned background, including objects that stopped moving recently.
      diff:   absolute difference against the previous frame. Catches only what
              changed since the last frame, so it ignores slow lighting drift.
      hybrid: MOG2 AND diff. Only pixels that are both foreground and changing
              right now count, which cuts false positives from either method alone.
    """

    def __init__(self, cfg: MotionConfig):
        if cfg.method not in ("mog2", "diff", "hybrid"):
            raise ValueError(f"Unknown motion method: {cfg.method}")
        self.cfg = cfg
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=cfg.mog2_history,
            varThreshold=cfg.mog2_var_threshold,
            detectShadows=True,
        )
        self._prev_gray: np.ndarray | None = None
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._frames_seen = 0
        self.mask: np.ndarray | None = None  # last cleaned mask at process resolution
        self._scale = 1.0                    # full-frame px per process px

    @property
    def warmed_up(self) -> bool:
        return self._frames_seen > self.cfg.warmup_frames

    def process(self, frame: np.ndarray) -> tuple[float, list[Box]]:
        """Update with a new BGR frame. Returns (motion_ratio, motion_boxes)."""
        h, w = frame.shape[:2]
        pw = min(self.cfg.process_width, w)
        self._scale = w / pw
        small = cv2.resize(frame, (pw, int(h / self._scale)), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)

        mask = None
        if self.cfg.method in ("mog2", "hybrid"):
            fg = self._bg.apply(small)
            # MOG2 marks shadows as 127; keep only confident foreground (255).
            _, mask = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)

        if self.cfg.method in ("diff", "hybrid"):
            if self._prev_gray is None or self._prev_gray.shape != gray.shape:
                diff_mask = np.zeros_like(gray)
            else:
                delta = cv2.absdiff(self._prev_gray, gray)
                _, diff_mask = cv2.threshold(delta, self.cfg.diff_threshold, 255, cv2.THRESH_BINARY)
                # Frame differencing yields thin edges; dilate so it overlaps MOG2 blobs.
                diff_mask = cv2.dilate(diff_mask, self._kernel, iterations=2)
            mask = diff_mask if mask is None else cv2.bitwise_and(mask, diff_mask)
        self._prev_gray = gray
        self._frames_seen += 1

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        mask = cv2.dilate(mask, self._kernel, iterations=2)

        boxes: list[Box] = []
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        clean = np.zeros_like(mask)
        for c in contours:
            if cv2.contourArea(c) < self.cfg.min_blob_area:
                continue
            cv2.drawContours(clean, [c], -1, 255, thickness=cv2.FILLED)
            x, y, bw, bh = cv2.boundingRect(c)
            s = self._scale
            boxes.append((int(x * s), int(y * s), int((x + bw) * s), int((y + bh) * s)))

        if not self.warmed_up:
            clean[:] = 0
            boxes = []
        self.mask = clean
        ratio = float(np.count_nonzero(clean)) / clean.size
        return ratio, boxes

    def region_ratio(self, box: Box) -> float:
        """Share of moving pixels inside a full-frame box, using the last mask."""
        if self.mask is None:
            return 0.0
        s = self._scale
        mh, mw = self.mask.shape
        x1 = max(0, min(mw, int(box[0] / s)))
        y1 = max(0, min(mh, int(box[1] / s)))
        x2 = max(0, min(mw, int(box[2] / s)))
        y2 = max(0, min(mh, int(box[3] / s)))
        if x2 <= x1 or y2 <= y1:
            return 0.0
        region = self.mask[y1:y2, x1:x2]
        return float(np.count_nonzero(region)) / region.size
