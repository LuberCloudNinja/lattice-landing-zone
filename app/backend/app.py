#!/usr/bin/env python3
"""Placeholder app-tier backend -- stands in for config.WEBAPP_SOURCE (SPEC.md
Section 0), which has not been set yet. Stdlib-only (no dependencies to
install at boot beyond Python itself), run three ways from this one
container image: as threetier_stack.py's AppFunction (Lambda, via the AWS
Lambda Web Adapter -- see the Dockerfile), and as the raw process behind
lattice_stack.py's INSTANCE/IP/ALB target-group demos (via user-data on
LatticeInstanceTargetHost).

Listens on PORT (see threetier_stack.py -- must match APP_TIER_PORT there).
Replace this whole directory with the real app once WEBAPP_SOURCE is set.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming convention)
        if self.path == "/api/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(200, {"message": "placeholder app-tier backend", "path": self.path})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"placeholder backend listening on :{PORT}")
    server.serve_forever()
