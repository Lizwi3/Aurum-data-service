"""Run the site locally without Vercel:  python dev_server.py   ->  http://127.0.0.1:8787
Serves index.html and GET /api/agsi-latest (real sources; set FRED_API_KEY first)."""
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from agsi_service import service

ROOT = os.path.dirname(os.path.abspath(__file__))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/agsi-latest":
            try:
                body, code = service.latest_json().encode(), 200
            except Exception:
                body, code = b'{"error":"The data service failed. Try again shortly."}', 500
            self.send_response(code)
            for k, v in service.headers(self.headers.get("Origin")).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        elif self.path.split("?")[0] in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
        else:
            self.send_error(404)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8787"))
    print(f"AURUM on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
