# AegisMesh-AI — The Complete Explanation

A team guide to what this project is, how every part works, what each thing on the screen means,
and what we are demonstrating to the faculty.

Read order for someone new: Part 1 (the idea) → Part 3 (the UI) → Part 4 (the demo story) → the rest as needed.

---

## Part 1 — The big picture

### 1.1 One-paragraph summary

AegisMesh-AI is a small computer network that we built **from scratch**. Five computers ("nodes", A to E)
talk to each other over UDP using **our own wire format** (our own frame layout, our own error detection,
our own reliability protocol, our own routing algorithm). On top of that network runs an **AI-style
operations agent** that watches the network's health, and when something breaks (a bad link, or a
misbehaving node) it **fixes the network by itself** without a human touching anything. A live dashboard
shows all of this happening.

### 1.2 Why this is not a normal student networking project

Most networking projects either (a) use a simulator such as Packet Tracer or Mininet, where the
"network" is a drawing, or (b) just call an HTTP library and never see a single byte on the wire.

We do the opposite. Everything the textbook says happens *inside* the network is implemented by us in code:

| Textbook topic | What we wrote |
|---|---|
| Framing and byte stuffing | `aegismesh/framing.py` |
| CRC error detection | `aegismesh/crc32.py` |
| Selective Repeat flow control | `aegismesh/selective_repeat.py` |
| Link-state routing and Dijkstra | `aegismesh/lsa_engine.py`, `topology_db.py`, `dijkstra.py` |
| Deep packet inspection and firewall | `aegismesh/dpi_inspector.py`, `firewall.py`, `quarantine.py` |
| Software-defined networking (a "control plane" that reconfigures the "data plane") | `aegismesh/netops_agent.py`, `mesh_tools.py`, `mcp_server.py` |

The only thing we borrow from the operating system is the ability to send a UDP datagram. We then
build a whole networking stack on top of those datagrams.

### 1.3 What we are demonstrating, in one line

> A network built from first principles that **detects its own failures and attacks, and repairs
> itself in well under a second once it has decided to act, without dropping data.**

### 1.4 The two "planes" (very important idea)

Modern networks (SDN = Software-Defined Networking) are split into two parts:

- **Data plane** — the part that moves user data from A to E as fast as possible. In our project this is
  the nodes: framing, CRC, retransmission, forwarding by routing table.
- **Control plane** — the part that *decides* how the network should behave: which paths to use, which
  links are bad, which nodes to block. In our project this is the AI NetOps agent.

The agent never touches user data. It only reads statistics and sends configuration commands
("change this cost", "block this node"). That separation is exactly the SDN idea.

```
                +-------------------------------------------+
   CONTROL      |  AI NetOps agent  (netops_agent.py)       |
   PLANE        |  reads telemetry -> decides -> acts       |
                +----------------+--------------------------+
                                 | HTTP: /status (read), /cost, /quarantine (write)
        ---------------------------------------------------------------
                                 v
   DATA PLANE      5 nodes A..E exchanging our own frames over UDP
```

### 1.5 The network we built

Five nodes, six links. The number on each link is its **cost** (lower = preferred).

```
        B ---(1)--- C
       /|            \
    (1) |             (1)
     /  |               \
    A  (2)               E
     \  |               /
    (2) |             (2)
       \|            /
        D -----------
```

Links: A–B (1), B–C (1), C–E (1), B–D (2), D–E (2), A–D (2).

- Normal path A → E is **A → B → C → E**, total cost 3 (the top route in the drawing).
- Backup path is **A → D → E**, total cost 4 (the bottom route). It is longer, so it is only used when the
  main path fails. That is why when B–C breaks, traffic switches to A → D → E.

---

## Part 2 — How each piece works

### 2.1 The frame: our own packet format (`framing.py`)

Every message on the wire is a **frame**. Frame layout (all numbers big-endian):

```
 7E | src | dst | type | seq(2) | ack(2) | length(2) | payload (0..1024) | CRC-32 (4) | 7E
 ^                                                                                    ^
 start flag                                                                    end flag
```

