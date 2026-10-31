"""Network-impairment CLI / library (also behind the dashboard's chaos panel).

    python -m aegismesh.chaos_injector sever B C        # 90% loss + 400 ms RTT
    python -m aegismesh.chaos_injector heal B C
    python -m aegismesh.chaos_injector infect D         # node floods bad CRC + forbidden traffic
    python -m aegismesh.chaos_injector cure D
    python -m aegismesh.chaos_injector stream A E       # numbered test stream A -> E
    python -m aegismesh.chaos_injector stop A
"""

import argparse
import json

from .config import MeshConfig
from .mesh_tools import MeshClient


class Chaos:
    def __init__(self, client: MeshClient):
        self.c = client

    def sever(self, a, b, loss=0.9, rtt_ms=400, corrupt=0.0):
        half = int(rtt_ms / 2)  # delay is applied in each direction
        return [self.c.call(x, "/chaos", {"neighbor": y, "loss": loss, "delay_ms": half, "corrupt": corrupt})
                for x, y in ((a, b), (b, a))]

    def heal(self, a, b):
        return [self.c.call(x, "/chaos", {"neighbor": y, "loss": 0, "delay_ms": 0, "corrupt": 0})
                for x, y in ((a, b), (b, a))]

    def infect(self, node, on=True):
        return self.c.call(node, "/infect", {"on": on})

    def stream(self, src, dst, on=True, interval_ms=40):
        return self.c.call(src, "/stream", {"dst": dst, "on": on, "interval_ms": interval_ms})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="mesh.json")
    ap.add_argument("cmd", choices=["sever", "heal", "infect", "cure", "stream", "stop"])
    ap.add_argument("nodes", nargs="+")
    a = ap.parse_args()
    ch = Chaos(MeshClient(MeshConfig(a.config)))
    n = [x.upper() for x in a.nodes]
    out = {"sever": lambda: ch.sever(*n[:2]), "heal": lambda: ch.heal(*n[:2]),
           "infect": lambda: ch.infect(n[0]), "cure": lambda: ch.infect(n[0], False),
           "stream": lambda: ch.stream(n[0], n[1]), "stop": lambda: ch.stream(n[0], n[0], False)}[a.cmd]()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
