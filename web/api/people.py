"""GET /api/people: the preset loved ones and the voices offered in "Add someone"."""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _people import PEOPLE, VOICES  # noqa: E402
from talk import send_json  # noqa: E402


def listing() -> dict:
    people = [
        {"id": pid, "name": p["name"], "relationship": p["relationship"]}
        for pid, p in PEOPLE.items()
    ]
    return {"people": people, "voices": VOICES}


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime entry point."""

    def do_GET(self):
        send_json(self, 200, listing())