| Field | Size | Meaning |
|---|---|---|
| Start/End flag `0x7E` | 1 byte each | Marks where a frame begins and ends |
| `src`, `dst` | 1 byte each | Node IDs (A=1 ... E=5). Up to 256 nodes |
| `type` | 1 byte | `01` DATA, `02` ACK, `03` NACK, `04` LSA flood, `05` HEARTBEAT, `06` QUARANTINE |
| `seq`, `ack` | 2 bytes each | Sequence number of this frame / acknowledgment number |
| `length` | 2 bytes | How many payload bytes follow (max 1024) |
| `CRC-32` | 4 bytes | Error-detection code over header + payload |

**Byte stuffing — why it exists.** The flag byte `0x7E` marks frame boundaries. But what if the data itself
contains the value `0x7E`? The receiver would think the frame ended early. The fix:

- Inside a frame, every `0x7E` is sent as the two bytes `0x7D 0x5E`.
- Every `0x7D` (the escape byte) is sent as `0x7D 0x5D`.

The receiver reverses this. After stuffing, a real `0x7E` can only mean "frame boundary". This is the
same idea as PPP/HDLC framing. In the UI's wire inspector the stuffed pairs are shown in **pink**.

Order of operations when sending: build header + payload → compute CRC over them → append CRC → **then**
stuff the whole thing → add the two flags. The receiver does it in reverse: strip flags → un-stuff →
check CRC.

### 2.2 CRC-32: detecting corruption (`crc32.py`)

The sender computes a 32-bit checksum over the header and payload and appends it. The receiver recomputes
it. If the numbers differ, the frame was damaged in transit and is **dropped**.

- We use the IEEE 802.3 polynomial (the same one Ethernet uses), `0xEDB88320` in reflected form.
- The idea is polynomial division over GF(2): treat the data as one huge binary polynomial, divide by a
  fixed 33-bit polynomial, and the 32-bit remainder is the CRC. We generate a 256-entry lookup table by
  doing that division bit by bit, then use the table for speed.
- The unit tests prove it matches Python's `zlib.crc32` and the standard check value (`CRC("123456789")
  = 0xCBF43926`), and that **flipping any single bit of any frame is always detected**.

### 2.3 Selective Repeat ARQ: reliable delivery (`selective_repeat.py`)

UDP is unreliable: packets can vanish. ARQ (Automatic Repeat reQuest) adds reliability by acknowledging
frames and re-sending lost ones.

We use **Selective Repeat**, the efficient variant:

- **Sliding window of 8.** The sender may have up to 8 unacknowledged frames "in flight" at once.
- **Modulo-16 sequence numbers.** Sequence numbers wrap 0..15. The rule is that the sequence space must be
  at least *window_send + window_receive* (8 + 8 = 16). If it were smaller, the receiver could not tell a
  new frame from an old retransmission.
- **Receiver buffers out-of-order frames.** If frames 0, 2, 3 arrive but 1 is lost, the receiver keeps 2 and
  3 and waits for 1 (Go-Back-N would throw them away).
- **Selective ACK (SACK).** Each ACK says "I have everything below N" plus a bitmap of which later frames
  are already buffered. The sender then retransmits **only the missing ones**.
- **Independent timers.** Every in-flight frame has its own timestamp; when it exceeds the
  retransmission timeout (RTO) that frame alone is resent.
- **Adaptive RTO.** The timeout is not fixed. It follows the measured round-trip time using the
  Jacobson/Karels formula (`RTO = SRTT + 4 × RTTVAR`), clamped between 80 ms and 2 s, and uses Karn's rule
  (retransmitted frames are not used for timing, because you can't tell which copy was acknowledged).
  Repeated timeouts back off exponentially.

**Important design choice:** ARQ runs **per hop**, on each link separately (A↔B, B↔C...), not end to end.
Each link recovers its own losses locally, which is faster and keeps a bad link from hurting the rest.

### 2.4 Discovering neighbours: heartbeats (`node.py`)

Every 0.25 seconds each node sends a **HEARTBEAT ping** to each neighbour, who replies with a pong.
From these we measure, per link:

- **Round-trip time (RTT)** — from the timestamp echoed in the pong.
- **Loss %** — of the last 8 pings, how many got no answer within 1 second.
- **RTT variance (jitter).**
- **Link up/down** — a link is declared down if nothing at all is heard for 8 seconds.

(The proposal says 1 second between heartbeats; we use 0.25 s so failures are noticed quickly during a
3-minute demo. It is one number in `mesh.json`.)

### 2.5 Link-state routing (`lsa_engine.py`, `topology_db.py`, `dijkstra.py`)

How does node A know the way to E? Every node builds the **same map of the whole network** and computes
paths itself.

1. **Advertise.** Each node creates a **Link-State Advertisement (LSA)**: "I am node X, my neighbours are
   these, and my link costs are these." It carries a sequence number.
2. **Flood.** The LSA is sent to all neighbours, who forward it to all *their* neighbours (except the one
   it came from), and so on until everyone has it. Sequence numbers make flooding **loop-free**: a node
   only forwards an LSA that is *newer* than the one it already has, so an old copy dies out immediately.
3. **Store.** Each node keeps all LSAs in its **Link-State Database (LSDB)**. Because every node
   receives every LSA, every LSDB converges to the same complete graph.
4. **Compute.** Each node runs **Dijkstra's shortest-path algorithm** on that graph with itself as the
   source, and writes the result into its **Forwarding Information Base (FIB)**: "to reach E, send to B".
5. **Refresh.** LSAs are re-sent every second so a lost flood packet heals itself. Entries older than 10
   seconds are deleted.

Extra rules we added: a link only counts if **both ends advertise it** (two-way check), and LSAs carry a
cryptographic tag (see 2.8) so they can't be forged.

**Dijkstra complexity:** with a binary heap it is O((V + E) log V). For 5 nodes it takes about 0.04
milliseconds, which is why re-routing feels instant.

### 2.6 Forwarding data (`node.py`)

User data rides inside DATA frames. Inside the frame payload we put a tiny extra header that behaves like
IP/UDP:

```
 "AG" magic (2) | TTL (1) | proto (1) | src port (2) | dst port (2) | application data
