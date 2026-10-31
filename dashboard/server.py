"""Observability dashboard server (stdlib only): static UI + SSE telemetry + chaos bridge.

    python dashboard/server.py --config mesh.json --port 8080

SSE (text/event-stream) carries the live telemetry stream; the browser needs no
WebSocket library. Chaos / stream buttons POST to /api/action.
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aegismesh.chaos_injector import Chaos  # noqa: E402
from aegismesh.config import MeshConfig  # noqa: E402
from aegismesh.mesh_tools import MeshClient  # noqa: E402


def make_handler(cfg, client, chaos, trace_path):
    index = open(os.path.join(ROOT, "dashboard", "index.html"), "rb").read()

    def read_trace(offset):
        if not os.path.exists(trace_path):
            return [], 0
        with open(trace_path, "r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            end = f.tell()
        lines = []
        for ln in data.splitlines():
            try:
                lines.append(json.loads(ln))
            except ValueError:
                pass
        return lines, end

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(index)))
                self.end_headers()
                self.wfile.write(index)
            elif self.path == "/config":
                body = json.dumps({"nodes": cfg.nodes, "links": cfg.links, "observer": cfg.observer}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/events"):
                self.stream()
            else:
                self.send_error(404)

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            offset = 0
            first = True
            try:
                while True:
                    statuses = client.collect()
                    obs = cfg.observer
                    wire = client.call(obs, "/wire?n=14")
                    lines, offset = read_trace(offset)
                    if first:
                        lines = lines[-60:]
                        first = False
                    msg = {"t": time.time(), "statuses": statuses,
                           "wire": wire if isinstance(wire, list) else [], "agent": lines}
                    self.wfile.write(("data: %s\n\n" % json.dumps(msg)).encode())
                    self.wfile.flush()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self):
            if self.path != "/api/action":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            act, nodes = req.get("action"), [str(x).upper() for x in req.get("nodes", [])]
            try:
                res = {
                    "sever": lambda: chaos.sever(*nodes[:2]),
                    "heal": lambda: chaos.heal(*nodes[:2]),
                    "infect": lambda: chaos.infect(nodes[0]),
                    "cure": lambda: chaos.infect(nodes[0], False),
                    "release": lambda: client.release_node(nodes[0]),
                    "stream": lambda: chaos.stream(nodes[0], nodes[1]),
                    "stop": lambda: chaos.stream(nodes[0], nodes[0], False),
                }[act]()
                body = json.dumps({"ok": True, "result": res}).encode()
                code = 200
            except (KeyError, IndexError) as e:
                body, code = json.dumps({"ok": False, "error": repr(e)}).encode(), 400
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "mesh.json"))
    ap.add_argument("--runtime", default=os.path.join(ROOT, "runtime"))
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    cfg = MeshConfig(a.config)
    client = MeshClient(cfg)
    h = make_handler(cfg, client, Chaos(client), os.path.join(a.runtime, "agent_trace.jsonl"))
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), h)
    srv.daemon_threads = True
    print("dashboard on http://localhost:%d" % a.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
