"""Link-State Database: latest LSA per origin, with two-way link verification."""

import time


class LSDB:
    def __init__(self, max_age=10.0):
        self.max_age = max_age
        self.entries = {}  # origin -> {"seq", "links": {nbr: cost}, "at"}

    def install(self, origin, seq, links, now=None):
        """Install if newer. Returns None if stale, else True when links changed
        (rerun SPF) / False when it was only a refresh (still re-flood)."""
        now = now or time.monotonic()
        cur = self.entries.get(origin)
        if cur and seq <= cur["seq"]:
            return None
        changed = cur is None or cur["links"] != links
        self.entries[origin] = {"seq": seq, "links": dict(links), "at": now}
        return changed

    def refresh(self, origin, now=None):
        if origin in self.entries:
            self.entries[origin]["at"] = now or time.monotonic()

    def expire(self, now=None):
        now = now or time.monotonic()
        dead = [o for o, e in self.entries.items() if now - e["at"] > self.max_age]
        for o in dead:
            del self.entries[o]
        return dead

    def graph(self, excluded=()):
        """Directed graph with only links both ends advertise (two-way check).
        Cost of u->v is what u advertises. Excluded (quarantined) nodes removed."""
        g = {}
        for u, e in self.entries.items():
            if u in excluded:
                continue
            for v, c in e["links"].items():
                if v in excluded:
                    continue
                back = self.entries.get(v)
                if back and u in back["links"]:
                    g.setdefault(u, {})[v] = c
        return g

    def snapshot(self):
        now = time.monotonic()
        return {o: {"seq": e["seq"], "links": e["links"], "age": round(now - e["at"], 1)}
                for o, e in self.entries.items()}
