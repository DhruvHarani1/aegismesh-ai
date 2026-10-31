"""Stateful 5-tuple firewall: (src, dst, proto, sport, dport).

Default policy is deny for destination ports not in the allow list. Dynamic
rules (e.g. block everything from a node) can be added at runtime, and each
permitted flow is tracked in a flow table.
"""

import time


class Firewall:
    def __init__(self, allowed_ports=(5000, 5001)):
        self.allowed_ports = set(allowed_ports)
        self.blocked_src = {}   # node id -> reason
        self.flows = {}         # 5-tuple -> {"packets", "bytes", "first", "last"}
        self.denied = 0

    def block_source(self, node_id, reason="manual"):
        self.blocked_src[node_id] = reason

    def check(self, src, dst, proto, sport, dport, size=0):
        """Return (allowed, reason)."""
        if src in self.blocked_src:
            self.denied += 1
            return False, "source node %d blocked (%s)" % (src, self.blocked_src[src])
        if dport not in self.allowed_ports:
            self.denied += 1
            return False, "dst port %d not permitted" % dport
        key = (src, dst, proto, sport, dport)
        now = time.time()
        fl = self.flows.get(key)
        if fl is None:
            if len(self.flows) > 512:
                oldest = min(self.flows, key=lambda k: self.flows[k]["last"])
                del self.flows[oldest]
            fl = self.flows[key] = {"packets": 0, "bytes": 0, "first": now, "last": now}
        fl["packets"] += 1
        fl["bytes"] += size
        fl["last"] = now
        return True, "ok"

    def snapshot(self):
        return {"allowed_ports": sorted(self.allowed_ports), "denied": self.denied,
                "blocked": {str(k): v for k, v in self.blocked_src.items()},
                "flows": [{"tuple": list(k), **v} for k, v in list(self.flows.items())[-8:]]}