```

When a frame arrives at a node that is not its destination, the node looks up the destination in its FIB,
decrements the TTL (so a routing loop can't last forever), and submits the frame to the Selective Repeat
sender of the next-hop link.

**No loss on re-route:** when the routing table changes, any frames still waiting on the old link (queued
or unacknowledged) are moved to the new next hop. That is how the stream survives a link failure with zero
missing frames.

### 2.7 Deep Packet Inspection and the firewall (`dpi_inspector.py`, `firewall.py`)

Every DATA frame is examined layer by layer before being forwarded or delivered:

| Layer | Check |
|---|---|
| L2 (link) | Frame structure valid, CRC correct. Failures are counted per neighbour. |
| L3 (network) | Source and destination are real node IDs; TTL not expired. |
| L4 (transport) | The "AG" header is well-formed; the **5-tuple** (src, dst, protocol, source port, destination port) is checked against the firewall. Only ports 5000 and 5001 are allowed; anything else is denied. |
| L7 (application) | Text ports must carry valid, printable UTF-8. Binary garbage is rejected. |

The firewall is **stateful**: it keeps a flow table of every allowed 5-tuple with packet/byte counters.
Every violation is recorded **against the neighbour that handed us the frame**, in a 5-second sliding
window. That "who is misbehaving" count is the evidence the AI agent uses to catch a rogue node.

### 2.8 Zero-trust and quarantine (`quarantine.py`, `lsa_engine.py`)

"Zero trust" means no node is trusted just because it is on the network.

- **Authenticated control traffic.** LSAs and quarantine messages carry an 8-byte **HMAC-SHA256** tag
  computed with a shared secret. A node without the key cannot forge routing information or fake a
  quarantine order. Messages that fail the check are dropped and counted as a violation.
- **Quarantine.** When a node is quarantined, every other node: (1) removes it from its routing map and
  recomputes paths, (2) adds a firewall rule blocking its traffic, and (3) **drops anything arriving from
  its network address before even parsing it**. The rogue is isolated at the socket level.

### 2.9 The control API and chaos (`control_api.py`, `chaos_injector.py`)

Each node also runs a small HTTP server (ports 9101–9105) protected by a token. It serves live statistics
(`/status`, `/wire`) and accepts commands (`/cost`, `/quarantine`, `/chaos`, `/infect`, `/stream`).
The agent, the dashboard and the chaos buttons all use this one API.

**Chaos injection** deliberately breaks things so we can show recovery:

- **Sever link** sets 90% packet loss and 400 ms round-trip delay on both directions of a link. Packets
  are dropped or delayed in software right before they are sent. (The link still "works" a little — it is
  degraded, not cut, which is the harder and more realistic case.)
- **Infect node** puts a node into rogue mode: it continuously sends frames with **corrupted CRCs** and
  well-formed frames that **break policy** (forbidden port 6666, or binary junk to a text port).

### 2.10 The AI NetOps agent (`netops_agent.py`, `anomaly_detector.py`, `mesh_tools.py`)

The agent runs a closed loop every 0.5 seconds:

```
 INGEST  ->  DETECT  ->  DIAGNOSE  ->  ACTUATE  ->  VERIFY  ->  POST-MORTEM
 poll all    packet loss   is there a      call an      did the     write an
 nodes       > 50%? DPI    safe backup     MCP tool     path really incident
             alarms?       path?                        change?     report
