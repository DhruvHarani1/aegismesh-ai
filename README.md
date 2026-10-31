# AegisMesh-AI

Self-healing zero-trust mesh overlay over raw UDP, built from `AegisMesh_AI_Project_Proposal.docx`.
Pure Python 3.10+ standard library (nothing to install). Optional: `pip install mcp` for the MCP server.

## Layout

| Path | Proposal module |
|---|---|
| `aegismesh/crc32.py`, `framing.py` | Member 1: CRC-32 (IEEE 802.3, bitwise table), 0x7E/0x7D byte-stuffed frames |
| `aegismesh/selective_repeat.py` | Member 1: Selective Repeat ARQ, Ws=Wr=8, modulo 16, per-frame timers, SACK, adaptive RTO |
| `aegismesh/lsa_engine.py`, `topology_db.py`, `dijkstra.py` | Member 2: LSA flooding (seq-numbered, HMAC-signed), LSDB, Dijkstra, FIB |
| `aegismesh/dpi_inspector.py`, `firewall.py`, `quarantine.py` | Member 3: L2-L7 DPI, stateful 5-tuple firewall, rogue-node isolation |
| `aegismesh/mcp_server.py`, `netops_agent.py`, `anomaly_detector.py`, `mesh_tools.py` | Member 4: 4 MCP tools, closed-loop agent, telemetry triggers |
| `dashboard/`, `aegismesh/chaos_injector.py` | Member 5: live topology/hex/ARQ/agent UI, chaos panel |
| `aegismesh/node.py`, `control_api.py` | Node runtime + per-node HTTP control/telemetry API |

## Run it on one machine (5 local processes)

```bash
python -m unittest discover -s tests -v          # unit tests
python run_local.py                              # terminal 1: nodes A-E on 127.0.0.1
python -m aegismesh.netops_agent                 # terminal 2: AI agent
python dashboard/server.py                       # terminal 3: http://localhost:8080
```

In the dashboard: press **Start** (stream A -> E), then follow the viva script:

1. Steady state: path A-B-C-E glows, wire inspector shows `7E` flags, stuffed `7D xx` pairs, CRC.
2. **Sever link** B-C (90% loss + 400 ms RTT): ARQ window shows retransmits, link pulses red.
3. Agent detects loss, calls `update_link_cost(B, C, 9999)`, path flips to A-D-E, 0 frames missing.
4. **Infect node** D: it floods bad-CRC frames and forbidden-port / non-UTF-8 packets; peers' DPI
   counts them, the agent calls `quarantine_node(D)`, sockets to D are dropped. RCA reports land in `runtime/rca/`.

Note: with D quarantined and B-C still severed, the only path left is the degraded one. Press
**Heal link** (the agent restores the cost after ~6 clean polls) or **Release quarantine** to recover.

CLI equivalents: `python -m aegismesh.chaos_injector sever B C | heal B C | infect D | cure D | stream A E`.

## Run it on 5 laptops

1. Put every laptop on the same network; edit `mesh.json`: set each node's `host` to that laptop's LAN IP
   (the address must match the source IP its packets leave from). Copy the same file to all laptops.
2. Allow UDP ports 9001-9005 and TCP 9101-9105 through each firewall.
3. Laptop 1..5 run `python -m aegismesh.node --config mesh.json --name A` (..E).
4. Laptop 5 also runs the agent and `dashboard/server.py` (it reaches every node through the control API).
5. Change `secret` and `token` in `mesh.json` for anything beyond a demo.

## MCP server

`pip install mcp` then `python -m aegismesh.mcp_server --config mesh.json` exposes `get_mesh_topology`,
`get_link_telemetry`, `update_link_cost`, `quarantine_node` to any MCP client.

## Deviations from the proposal (deliberate)

- **ARQ is hop-by-hop** (per link), so every hop recovers losses locally; end-to-end frames are forwarded via the FIB.
  Frames queued on a link that stops being the next hop are migrated to the new path (no loss on reroute).
- **Heartbeat 0.25 s** (not 1.0 s) so detection fits a 3-minute demo; set `params.hb_interval` in `mesh.json`.
- **Agent is a rule-based state graph**, not a LangGraph/LLM agent, so it works offline and deterministically.
  The tool contract is the same; point any MCP-capable LLM agent at `mcp_server.py` to replace the reasoner.
- **Dashboard uses vanilla SVG + Server-Sent Events** instead of D3 + WebSocket: no CDN or npm needed offline.
- LSA flooding is unacknowledged but refreshed every second, so a lost LSA self-heals; LSAs and quarantine
  frames carry an 8-byte HMAC-SHA256 tag so a rogue node cannot forge control traffic.
