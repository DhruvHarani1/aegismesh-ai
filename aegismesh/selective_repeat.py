"""Selective Repeat ARQ (Ws = Wr = 8, sequence space modulo 16).

Transport-agnostic: the sender calls `send_fn(wire_seq, item)` and the
receiver calls `deliver_fn(item)`; the caller moves ACKs between them.
ACK format: `ack` = receiver's next expected wire seq (cumulative), plus a SACK
bitmap byte where bit i means the frame at offset i+1 from `ack` is buffered.
Each in-flight frame has its own send timestamp -> independent timer, with an
adaptive RTO (Jacobson/Karels; Karn's rule: retransmitted frames give no RTT sample).
"""

import time
from collections import deque

WINDOW = 8
MODULO = 16  # needs MODULO >= Ws + Wr


class SRSender:
    def __init__(self, send_fn, window=WINDOW, modulo=MODULO, rto_init=0.4,
                 rto_min=0.08, rto_max=2.0, max_queue=4096):
        assert modulo >= 2 * window
        self.send_fn, self.W, self.M = send_fn, window, modulo
        self.rto_min, self.rto_max = rto_min, rto_max
        self.rto = rto_init
        self.srtt = None
        self.rttvar = 0.0
        self.base = 0          # absolute index of oldest unacked frame
        self.next = 0          # absolute index of next new frame
        self.inflight = {}     # abs -> dict(item, sent_at, tries, acked)
        self.queue = deque()
        self.max_queue = max_queue
        self.sent = self.retransmits = self.dropped_queue = 0

    def submit(self, item) -> bool:
        if len(self.queue) >= self.max_queue:
            self.dropped_queue += 1
            return False
        self.queue.append(item)
        self.pump()
        return True

    def pump(self):
        while self.queue and self.next - self.base < self.W:
            item = self.queue.popleft()
            self.inflight[self.next] = {"item": item, "sent_at": time.monotonic(),
                                        "tries": 1, "acked": False}
            self.send_fn(self.next % self.M, item)
            self.sent += 1
            self.next += 1

    def on_ack(self, ack_wire, sack_bitmap=0):
        now = time.monotonic()
        outstanding = self.next - self.base
        delta = (ack_wire - self.base) % self.M
        if delta <= outstanding:
            for a in range(self.base, self.base + delta):
                self._mark(a, now)
        for i in range(8):
            if sack_bitmap & (1 << i):
                a = self.base + delta + 1 + i
                if a < self.next:
                    self._mark(a, now)
        while self.base < self.next and self.inflight[self.base]["acked"]:
            del self.inflight[self.base]
            self.base += 1
        self.pump()

    def _mark(self, a, now):
        st = self.inflight.get(a)
        if st is None or st["acked"]:
            return
        st["acked"] = True
        if st["tries"] == 1:
            self._sample(now - st["sent_at"])

    def _sample(self, rtt):
        if self.srtt is None:
            self.srtt, self.rttvar = rtt, rtt / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - rtt)
            self.srtt = 0.875 * self.srtt + 0.125 * rtt
        self.rto = min(self.rto_max, max(self.rto_min, self.srtt + 4 * self.rttvar))

    def tick(self, now=None):
        now = now or time.monotonic()
        n = 0
        for a in range(self.base, self.next):
            st = self.inflight.get(a)
            if st is None:  # acked (and slid out) by a re-entrant send_fn
                continue
            if not st["acked"] and now - st["sent_at"] >= self.rto * min(2 ** (st["tries"] - 1), 8):
                st["tries"] += 1
                st["sent_at"] = now
                self.send_fn(a % self.M, st["item"])
                self.retransmits += 1
                n += 1
        return n

    def extract(self, pred, filler):
        """Remove (and return) every unacked or queued item for which pred(item) is true.

        Queued items simply leave. In-flight items keep their sequence slot: the receiver is
        still waiting for that wire seq, so the slot is re-armed with `filler` (a no-op item)
        instead of being abandoned; abandoning it would desynchronise sender and receiver
        permanently."""
        taken = [it for it in self.queue if pred(it)]
        if taken:
            self.queue = deque(it for it in self.queue if not pred(it))
        for a in range(self.base, self.next):
            st = self.inflight[a]
            if not st["acked"] and pred(st["item"]):
                taken.append(st["item"])
                st["item"] = filler
        self.pump()
        return taken

    def snapshot(self):
        """Per-slot state for the visual buffer timeline."""
        now = time.monotonic()
        slots = []
        for a in range(self.base, self.base + self.W):
            if a < self.next:
                st = self.inflight[a]
                state = "acked" if st["acked"] else ("retx" if st["tries"] > 1 else "sent")
                slots.append({"seq": a % self.M, "state": state, "tries": st["tries"],
                              "age_ms": int((now - st["sent_at"]) * 1000)})
            else:
                slots.append({"seq": a % self.M, "state": "free", "tries": 0, "age_ms": 0})
        return {"base": self.base % self.M, "next": self.next % self.M, "queued": len(self.queue),
                "rto_ms": int(self.rto * 1000), "sent": self.sent, "retransmits": self.retransmits,
                "slots": slots}


class SRReceiver:
    def __init__(self, deliver_fn, window=WINDOW, modulo=MODULO):
        assert modulo >= 2 * window
        self.deliver_fn, self.W, self.M = deliver_fn, window, modulo
        self.base = 0
        self.buf = {}
        self.delivered = self.duplicates = self.out_of_order = 0

    def on_data(self, wire_seq, item):
        """Process a data frame; return (ack_wire, sack_bitmap) to send back."""
        offset = (wire_seq - self.base) % self.M
        if offset < self.W:
            a = self.base + offset
            if a in self.buf:
                self.duplicates += 1
            else:
                if offset:
                    self.out_of_order += 1
                self.buf[a] = item
            while self.base in self.buf:
                self.deliver_fn(self.buf.pop(self.base))
                self.delivered += 1
                self.base += 1
        else:
            self.duplicates += 1  # stale retransmission (earlier window); re-ACK below
        return self.ack_state()

    def ack_state(self):
        bitmap = 0
        for i in range(8):
            if (self.base + 1 + i) in self.buf:
                bitmap |= 1 << i
        return self.base % self.M, bitmap
