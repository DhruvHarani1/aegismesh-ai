"""Userspace deep packet inspection of DATA frames, L2 through L7.

L2: frame already CRC-validated by framing.decode (CRC failures are counted by
    the node per link). L3: src/dst node ids valid. L4: app header magic/ttl and
    ports + firewall 5-tuple. L7: text ports must carry valid printable UTF-8.
Violations are tracked per *link* (the neighbor that handed us the frame) in a
sliding window so the AI agent can spot rogue nodes.
"""

import time
from collections import defaultdict, deque

from . import appproto
from .firewall import Firewall


class DPIInspector:
    def __init__(self, known_ids, firewall: Firewall, window=5.0):
        self.known_ids = set(known_ids)
        self.fw = firewall
        self.window = window
        self.events = defaultdict(deque)   # neighbor id -> deque of timestamps
        self.totals = defaultdict(int)     # neighbor id -> total violations
        self.last_reason = {}
        self.inspected = 0

    def record_violation(self, neighbor, reason):
        self.events[neighbor].append(time.monotonic())
        self.totals[neighbor] += 1
        self.last_reason[neighbor] = reason

    def recent(self, neighbor):
        q = self.events[neighbor]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def inspect(self, frame, from_neighbor):
        """Return (allowed, reason, parsed); parsed is the app tuple on success."""
        self.inspected += 1
        if frame.src not in self.known_ids or frame.dst not in self.known_ids:
            return self._deny(from_neighbor, "L3 unknown node id src=%d dst=%d" % (frame.src, frame.dst))
        parsed = appproto.unpack(frame.payload)
        if parsed is None:
            return self._deny(from_neighbor, "L4 malformed app header")
        ttl, proto, sport, dport, data = parsed
        if ttl == 0:
            return self._deny(from_neighbor, "L3 ttl expired")
        ok, why = self.fw.check(frame.src, frame.dst, proto, sport, dport, len(data))
        if not ok:
            return self._deny(from_neighbor, "L4 firewall: " + why)
        if dport in (appproto.PORT_STREAM, appproto.PORT_CHAT):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return self._deny(from_neighbor, "L7 payload is not valid UTF-8")
            if not text.isprintable():
                return self._deny(from_neighbor, "L7 non-printable payload")
        return True, "ok", parsed

    def _deny(self, neighbor, reason):
        self.record_violation(neighbor, reason)
        return False, reason, None

    def snapshot(self):
        return {"inspected": self.inspected,
                "recent": {str(n): self.recent(n) for n in list(self.events)},
                "totals": {str(n): c for n, c in self.totals.items()},
                "last_reason": {str(n): r for n, r in self.last_reason.items()}}
