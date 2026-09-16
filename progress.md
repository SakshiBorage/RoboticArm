# Factory Arm Project — Progress Notes

Last updated: 2026-09-16

## Setup
- Project folder: `~/factory-arm-project/`
- Virtual environment: `~/factory-arm-project/venv/` (activate with `source venv/bin/activate`)
- Mac is Apple Silicon — Docker Desktop's "Use Rosetta for x86/amd64 emulation" is ON (Settings → General). This fixed the earlier lag; without it URSim feels unusably slow.

## URSim
- Runs via Docker: `universalrobots/ursim_e-series`
- Pendant / 3D view: http://localhost:6080/vnc.html
- Ports: 29999 (Dashboard Server — coarse commands), 30002 (Secondary Interface — URScript motion), 30001-30004 (primary/secondary/RTDE range)

Start container:
```bash
docker run -d -p 5900:5900 -p 6080:6080 -p 29999:29999 -p 30001-30004:30001-30004 \
  --name ursim -v ~/ursim-data:/ursim/programs \
  universalrobots/ursim_e-series
```

Stop/remove when done:
```bash
docker stop ursim
docker rm ursim
```

## Phase status

- [x] **Phase 0 — Own architecture diagram.** Reconciled the three source docs (high-level write-up, locked architecture, Fig.1 digital-twin diagram). Guard is the only thing that can deny; router assigns tier before the agent runs; approval doesn't skip re-verification against live state.
- [x] **Phase 1 — Prove the fault source is real.**
  - Powered on URSim, confirmed `robotmode: RUNNING`.
  - Sent a basic move, watched it happen live in the pendant.
  - Deliberately triggered a fault — confirmed a genuine **Protective Stop** in the Log tab (RTMachine: "Safety Mode changed to Protective Stop"), not something invented. Originally logged as "auto-recovered ~8s later" — **correction from Phase 3 retesting below: auto-recovery is not reliable.** The same class of protective stop was reproduced later and stayed in `PROTECTIVE_STOP` for 70+ seconds with no auto-clear; it only cleared once `clear_protective_stop()` (dashboard `unlock protective stop` + `close safety popup`) was called. Treat auto-recovery as the exception, not the default — the "alarm reset" Tier 1 op is likely load-bearing, not cosmetic.
  - CLOSED (via Phase 3 below): `ursim_check_status.py`/the real adapter's `get_status()` does catch the non-NORMAL `safetystatus` programmatically — confirmed via `monitor.py` live against a real triggered fault.
  - Also worth noting: `ursim_trigger_fault_v2.py`'s original `movej(a=500, v=500)` approach stopped reproducing the fault on retest (likely because `movej`'s accel/vel targets get silently clamped by the controller rather than honored literally, and/or it depends on the arm's starting pose having enough delta to build dangerous velocity). Switched to `speedj([10,10,10,10,10,10], a=40, t=3)` — a direct per-joint overspeed command — which reliably triggered a real `PROTECTIVE_STOP` on demand, including on a freshly restarted container. Worth updating the trigger script itself later; not done yet since Phase 3's focus was `monitor.py`.
- [x] **Phase 2 — Build the real `URSimController`.** Built `controller_adapter.py` (abstract 8-method interface) and `ursim_controller.py` (implementation wrapping the Dashboard Server / Secondary Interface socket calls). Interface was derived from these notes, not a saved spec.
  - Methods: `power_on`, `power_off`, `get_status`, `clear_protective_stop`, `reset_program_pointer`, `move_joints`, `set_payload`, `emergency_stop`.
  - Live-verified against real URSim: `power_on`, `get_status`, `move_joints`, `clear_protective_stop` (recovered a real stuck `PROTECTIVE_STOP` → `NORMAL`), `set_payload`, `emergency_stop`. `reset_program_pointer` correctly returned `False` with no program loaded (stop succeeded, play failed — expected, not a bug).
  - Not yet tested: `power_off` (would leave the sim down for the rest of the session — skipped for now).
  - Two Tier 1 whitelist ops deliberately NOT given their own adapter method: quick-master after battery loss (real-hardware-only, not simulable in URSim) and sub-mm TCP write (folded into `move_joints`/a small offset script rather than a dedicated primitive).
- [x] **Phase 3 — Build & validate `monitor.py`.** Didn't exist yet — built it (debounced fault/recovery/stuck state machine polling `ControllerAdapter.get_status()`) and validated all three thresholds live against a real triggered protective stop rather than placeholder data:
  - `poll_interval=0.2s` — caught a real fault within one poll cycle.
  - `debounce_count=1` — validated safe: no false positives across ~10 idle-to-fault transitions during testing, so a single non-NORMAL read is trustworthy on this signal (no observed flicker/noise requiring debounce > 1).
  - `stuck_timeout=15.0s` — set with margin above the one ~8s auto-recovery case from Phase 1, while still flagging genuinely stuck faults promptly (validated against a real 70+s stuck protective stop).
  - Found and fixed a real gap during testing: `poll_once()` had no error handling — a transient dashboard TCP failure would have crashed the whole monitor loop. Added a guard that emits a `POLL_ERROR` event and continues instead; verified against an actual refused connection (pointed the adapter at a closed port).
