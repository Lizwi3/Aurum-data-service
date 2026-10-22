"""Vercel Python function: GET /api/agsi-latest"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agsi_service import service  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _send(self, status, body=b""):
        self.send_response(status)
        for k, v in service.headers(self.headers.get("Origin")).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        try:
            self._send(200, service.latest_json().encode("utf-8"))
        except Exception:  # never leak internals
            self._send(500, b'{"error":"The data service failed. Try again shortly."}')
