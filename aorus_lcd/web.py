"""Local web UI (stdlib only), served by the daemon on 127.0.0.1.

Protection model: it only binds to loopback; it rejects requests whose Host
header is not the loopback address (DNS rebinding), and every POST must carry
an `X-Aorus-Lcd: 1` header, which a web page on another origin cannot add
without a CORS preflight this server never approves. It grants nothing a
local program could not already do: /dev/nvidiactl is world-accessible.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .controller import Busy
from .transport import TransportError

API_HEADER = "X-Aorus-Lcd"


def _page():
    return resources.files("aorus_lcd").joinpath("static/index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "aorus-lcd"
    ctl = None
    max_body = 32 << 20

    def log_message(self, *args):          # keep the journal quiet
        pass

    # -- helpers ---------------------------------------------------------------------
    def _allowed_host(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _send(self, code, body=b"", mime="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.max_body:
            raise ValueError(f"upload too large (limit {self.max_body >> 20} MB)")
        return self.rfile.read(length)

    def _json(self):
        data = json.loads(self._body() or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # -- routes ------------------------------------------------------------------------
    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, {"error": "forbidden host"})
        if self.path in ("/", "/index.html"):
            return self._send(200, _page(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, self.ctl.status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed_host() or self.headers.get(API_HEADER) != "1":
            return self._send(403, {"error": "forbidden"})
        ctl = self.ctl
        try:
            if self.path == "/api/media":
                return self._send(200, ctl.save_media(self.headers.get("X-Filename", ""), self._body()))
            data = self._json()
            if self.path == "/api/preview":
                image, mime, seconds = ctl.preview(data.get("content", {}))
                return self._send(200, image, mime, {"X-Estimate-Seconds": f"{seconds:.1f}"})
            if self.path == "/api/content":
                ctl.start_content(data.get("content", {}))
                return self._send(202, {"ok": True})
            if self.path == "/api/overlay":
                ctl.set_overlay(data.get("overlay", {}))
            elif self.path == "/api/lighting":
                ctl.set_lighting(data.get("lighting", {}))
            elif self.path == "/api/power":
                ctl.set_power(bool(data.get("on")))
            elif self.path == "/api/reset":
                ctl.reset()
            elif self.path == "/api/presets":
                action, name = data.get("action"), str(data.get("name", ""))
                {"save": ctl.save_preset, "apply": ctl.apply_preset, "delete": ctl.delete_preset}[action](name)
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, ctl.status())
        except Busy as e:
            self._send(409, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            self._send(400, {"error": str(e)})
        except (TransportError, OSError) as e:
            self._send(503, {"error": str(e)})


def serve(ctl, bind="127.0.0.1", port=5090, max_upload_mb=32):
    """Start the UI in a background thread; returns the server (call .shutdown())."""
    handler = type("BoundHandler", (Handler,), {"ctl": ctl, "max_body": max_upload_mb << 20})
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="aorus-lcd-web", daemon=True).start()
    return server
