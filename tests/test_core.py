import os
import random
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aegismesh import framing  # noqa: E402
from aegismesh.crc32 import crc32, crc32_bitwise  # noqa: E402
from aegismesh.dijkstra import build_fib, path_to, shortest_paths  # noqa: E402
from aegismesh.lsa_engine import decode_lsa, encode_lsa  # noqa: E402
from aegismesh.selective_repeat import SRReceiver, SRSender  # noqa: E402
from aegismesh.topology_db import LSDB  # noqa: E402


class CRC(unittest.TestCase):
    def test_matches_zlib(self):
        for data in (b"", b"123456789", bytes(range(256)) * 4, os.urandom(1000)):
            self.assertEqual(crc32(data), zlib.crc32(data))
            self.assertEqual(crc32_bitwise(data), zlib.crc32(data))

    def test_check_value(self):
        self.assertEqual(crc32(b"123456789"), 0xCBF43926)


class Framing(unittest.TestCase):
    def test_roundtrip_with_special_bytes(self):
        payload = bytes([0x7E, 0x7D, 0x5E, 0x5D, 0x00, 0x7E]) * 50
        f = framing.Frame(1, 5, framing.T_DATA, 513, 7, payload)
        wire = framing.encode(f)
        self.assertEqual(wire[0], 0x7E)
        self.assertEqual(wire[-1], 0x7E)
        self.assertNotIn(0x7E, wire[1:-1])  # all inner flags are stuffed
        self.assertEqual(framing.decode(wire), f)

    def test_stuffing_table(self):
        self.assertEqual(framing.stuff(b"\x7e"), b"\x7d\x5e")
        self.assertEqual(framing.stuff(b"\x7d"), b"\x7d\x5d")
        self.assertEqual(framing.unstuff(b"\x7d\x5e\x7d\x5d"), b"\x7e\x7d")

    def test_single_bit_corruption_always_detected(self):
        wire = framing.encode(framing.Frame(2, 3, framing.T_DATA, 1, 0, b"hello mesh"))
        for i in range(1, len(wire) - 1):
            for bit in range(8):
                bad = bytearray(wire)
                bad[i] ^= 1 << bit
                with self.assertRaises(framing.FrameError):
                    framing.decode(bytes(bad))

    def test_max_payload(self):
        framing.decode(framing.encode(framing.Frame(1, 2, framing.T_DATA, 0, 0, b"a" * 1024)))
        with self.assertRaises(framing.BadLength):
            framing.encode(framing.Frame(1, 2, framing.T_DATA, 0, 0, b"a" * 1025))


class SelectiveRepeat(unittest.TestCase):
    def run_channel(self, loss, n=300, seed=1):
        rnd = random.Random(seed)
        got = []
        rx = SRReceiver(got.append)
        acks = []
        tx = None

        def send(seq, item):
            if rnd.random() >= loss:
                acks.append(rx.on_data(seq, item))

        tx = SRSender(send, rto_init=0.001, rto_min=0.001)
        for i in range(n):
            tx.submit(i)
        import time
        deadline = time.monotonic() + 20
        while len(got) < n and time.monotonic() < deadline:
            while acks:
                a = acks.pop(0)
                if rnd.random() >= loss:
                    tx.on_ack(*a)
            tx.tick()
            time.sleep(0.0005)
        return got, tx

    def test_reliable_in_order_over_lossy_channel(self):
        got, tx = self.run_channel(0.3)
        self.assertEqual(got, list(range(300)))
        self.assertGreater(tx.retransmits, 0)

    def test_window_never_exceeds_eight(self):
        sent = []
        tx = SRSender(lambda s, i: sent.append(s))
        for i in range(50):
            tx.submit(i)
        self.assertEqual(len(sent), 8)

    def test_out_of_order_is_buffered_and_sacked(self):
        got = []
        rx = SRReceiver(got.append)
        ack, bm = rx.on_data(2, "c")
        self.assertEqual((ack, bm), (0, 0b10))  # base 0 expected; frame at offset 2 buffered
        rx.on_data(1, "b")
        ack, bm = rx.on_data(0, "a")
        self.assertEqual(got, ["a", "b", "c"])
        self.assertEqual(ack, 3)

    def test_retransmits_only_missing_frames(self):
        sent = []
        tx = SRSender(lambda s, i: sent.append(s), rto_init=0.01, rto_min=0.01)
        for i in range(4):
            tx.submit(i)
        tx.on_ack(0, 0b101)  # frames 1 and 3 buffered at receiver; 0 and 2 missing
        sent.clear()
        import time
        time.sleep(0.03)
        tx.tick()
        self.assertEqual(sorted(sent), [0, 2])


class RerouteKeepsSequenceInSync(unittest.TestCase):
    def test_extract_does_not_strand_receiver(self):
        """Moving in-flight frames to another path must not leave the receiver waiting forever."""
        got = []
        rx = SRReceiver(got.append)
        drop = {"on": True}
        acks = []
        tx = None

        def send(seq, item):
            if not drop["on"]:
                acks.append(rx.on_data(seq, item))  # ACK travels back later, like a real network

        def flush():
            while acks:
                tx.on_ack(*acks.pop(0))

        tx = SRSender(send, rto_init=0.001, rto_min=0.001)
        for i in range(12):
            tx.submit(("real", i))
        moved = tx.extract(lambda it: it[0] == "real", ("filler", None))
        self.assertEqual(sorted(i for _, i in moved), list(range(12)))
        drop["on"] = False       # link recovers
        import time
        time.sleep(0.01)
        tx.tick()
        flush()
        for i in range(20):
            tx.submit(("real2", i))
        for _ in range(100):
            tx.tick()
            flush()
            time.sleep(0.002)
        self.assertEqual([x for x in got if x[0] == "real2"], [("real2", i) for i in range(20)])
        self.assertEqual(tx.base, tx.next)


class Routing(unittest.TestCase):
    G = {"A": {"B": 1, "D": 2}, "B": {"A": 1, "C": 1, "D": 2}, "C": {"B": 1, "E": 1},
         "D": {"A": 2, "B": 2, "E": 2}, "E": {"C": 1, "D": 2}}

    def test_shortest_path_and_reroute(self):
        dist, prev = shortest_paths(self.G, "A")
        self.assertEqual(path_to(prev, "A", "E"), ["A", "B", "C", "E"])
        g = {u: dict(v) for u, v in self.G.items()}
        g["B"]["C"] = 9999
        fib = build_fib(g, "A")
        self.assertEqual(fib["E"], ("D", 4))

    def test_lsdb_two_way_check_and_staleness(self):
        db = LSDB()
        self.assertTrue(db.install(1, 10, {2: 1}))
        self.assertIsNone(db.install(1, 10, {2: 5}))  # stale
        self.assertEqual(db.graph(), {})              # node 2 has not confirmed the link
        db.install(2, 1, {1: 1})
        self.assertEqual(db.graph(), {1: {2: 1}, 2: {1: 1}})
        self.assertEqual(db.graph(excluded={2}), {})

    def test_lsa_auth(self):
        p = encode_lsa(b"k", 3, 77, {1: 4, 2: 5})
        self.assertEqual(decode_lsa(b"k", p), (3, 77, {1: 4, 2: 5}))
        self.assertIsNone(decode_lsa(b"wrong", p))


if __name__ == "__main__":
    unittest.main()