```

1. **Ingest** — collects `/status` from all five nodes.
2. **Detect** — the anomaly detector turns numbers into events:
   - *Degraded link*: measured loss above 50% for 2 polls in a row (debounced so one bad sample doesn't
     trigger anything).
   - *Rogue node*: 12 or more DPI/CRC violations within 5 seconds attributed to one node.
   - *Recovered link*: loss at or below 10% for 6 polls — the agent then restores the original cost.
3. **Diagnose** — for a bad link it removes that link from the map, finds the best alternative path, and
   checks no hop on it is above 20% loss. It will **not** switch to a path that is also bad. It writes its
   reasoning in plain English to the trace.
4. **Actuate** — calls one of the four tools (below).
5. **Verify** — keeps polling until the route has really changed, and measures how long it took.
6. **Post-mortem** — writes a markdown incident report to `runtime/rca/` (evidence, action, before/after
   path, timing, final graph costs).

**The four tools (the "MCP tool contract")**:

| Tool | What it does |
|---|---|
| `get_mesh_topology()` | Returns active nodes, edges and current costs |
| `get_link_telemetry(a, b)` | Returns loss %, latency variance, CRC error count for a link |
| `update_link_cost(a, b, metric)` | Overrides a link's cost. Both endpoints re-flood an LSA and every node re-runs Dijkstra |
| `quarantine_node(id, reason)` | Tells every node to isolate that node |

**MCP** (Model Context Protocol) is an open standard that lets an AI system call tools in a uniform way.
`mcp_server.py` exposes exactly these four tools as an MCP server, so any MCP-capable AI (for example an
LLM agent) could drive our network. **Honest note:** our built-in agent is a deterministic rule-based
reasoner (a state machine), not a large language model. It is explainable and works offline. The
architecture is ready for an LLM to replace the reasoning step, because the tools are the same.

The trick behind "reroute": setting a link's cost to 9999 does not delete the link, it just makes
Dijkstra prefer any other path. When the link heals, cost goes back to normal.

---

## Part 3 — The dashboard, element by element

Open `http://localhost:8080` after running `python start.py`.

### 3.1 Live mesh topology (top left)

