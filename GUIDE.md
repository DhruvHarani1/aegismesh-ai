# AegisMesh-AI User Guide

## 1. Start it

```bash
python start.py
```

This launches the five mesh nodes (A-E), the AI NetOps agent and the dashboard, starts a test stream
A -> E, and opens http://localhost:8080. Press Ctrl+C in the terminal to stop everything.
Options: `--no-browser`, `--no-stream`, `--port 9000`.

Requirements: Python 3.10+ only. No packages to install.

## 2. Run the demo

1. **Steady state.** The path A-B-C-E glows on the topology. The wire inspector shows frames with orange
   `7E` flags, pink stuffed `7D xx` pairs and a teal CRC-32. The delivery panel shows 0 missing frames.
   (If the stream is not running, pick the source and destination and press **Start**.)
2. **Sever a link.** Choose `B-C` and press **Sever link**. It injects 90% loss and 400 ms RTT. The
   Selective Repeat window shows retransmissions and the link pulses red.
3. **Watch the AI heal it.** Within a few seconds the decision trace shows the agent detecting the loss,
   verifying an alternative path, calling `update_link_cost(B, C, 9999)` and confirming the new path
   A-D-E. The stream keeps flowing with no frames lost. A post-mortem lands in `runtime/rca/`.
4. **Infect a node.** Choose `Node D` and press **Infect node**. D floods corrupted-CRC frames and
   forbidden-port packets. Peers' deep packet inspection flags it and the agent calls
   `quarantine_node(D)`; D is dropped at the socket and shown as QUARANTINED.
5. **Recover.** With D quarantined and B-C still severed, the only path left is the degraded one.
   Press **Heal link** (the agent restores the cost after about 6 clean polls) or
   **Release quarantine**, and **Cure** to stop D misbehaving.

The same actions are available from the command line:

```bash
python -m aegismesh.chaos_injector sever B C
python -m aegismesh.chaos_injector heal B C
python -m aegismesh.chaos_injector infect D
python -m aegismesh.chaos_injector cure D
```

## 3. Run on five laptops

1. Put all laptops on the same network.
2. Edit `mesh.json`: set each node's `host` to that laptop's LAN IP (it must match the source IP its
   packets leave from). Copy the same file to every laptop.
3. Allow UDP 9001-9005 and TCP 9101-9105 through each laptop's firewall.
4. On each laptop run its own node:

   ```bash
   python -m aegismesh.node --config mesh.json --name A
   ```

   (use `B`, `C`, `D`, `E` on the other laptops).
5. On the observer laptop (Laptop 5), also run the agent and dashboard:

   ```bash
   python -m aegismesh.netops_agent
   python dashboard/server.py
   ```

6. Change `secret` and `token` in `mesh.json` for anything beyond a demo.

## 4. Deviations from the proposal

- **The agent is a rule-based state machine, not LangGraph or an LLM.** It runs offline and
  deterministically. It uses the same four MCP tool contract; point any MCP-capable LLM agent at
  `aegismesh/mcp_server.py` (`pip install mcp`) to replace the reasoner.
- **The dashboard uses plain SVG and server-sent events instead of D3 and WebSocket.** No CDN or npm
  is needed, so it works offline.
- **Heartbeats run every 0.25 s instead of 1.0 s,** so failures are detected fast enough for a
  3-minute demo. Change `params.hb_interval` in `mesh.json` to restore 1.0 s.
- **ARQ runs per hop.** Each link recovers its own losses locally. Frames queued on a link that stops
  being the next hop are moved to the new path, so a reroute loses nothing in flight.
- LSA flooding is unacknowledged but refreshed every second. LSAs and quarantine frames carry an
  8-byte HMAC-SHA256 tag so a rogue node cannot forge control traffic.

## 5. Troubleshooting

- **Dashboard shows "connecting"**: the dashboard process failed to start; check that port 8080 is free
  or use `--port`.
- **A node shows "down"**: another process holds its UDP port (9001-9005); stop old runs first.
- **Reset everything**: Ctrl+C, then `python start.py` again.
