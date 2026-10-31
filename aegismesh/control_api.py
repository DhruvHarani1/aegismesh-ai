"""Per-node HTTP/JSON control + telemetry API (consumed by the AI agent, the
dashboard and the chaos injector). Guarded by a shared token header."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def serve(node, host, port):
    cfg = node.cfg

    def nid(x):
        return cfg.ident(x)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            if cfg.token and self.headers.get("X-Aegis-Token") != cfg.token:
                self._send(401, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/status":
                self._send(200, node.status())
            elif u.path == "/wire":
                self._send(200, node.wire_dump(int(q.get("n", ["25"])[0])))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                self._send(200, self.route(urlparse(self.path).path, body))
            except (KeyError, ValueError) as e:
                self._send(400, {"error": "bad request: %r" % (e,)})

        def route(self, path, b):
            if path == "/chaos":
                nb = b.get("neighbor", "*")
                node.set_chaos("*" if nb == "*" else nid(nb), b.get("loss", 0.0),
                               b.get("delay_ms", 0), b.get("corrupt", 0.0))
                return {"ok": True}
            if path == "/cost":
                node.set_cost(nid(b["neighbor"]), b.get("cost"))
                return {"ok": True}
            if path == "/quarantine":
                return {"ok": True, "new": node.do_quarantine(nid(b["node"]), b.get("reason", "manual"))}
            if path == "/unquarantine":
                return {"ok": node.do_unquarantine(nid(b["node"]))}
            if path == "/infect":
                node.rogue = bool(b.get("on", True))
                node.log("ROGUE MODE %s" % ("ON" if node.rogue else "off"))
                return {"ok": True, "rogue": node.rogue}
            if path == "/stream":
                if not b.get("on", True):
                    node.stop_stream()
                else:
                    node.start_stream(nid(b["dst"]), b.get("count", 0), b.get("interval_ms", 50), b.get("size", 64))
                return {"ok": True}
            if path == "/send":
                ok = node.send_app(nid(b["dst"]), b.get("port", 5001), b.get("text", "").encode())
                return {"ok": ok}
            raise KeyError("unknown endpoint " + path)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
