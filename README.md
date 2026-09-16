# Factory Arm Project

A safety-gated autonomous recovery pipeline for a UR robot arm (simulated via URSim).
When a fault happens mid-motion, the system detects it, decides whether it's safe
to fix autonomously (Tier 1) or needs a human (Tier 2), and only a dedicated Guard
is allowed to approve or deny the actual fix — every decision is written to an
audit log.

For day-to-day working notes, decisions, and what's been validated so far, see
[`progress.md`](./progress.md). This file is the shorter "what is this and how do
I run it" overview.

## How it works

```
[URSim robot arm]
       |  raw socket commands (ports 29999 / 30002)
       v
ursim_controller.py (URSimController)   <-- the only file touching real sockets
       |  get_status() polled repeatedly
       v
monitor.py (SafetyMonitor)  --emits-->  FAULT_CONFIRMED / RECOVERED / STUCK / POLL_ERROR
       |
       v
service.py (Service.run_cycle)
       |
       +--> router.py    -> what tier is this fault? (1 or 2)
       +--> proposer.py  -> what should we do about it?
       +--> guard.py     -> is that actually safe? (only thing that can say no)
       +--> (if approved) ursim_controller.py -> actually do it
       +--> audit.py     -> write down exactly what happened
```

Tier 1 is a small, deliberately-scoped set of whitelisted recovery ops
(`clear_protective_stop`, `reset_program_pointer`, `set_payload`). Anything else
routes to Tier 2, which is currently a stub (logs `AWAITING_APPROVAL`, nothing
more) — Tier 1 is the current focus.

## Setup

- Python virtual environment: `venv/` (activate with `source venv/bin/activate`)
- Apple Silicon Macs: enable Docker Desktop's "Use Rosetta for x86/amd64
  emulation" (Settings -> General) — URSim is unusably slow without it.

Start URSim:
```bash
docker run -d -p 5900:5900 -p 6080:6080 -p 29999:29999 -p 30001-30004:30001-30004 \
  --name ursim -v ~/ursim-data:/ursim/programs \
  universalrobots/ursim_e-series
```

Pendant / 3D view: http://localhost:6080/vnc.html

Stop/remove when done:
```bash
docker stop ursim
docker rm ursim
```

## Running things

**Live demo** (watch a real move -> fault -> autonomous recovery -> confirmed
working-again move, on the pendant):
```bash
source venv/bin/activate
python3 demo.py
```
Open the pendant URL above *before* running this.

**Full service loop** against real URSim:
```bash
source venv/bin/activate
python3 service.py run --cycles 60
```

**Regression scenarios** (fast, no real hardware needed — uses an in-memory
fake controller):
```bash
source venv/bin/activate
python3 scenarios/run.py
```

**Audit trail** of every cycle the service has run:
```bash
tail -f audit_log.jsonl
```

## File map

| File | Role |
|---|---|
| `controller_adapter.py` | Abstract 8-method interface any robot controller (sim or real) must implement |
| `ursim_controller.py` | Real implementation — wraps Dashboard Server (29999) + Secondary Interface (30002) sockets |
| `monitor.py` | Polls status, debounces reads, emits fault/recovery/stuck events |
| `models.py` | Shared dataclasses: `Fault`, `Proposal`, `GateResult`, `CycleResult` |
| `router.py` | Assigns Tier 1/2 to a fault, before any proposal exists |
| `proposer.py` | `DefaultProposer` (real, whitelisted recoveries) + `ScriptedProposer` (deterministic, for demos/tests) |
| `guard.py` | The only thing that can deny a proposal — runs a fixed gate checklist |
| `audit.py` | Appends one JSON line per cycle to `audit_log.jsonl` |
| `service.py` | Wires fault -> tier -> proposal -> guard -> execute -> audit; CLI entry point |
| `demo.py` | Watchable end-to-end demo against the real URSim pendant |
| `scenarios/` | In-memory fake controller + regression suite, no real hardware needed |
| `ursim_power.py`, `ursim_basic_move.py`, `ursim_check_status.py`, `ursim_trigger_fault*.py` | Early standalone exploration scripts (pre-adapter) |

## Current status

Tier 1 is built and live-validated end-to-end against real URSim, both the
autonomous-recovery path and the deliberate-denial path. Tier 2 is a stub.
See [`progress.md`](./progress.md) for the full phase-by-phase history,
what's been proven live, and open decisions.
