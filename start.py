"""One-command launcher: 5 mesh nodes + NetOps agent + dashboard (single machine).

    python start.py                 # start everything, open the dashboard
    python start.py --no-browser    # don't open a browser tab
    python start.py --no-stream     # don't auto-start the A -> E test stream

Ctrl+C stops every process.
"""

import argparse
import os
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from aegismesh.chaos_injector import Chaos  # noqa: E402
from aegismesh.config import MeshConfig  # noqa: E402
from aegismesh.mesh_tools import MeshClient  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--config", default=os.path.join(ROOT, "mesh.json"))
ap.add_argument("--port", type=int, default=8080)
ap.add_argument("--no-browser", action="store_true")
ap.add_argument("--no-stream", action="store_true")
a = ap.parse_args()

cfg = MeshConfig(a.config)
os.makedirs(os.path.join(ROOT, "runtime"), exist_ok=True)
py = [sys.executable, "-u"]
procs = [subprocess.Popen(py + ["-m", "aegismesh.node", "--config", a.config, "--name", n],
                          cwd=ROOT, stdout=subprocess.DEVNULL) for n in cfg.nodes]
time.sleep(3)  # let the mesh converge before the agent starts watching
procs.append(subprocess.Popen(py + ["-m", "aegismesh.netops_agent", "--config", a.config], cwd=ROOT,
                              stdout=open(os.path.join(ROOT, "runtime", "agent.log"), "w")))
procs.append(subprocess.Popen(py + [os.path.join("dashboard", "server.py"), "--config", a.config,
                                    "--port", str(a.port)], cwd=ROOT, stdout=subprocess.DEVNULL))
time.sleep(1.5)
if not a.no_stream:
    src, dst = "A", cfg.observer
    Chaos(MeshClient(cfg)).stream(src, dst)
url = "http://localhost:%d" % a.port
print("AegisMesh running: %d nodes + agent + dashboard\n  dashboard: %s\n  Ctrl+C to stop" % (len(cfg.nodes), url), flush=True)
if not a.no_browser:
    webbrowser.open(url)
try:
    reported = set()
    while True:
        for p in procs:  # a crashed/killed node is reported but never takes the rest down
            if p.poll() is not None and p.pid not in reported:
                reported.add(p.pid)
                print("process %d exited (code %s); the rest keeps running" % (p.pid, p.returncode), flush=True)
        if len(reported) == len(procs):
            break
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    time.sleep(0.5)
    for p in procs:
        if p.poll() is None:
            p.kill()
