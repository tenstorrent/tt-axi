# Copyright 2026 Tenstorrent Inc.
"""Randomized isolate stress with end-to-end response accounting.

Random writes and reads (unique addresses, random W-beat delays) flow
through per-channel pump tasks while isolate_i and the downstream stall
knobs are toggled at random - including dropping isolate_i before
isolated_o (pulse) and re-isolating immediately after reopening. W beats
naturally lead or lag their AWs, exercising the demux's presentation-time
W-route commitment.

With no transaction IDs, responses arrive on one strictly ordered stream
per direction, so accounting is positional: b_events[i] / r_events[i]
belong to the i-th write / read issued, and a response is OKAY exactly
when its request's (unique) address reached the downstream model - in
issue order there too.

Checks, at end of test after a full drain:
  - every issued write gets exactly one B; OKAY iff its AW reached the
    downstream model, DECERR otherwise (no lost or duplicated responses);
  - the downstream model's AW-address and W-data sequences equal the
    issue-order subsequences of the OKAY writes (no W data stranded on or
    leaked from the error slave, no reordering);
  - every read gets exactly one R; OKAY Rs carry the downstream payload
    for their address, DECERR Rs carry the 0x1501A7ED marker;
  - no OKAY response completes while isolated_o is high (the Isolate
    state disconnects the downstream B/R paths);
  - coverage floors: the run must produce both OKAY and DECERR outcomes
    on both channels, else the isolation checks are vacuous;
  - the final drain completes within a bounded time (deadlock watchdog).

Cycle-by-cycle select stability is guarded throughout by the SVAs in
tb_axi_lite_isolate.sv and the demux's own select-stability assertions.
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
    rdata_for,
    setup,
    wait_until,
)

N_ROUNDS = 60


@cocotb.test()
async def test_randomized_isolate_stress(dut):
    """Constrained-random traffic against random isolate/stall toggling."""
    random.seed(0x0CA1501A)
    slave, mon = await setup(dut)

    # Per-channel work queues; the pumps serialize signal ownership. W beats
    # are enqueued in AW order, as AXI requires; a W beat may still reach
    # the wires before its AW is accepted (W-before-AW).
    aw_queue, w_queue, ar_queue = [], [], []
    writes, reads = [], []

    async def aw_pump():
        while True:
            if aw_queue:
                t = aw_queue.pop(0)
                await issue_aw(dut, t["addr"], cycles=4000)
            else:
                await RisingEdge(dut.clk_i)

    async def w_pump():
        while True:
            if w_queue:
                t = w_queue.pop(0)
                await issue_w(dut, t["data"], w_delay=t["delay"], cycles=4000)
            else:
                await RisingEdge(dut.clk_i)

    async def ar_pump():
        while True:
            if ar_queue:
                t = ar_queue.pop(0)
                await issue_read(dut, t["addr"], cycles=4000)
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
            data = random.getrandbits(32)
            aw_queue.append({"addr": next_addr})
            w_queue.append({"data": data, "delay": random.randint(0, 3)})
            writes.append({"addr": next_addr, "data": data})
            next_addr += 0x40
        for _ in range(random.randint(0, 2)):
            ar_queue.append({"addr": next_addr})
            reads.append({"addr": next_addr})
            next_addr += 0x40

        await ClockCycles(dut.clk_i, random.randint(1, 12))

    # Final drain: everything permissive, de-isolated. Bounded wait doubles
    # as the deadlock watchdog for the whole random run.
    slave.accept_aw = slave.accept_w = slave.accept_ar = True
    slave.release_b = slave.release_r = True
    dut.isolate_i.value = 0
    n_w = len(writes)
    n_r = len(reads)
    await wait_until(
        dut,
        lambda: not aw_queue and not w_queue and not ar_queue
        and len(mon.b_events) >= n_w
        and len(mon.r_events) >= n_r,
        5000,
        "final drain of all outstanding transactions (deadlock watchdog)",
    )
    for p in pumps:
        p.kill()
    await ClockCycles(dut.clk_i, 10)

    # ---- Accounting (positional: single ordered response stream) ----
    assert len(mon.b_events) == n_w, (
        f"B count mismatch: {len(mon.b_events)} responses for {n_w} writes "
        f"(duplicate or spurious B)"
    )
    assert len(mon.r_events) == n_r, (
        f"R count mismatch: {len(mon.r_events)} responses for {n_r} reads "
        f"(duplicate or spurious R)"
    )

    delivered_w = [i for i, e in enumerate(mon.b_events) if e["resp"] == AXI_RESP_OKAY]
    for i, e in enumerate(mon.b_events):
        assert e["resp"] in (AXI_RESP_OKAY, AXI_RESP_DECERR), f"write {i}: bresp={e}"
    assert slave.aw_addrs == [writes[i]["addr"] for i in delivered_w], (
        f"downstream AW sequence does not match the OKAY writes: "
        f"{[hex(a) for a in slave.aw_addrs]}"
    )
    assert slave.w_beats == [writes[i]["data"] for i in delivered_w], (
        f"downstream W data does not match the OKAY writes (stranded or "
        f"leaked W data): {[hex(d) for d in slave.w_beats]}"
    )

    delivered_r = [j for j, e in enumerate(mon.r_events) if e["resp"] == AXI_RESP_OKAY]
    assert slave.ar_addrs == [reads[j]["addr"] for j in delivered_r], (
        f"downstream AR sequence does not match the OKAY reads: "
        f"{[hex(a) for a in slave.ar_addrs]}"
    )
    for j, e in enumerate(mon.r_events):
        if e["resp"] == AXI_RESP_OKAY:
            assert e["data"] == rdata_for(reads[j]["addr"]), f"read {j}: {e}"
        else:
            assert e["resp"] == AXI_RESP_DECERR, f"read {j}: rresp={e}"
            assert e["data"] == DECERR_DATA, f"read {j}: DECERR without marker: {e}"

    # Continuous invariant: an OKAY response only exists on the downstream
    # path, which the Isolate state disconnects - so no OKAY handshake may
    # ever complete while isolated_o is high.
    for e in mon.b_events + mon.r_events:
        if e["resp"] == AXI_RESP_OKAY:
            assert e["isolated"] == 0, f"OKAY response completed while isolated: {e}"

    # Coverage floors: with this fixed seed the run must exercise BOTH
    # routes on BOTH channels, or every check above about isolation is
    # vacuously satisfied by an all-downstream (or all-terminated) run.
    n_okay_w = len(delivered_w)
    n_decerr_w = n_w - n_okay_w
    n_okay_r = len(delivered_r)
    n_decerr_r = n_r - n_okay_r
    assert n_okay_w > 0 and n_decerr_w > 0, (
        f"write coverage floor not met: {n_okay_w} OKAY / {n_decerr_w} DECERR"
    )
    assert n_okay_r > 0 and n_decerr_r > 0, (
        f"read coverage floor not met: {n_okay_r} OKAY / {n_decerr_r} DECERR"
    )

    dut._log.info(
        f"CHK-RANDOM-ACCOUNTING: {n_w} writes ({n_okay_w} OKAY / {n_decerr_w} DECERR), "
        f"{n_r} reads ({n_okay_r} OKAY / {n_decerr_r} DECERR), all responses accounted"
    )
