"""Control-plane client: the four actuation tools of the spec plus telemetry
collection. Used directly by the NetOps agent and wrapped by the MCP server.
Talks to every node's HTTP control API (so it works across laptops)."""

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import MeshConfig


class MeshClient:
    def __init__(self, cfg: MeshConfig, timeout=1.5):
        self.cfg, self.timeout = cfg, timeout
        self.pool = ThreadPoolExecutor(max_workers=8)

    # --- raw HTTP -----------------------------------------------------
    def _call(self, node, path, body=None):
        url = self.cfg.ctl_url(node) + path
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Aegis-Token": self.cfg.token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def call(self, node, path, body=None):
        try:
            return self._call(node, path, body)
        except Exception as e:  # node down / unreachable
            return {"error": str(e)}

    def collect(self):
        """{node name: /status JSON or {'error':...}} fetched in parallel."""
        names = list(self.cfg.nodes)
        return dict(zip(names, self.pool.map(lambda n: self.call(n, "/status"), names)))

    # --- MCP tool contract --------------------------------------------
    def get_mesh_topology(self, statuses=None):
        """Active nodes, edges and real-time link costs from the live LSDB(s)."""
        statuses = statuses or self.collect()
        nodes, edges = {}, {}
        for name, st in statuses.items():
            if "error" in st:
                nodes[name] = {"up": False}
                continue
            nodes[name] = {"up": True, "id": st["id"], "rogue_flag": st["rogue"],
                           "quarantined": sorted(st["quarantine"])}
            for l in st["links"]:
                if not l["up"]:
                    continue
                key = "-".join(sorted((name, l["neighbor"])))
                e = edges.setdefault(key, {"a": min(name, l["neighbor"]), "b": max(name, l["neighbor"]),
                                           "cost": {}})
                e["cost"][name] = l["cost"]
        for e in edges.values():
            e["cost_max"] = max(e["cost"].values())
        return {"nodes": nodes, "edges": list(edges.values())}

    def get_link_telemetry(self, node_a, node_b, statuses=None):
        """Packet drop %, latency variance and CRC errors for an edge (both directions merged)."""
        a, b = self.cfg.node_name(node_a), self.cfg.node_name(node_b)
        statuses = statuses or self.collect()
        out = {"link": "%s-%s" % (a, b), "views": {}}
        for x, y in ((a, b), (b, a)):
            st = statuses.get(x, {})
            for l in st.get("links", []):
                if l["neighbor"] == y:
                    out["views"][x] = {k: l[k] for k in (
                        "up", "loss_pct", "rtt_ms", "rtt_var", "crc_errors", "crc_recent",
                        "retransmits", "cost", "override", "dpi_recent")}
        losses = [v["loss_pct"] for v in out["views"].values() if v["loss_pct"] is not None]
        out["packet_drop_pct"] = max(losses) if losses else None
        out["latency_variance"] = max([v["rtt_var"] or 0 for v in out["views"].values()] or [0])
        out["crc_errors"] = sum(v["crc_errors"] for v in out["views"].values())
        return out

    def update_link_cost(self, node_a, node_b, new_metric):
        """Override a link's weight at both endpoints (None restores the configured cost);
        the endpoints re-advertise an LSA and every node reruns Dijkstra."""
        a, b = self.cfg.node_name(node_a), self.cfg.node_name(node_b)
        r1 = self.call(a, "/cost", {"neighbor": b, "cost": new_metric})
        r2 = self.call(b, "/cost", {"neighbor": a, "cost": new_metric})
        return {"link": "%s-%s" % (a, b), "cost": new_metric, "results": [r1, r2]}

    def quarantine_node(self, node_id, reason):
        """Tell every other node to purge `node_id` from its FIB and drop its socket traffic."""
        target = self.cfg.node_name(node_id)
        res = {n: self.call(n, "/quarantine", {"node": target, "reason": reason})
               for n in self.cfg.nodes if n != target}
        return {"quarantined": target, "reason": reason, "results": res}

    def release_node(self, node_id):
        target = self.cfg.node_name(node_id)
        return {n: self.call(n, "/unquarantine", {"node": target}) for n in self.cfg.nodes if n != target}
