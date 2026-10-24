"""Entry point: python main.py [--config config.yaml] [--source 0] [--no-window] [--no-audio]"""

from __future__ import annotations

import argparse
import logging

from vision_backend.app import VisionApp
from vision_backend.config import load_config
from vision_backend.yolo_detector import AsyncDetector, YoloDetector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YOLO + motion vision backend with Cartesia voice triggers")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--source", help="override video.source (webcam index or video file path)")
    p.add_argument("--no-window", action="store_true", help="run headless (no OpenCV window)")
    p.add_argument("--no-audio", action="store_true", help="don't play audio locally; only log state")
    p.add_argument("--max-seconds", type=float, help="stop after N seconds (useful for testing)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    if args.source is not None:
        cfg.video.source = int(args.source) if args.source.isdigit() else args.source
    if args.no_window:
        cfg.video.show_window = False
    if args.no_audio:
        cfg.audio.enabled = False

    logging.info("Loading YOLO model %s ...", cfg.yolo.model)
    detector = AsyncDetector(YoloDetector(cfg.yolo))
    VisionApp(cfg, detector).run(max_seconds=args.max_seconds)


if __name__ == "__main__":
    main()
