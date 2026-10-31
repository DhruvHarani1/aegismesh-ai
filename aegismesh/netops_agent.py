"""Autonomous NetOps agent: closed-loop ingest -> diagnose -> actuate -> verify -> post-mortem.

The loop is modelled as an explicit state graph (the same node/edge shape you
would hand to LangGraph's StateGraph) with a rule-based reasoner, so it runs
with zero dependencies. All control actions go through the MCP tool contract in
mesh_tools.MeshClient. Reasoning is streamed to runtime/agent_trace.jsonl,
which the dashboard tails as the "AI thought terminal".

    python -m aegismesh.netops_agent --config mesh.json
"""

import argparse
import json
import os
import time

from .anomaly_detector import Anomaly, AnomalyDetector
from .config import MeshConfig
from .dijkstra import path_to, shortest_paths
from .mesh_tools import MeshClient

BLOCKED_COST = 9999


class Trace:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def emit(self, kind, text, **extra):
        rec = {"t": time.time(), "kind": kind, "text": text, **extra}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print("%s [%s] %s" % (time.strftime("%H:%M:%S"), kind, text), flush=True)


class NetOpsAgent:
    def __init__(self, cfg: MeshConfig, runtime_dir="runtime", poll=0.5, watch=("A", "E")):
        self.cfg = cfg
        self.client = MeshClient(cfg)
        self.det = AnomalyDetector()
        self.poll = poll
        self.watch = watch                        # (src, dst) path whose health we verify
        self.rt = runtime_dir
        self.trace = Trace(os.path.join(runtime_dir, "agent_trace.jsonl"))
        self.overridden = {}                      # link key -> original cost
        self.incidents = 0
        self.timeline = []

    # --- graph nodes -------------------------------------------------
    def ingest(self, state):
        state["statuses"] = self.client.collect()
        state["anomalies"] = self.det.evaluate(state["statuses"], self.client)
        return state

    def diagnose(self, state, an: Anomaly):
        st, c = state["statuses"], self.cfg
        if an.kind == "link_degraded":
            a, b = an.subject.split("-")
            t = an.evidence
            self.trace.emit("thought", "Packet loss on link %s exceeded threshold (%.0f%%), rtt var %s, crc %s." % (
                an.subject, t["packet_drop_pct"], t["latency_variance"], t["crc_errors"]))
            graph = self._graph(st)
            alt = {u: {v: w for v, w in nb.items() if {u, v} != {a, b}} for u, nb in graph.items()}
            src, dst = self.watch
            dist, prev = shortest_paths(alt, src)
            path = path_to(prev, src, dst)
            if not path:
                self.trace.emit("thought", "No alternative %s->%s path avoiding %s; leaving link in service." % (
                    src, dst, an.subject))
                return None
            weak = [(x, y) for x, y in zip(path, path[1:]) if self._loss(st, x, y) > 20]
            if weak:
                self.trace.emit("thought", "Alternative path %s has degraded hop(s) %s; not safe." % ("->".join(path), weak))
                return None
            self.trace.emit("thought", "Alternative path %s (cost %s) verified healthy: 0 hops above 20%% loss." % (
                "->".join(path), dist[dst]))
            return {"action": "update_link_cost", "a": a, "b": b, "metric": BLOCKED_COST, "alt": path}
        if an.kind == "rogue_node":
            obs = an.evidence["observers"]
            reasons = {o["reason"] for o in obs if o["reason"]}
            self.trace.emit("thought", "DPI/CRC alarms attributed to node %s by %s (score %d). Reasons: %s" % (
                an.subject, ", ".join(o["observer"] for o in obs), an.evidence["score"], "; ".join(sorted(reasons)) or "n/a"))
            rest = {u: {v: w for v, w in nb.items() if an.subject not in (u, v)}
                    for u, nb in self._graph(st).items() if u != an.subject}
            others = [n for n in self.cfg.nodes if n != an.subject
                      and n not in {q for s_ in st.values() for q in s_.get("quarantine", {})}
                      and "error" not in st.get(n, {})]
            if others:
                reach, _ = shortest_paths(rest, others[0])
                cut = [n for n in others if n not in reach]
                if cut:
                    self.trace.emit("thought", "Refusing to quarantine %s: it would cut %s off from the mesh. Alerting an operator instead." % (
                        an.subject, ", ".join(cut)))
                    return None
            return {"action": "quarantine_node", "node": an.subject,
                    "reason": "; ".join(sorted(reasons))[:120] or "DPI violations"}
        if an.kind == "link_recovered":
            if an.subject not in self.overridden:
                return None
            a, b = an.subject.split("-")
            self.trace.emit("thought", "Link %s healthy again (loss <= %.0f%% for %d polls); restoring cost." % (
                an.subject, self.det.recover_loss, self.det.recover_polls))
            return {"action": "update_link_cost", "a": a, "b": b, "metric": None}
        return None

    def actuate(self, plan):
        if plan["action"] == "update_link_cost":
            m = plan["metric"]
            self.trace.emit("tool_call", "Calling MCP tool: update_link_cost(%s, %s, %s)" % (plan["a"], plan["b"], m))
            res = self.client.update_link_cost(plan["a"], plan["b"], m)
            key = "-".join(sorted((plan["a"], plan["b"])))
            if m is None:
                self.overridden.pop(key, None)
            else:
                self.overridden[key] = True
        else:
            self.trace.emit("tool_call", "Calling MCP tool: quarantine_node(%s, %r)" % (plan["node"], plan["reason"]))
            res = self.client.quarantine_node(plan["node"], plan["reason"])
        self.trace.emit("tool_result", json.dumps({k: v for k, v in res.items() if k != "results"}))
        return res

    def verify(self, plan, t0, timeout=6.0):
        """Wait until the watched path no longer uses the blocked link / node; return convergence info."""
        src, dst = self.watch
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.client.collect()
            node = st.get(src, {})
            path = (node.get("paths") or {}).get(dst)
            if path:
                if plan["action"] == "update_link_cost" and plan["metric"] is not None:
                    hops = {frozenset(p) for p in zip(path, path[1:])}
                    if frozenset((plan["a"], plan["b"])) not in hops:
                        return {"ok": True, "path": path, "ms": int((time.time() - t0) * 1000),
                                "spf_ms": node["spf_ms"], "statuses": st}
                elif plan["action"] == "quarantine_node" and plan["node"] not in path:
                    return {"ok": True, "path": path, "ms": int((time.time() - t0) * 1000),
                            "spf_ms": node["spf_ms"], "statuses": st}
                elif plan["action"] == "update_link_cost":
                    return {"ok": True, "path": path, "ms": int((time.time() - t0) * 1000),
                            "spf_ms": node["spf_ms"], "statuses": st}
            time.sleep(0.1)
        return {"ok": False, "path": (self.client.collect().get(src, {}).get("paths") or {}).get(dst),
                "ms": int((time.time() - t0) * 1000), "spf_ms": None, "statuses": None}

    def postmortem(self, an, plan, result, before):
        self.incidents += 1
        os.makedirs(os.path.join(self.rt, "rca"), exist_ok=True)
        path = os.path.join(self.rt, "rca", "incident_%03d_%s.md" % (self.incidents, an.kind))
        src, dst = self.watch
        lines = [
            "# Incident %03d - %s (%s)" % (self.incidents, an.kind, an.subject),
            "", "Detected: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "", "## Evidence", "```json", json.dumps(an.evidence, indent=2, default=str), "```",
            "", "## Action", "```json", json.dumps({k: v for k, v in plan.items()}, indent=2), "```",
            "", "## Outcome",
            "- Watched flow %s -> %s path before: `%s`" % (src, dst, " -> ".join(before or ["?"])),
            "- Path after: `%s`" % " -> ".join(result["path"] or ["none"]),
            "- Remediation verified: %s in %d ms (SPF run %s ms at %s)" % (
                result["ok"], result["ms"], result["spf_ms"], src),
            "", "## Graph metrics at resolution"]
        if result.get("statuses"):
            topo = self.client.get_mesh_topology(result["statuses"])
            for e in topo["edges"]:
                lines.append("- %s-%s cost=%s" % (e["a"], e["b"], e["cost_max"]))
            for n, v in topo["nodes"].items():
                if v.get("quarantined"):
                    lines.append("- %s sees quarantined: %s" % (n, ", ".join(v["quarantined"])))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.trace.emit("rca", "Post-mortem written: %s (path %s, converged in %d ms)" % (
            path.replace("\\", "/"), "->".join(result["path"] or ["none"]), result["ms"]))

    # --- helpers -------------------------------------------------------
    def _graph(self, statuses):
        g = {}
        for u, st in statuses.items():
            for l in st.get("links", []):
                if l["up"] and not l["quarantined"]:
                    g.setdefault(u, {})[l["neighbor"]] = l["cost"]
        return g

    def _loss(self, statuses, x, y):
        t = self.client.get_link_telemetry(x, y, statuses)
        return t["packet_drop_pct"] or 0.0

    # --- main loop ------------------------------------------------------
    def run(self):
        self.trace.emit("info", "NetOps agent online. Polling %d nodes every %.1fs; watching %s->%s." % (
            len(self.cfg.nodes), self.poll, *self.watch))
        while True:
            try:
                state = self.ingest({})
                for an in state["anomalies"]:
                    before = (state["statuses"].get(self.watch[0], {}).get("paths") or {}).get(self.watch[1])
                    t0 = time.time()
                    plan = self.diagnose(state, an)
                    if plan is None:
                        continue
                    self.actuate(plan)
                    result = self.verify(plan, t0)
                    if result["ok"]:
                        self.trace.emit("thought", "Verified: %s->%s now %s after %d ms (Dijkstra %s ms)." % (
                            *self.watch, "->".join(result["path"]), result["ms"], result["spf_ms"]))
                    else:
                        self.trace.emit("thought", "Remediation NOT confirmed within timeout.")
                    self.postmortem(an, plan, result, before)
            except Exception as e:
                self.trace.emit("error", "agent loop error: %r" % (e,))
            time.sleep(self.poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="mesh.json")
    ap.add_argument("--runtime", default="runtime")
    ap.add_argument("--src", default="A")
    ap.add_argument("--dst", default="E")
    a = ap.parse_args()
    NetOpsAgent(MeshConfig(a.config), a.runtime, watch=(a.src, a.dst)).run()


if __name__ == "__main__":
    main()
