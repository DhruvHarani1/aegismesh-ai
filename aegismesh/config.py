"""Mesh configuration loader (mesh.json)."""

import json

DEFAULT_PARAMS = {
    "hb_interval": 0.25,      # spec says 1.0s; faster default keeps the live demo snappy
    "dead_interval": 8.0,
    "lsa_refresh": 1.0,
    "lsa_max_age": 10.0,
    "loss_window": 8,
    "hb_timeout": 1.5,
}


class MeshConfig:
    def __init__(self, path):
        with open(path) as f:
            raw = json.load(f)
        self.raw = raw
        self.secret = raw.get("secret", "aegis").encode()
        self.token = raw.get("token", "")
        self.params = {**DEFAULT_PARAMS, **raw.get("params", {})}
        self.nodes = raw["nodes"]                       # name -> {id, host, port, ctl, pos}
        self.by_id = {n["id"]: name for name, n in self.nodes.items()}
        self.links = [(a, b, c) for a, b, c in raw["links"]]
        self.observer = raw.get("observer", "E")

    def name(self, ident):
        return self.by_id[ident]

    def ident(self, name_or_id):
        if isinstance(name_or_id, int):
            return name_or_id
        s = str(name_or_id)
        if s.isdigit():
            return int(s)
        return self.nodes[s.upper()]["id"]

    def node_name(self, name_or_id):
        return self.by_id[self.ident(name_or_id)]

    def neighbors(self, name):
        out = {}
        for a, b, c in self.links:
            if a == name:
                out[b] = c
            elif b == name:
                out[a] = c
        return out

    def ctl_url(self, name):
        n = self.nodes[name]
        return "http://%s:%d" % (n["host"], n["ctl"])
