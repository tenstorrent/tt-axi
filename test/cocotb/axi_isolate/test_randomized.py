# Copyright 2026 Tenstorrent Inc.
"""Randomized isolate stress with end-to-end response accounting.

Random writes and reads (random IDs, burst lengths, W gaps) flow through
per-channel pump tasks while isolate_i and the downstream stall knobs are
toggled at random - including dropping isolate_i before isolated_o (pulse)
and re-isolating immediately after reopening. W bursts naturally lead their
stalled AWs, exercising the demux's presentation-time W-route commitment.

Checks, at end of test after a full drain:
  - every issued write gets exactly one B; OKAY iff its AW reached the
    downstream model, DECERR otherwise (no lost or duplicated responses);
  - every issued read returns a full-length burst with exactly one last
    beat; OKAY bursts match downstream deliveries; every DECERR beat
    carries the 0x1501A7ED marker;
  - every W beat the downstream model received belongs to a delivered
    write (no W data stranded on or leaked from the error slave);
  - no OKAY response completes while isolated_o is high (the Isolate
    state disconnects the downstream B/R paths);
  - coverage floors: the run must produce both OKAY and DECERR outcomes
    on both channels, else the isolation checks are vacuous;
  - the final drain completes within a bounded time (deadlock watchdog).

Cycle-by-cycle select stability is guarded throughout by the SVAs in
tb_axi_isolate.sv and the demux's own slv_*_select_stable assertions.
Fixed seed: deterministic stimulus for reproducible regression.
"""

import random

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import ClockCycles, RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_DECERR,
    AXI_RESP_OKAY,
    DECERR_DATA,
    issue_aw,
    issue_read,
    issue_w,
    setup,
    wait_until,
)

N_ROUNDS = 60
N_IDS = 16


