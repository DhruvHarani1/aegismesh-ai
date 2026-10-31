"""Launch all five mesh nodes as local processes (single-machine test mode).

    python run_local.py [mesh.json]

On the real 5-laptop setup, instead run on each laptop:
    python -m aegismesh.node --config mesh.json --name <A..E>
with the `host` fields in mesh.json set to each laptop's LAN IP.
"""

import os
import signal
import subprocess
import sys
import time

cfg_path = sys.argv[1] if len(sys.argv) > 1 else "mesh.json"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aegismesh.config import MeshConfig  # noqa: E402

cfg = MeshConfig(cfg_path)
procs = [subprocess.Popen([sys.executable, "-u", "-m", "aegismesh.node", "--config", cfg_path, "--name", n])
         for n in cfg.nodes]
print("started nodes:", ", ".join(cfg.nodes), "- Ctrl+C to stop", flush=True)
try:
    while all(p.poll() is None for p in procs):
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
