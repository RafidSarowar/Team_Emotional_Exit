"""Run the web app locally: python web/dev_server.py  ->  http://localhost:8000

Serves web/public and routes POST /api/talk to the same code Vercel runs.
Reads API keys from the repo's .env file.
"""

from __future__ import annotations

import json
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")
sys.path.insert(0, str(WEB / "api"))
import talk  # noqa: E402


class DevHandler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/talk":
            self.send_error(404)
            return
        talk.handler.do_POST(self)


if __name__ == "__main__":
    missing = [k for k in ("ANTHROPIC_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID") if not os.environ.get(k)]
    if missing:
        print(f"Warning: missing in .env: {', '.join(missing)}")
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(DevHandler, directory=str(WEB / "public")))
    print(f"Call Mom running at http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
