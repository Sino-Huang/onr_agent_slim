import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
HTML_FILE = "mission1_progress_update.html"


class MethodologyHandler(SimpleHTTPRequestHandler):
    def _map_root(self):
        if urlsplit(self.path).path == "/":
            self.path = f"/{HTML_FILE}"

    def do_GET(self):
        self._map_root()
        super().do_GET()

    def do_HEAD(self):
        self._map_root()
        super().do_HEAD()


handler = partial(MethodologyHandler, directory=SCRIPT_DIR)

try:
    server = ThreadingHTTPServer(("127.0.0.1", 14399), handler)
except OSError as exc:
    print(
        f"Cannot start methodology pitch: port 14399 on 127.0.0.1 "
        f"is unavailable ({exc}).",
        file=sys.stderr,
    )
    raise SystemExit(1)

print("Serving methodology pitch at http://127.0.0.1:14399/")
print("Press Ctrl+C to stop.")

try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nPresentation server stopped.")
finally:
    server.server_close()
