# axi_isolate block-level testbench (cocotb + VCS)

Verifies `axi_isolate` with `TerminateTransaction = 1` (the SEP/SMC reset-controller
configuration): pass-through and termination behavior, drain semantics, demux select
routing stability in both directions, counter sizing, ATOP handling, and randomized
isolate stress.

## Running

```bash
module load synopsys/vcs/W-2024.09-SP2-5
source <a python env with cocotb 1.x>/bin/activate

cd test/cocotb/axi_isolate
make sim-vcs                             # full suite (15 tests)
make sim-vcs MODULE=test_drain TESTCASE=test_decerr_during_drain WAVES=1
```

The Makefile resolves `common_cells` through bender (first run clones it into
`.bender/`).

`WAVES=1` dumps `tb_axi_isolate.vcd` (`nWave tb_axi_isolate.vcd &` after
`module load synopsys/verdi/X-2025.06-SP2-3`).

## Bench structure

* `tb_axi_isolate.sv` — flattens struct ports for VPI, clock, `obs_*` probes of
  internals, and the select-stability SVAs (anchored on demux-INTERNAL valids;
  a host-level antecedent would false-positive on benign gate stalls). An elab
  guard `$fatal`s unless `NUM_PENDING == 4` and `InnerPending == 7`.
* `helpers.py` — probes, AXI drivers, `DownstreamSlave` (backpressure knobs,
  records all deliveries), `RespMonitor` (records every B/R with the isolate
  state at completion). Timing convention: test deposits land just after the
  posedge via `settle()`; `ReadOnly` only in background samplers.
* Key numbers (NUM_PENDING=4): demux admission ceiling **5** < arithmetic
  ceiling **6** < inner drain threshold **7**, so the inner's counters can
  never saturate — the saturation tests and elab guard exercise this.

Each test logs `CHK-*` lines marking the property it just proved.

## Tests

### test_sanity

* **test_passthrough_and_isolated_decerr** — baseline: resets isolated, passes
  4-beat write/read through with ID echo and exact data, isolates on an empty
  drain, DECERRs writes and full-length reads (`0x1501A7ED` marker) while
  isolated with nothing leaking downstream, reopens cleanly.

### test_drain — termination while a drain is in progress

* **test_decerr_during_drain** — loads the drain (in-flight write + read,
  responses withheld), then offers a new write and read. Both must DECERR
  *while the drain is still open* (`isolated_o` low); the in-flight pair
  completes OKAY untouched.
* **test_w_interlock_midburst_drain** — isolate lands mid W-burst: remaining
  beats drain through; a drain-window write is held by the W interlock until
  the burst closes, then DECERRed.
* **test_same_id_hash_write_during_drain** — drain-window write colliding with
  the in-flight ID hash is held by the ID interlock until the in-flight B
  returns, then terminated: stalls at demux until in-flight finishes, never a deadlock.
* **test_w_before_aw_during_drain** — W offered before its AW (legal AXI4):
  beats stall with no committed route, then follow the late AW's routing
  decision to the error slave — nothing downstream.

### test_select_freeze — routing stability under committed requests

* **test_sel_aw_frozen_while_unaccepted** — the AW select holds while an AW is
  presented-unaccepted, updates only after the handshake, and the stalled
  write still lands downstream. A read DECERRs meanwhile: the AR select moved
  independently.
* **test_sel_ar_frozen_while_unaccepted** — AR mirror with the divergence
  check the other way (a write DECERRs while the AR select is frozen). The
  two directions together prove the channels' selects are fully independent.

### test_deisolate — the select's 1 -> 0 direction

* **test_deisolate_parked_aw_at_err** — `isolate_i` falls with an AW parked at
  the busy error slave: the select holds 1 until acceptance, the parked write
  DECERRs, and the next write's W data lands on the same port as its AW.
* **test_deisolate_parked_ar_at_err** — AR mirror; the parked read returns
  DECERR, never real data.
* **test_isolate_pulse_mid_drain** — `isolate_i` deasserts mid-drain (violating
  the hold-until-`isolated_o` convention). A write offered during the residual
  drain parks and is delivered once the FSM reopens: bounded stall, never a
  deadlock or mis-route.

### test_saturation — demux backpressure vs inner counter sizing

* **test_aw_gate_closes_before_inner_saturates** — fills the write path to the
  demux ceiling (5) with B withheld. The 6th AW must stall at the *gate*:
  nothing is presented internally, so no stability obligation exists and
  `isolate_i` must flip the select immediately despite the stalled host. A
  `PeakTracker` must end with peak == 5 (the ceiling was really reached) and
  peak < 7 (the inner cannot saturate).
* **test_ar_gate_closes_before_inner_saturates** — AR mirror, R withheld.

### test_atop — atomics change a transaction's shape

* **test_atop_drain_credit** — an atomic load owes B and R; the inner injects
  an AR credit. Releasing only the B must NOT close the drain. While isolated,
  ATOPs are absorbed by the atop_filter and answered SLVERR (B + one R).
* **test_atop_store_no_credit** — the store flavor owes only a B: no credit
  may be injected (crediting must key on `atop[ATOP_R_RESP]`, not on any
  non-zero atop), drain closes on the B alone, filtered store gets SLVERR B
  with no R.

### test_randomized — the unknown-unknowns net

* **test_randomized_isolate_stress** — 60 rounds of seeded random traffic
  through per-channel pumps while `isolate_i` and the stall knobs toggle at
  random (mid-drain pulses included). End-of-run accounting between issued /
  delivered / answered: one B per write; OKAY iff delivered downstream (per
  ID); full-length R bursts; DECERR beats carry the marker; W-beat
  conservation (W data never splits from its AW's route); no OKAY while
  `isolated_o`. Coverage floors keep the checks non-vacuous; the bounded
  final drain is the deadlock watchdog.