@cocotb.test()
async def test_randomized_isolate_stress(dut):
    """Constrained-random traffic against random isolate/stall toggling."""
    random.seed(0x0CA1501A)
    slave, mon = await setup(dut)

    # Per-channel work queues; the pumps serialize signal ownership. W
    # bursts are enqueued in AW order, as AXI requires; a W burst may still
    # reach the wires before its AW is accepted (W-before-AW).
    aw_queue, w_queue, ar_queue = [], [], []
    writes, reads = [], []

    async def aw_pump():
        while True:
            if aw_queue:
                t = aw_queue.pop(0)
                await issue_aw(dut, t["addr"], t["id"], t["beats"], cycles=4000)
            else:
                await RisingEdge(dut.clk_i)

    async def w_pump():
        while True:
            if w_queue:
                t = w_queue.pop(0)
                await issue_w(dut, t["data"], w_gap=t["gap"], cycles=4000)
            else:
                await RisingEdge(dut.clk_i)

    async def ar_pump():
        while True:
            if ar_queue:
                t = ar_queue.pop(0)
                await issue_read(dut, t["addr"], t["id"], t["beats"], cycles=4000)
            else:
                await RisingEdge(dut.clk_i)

    pumps = [cocotb.start_soon(p()) for p in (aw_pump, w_pump, ar_pump)]

    next_addr = 0x1000_0000
    for _ in range(N_ROUNDS):
        # Isolate control: assert at random; deassert possibly before
        # isolated_o, so mid-drain pulses and parked-at-err windows occur.
        if int(dut.isolate_i.value) == 0:
            if random.random() < 0.18:
                dut.isolate_i.value = 1
        elif random.random() < 0.30:
            dut.isolate_i.value = 0

        # Downstream stall knobs: mostly permissive so traffic keeps moving.
        slave.accept_aw = random.random() < 0.8
        slave.accept_w = random.random() < 0.8
        slave.accept_ar = random.random() < 0.8
        slave.release_b = random.random() < 0.75
        slave.release_r = random.random() < 0.75

        for _ in range(random.randint(0, 2)):
            tid = random.randint(0, N_IDS - 1)
            beats = random.randint(1, 4)
            aw_queue.append({"addr": next_addr, "id": tid, "beats": beats})
            w_queue.append({"data": [random.getrandbits(32) for _ in range(beats)],
                            "gap": random.randint(0, 2)})
            writes.append({"id": tid, "beats": beats})
            next_addr += 0x40
        for _ in range(random.randint(0, 2)):
            tid = random.randint(0, N_IDS - 1)
            beats = random.randint(1, 4)
            ar_queue.append({"addr": next_addr, "id": tid, "beats": beats})
            reads.append({"id": tid, "beats": beats})
            next_addr += 0x40

        await ClockCycles(dut.clk_i, random.randint(1, 12))

    # Final drain: everything permissive, de-isolated. Bounded wait doubles
    # as the deadlock watchdog for the whole random run.
    slave.accept_aw = slave.accept_w = slave.accept_ar = True
    slave.release_b = slave.release_r = True
    dut.isolate_i.value = 0
    n_w = len(writes)
    n_r_last = len(reads)
    await wait_until(
        dut,
        lambda: not aw_queue and not w_queue and not ar_queue
        and len(mon.b_events) >= n_w
        and sum(1 for e in mon.r_events if e["last"]) >= n_r_last,
        5000,
        "final drain of all outstanding transactions (deadlock watchdog)",
    )
    for p in pumps:
        p.kill()
    await ClockCycles(dut.clk_i, 10)

    # ---- Accounting ----
    assert len(mon.b_events) == n_w, (
        f"B count mismatch: {len(mon.b_events)} responses for {n_w} writes "
        f"(duplicate or spurious B)"
    )
    total_okay_w = total_decerr_w = 0
    for tid in range(N_IDS):
        issued = sum(1 for t in writes if t["id"] == tid)
        b_events = mon.b_of(tid)
        assert len(b_events) == issued, (
            f"id={tid}: {len(b_events)} B responses for {issued} writes"
        )
        downstream = sum(1 for r in slave.aw_records if r["id"] == tid)
        okay = sum(1 for e in b_events if e["resp"] == AXI_RESP_OKAY)
        decerr = sum(1 for e in b_events if e["resp"] == AXI_RESP_DECERR)
        assert okay == downstream, (
            f"id={tid}: {okay} OKAY B but {downstream} AWs delivered downstream"
        )
        assert okay + decerr == issued, (
            f"id={tid}: unexpected B resp mix: {b_events}"
        )
        total_okay_w += okay
        total_decerr_w += decerr

        r_issued = [t for t in reads if t["id"] == tid]
        r_events = mon.r_of(tid)
        lasts = sum(1 for e in r_events if e["last"])
        assert lasts == len(r_issued), (
            f"id={tid}: {lasts} R bursts completed for {len(r_issued)} reads"
        )
        assert len(r_events) == sum(t["beats"] for t in r_issued), (
            f"id={tid}: R beat count mismatch (truncated or stretched burst)"
        )
        ds_reads = sum(1 for r in slave.ar_records if r["id"] == tid)
        okay_lasts = sum(1 for e in r_events if e["last"] and e["resp"] == AXI_RESP_OKAY)
        assert okay_lasts == ds_reads, (
            f"id={tid}: {okay_lasts} OKAY R bursts but {ds_reads} ARs delivered downstream"
        )
        for e in r_events:
            if e["resp"] == AXI_RESP_DECERR:
                assert e["data"] == DECERR_DATA, f"id={tid}: DECERR beat without marker: {e}"

    # Every W beat the downstream saw belongs to a delivered write: no W
    # data stranded at the error slave or leaked across the demux ports.
    ds_w_beats = sum(r["len"] + 1 for r in slave.aw_records)
    assert len(slave.w_beats) == ds_w_beats, (
        f"downstream W beats {len(slave.w_beats)} != owed {ds_w_beats}"
    )

    # Continuous invariant: an OKAY response only exists on the downstream
    # path, which the Isolate state disconnects - so no OKAY handshake may
    # ever complete while isolated_o is high.
    for e in mon.b_events + mon.r_events:
        if e["resp"] == AXI_RESP_OKAY:
            assert e["isolated"] == 0, f"OKAY response completed while isolated: {e}"

    # Coverage floors: with this fixed seed the run must exercise BOTH
    # routes on BOTH channels, or every check above about isolation is
    # vacuously satisfied by an all-downstream (or all-terminated) run.
    total_okay_r = sum(1 for e in mon.r_events if e["last"] and e["resp"] == AXI_RESP_OKAY)
    total_decerr_r = sum(1 for e in mon.r_events if e["resp"] == AXI_RESP_DECERR)
    assert total_okay_w > 0 and total_decerr_w > 0, (
        f"write coverage floor not met: {total_okay_w} OKAY / {total_decerr_w} DECERR"
    )
    assert total_okay_r > 0 and total_decerr_r > 0, (
        f"read coverage floor not met: {total_okay_r} OKAY bursts / "
        f"{total_decerr_r} DECERR beats"
    )

    dut._log.info(
        f"CHK-RANDOM-ACCOUNTING: {n_w} writes ({total_okay_w} OKAY / "
        f"{total_decerr_w} DECERR), {n_r_last} reads ({total_okay_r} OKAY / "
        f"{total_decerr_r} DECERR beats), all responses accounted"
    )
