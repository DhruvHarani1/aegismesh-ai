"""AegisMesh node: raw-UDP data plane + link-state routing + DPI + control API.

Run one per machine:  python -m aegismesh.node --config mesh.json --name A
Threads: rx (UDP), timer (ARQ timers, heartbeats, LSA refresh, chaos delay
queue), stream generator (on demand) and the HTTP control API (control_api.py).
All shared state is guarded by one RLock.
"""

import argparse
import heapq
import os
import random
import socket
import struct
import sys
import threading
import time
from collections import deque

from . import appproto, framing
from .config import MeshConfig
from .dijkstra import build_fib
from .dpi_inspector import DPIInspector
from .firewall import Firewall
from .framing import (BROADCAST, Frame, FrameError, T_ACK, T_DATA, T_HEARTBEAT, T_LSA,
                      T_QUARANTINE)
from .lsa_engine import decode_lsa, encode_lsa, sign, verify
from .quarantine import QuarantineManager
from .selective_repeat import SRReceiver, SRSender
from .topology_db import LSDB

HB = struct.Struct(">BId")  # kind(0 ping/1 pong) seq ts


class Link:
    def __init__(self, node, name, ident, addr, base_cost, params):
        self.name, self.id, self.addr, self.base_cost = name, ident, addr, base_cost
        self.override = None
        self.last_heard = 0.0
        self.up = False
        self.hb_seq = 0
        self.pings = {}                                  # seq -> sent monotonic
        self.window = deque(maxlen=params["loss_window"])  # True=answered
        self.rtts = deque(maxlen=20)
        self.crc_events = deque()
        self.crc_total = 0
        self.chaos = {"loss": 0.0, "delay_ms": 0, "corrupt": 0.0}
        self.chaos_dropped = 0
        self.sender = SRSender(lambda seq, f: node._tx(self, Frame(f.src, f.dst, T_DATA, seq, 0, f.payload)))
        self.receiver = SRReceiver(lambda f: node._on_deliver(self, f))

    @property
    def cost(self):
        return self.base_cost if self.override is None else self.override

    def loss_pct(self):
        if not self.window:
            return None
        return round(100.0 * (1 - sum(self.window) / len(self.window)), 1)

    def rtt_stats(self):
        if not self.rtts:
            return None, None
        m = sum(self.rtts) / len(self.rtts)
        var = sum((x - m) ** 2 for x in self.rtts) / len(self.rtts)
        return round(m * 1000, 2), round(var * 1e6, 2)

    def crc_recent(self, horizon=5.0):
        cutoff = time.monotonic() - horizon
        while self.crc_events and self.crc_events[0] < cutoff:
            self.crc_events.popleft()
        return len(self.crc_events)


