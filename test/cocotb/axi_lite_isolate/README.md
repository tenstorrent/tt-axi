# axi_lite_isolate block-level testbench (cocotb + VCS)

Verifies `axi_lite_isolate` with `TerminateTransaction = 1` (the SEP crypto
isolation configuration): pass-through and termination behavior, drain
semantics, demux select routing stability in both directions, counter sizing,
and randomized isolate stress. AXI4-Lite counterpart of the `axi_isolate`
bench next door; no ATOPs, no IDs, no bursts.

## Running

```bash
module load synopsys/vcs/W-2024.09-SP2-5
source <a python env with cocotb 1.x>/bin/activate

cd test/cocotb/axi_lite_isolate
make sim-vcs                             # full suite (12 tests)
make sim-vcs MODULE=test_drain TESTCASE=test_slverr_during_drain WAVES=1
```

The Makefile resolves `common_cells` through bender (first run clones it into
`.bender/`).

`WAVES=1` dumps `tb_axi_lite_isolate.vcd` (`nWave tb_axi_lite_isolate.vcd &`
after `module load synopsys/verdi/X-2025.06-SP2-3`).

## Bench structure

* `tb_axi_lite_isolate.sv` — flattens struct ports for VPI, clock, `obs_*`
  probes of internals, and the select-stability SVAs (anchored on
  demux-INTERNAL valids; a host-level antecedent would false-positive on
  benign select-FIFO gate stalls). An elab guard `$fatal`s unless
  `NUM_PENDING == 4` and `InnerPending == 9`.
* `helpers.py` — probes, AXI4-Lite drivers, `DownstreamSlave` (backpressure
  knobs, records all deliveries), `RespMonitor` (records every B/R with the
  isolate state at completion). Timing convention: test deposits land just
  after the posedge via `settle()`; `ReadOnly` only in background samplers.
* Key numbers (NUM_PENDING=4): the demux's select FIFOs bound writes at
  **2\*4 = 8** (W-select FIFO + B-select FIFO) and reads at **4**, both below
  the inner drain threshold **9**, so the inner's counters can never
  saturate — the saturation tests and elab guard exercise this.
* Key AXI4-Lite differences from the full bench: with no IDs the DUT has ONE
  strictly ordered response stream per direction, so termination SLVERRs
  queue *behind* withheld in-flight responses instead of overtaking them,
  and all accounting is positional (`b_events[i]` belongs to the i-th write).

Each test logs `CHK-*` lines marking the property it just proved.

## Tests

### test_sanity

* **test_passthrough_and_isolated_slverr** — baseline: resets isolated,
  passes a write/read through with exact address and data, isolates on an
  empty drain, SLVERRs writes and reads (`0x1501A7ED` marker) while isolated
  with nothing leaking downstream, reopens cleanly.

### test_drain — termination while a drain is in progress

* **test_slverr_during_drain** — loads the drain (in-flight write + read,
  responses withheld), then offers a new write and read. Both are captured
  by the error slave with nothing leaking downstream; their SLVERRs arrive
  *after* the in-flight responses (strict lite response ordering), in order
  `[OKAY, SLVERR]` on both channels.
* **test_w_lags_aw_into_drain** — isolate lands with a write's W beat still
  owed: the owed beat drains through to the downstream port while a
  drain-window write's beat, queued behind it on the single W channel,
  follows its own AW to the error slave.
* **test_w_before_aw_during_drain** — W offered before its AW (legal AXI4):
  the beat stalls with no committed route, then follows the late AW's
  routing decision to the error slave — nothing downstream.

### test_select_freeze — routing stability under committed requests

* **test_sel_aw_frozen_while_unaccepted** — the AW select holds while an AW
  is presented-unaccepted, updates only after the handshake, and the stalled
  write still lands downstream with its W data. A read SLVERRs meanwhile:
  the AR select moved independently.
* **test_sel_ar_frozen_while_unaccepted** — AR mirror with the divergence
  check the other way (a write SLVERRs while the AR select is frozen). The
  two directions together prove the channels' selects are fully independent.

### test_deisolate — the select's 1 -> 0 direction

* **test_deisolate_parked_aw_at_err** — `isolate_i` falls with an AW parked
  at the busy error slave (previous SLVERR B withheld by the host): the
  select holds 1 until acceptance, the parked write SLVERRs, and the next
  write's W data lands on the same port as its AW.
* **test_deisolate_parked_ar_at_err** — AR mirror; the parked read returns
  SLVERR, never real data.
* **test_isolate_pulse_mid_drain** — `isolate_i` deasserts mid-drain
  (violating the hold-until-`isolated_o` convention). A write offered during
  the residual drain parks and is delivered once the FSM reopens: bounded
  stall, never a deadlock or mis-route.

### test_saturation — demux backpressure vs inner counter sizing

* **test_aw_gate_closes_before_inner_saturates** — fills the write path to
  the demux ceiling (8) with B withheld: 4 complete writes park in the
  B-select FIFO, 4 AW-only writes fill the W-select FIFO. The 9th AW must
  stall at the *gate*: nothing is presented internally, so no stability
  obligation exists and `isolate_i` must flip the select immediately despite
  the stalled host. A `PeakTracker` must end with peak == 8 (the ceiling was
  really reached) and peak < 9 (the inner cannot saturate).
* **test_ar_gate_closes_before_inner_saturates** — AR mirror: the R-select
  FIFO closes the read gate at 4.

### test_randomized — the unknown-unknowns net

* **test_randomized_isolate_stress** — 60 rounds of seeded random traffic
  through per-channel pumps while `isolate_i` and the stall knobs toggle at
  random (mid-drain pulses included). End-of-run positional accounting
  between issued / delivered / answered: one B per write, OKAY iff its
  unique address reached the downstream model; the downstream AW/W
  sequences equal the issue-order OKAY subsequences (W data never splits
  from its AW's route); OKAY Rs carry the downstream payload, SLVERR Rs the
  marker; no OKAY while `isolated_o`. Coverage floors keep the checks
  non-vacuous; the bounded final drain is the deadlock watchdog.
