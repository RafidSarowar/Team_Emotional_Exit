"""Plain data types passed between the detectors and the scenario engine."""

from __future__ import annotations

from dataclasses import dataclass, field

Box = tuple[int, int, int, int]  # x1, y1, x2, y2 in full-frame pixels


@dataclass
class Detection:
    label: str
    confidence: float
    box: Box
    motion: float = 0.0  # share of moving pixels inside the box (filled in by the motion stage)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "box": list(self.box),
            "motion": round(self.motion, 4),
        }


@dataclass
class Observation:
    """Everything the scenario engine needs to know about one frame."""

    timestamp: float
    detections: list[Detection] = field(default_factory=list)
    motion_ratio: float = 0.0          # share of moving pixels in the whole frame
    motion_boxes: list[Box] = field(default_factory=list)

    def labels(self) -> set[str]:
        return {d.label for d in self.detections}