| You see | Meaning |
|---|---|
| Circles A–E | The five nodes. Small text under the letter shows `spf 0.04ms` (how long that node's last Dijkstra run took), or `ROGUE`, `QUARANTINED`, or `down`. |
| **Green line** | Healthy link: measured loss under 10%. |
| **Amber line** | Degraded: 10–50% loss. |
| **Red pulsing line** | Failing: 50% or more loss. |
| **Grey dashed line** | Link is down, or touches a quarantined node. |
| **Thick glowing line** | The **active path** that the A→E stream is currently taking. This is the most important thing to watch: it jumps when the network heals itself. |
| Label `cost 1 · 0% loss` | The cost Dijkstra uses for that link and the loss measured by heartbeats. `cost 9999` means the agent has blocked the link. |
| Red circle | A node in rogue mode. Grey dashed circle: quarantined node. |

**Buttons under the map:**

- **Link dropdown + Sever link** — injects 90% loss and 400 ms delay on the chosen link.
- **Heal link** — removes the impairment.
- **Node dropdown + Infect node / Cure** — turn rogue behaviour on or off.
- **Release quarantine** — lets a quarantined node back in.
- **Stream: source → destination, Start / Stop** — a continuous numbered test stream (about 25 frames per
  second). This is the "user traffic" we are protecting. `start.py` starts A → E automatically.

### 3.2 Selective Repeat window (middle left)

Eight boxes = the sender's window of 8 for the link chosen in the dropdown (default B→C). Each box is one
sequence number:

- **Blue "sent"** — transmitted, waiting for ACK.
- **Green "acked"** — acknowledged (but the window can't slide until earlier ones are acked too).
- **Orange pulsing "retx ×N"** — retransmitted N times. This is the *retransmit alarm*: lots of orange =
  the link is losing packets.
- **Faded "free"** — slot available.

Below it: base/next sequence numbers, queued frames, current RTO, total frames sent and retransmits. When
you sever B–C, watch this panel fill with orange.

### 3.3 Link telemetry (bottom left)

A table with one row per link: **loss %**, **RTT ms**, **RTT variance** (jitter), **CRC errors** (frames that
failed the checksum), **DPI 5s** (policy violations in the last 5 seconds), **retx** (retransmissions) and
**cost**. This is the same raw data the AI agent reads. Numbers turn amber/red when the link is unhealthy.

### 3.4 AI NetOps agent — decision trace (top right)

A live "thought terminal". Line colours:

- **Blue `[thought]`** — the agent's reasoning ("loss on B-C is 88%, alternative path A→D→E verified healthy").
- **Yellow `[tool_call]`** — it is calling an MCP tool.
- **Green `[tool_result]`** — the tool's answer.
- **Purple `[rca]`** — it wrote a post-mortem report.
- **Grey `[info]`**, **red `[error]`**.

This is the proof that the **decision was made by the control plane**, not by a human.

### 3.5 Delivery panel

Shows the test stream as seen **at the destination node**: unique frames received, **missing / in flight**
(should stay at or near 0 — this is the "no frames lost" claim), average latency in milliseconds, and
seconds since the last frame arrived (if this climbs, the stream is stalled).

### 3.6 Wire inspector

The raw bytes of the last frames seen by node E, in hex. Colour code: **orange `7E`** = frame flag, **pink
`7D xx`** = a stuffed escape pair, **teal** = the CRC-32. The line above each frame decodes it: direction
(tx/rx), neighbour, type, source→destination, sequence, ack, length, CRC. Frames that fail validation show
**INVALID** with the reason (for example a CRC mismatch). This panel is how we prove the protocol is real:
the examiner can literally see our byte-level format.

### 3.7 Node event log (bottom right)

A merged log of what the nodes report: `link B UP`, `FIB updated`, `QUARANTINE node D`,
`DPI drop from D: ...`, `chaos on ...`.

---

## Part 4 — What we demonstrate (the story to tell, with what is happening underneath)

Approximate timeline for a live demo (the whole thing fits in about 3 minutes).

### Scene 1 — Steady state (what a healthy network looks like)

**Say:** "These are five independent nodes exchanging our own frames over UDP. Data from A reaches E over
the cheapest path, A→B→C→E."

**Point at:** the glowing A–B–C–E path; the wire inspector (flags, stuffed bytes, CRC); the delivery panel
(0 missing).

**Underneath:** heartbeats every 0.25 s; LSAs flooding every second; every node holds the same LSDB and
runs Dijkstra; Selective Repeat acknowledges every hop.

### Scene 2 — Chaos: a link degrades

**Do:** pick `B-C`, press **Sever link**.

**Say:** "We now inject 90% packet loss and 400 ms of latency on the B–C link, simulating physical
degradation."

**Point at:** the Selective Repeat panel turning orange (retransmissions); the link turning red and
pulsing; loss % rising in the telemetry table.

**Underneath:** B and C drop 90% of the frames they send each other, and delay the rest. Selective Repeat
keeps retransmitting only the missing frames. The heartbeat window fills with failures so the measured
loss climbs above 50%.

### Scene 3 — Autonomous self-healing

**Say:** "Nobody touches anything. The AI agent reads telemetry, sees the problem and fixes it."

**Point at:** the decision trace. It shows, in order: loss detected → backup path A→D→E verified healthy →
`update_link_cost(B, C, 9999)` → verified, converged in tens of milliseconds. The glowing path moves to
A–D–E. The delivery panel stays at 0 missing.

**Underneath:** both ends of B–C advertise cost 9999 in a new LSA; it floods across the mesh in
milliseconds; every node re-runs Dijkstra (≈0.04 ms) and updates its FIB; frames queued for the bad link
are moved to the new path. The agent verifies by checking the new path and writes an incident report.

**Numbers to quote (measured on our run):** *detection* takes several seconds (about 4–6 s: the agent needs
enough heartbeat samples to be sure and two confirming polls, so it doesn't react to noise); once it acts,
the time from the agent's command to a verified new path was **30–45 ms**, and the shortest-path
computation itself was **~0.04 ms**. Frames missing at the destination: **0**. Be precise in the viva:
"sub-second repair after the decision", not "sub-second from the moment the link breaks".

### Scene 4 — Rogue node

**Do:** pick `Node D`, press **Infect node**.

**Say:** "Now a node is compromised. It floods corrupted frames and traffic that violates policy."

**Point at:** red circle on D; INVALID frames in the wire inspector; DPI counters rising in the telemetry
table; then the trace: DPI alarms attributed to D → `quarantine_node(D, reason)` → D turns grey dashed and
says QUARANTINED; a second incident report.

**Underneath:** neighbours' DPI count L2 CRC failures, forbidden-port frames and non-UTF-8 payloads
against the link they came from. Twelve or more in 5 seconds trips the detector. The agent orders every
node to quarantine D: routes through D are removed, a firewall rule blocks D, and D's packets are dropped at
the socket before parsing. The flood is stopped without taking the network down.

### Scene 5 — Recovery

**Do:** press **Heal link** and **Cure**, optionally **Release quarantine**.

**Say:** "When the link is healthy again the agent notices and restores the original cost automatically."

Note: after D is quarantined *and* B–C is still severed, the only path to E is the degraded B–C link
(there's no other route), so the stream will stutter until you heal it. That is correct behaviour, not a
bug, and is a good thing to point out: "the agent only avoids bad links when a good alternative exists."

---

## Part 5 — Mapping to the syllabus (for the faculty)

| Syllabus topic | Concept | Where it is in the project | What proves it live |
|---|---|---|---|
| Data Link Layer | Framing and byte stuffing | `framing.py` | Pink `7D xx` pairs in the wire inspector |
| Data Link Layer | Error detection, CRC-32 | `crc32.py` | INVALID frames from a corrupted CRC; unit test flips every bit |
| Transport Layer | Flow control, Selective Repeat | `selective_repeat.py` | The 8-slot window panel with retransmissions |
| Network Layer | Dynamic routing, Dijkstra | `lsa_engine.py`, `topology_db.py`, `dijkstra.py` | The glowing path switching |
| Security | DPI, firewall, zero trust | `dpi_inspector.py`, `firewall.py`, `quarantine.py` | Node D being detected and isolated |
| Modern systems | SDN, autonomous AI control plane, MCP | `netops_agent.py`, `mcp_server.py` | The decision trace and incident reports |

---

## Part 6 — Who explains what (viva preparation)

| Member | Area | Key questions to be ready for |
|---|---|---|
| 1 Transport & framing | `framing.py`, `crc32.py`, `selective_repeat.py` | Why is the sequence space ≥ Ws + Wr? What happens if the window is too big? How is the timeout computed? Why must stuffing happen after the CRC? How does CRC detect errors (polynomial division)? |
| 2 Routing & topology | `lsa_engine.py`, `topology_db.py`, `dijkstra.py` | How does flooding avoid loops? How fast does it converge? Dijkstra complexity? Why a two-way check? What if an LSA is lost? |
| 3 DPI & security | `dpi_inspector.py`, `firewall.py`, `quarantine.py` | What does each layer's check do? What is a 5-tuple and why stateful? How is a rogue node isolated? Why HMAC on control frames? |
| 4 AI / control plane | `netops_agent.py`, `anomaly_detector.py`, `mcp_server.py` | What is MCP? Why separate control and data plane? Why debounce? How does the agent know it worked? Why is it not an LLM? |
| 5 UI & chaos | `dashboard/`, `chaos_injector.py` | How does the browser get live data (server-sent events)? How are loss and delay injected? What does each panel show? |

### Likely questions and short answers

**Q: Is it really raw sockets?**
It is UDP datagram sockets. We do not use TCP or HTTP for the data plane. Everything above UDP — framing,
CRC, reliability, routing — is ours. (We did not use raw IP sockets, which need administrator rights and
differ across operating systems.)

**Q: Why does reliability run per hop rather than end to end?**
Each hop repairs its own losses locally and quickly, and one bad link doesn't force whole-path
retransmissions. It also lets us move queued frames to a new path when routes change.

**Q: Why not use a bigger window or more sequence numbers?**
With modulo 16 the window can be at most 8 for Selective Repeat to be correct. Larger windows need a larger
sequence space.

**Q: How do you know the network healed itself and a human didn't?**
The decision trace shows each thought and tool call with timestamps, and an incident report is written. The
human only pressed the chaos button.

**Q: Why not just let Dijkstra handle the failure automatically?**
Dijkstra only reacts to links that are *down*. A link with 90% loss still answers occasionally, so it stays
"up" and keeps attracting traffic. Detecting *quality* problems and rewriting costs is the control plane's
job. That is precisely the value of the AI agent.

**Q: Is the agent really AI?**
It is an autonomous, closed-loop agent with explicit reasoning steps, but the reasoning is rule-based, not a
language model. We say this openly. The MCP server lets any LLM take over the same tools.

**Q: Did you test it on five laptops?**
Our automated tests and verification runs used five processes on one machine communicating over real UDP
sockets. The same code runs one node per laptop; only the IP addresses in `mesh.json` change. (Do a dry run
on the real laptops before the viva.)

**Q: What are the limits?**
LSA flooding is not individually acknowledged (it is refreshed every second instead). Chaos is injected in
software at the sender, not on a real radio link. Dijkstra timing quoted is for 5 nodes. The shared secret is
a demo value. These are acceptable trade-offs for a teaching system.

---

## Part 7 — Running it

```bash
python start.py          # everything: nodes + agent + dashboard + stream; opens the browser
```

Ctrl+C stops it. More detail, laptop setup and troubleshooting are in [GUIDE.md](../GUIDE.md).

Key files to know: `mesh.json` (topology, IPs, ports, secrets), `runtime/agent_trace.jsonl` (the agent's
reasoning log), `runtime/rca/` (incident reports), `tests/test_core.py` (unit tests:
`python -m unittest discover -s tests -v`).

### Tunable numbers (all in the code, for reference)

| Setting | Value |
|---|---|
| Heartbeat interval | 0.25 s (`hb_interval`) |
| Heartbeat reply timeout | 1.0 s (`hb_timeout`) |
| Loss measurement window | last 8 heartbeats |
| Link declared dead after | 8 s of silence |
| LSA refresh / max age | 1 s / 10 s |
| ARQ window / sequence modulo | 8 / 16 |
| ARQ timeout range | 80 ms – 2 s, adaptive |
| Agent polling | every 0.5 s |
| Degraded link trigger | > 50% loss for 2 polls |
| Link recovery trigger | ≤ 10% loss for 6 polls |
| Rogue trigger | ≥ 12 violations within 5 s |
| Blocked-link cost | 9999 |
| Allowed ports | 5000 (stream), 5001 (text) |

---

## Part 8 — Glossary

- **ACK / SACK** — acknowledgment / selective acknowledgment (which later frames were also received).
- **ARQ** — Automatic Repeat reQuest: retransmit lost frames until acknowledged.
- **Byte stuffing** — escaping special bytes so a flag value inside data isn't mistaken for a frame boundary.
- **Control plane / data plane** — the decision-making part / the packet-moving part of a network.
- **CRC** — cyclic redundancy check, a checksum that detects accidental corruption.
- **Dijkstra** — algorithm that finds the cheapest path from one node to all others.
- **DPI** — deep packet inspection: looking inside packets beyond the header.
- **FIB** — forwarding information base: the routing table ("to reach E, go via B").
- **Flooding** — sending a message to all neighbours, who pass it on, until everyone has it.
- **HMAC** — a keyed hash that proves a message came from someone holding the secret.
- **LSA / LSDB** — link-state advertisement / link-state database (the network map).
- **MCP** — Model Context Protocol: a standard way for AI systems to call tools.
- **Quarantine** — isolating a misbehaving node so none of its traffic is accepted.
- **RCA** — root cause analysis: the incident report.
- **RTO / RTT** — retransmission timeout / round-trip time.
- **SDN** — software-defined networking: central software controlling network behaviour.
- **TTL** — time to live: hop limit that stops packets looping forever.
- **Zero trust** — never assume a node is trustworthy just because it is on the network.