- [ ] **Phase 4 — Sensor transport.** Skipped for now — software-only, no hardware access yet.
- [~] **Phase 5 — Full loop end-to-end.** Built the harness that didn't exist: `models.py` (Fault/Proposal/GateResult/CycleResult), `router.py` (tiers a fault before any proposal exists — Tier 1 only for fault types with a known whitelisted recovery, everything else defaults to Tier 2), `proposer.py` (`DefaultProposer` + `ScriptedProposer` for deterministic demos), `guard.py` (the only thing that can deny — gate list: controller reachable, op has controller support, Tier 1 whitelist / Tier 2 approval stub, params in bounds, live-state re-verification), `audit.py` (JSONL trail), `service.py` (wires it all + CLI `python3 service.py run --cycles N`), and `scenarios/` (`fake_controller.py` + `run.py`, 5/5 scenarios passing against an in-memory controller — no real hardware needed for regression runs).
  - Scope decision (open question above, now resolved): a denied Tier 1 proposal **dead-ends as escalate-only** — no auto-reroute to Tier 2. Tier 2 itself is a deliberate stub for now (logs `AWAITING_APPROVAL`, nothing more) — current focus is getting Tier 1 solid; Tier 2 build-out is explicitly next-phase work, not started.
  - Gate list is a fresh, honestly-scoped set (5 gates), not a recovery of the old "12/12 gates" figure — that harness isn't in this codebase.
  - Live-verified against real URSim end-to-end, both directions:
    - Real triggered `PROTECTIVE_STOP` → Tier 1 → `clear_protective_stop` proposed → all gates APPROVED → executed against the real arm → arm autonomously recovered to `NORMAL`, no human step.
    - Real triggered `PROTECTIVE_STOP` → deliberately proposed `set_payload(mass_kg=999)` via `ScriptedProposer` → Guard DENIED on `params_in_bounds` → not executed → arm correctly left untouched in `PROTECTIVE_STOP` (manually cleared afterward for cleanup).
  - Bug found and fixed during this validation: `guard.py`'s gate-recording helper stored the *failure* reason string in the audit log even when a gate passed (e.g. `"passed": true, "reason": "controller has no method 'clear_protective_stop'"` — misleading, since it actually passed). Fixed to blank the reason on pass; re-verified both the fake-controller scenarios and the live run produce clean audit entries.
  - Not yet done: the full `--cycles 60` unattended soak run, and real Tier 2 approval flow (currently a stub).
- [ ] **Phase 6 — Record the demo.** At least one Tier 1 autonomous fix, and (more important) one deliberate Tier 1 gate **denial** via `ScriptedProposer`. Functionally both already demonstrated live in Phase 5 above — what's left here is actually recording it.

## Scripts in this folder
- `ursim_power.py` — powers on + releases brakes via Dashboard Server (29999)
- `ursim_basic_move.py` — sends a basic safe move via Secondary Interface (30002)
- `ursim_trigger_fault.py` — angle-based fault attempt (may or may not trigger depending on joint range)
- `ursim_trigger_fault_v2.py` — acceleration/velocity-based fault attempt via `movej(a=500, v=500)`. Stopped reliably reproducing the fault as of Phase 3 retest (see note above) — `speedj([10]*6, a=40, t=3)` is the reliable trigger now, not yet promoted into this file.
- `ursim_check_status.py` — reads `safetystatus` + `robotmode` via Dashboard Server
- `controller_adapter.py` — abstract 8-method `ControllerAdapter` interface (Phase 2)
- `ursim_controller.py` — `URSimController`, the real implementation wrapping Dashboard Server + Secondary Interface socket calls (Phase 2)
- `monitor.py` — `SafetyMonitor`, debounced fault/recovery/stuck polling on top of `get_status()` (Phase 3)
- `models.py` — shared dataclasses: `Fault`, `Proposal`, `GateResult`, `CycleResult` (Phase 5)
- `router.py` — assigns Tier 1/2 to a fault before any proposal exists (Phase 5)
- `proposer.py` — `DefaultProposer` (real, whitelisted recoveries only) + `ScriptedProposer` (deterministic, for demos/tests) (Phase 5)
- `guard.py` — the only thing that can deny a proposal; 5-gate check (Phase 5)
- `audit.py` — JSONL audit trail, `audit_log.jsonl` (Phase 5)
- `service.py` — wires fault → tier → proposal → guard → execute → audit; CLI `python3 service.py run --cycles N` (Phase 5)
- `scenarios/fake_controller.py`, `scenarios/run.py` — in-memory `ControllerAdapter` + regression suite, no real hardware needed (Phase 5)

## Run order (typical session)
```bash
cd ~/factory-arm-project
source venv/bin/activate
python3 ursim_power_on.py
python3 ursim_trigger_fault_v2.py
python3 ursim_check_status.py
```

## Open questions / decisions not yet made
- Tier 1 whitelist is currently 5 candidate operations (alarm reset, program-pointer reset, quick-master after verified battery loss, bounded payload write, sub-mm TCP write) — not finalized.
- Whether a Tier 1 gate denial should auto-reroute as a Tier 2 proposal, or dead-end as escalate-only. Not settled in the locked doc.
- Whether to add Fig.1's "simulate outcome first" idea as an extra gate inside the guard stage. Deliberately not added yet.
- Whether to add a reprompt/retry loop for Tier 2 (Fig.1 has one, locked doc doesn't). Not added yet — would need explicit scoping to Tier 2 only.