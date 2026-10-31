"""Zero-trust node isolation: quarantined peers are dropped at the socket."""

import time


class QuarantineManager:
    def __init__(self):
        self.nodes = {}  # id -> {"reason", "at"}

    def add(self, node_id, reason):
        new = node_id not in self.nodes
        if new:
            self.nodes[node_id] = {"reason": reason, "at": time.time()}
        return new

    def remove(self, node_id):
        return self.nodes.pop(node_id, None) is not None

    def __contains__(self, node_id):
        return node_id in self.nodes

    def ids(self):
        return set(self.nodes)

    def snapshot(self):
        return {str(k): v for k, v in self.nodes.items()}
