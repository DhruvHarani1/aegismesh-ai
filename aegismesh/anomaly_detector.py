"""Telemetry triggers: turn raw per-link / per-node stats into discrete anomalies.

Debounced: a condition must hold for `confirm` consecutive polls before it
fires, and a degraded link needs `recover_polls` clean polls to be declared healed.
"""

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Anomaly:
    kind: str                # link_degraded | link_recovered | rogue_node
    subject: str             # "B-C" or node name
    evidence: dict = field(default_factory=dict)


class AnomalyDetector:
    def __init__(self, loss_threshold=50.0, confirm=2, recover_loss=10.0, recover_polls=6,
                 rogue_threshold=12, crc_flood=40):
        self.loss_threshold, self.confirm = loss_threshold, confirm
        self.recover_loss, self.recover_polls = recover_loss, recover_polls
        self.rogue_threshold = rogue_threshold
        self.crc_flood = crc_flood
        self.bad = defaultdict(int)
        self.good = defaultdict(int)
        self.rogue_hits = defaultdict(int)
        self.degraded = set()
        self.flagged_rogue = set()

    def evaluate(self, statuses, client):
        out = []
        seen = set()
        isolated = {n for st in statuses.values() for n in st.get("quarantine", {})}
        # a node that is no longer isolated (released, or the order failed) may be flagged again
        self.flagged_rogue &= isolated
        for a, st in statuses.items():
            for l in st.get("links", []):
                if a in isolated or l["neighbor"] in isolated:
                    continue  # links to a quarantined node are dead by design, not a fault
                key = "-".join(sorted((a, l["neighbor"])))
                if key in seen:
                    continue
                seen.add(key)
                t = client.get_link_telemetry(a, l["neighbor"], statuses)
                loss = t["packet_drop_pct"]
                if loss is None:
                    continue
                if loss > self.loss_threshold:
                    self.bad[key] += 1
                    self.good[key] = 0
                    if self.bad[key] >= self.confirm and key not in self.degraded:
                        self.degraded.add(key)
                        out.append(Anomaly("link_degraded", key, t))
                elif loss <= self.recover_loss:
                    self.bad[key] = 0
                    if key in self.degraded:
                        self.good[key] += 1
                        if self.good[key] >= self.recover_polls:
                            self.degraded.discard(key)
                            out.append(Anomaly("link_recovered", key, t))
        # rogue-node detection. Policy violations (well-formed frames that break the rules, forged
        # control frames) are strong evidence. CRC failures alone are what a noisy link produces, so
        # they only count at flood rates, far above anything line noise causes.
        score = defaultdict(int)
        detail = defaultdict(list)
        for observer, st in statuses.items():
            for l in st.get("links", []):
                n = l["dpi_recent"] + (l["crc_recent"] if l["crc_recent"] >= self.crc_flood else 0)
                if n:
                    score[l["neighbor"]] += n
                    reason = st["dpi"]["last_reason"].get(str(l["id"])) or st["dpi"].get("last_l2_reason", {}).get(str(l["id"]))
                    detail[l["neighbor"]].append({"observer": observer, "dpi_recent": l["dpi_recent"],
                                                  "crc_recent": l["crc_recent"], "reason": reason})
        for node, s in score.items():
            if s >= self.rogue_threshold and node not in self.flagged_rogue:
                self.flagged_rogue.add(node)
                out.append(Anomaly("rogue_node", node, {"score": s, "observers": detail[node]}))
        return out
