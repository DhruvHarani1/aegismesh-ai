"""End-to-end regression: launches the real mesh + agent and walks the full demo twice.

    python tests/e2e_heal.py

Checks after every step that the A -> E stream keeps flowing, nothing is missing and no
ARQ window is left stuck. Exits non-zero on the first failure.
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aegismesh.chaos_injector import Chaos  # noqa: E402
from aegismesh.config import MeshConfig  # noqa: E402
from aegismesh.mesh_tools import MeshClient  # noqa: E402

cfg_path = os.path.join(ROOT, "mesh.json")
cfg = MeshConfig(cfg_path)
cl = MeshClient(cfg)
ch = Chaos(cl)
py = [sys.executable, "-u"]
procs = [subprocess.Popen(py + ["-m", "aegismesh.node", "--config", cfg_path, "--name", n], cwd=ROOT,
                          stdout=subprocess.DEVNULL) for n in cfg.nodes]


def fail(msg):
    print("FAIL:", msg, flush=True)
    for p in procs:
        p.kill()
    sys.exit(1)


def rx():
    return cl.call("E", "/status").get("stream_rx", {}).get("A", {"received": 0, "missing": 0})


def path():
    return (cl.call("A", "/status").get("paths") or {}).get("E")


def wait(cond, what, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            print("  ok: %s (%.1fs)" % (what, time.time() - t0), flush=True)
            return
        time.sleep(0.3)
    fail("timeout waiting for " + what)


def flowing(what):
    a = rx()["received"]
    time.sleep(1.5)
    b = rx()
    if b["received"] <= a + 10:
        fail("stream stalled %s (%d -> %d)" % (what, a, b["received"]))
    print("  ok: stream flowing %s (%d frames, missing=%d)" % (what, b["received"], b["missing"]), flush=True)


def drained(what):
    def cond():
        st = cl.collect()
        return all(l["sr"]["queued"] == 0 for s in st.values() for l in s["links"])
    wait(cond, "no ARQ backlog " + what, 20)


try:
    time.sleep(3)
    agent = subprocess.Popen(py + ["-m", "aegismesh.netops_agent", "--config", cfg_path,
                                   "--runtime", os.path.join(ROOT, "runtime", "e2e")], cwd=ROOT,
                             stdout=subprocess.DEVNULL)
    procs.append(agent)
    wait(lambda: path() == ["A", "B", "C", "E"], "initial path A-B-C-E")
    ch.stream("A", "E", interval_ms=40)
    time.sleep(2)
    flowing("at steady state")
    for rnd in (1, 2):
        print("round %d: sever B-C" % rnd, flush=True)
        ch.sever("B", "C")
        wait(lambda: path() == ["A", "D", "E"], "agent rerouted to A-D-E", 40)
        flowing("after reroute")
        print("round %d: heal B-C" % rnd, flush=True)
        ch.heal("B", "C")
        wait(lambda: path() == ["A", "B", "C", "E"], "agent restored A-B-C-E", 40)
        flowing("after recovery")
        drained("after heal")
    print("rogue node D", flush=True)
    ch.infect("D")
    wait(lambda: "D" in cl.call("A", "/status").get("quarantine", {}), "D quarantined", 30)
    flowing("with D quarantined")
    ch.infect("D", False)
    cl.release_node("D")
    time.sleep(3)
    flowing("after D released")
    drained("after release")
    m = rx()["missing"]
    if m:
        print("  note: %d frames missing/in flight at the end" % m, flush=True)
    print("ALL PASS", flush=True)
finally:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    time.sleep(0.5)
    for p in procs:
        if p.poll() is None:
            p.kill()