class Node:
    def __init__(self, cfg: MeshConfig, name: str):
        self.cfg, self.name = cfg, name
        self.id = cfg.nodes[name]["id"]
        self.p = cfg.params
        self.lock = threading.RLock()
        self.running = False
        self.started = time.time()
        self.quarantine = QuarantineManager()
        self.firewall = Firewall()
        self.dpi = DPIInspector(cfg.by_id.keys(), self.firewall)
        self.lsdb = LSDB(self.p["lsa_max_age"])
        self.lsa_seq = int(time.time())
        self.fib = {}            # dst id -> (next hop id, cost)
        self.spf_ms = 0.0
        self.spf_runs = 0
        self.links = {}
        self.addr_map = {}
        for nbr, cost in cfg.neighbors(name).items():
            n = cfg.nodes[nbr]
            link = Link(self, nbr, n["id"], (n["host"], n["port"]), cost, self.p)
            self.links[n["id"]] = link
            self.addr_map[link.addr] = link
        self.wire = deque(maxlen=300)
        self.events = deque(maxlen=120)
        self.delayed = []        # heap of (release, n, link, wire)
        self._n = 0
        self.rogue = False
        self.app_rx = {}         # src id -> stats for port 5000
        self.chat = deque(maxlen=20)
        self.app_tx = {"sent": 0, "no_route": 0, "running": False, "dst": None}
        self.counters = {"rx_frames": 0, "tx_frames": 0, "forwarded": 0, "no_route": 0,
                         "quarantine_drops": 0, "unknown_src": 0, "lsa_rejected": 0}
        self.sock = None

    # ------------------------------------------------------------ logging
    def log(self, msg):
        line = "%s [%s] %s" % (time.strftime("%H:%M:%S"), self.name, msg)
        self.events.append({"t": time.time(), "msg": msg})
        print(line, flush=True)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        n = self.cfg.nodes[self.name]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(("0.0.0.0", n["port"]))
        if hasattr(socket, "SIO_UDP_CONNRESET"):  # Windows: ignore ICMP port-unreachable resets
            self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.sock.settimeout(0.2)
        self.running = True
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._timer_loop, daemon=True).start()
        from .control_api import serve
        serve(self, "0.0.0.0", n["ctl"])
        with self.lock:
            self._originate_lsa()
        self.log("up: id=%d udp=%d ctl=%d neighbors=%s" % (
            self.id, n["port"], n["ctl"], ",".join(l.name for l in self.links.values())))

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ transmit
    def _tx(self, link, frame):
        if link.id in self.quarantine:
            return
        self._send_wire(link, framing.encode(frame))

    def _send_wire(self, link, wire):
        ch = link.chaos
        if ch["loss"] and random.random() < ch["loss"]:
            link.chaos_dropped += 1
            return
        if ch["corrupt"] and random.random() < ch["corrupt"] and len(wire) > 3:
            i = random.randrange(1, len(wire) - 1)
            wire = wire[:i] + bytes([wire[i] ^ (1 << random.randrange(8))]) + wire[i + 1:]
            if wire[i] in (framing.FLAG,):  # keep it a single bit-flip inside the frame
                wire = wire[:i] + b"\x00" + wire[i + 1:]
        if ch["delay_ms"]:
            self._n += 1
            heapq.heappush(self.delayed, (time.monotonic() + ch["delay_ms"] / 1000.0, self._n, link, wire))
            return
        self._raw_send(link, wire)

    def _raw_send(self, link, wire):
        try:
            self.sock.sendto(wire, link.addr)
        except OSError:
            return
        self.counters["tx_frames"] += 1
        self.wire.append((time.time(), "tx", link.name, wire))

    # ------------------------------------------------------------ receive
    def _rx_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if not self.running:
                    return
                continue
            try:
                with self.lock:
                    self._handle(data, addr)
            except Exception as e:  # never let one bad packet kill the node
                self.log("rx error: %r" % (e,))

    def _handle(self, data, addr):
        link = self.addr_map.get(addr)
        if link is None:
            self.counters["unknown_src"] += 1
            return
        if link.id in self.quarantine:
            self.counters["quarantine_drops"] += 1
            return
        self.counters["rx_frames"] += 1
        self.wire.append((time.time(), "rx", link.name, data))
        try:
            f = framing.decode(data)
        except FrameError as e:
            link.crc_total += 1
            link.crc_events.append(time.monotonic())
            self.dpi.record_violation(link.id, "L2 %s: %s" % (type(e).__name__, e))
            return
        self._heard(link)
        if f.ftype == T_HEARTBEAT:
            self._on_heartbeat(link, f)
        elif f.ftype == T_ACK:
            link.sender.on_ack(f.ack, f.payload[0] if f.payload else 0)
        elif f.ftype == T_DATA:
            ack, bitmap = link.receiver.on_data(f.seq, f)
            self._tx(link, Frame(self.id, link.id, T_ACK, 0, ack, bytes([bitmap])))
        elif f.ftype == T_LSA:
            self._on_lsa(link, f)
        elif f.ftype == T_QUARANTINE:
            self._on_quarantine_frame(link, f)

    def _heard(self, link):
        link.last_heard = time.monotonic()
        if not link.up:
            link.up = True
            self.log("link %s UP" % link.name)
            self._originate_lsa()

    # ------------------------------------------------------------ heartbeats
    def _on_heartbeat(self, link, f):
        if len(f.payload) < HB.size:
            return
        kind, seq, ts = HB.unpack_from(f.payload)
        if kind == 0:
            self._tx(link, Frame(self.id, link.id, T_HEARTBEAT, f.seq, 0, HB.pack(1, seq, ts)))
        else:
            if link.pings.pop(seq, None) is not None:
                link.window.append(True)
                link.rtts.append(time.monotonic() - ts)

    def _send_pings(self, now):
        for link in self.links.values():
            if link.id in self.quarantine:
                continue
            link.hb_seq = (link.hb_seq + 1) & 0xFFFFFFFF
            link.pings[link.hb_seq] = now
            self._tx(link, Frame(self.id, link.id, T_HEARTBEAT, link.hb_seq & 0xFFFF, 0,
                                 HB.pack(0, link.hb_seq, now)))
            for seq, t in list(link.pings.items()):
                if now - t > self.p["hb_timeout"]:
                    del link.pings[seq]
                    link.window.append(False)

    # ------------------------------------------------------------ link-state
    def _my_links(self):
        return {l.id: l.cost for l in self.links.values()
                if l.up and l.id not in self.quarantine}

    def _originate_lsa(self):
        self.lsa_seq += 1
        links = self._my_links()
        self.lsdb.install(self.id, self.lsa_seq, links)
        self._flood(Frame(self.id, BROADCAST, T_LSA, 0, 0, encode_lsa(self.cfg.secret, self.id, self.lsa_seq, links)))
        self._recompute()

    def _flood(self, frame, exclude=None):
        for l in self.links.values():
            if l.id != exclude and l.up and l.id not in self.quarantine:
                self._tx(l, frame)

    def _on_lsa(self, link, f):
        dec = decode_lsa(self.cfg.secret, f.payload)
        if dec is None:
            self.counters["lsa_rejected"] += 1
            self.dpi.record_violation(link.id, "L7 LSA failed authentication")
            return
        origin, seq, links = dec
        if origin == self.id or origin in self.quarantine:
            return
        changed = self.lsdb.install(origin, seq, links)
        if changed is None:
            return
        self._flood(f, exclude=link.id)
        if changed:
            self._recompute()

    def _recompute(self):
        t0 = time.perf_counter()
        graph = self.lsdb.graph(excluded=self.quarantine.ids())
        new = build_fib(graph, self.id)
        self.spf_ms = round((time.perf_counter() - t0) * 1000, 3)
        self.spf_runs += 1
        if {d: v[0] for d, v in new.items()} != {d: v[0] for d, v in self.fib.items()}:
            self.log("FIB updated (SPF %.3f ms): %s" % (self.spf_ms, self._fmt_fib(new)))
        self.fib = new
        self._reroute_pending()

    def _reroute_pending(self):
        """Move frames stuck on a link that is no longer the next hop onto the new path,
        so a route change loses nothing that was already in flight."""
        for link in self.links.values():
            def stale(f, link=link):
                nh = self.fib.get(f.dst)
                return nh is not None and nh[0] != link.id and f.dst != link.id
            for f in link.sender.extract(stale):
                out = self.links.get(self.fib[f.dst][0])
                if out is not None:
                    out.sender.submit(f)

    def _fmt_fib(self, fib):
        return ", ".join("%s via %s (%d)" % (self.cfg.name(d), self.cfg.name(nh), c)
                         for d, (nh, c) in sorted(fib.items()))

    def path_to(self, dst):
        """Hop list from this node's view (follows the local FIB then LSDB graph)."""
        from .dijkstra import path_to, shortest_paths
        dist, prev = shortest_paths(self.lsdb.graph(excluded=self.quarantine.ids()), self.id)
        p = path_to(prev, self.id, dst)
        return [self.cfg.name(i) for i in p] if p else None

    # ------------------------------------------------------------ quarantine
    def do_quarantine(self, node_id, reason, announce=True):
        with self.lock:
            if node_id == self.id:
                return False
            if not self.quarantine.add(node_id, reason):
                return False
            self.firewall.block_source(node_id, reason)
            link = self.links.get(node_id)
            if link:
                link.up = False
            self.lsdb.entries.pop(node_id, None)
            self.log("QUARANTINE node %s: %s" % (self.cfg.name(node_id), reason))
            if announce:
                body = struct.pack(">B", node_id) + reason.encode()[:200]
                self._flood(Frame(self.id, BROADCAST, T_QUARANTINE, 0, 0, body + sign(self.cfg.secret, body)))
            self._originate_lsa()
            return True

    def do_unquarantine(self, node_id):
        with self.lock:
            ok = self.quarantine.remove(node_id)
            self.firewall.blocked_src.pop(node_id, None)
            if ok:
                self.log("released %s from quarantine" % self.cfg.name(node_id))
            return ok

    def _on_quarantine_frame(self, link, f):
        body, mac = f.payload[:-8], f.payload[-8:]
        if len(body) < 1 or not verify(self.cfg.secret, body, mac):
            self.counters["lsa_rejected"] += 1
            self.dpi.record_violation(link.id, "L7 QUARANTINE failed authentication")
            return
        target = body[0]
        if target == self.id:
            return
        self.do_quarantine(target, body[1:].decode(errors="replace") + " (flooded)", announce=True)

    # ------------------------------------------------------------ data plane
    def _on_deliver(self, link, f):
        ok, reason, parsed = self.dpi.inspect(f, link.id)
        if not ok:
            self.log("DPI drop from %s: %s" % (link.name, reason)) if self.dpi.recent(link.id) <= 3 else None
            return
        ttl, proto, sport, dport, data = parsed
        if f.dst == self.id:
            self._app_receive(f.src, sport, dport, data)
            return
        nh = self.fib.get(f.dst)
        out = self.links.get(nh[0]) if nh else None
        if out is None or ttl <= 1:
            self.counters["no_route"] += 1
            return
        self.counters["forwarded"] += 1
        out.sender.submit(Frame(f.src, f.dst, T_DATA, 0, 0, appproto.decrement_ttl(f.payload)))

    def _app_receive(self, src, sport, dport, data):
        if dport == appproto.PORT_STREAM:
            try:
                seq_s, ts_s = data.split(b"|", 2)[:2]
                seq, ts = int(seq_s), float(ts_s)
            except ValueError:
                return
            st = self.app_rx.setdefault(src, {"seqs": set(), "max_seq": -1, "lat": deque(maxlen=50),
                                              "first": time.time(), "last": time.time(), "dups": 0})
            if seq in st["seqs"]:
                st["dups"] += 1
                return
            st["seqs"].add(seq)
            st["max_seq"] = max(st["max_seq"], seq)
            st["last"] = time.time()
            st["lat"].append(max(0.0, time.time() - ts))
        else:
            self.chat.append({"t": time.time(), "src": self.cfg.name(src), "text": data.decode(errors="replace")})

    def send_app(self, dst, dport, data, sport=40000):
        with self.lock:
            nh = self.fib.get(dst)
            link = self.links.get(nh[0]) if nh else None
            if link is None:
                self.app_tx["no_route"] += 1
                return False
            payload = appproto.pack(appproto.DEFAULT_TTL, appproto.PROTO_APP, sport, dport, data)
            ok = link.sender.submit(Frame(self.id, dst, T_DATA, 0, 0, payload))
            if ok:
                self.app_tx["sent"] += 1
            return ok

    def start_stream(self, dst, count=0, interval_ms=50, size=64):
        """count=0 -> run until stopped."""
        with self.lock:
            if self.app_tx["running"]:
                self.app_tx["running"] = False
                time.sleep(interval_ms / 1000.0 + 0.05)
            self.app_tx.update(running=True, dst=dst, sent=0, no_route=0)

        def run():
            i = 0
            while self.running and self.app_tx["running"] and (count == 0 or i < count):
                pad = b"x" * max(0, size - 24)
                self.send_app(dst, appproto.PORT_STREAM, b"%d|%.6f|" % (i, time.time()) + pad)
                i += 1
                time.sleep(interval_ms / 1000.0)
            self.app_tx["running"] = False
        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self.app_tx["running"] = False

    # ------------------------------------------------------------ chaos / rogue
    def set_chaos(self, neighbor, loss=0.0, delay_ms=0, corrupt=0.0):
        with self.lock:
            targets = self.links.values() if neighbor in ("*", None) else [self.links[neighbor]]
            for l in targets:
                l.chaos = {"loss": float(loss), "delay_ms": int(delay_ms), "corrupt": float(corrupt)}
            self.log("chaos on %s: loss=%.0f%% delay=%dms corrupt=%.0f%%" % (
                neighbor if neighbor in ("*", None) else self.cfg.name(neighbor), loss * 100, delay_ms, corrupt * 100))

    def set_cost(self, neighbor, cost):
        with self.lock:
            link = self.links[neighbor]
            link.override = None if cost is None else int(cost)
            self.log("cost override on link %s -> %s" % (link.name, "none" if cost is None else cost))
            self._originate_lsa()

    def _rogue_tick(self):
        for l in list(self.links.values()):
            if l.id in self.quarantine:
                continue
            # corrupted-CRC garbage straight onto the wire
            good = framing.encode(Frame(self.id, l.id, T_DATA, 0, 0, b"garbage" * 4))
            i = random.randrange(2, len(good) - 2)
            bad = good[:i] + bytes([good[i] ^ 0x55 if good[i] ^ 0x55 != framing.FLAG else 0]) + good[i + 1:]
            self._raw_send(l, bad)
            # well-formed frames that violate policy (forbidden port / binary junk)
            dst = random.choice([i for i in self.cfg.by_id if i != self.id])
            if random.random() < 0.5:
                pl = appproto.pack(16, 1, 666, 6666, b"exfil")
            else:
                pl = appproto.pack(16, 1, 666, appproto.PORT_CHAT, bytes([0xFF, 0xFE, 0x00, 0x01]))
            l.sender.submit(Frame(self.id, dst, T_DATA, 0, 0, pl))

    # ------------------------------------------------------------ timer loop
    def _timer_loop(self):
        next_hb = next_lsa = 0.0
        next_rogue = 0.0
        while self.running:
            try:
                with self.lock:
                    now = time.monotonic()
                    while self.delayed and self.delayed[0][0] <= now:
                        _, _, link, wire = heapq.heappop(self.delayed)
                        self._raw_send(link, wire)
                    for l in self.links.values():
                        if l.id not in self.quarantine:
                            l.sender.tick(now)
                    if now >= next_hb:
                        next_hb = now + self.p["hb_interval"]
                        self._send_pings(now)
                        for l in self.links.values():
                            if l.up and now - l.last_heard > self.p["dead_interval"]:
                                l.up = False
                                self.log("link %s DOWN (no frames for %.1fs)" % (l.name, self.p["dead_interval"]))
                                self._originate_lsa()
                    if now >= next_lsa:
                        next_lsa = now + self.p["lsa_refresh"]
                        if self.lsdb.expire():
                            self._recompute()
                        self._originate_lsa()
                    if self.rogue and now >= next_rogue:
                        next_rogue = now + 0.05
                        self._rogue_tick()
            except Exception as e:
                self.log("timer error: %r" % (e,))
            time.sleep(0.01)

    # ------------------------------------------------------------ status
    def status(self):
        with self.lock:
            nm = self.cfg.name
            links = []
            for l in self.links.values():
                rtt, var = l.rtt_stats()
                links.append({
                    "neighbor": l.name, "id": l.id, "up": l.up and l.id not in self.quarantine,
                    "quarantined": l.id in self.quarantine, "base_cost": l.base_cost,
                    "cost": l.cost, "override": l.override, "loss_pct": l.loss_pct(),
                    "rtt_ms": rtt, "rtt_var": var, "crc_errors": l.crc_total, "crc_recent": l.crc_recent(),
                    "dpi_recent": self.dpi.recent(l.id), "dpi_total": self.dpi.totals.get(l.id, 0),
                    "retransmits": l.sender.retransmits, "frames_sent": l.sender.sent,
                    "delivered": l.receiver.delivered, "out_of_order": l.receiver.out_of_order,
                    "chaos": dict(l.chaos), "chaos_dropped": l.chaos_dropped,
                    "sr": l.sender.snapshot()})
            rx = {}
            for src, st in self.app_rx.items():
                uniq = len(st["seqs"])
                lat = list(st["lat"])
                rx[nm(src)] = {"received": uniq, "max_seq": st["max_seq"],
                               "missing": st["max_seq"] + 1 - uniq, "dups": st["dups"],
                               "latency_ms": round(1000 * sum(lat) / len(lat), 1) if lat else None,
                               "idle_s": round(time.time() - st["last"], 2)}
            return {
                "name": self.name, "id": self.id, "uptime": round(time.time() - self.started, 1),
                "links": links, "rogue": self.rogue, "spf_ms": self.spf_ms, "spf_runs": self.spf_runs,
                "fib": {nm(d): {"next_hop": nm(v[0]), "cost": v[1]} for d, v in self.fib.items()},
                "lsdb": {nm(o): {"seq": e["seq"], "age": e["age"],
                                 "links": {nm(n): c for n, c in e["links"].items()}}
                         for o, e in self.lsdb.snapshot().items()},
                "quarantine": {nm(int(k)): v for k, v in self.quarantine.snapshot().items()},
                "firewall": self.firewall.snapshot(), "dpi": self.dpi.snapshot(),
                "counters": dict(self.counters), "stream_rx": rx, "stream_tx": dict(self.app_tx),
                "chat": list(self.chat), "events": list(self.events)[-25:],
                "paths": {nm(d): self.path_to(d) for d in self.fib}}

    def wire_dump(self, n=25):
        with self.lock:
            rows = list(self.wire)[-n:]
        out = []
        for t, d, nb, raw in rows:
            a = framing.annotate(raw)
            a.update(t=t, dir=d, neighbor=nb)
            out.append(a)
        return out


def main():
    ap = argparse.ArgumentParser(description="AegisMesh node")
    ap.add_argument("--config", default="mesh.json")
    ap.add_argument("--name", required=True, help="node name from the config, e.g. A")
    a = ap.parse_args()
    cfg = MeshConfig(a.config)
    node = Node(cfg, a.name.upper())
    node.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()


if __name__ == "__main__":
    main()
