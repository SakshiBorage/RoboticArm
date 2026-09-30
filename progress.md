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
  - Not yet done: the full `--cycles 60` unattended soak run.
- [~] **Phase 5b — Tier 2 built out: LLM-backed proposer + human approval.** Tier 1 stays exactly as-is (deterministic table, no API calls). New: `approval.py` (`cli_approval` — blocking terminal y/n prompt), `AgentProposer` + `CompositeProposer` in `proposer.py` (Tier 2 only; dispatches by tier so Tier 1 never touches the LLM). `service.run_cycle` now runs Tier 1 and Tier 2 through the **same** Guard path — Tier 2's only difference is the human-approval step before Guard, not a separate code path. `models.Proposal` gained a `reasoning` field so the LLM's rationale is visible in the approval prompt and the audit log.
  - Using OpenAI (`openai` 3.14.1, `client.responses.create` with `text.format = {type: json_schema, strict: true}`), not Anthropic — user's choice. Context sent per Tier 2 proposal: the fault as detected, a **freshly re-fetched** live status snapshot, a fixed catalog of only the real `ControllerAdapter` ops it may choose from (`clear_protective_stop`, `reset_program_pointer`, `move_joints`, `set_payload`, `emergency_stop`, or `escalate`), **how long the fault has been ongoing** (`SafetyMonitor.fault_duration_seconds()`, new), and **the last 5 audit log entries for this same fault_type** (`AuditLog.read_recent()`, new) so the LLM knows if this was already tried and what happened. Structured output only — no free-text parsing. Live-verified the context assembly (not the API call — no key yet) with a stub client: duration and prior-attempt history both land correctly in what gets sent.
  - Deliberately not tracking "what the arm was doing when it faulted" (last-commanded-action state) — that would need new state-tracking in `service.py`, scoped out for now; can revisit if duration + history prove insufficient once real LLM calls are tested.
  - Real `OPENAI_API_KEY` added to `.env` and live-tested (not just the stub): synthetic `JOINT_OVER_TEMPERATURE` fault → real GPT-4.1 call → returned a sensible, structured `{op: "emergency_stop", reasoning: "..."}`. Confirms the `text.format = {type: json_schema, strict: true}` round-trip works against the real API, not just the SDK's type definitions.
- [x] **`demo.py` rebuilt around 3 scenarios, random order, covers both tiers.** Two real Tier 1 triggers (pick-and-place overspeed, return-to-home overspeed — same proven `speedj` mechanism, different task context, since this URSim setup can't organically produce a second real fault type) + one synthetic Tier 2 fault (`JOINT_OVER_TEMPERATURE`, clearly labeled SIMULATED, injected directly via `service.run_cycle()`) that exercises the LLM proposal + live CLI approval prompt. Live-tested end-to-end (auto-approved via piped input): random order confirmed, both real triggers recovered correctly. The Tier 2 run surfaced a genuinely good finding, not a bug — the LLM saw the real (unfaulted) live status and proposed `clear_protective_stop`, the human approved it, but **Guard still denied it** because the real controller was never actually in `PROTECTIVE_STOP` — live proof that "approval doesn't skip re-verification against live state" (Phase 0) holds under real conditions, not just in a scenario test. Known limitation: since the synthetic fault never touches real controller state, `clear_protective_stop` proposals for it will always be denied this way — to see a Tier 2 proposal actually *execute*, the LLM needs to propose something without a live-state precondition (e.g. `emergency_stop`), or the synthetic scenario would need to fake controller status too (not done — added complexity not worth it for a demo script).
- [x] **Error/failure visibility fixed — real gaps found and closed.** User asked how failures would actually be surfaced; audit found several: `demo.py` never printed *why* something was denied (the reason was computed by `guard.py` but only ever written to `audit_log.jsonl`, never printed); `service.run()` silently dropped `POLL_ERROR`/`STUCK`/`RECOVERED` monitor events entirely (only `FAULT_CONFIRMED` was forwarded to a callback); `_execute(proposal)` had no error handling — a real execution failure would've raised uncaught, crashed the whole run, and never even reached `audit.record()` (which runs after `_execute`), so the failure wouldn't have been logged anywhere; `AgentProposer` exceptions were folded into a truncated string with no traceback saved.
  - Fixed: new `app_logging.py` (writes full detail incl. tracebacks to `service.log`, gitignored; console shows WARNING+ so failures are visible immediately, not just in the file). `_execute()` now wrapped in try/except — failures are caught, logged with full traceback, recorded in the audit entry as `"execution_error"`, and set on a new `CycleResult.execution_error` field — the run continues instead of crashing. `service.run()` gained an `on_event` callback so `POLL_ERROR`/`STUCK`/`RECOVERED` are surfaced (previously silently dropped) — both CLI (`service.py run`) and `demo.py` now print them. `demo.py`'s cycle reporting now prints the LLM's `reasoning`, the specific failing gate + reason on `DENIED`, and any `execution_error`.
  - Verified live (not just code review): deliberately broke a controller method mid-test — confirmed it doesn't crash, error appears on console immediately, full traceback lands in `service.log`, and the audit entry carries `execution_error`. Deliberately pointed a controller at a closed port — confirmed `POLL_ERROR` now flows through `on_event` and gets logged, where before it was silently swallowed.
  - `OPENAI_API_KEY` not set yet (`.env` created with a placeholder, gitignored) — user is getting one. **Verified the graceful-degradation path**: with no key, `AgentProposer` catches the error and returns `Proposal(op="escalate", reasoning="AgentProposer error: ...")` instead of crashing; `escalate` has no controller method so Guard denies it regardless of approval — safe default is "ask a human," never silently do nothing *or* silently act.
  - Not yet verified: an actual live OpenAI call (needs the real key), and that `strict: true` JSON-schema mode round-trips cleanly against the real API — schema shape was confirmed against the installed SDK's type definitions, not a live response.
  - Scenario suite grew from 5 to 7: added a Tier 2 real-op-approved case and a Tier 2 real-op-denied case; updated the old "AWAITING_APPROVAL" scenario since that state no longer exists — Tier 2 now resolves to APPROVED/DENIED synchronously through the same gate path as Tier 1.
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
- `proposer.py` — `DefaultProposer` (Tier 1, deterministic) + `ScriptedProposer` (tests/demos) + `AgentProposer` (Tier 2, OpenAI-backed) + `CompositeProposer` (dispatches by tier) (Phase 5 / 5b)
- `approval.py` — `cli_approval`, the Tier 2 human-approval gate (blocking terminal y/n) (Phase 5b)
- `guard.py` — the only thing that can deny a proposal; 5-gate check, applies to both tiers (Phase 5 / 5b)
- `audit.py` — JSONL audit trail, `audit_log.jsonl` (Phase 5)
- `service.py` — wires fault → tier → proposal → (Tier 2: approval) → guard → execute → audit; CLI `python3 service.py run --cycles N` (Phase 5 / 5b)
- `scenarios/fake_controller.py`, `scenarios/run.py` — in-memory `ControllerAdapter` + regression suite (7 scenarios), no real hardware needed (Phase 5 / 5b)
- `.env` — holds `OPENAI_API_KEY` (currently empty placeholder), gitignored

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