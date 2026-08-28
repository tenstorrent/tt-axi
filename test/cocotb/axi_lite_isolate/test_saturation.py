# Copyright 2026 Tenstorrent Inc.
"""The demux backpressures before the inner counters can saturate.

For NumPending=4 the demux's select FIFOs close its acceptance gates after
2*4 = 8 admitted writes (W-select FIFO + B-select FIFO, each 4 deep) and 4
admitted reads (R-select FIFO), while the inner's derived threshold
(InnerPending) is 9. So the inner can never reach the counter-saturation
branch of its FSM - the one Normal -> Drain path that skips Hold and
historically moved the select under a committed request.

A host request stalled at the closed demux gate is NOT presented at a
master port (no W-route commitment, no select-stability obligation), so the
select is free to flip while it waits: raising isolate_i in that state must
switch the select immediately and the stalled request must terminate at the
error slave once the gate reopens. Freezing the select on the HOST-level
handshake instead would deadlock in exactly this scenario - and the demux's
select-stability assertion is anchored on the presented condition for the
same reason.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import ReadOnly, RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_DECERR,
    AXI_RESP_OKAY,
    DECERR_DATA,
    DEMUX_MAX_R,
    DEMUX_MAX_W,
    INNER_PENDING,
    ST_DRAIN,
    ST_NORMAL,
    ar_unaccepted,
    aw_unaccepted,
    dbg,
    issue_aw,
    issue_read,
    issue_w,
    issue_write,
    pending_ar,
    pending_aw,
    rdata_for,
    sel_ar,
    sel_aw,
    settle,
    setup,
    state_ar,
    state_aw,
    wait_until,
)


class PeakTracker:
    """Samples a probe each cycle at ReadOnly and records its maximum."""

    def __init__(self, dut, probe):
        self.dut = dut
        self.probe = probe
        self.peak = 0

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk_i)
            await ReadOnly()
            self.peak = max(self.peak, self.probe(self.dut))


@cocotb.test()
async def test_aw_gate_closes_before_inner_saturates(dut):
    """Fill the write path to the demux ceiling with B withheld: 4 complete
    writes park in the B-select FIFO and 4 further AWs fill the W-select
    FIFO (their W beats blocked behind the full B FIFO). The demux stops
    accepting at 8 outstanding, the stalled 9th AW is never presented
    internally, the inner stays in Normal below its threshold, and raising
    isolate_i flips the select immediately despite the host-level stall."""
    slave, mon = await setup(dut)
    tracker = PeakTracker(dut, pending_aw)
    tracker_task = cocotb.start_soon(tracker.run())

    # Phase 1: 4 complete writes; with B withheld they occupy the B-select
    # FIFO (W transferred, B outstanding).
    slave.release_b = False
    for i in range(4):
        await issue_write(dut, 0x0000_1000 + 0x100 * i, 0x5A70_0000 + i)
    await wait_until(dut, lambda: slave.aw_count == 4 and slave.w_count == 4, 20,
                     "phase-1 writes downstream")

    # Phase 2: 4 AW-only writes; their W beats are blocked behind the full
    # B FIFO, so they occupy the W-select FIFO.
    for i in range(4):
        await issue_aw(dut, 0x0000_2000 + 0x100 * i)
    await wait_until(dut, lambda: slave.aw_count == DEMUX_MAX_W, 20,
                     "phase-2 AWs downstream")
    assert pending_aw(dut) == DEMUX_MAX_W and state_aw(dut) == ST_NORMAL, (
        f"inner not idle in Normal at the demux ceiling; {dbg(dut)}"
    )

    # Phase 3: the 9th AW stalls at the closed demux gate: host sees ready
    # low, but the request is NOT presented at a master port.
    aw9_task = cocotb.start_soon(issue_aw(dut, 0x0000_3000, cycles=4000))
    for _ in range(10):
        await RisingEdge(dut.clk_i)
    await settle(dut)
    assert dut.slv_aw_valid_i.value == 1 and dut.slv_aw_ready_o.value == 0, (
        f"9th AW not stalled at the gate; {dbg(dut)}"
    )
    assert aw_unaccepted(dut) == 0, (
        f"gate-stalled AW was presented internally; {dbg(dut)}"
    )
    assert state_aw(dut) == ST_NORMAL and pending_aw(dut) == DEMUX_MAX_W, (
        f"inner not in Normal at the demux ceiling; {dbg(dut)}"
    )
    dut._log.info(f"CHK-GATE-CLOSED-FIRST: demux stalls at {DEMUX_MAX_W}, "
                  f"inner idle below threshold {INNER_PENDING}; {dbg(dut)}")

    # Isolate while the host is stalled at the closed gate. Nothing is
    # committed, so the select must flip immediately - the scenario where
    # freezing on the HOST-level handshake would deadlock.
    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN and sel_aw(dut) == 1, 5,
                     "select flip under a gate-stalled host request")
    dut._log.info("CHK-SELECT-FREE-UNDER-GATE-STALL: sel_aw flipped with the host stalled")

    # Release B: the four parked responses drain, unblocking the phase-2 W
    # beats, which drain through the still-connected W channel; the gate
    # reopens as the W-select FIFO empties, and the stalled 9th write
    # terminates at the error slave.
    slave.release_b = True
    for i in range(4):
        await issue_w(dut, 0x5A70_0010 + i)
    await aw9_task
    await issue_w(dut, 0x5A70_00FF)
    await wait_until(dut, lambda: len(mon.b_events) == DEMUX_MAX_W + 1, 80,
                     "all nine B responses")
    assert [e["resp"] for e in mon.b_events] == [AXI_RESP_OKAY] * DEMUX_MAX_W + [
        AXI_RESP_DECERR
    ], f"b_events={mon.b_events}"
    assert slave.aw_count == DEMUX_MAX_W, f"stalled write leaked; {dbg(dut)}"
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 20, "isolation after drain")
    dut._log.info("CHK-STALLED-WRITE-TERMINATED: gate-stalled write DECERRed after the drain")

    tracker_task.kill()
    assert tracker.peak < INNER_PENDING, (
        f"pending_aw reached {tracker.peak}, inner threshold {INNER_PENDING} violated"
    )
    assert tracker.peak == DEMUX_MAX_W, (
        f"expected the demux ceiling {DEMUX_MAX_W}, saw peak {tracker.peak}"
    )
    dut._log.info(f"CHK-INNER-NEVER-SATURATES: peak pending_aw={tracker.peak} "
                  f"< InnerPending={INNER_PENDING}")

    # Recovery sanity.
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_write(dut, 0x0000_4000, 0x5A70_0100)
    await wait_until(dut, lambda: len(mon.b_events) == DEMUX_MAX_W + 2, 30,
                     "B after recovery")
    assert mon.b_events[-1]["resp"] == AXI_RESP_OKAY


@cocotb.test()
async def test_ar_gate_closes_before_inner_saturates(dut):
    """AR mirror: 4 reads with R withheld fill the R-select FIFO and close
    the demux gate; the stalled 5th AR is never presented, the inner stays
    below threshold, the select flips freely on isolate, and the stalled
    read DECERRs after the drain."""
    slave, mon = await setup(dut)
    tracker = PeakTracker(dut, pending_ar)
    tracker_task = cocotb.start_soon(tracker.run())

    slave.release_r = False
    for i in range(DEMUX_MAX_R):
        await issue_read(dut, 0x0000_1000 + 0x100 * i)
    await wait_until(dut, lambda: slave.ar_count == DEMUX_MAX_R, 20,
                     "all admitted reads downstream")

    ar5_task = cocotb.start_soon(issue_read(dut, 0x0000_2000, cycles=4000))
    for _ in range(10):
        await RisingEdge(dut.clk_i)
    await settle(dut)
    assert dut.slv_ar_valid_i.value == 1 and dut.slv_ar_ready_o.value == 0, (
        f"5th AR not stalled at the gate; {dbg(dut)}"
    )
    assert ar_unaccepted(dut) == 0, (
        f"gate-stalled AR was presented internally; {dbg(dut)}"
    )
    assert state_ar(dut) == ST_NORMAL and pending_ar(dut) == DEMUX_MAX_R, (
        f"inner not in Normal at the demux ceiling; {dbg(dut)}"
    )
    dut._log.info(f"CHK-AR-GATE-CLOSED-FIRST: demux stalls at {DEMUX_MAX_R}; {dbg(dut)}")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_ar(dut) == ST_DRAIN and sel_ar(dut) == 1, 5,
                     "AR select flip under a gate-stalled host request")
    dut._log.info("CHK-AR-SELECT-FREE-UNDER-GATE-STALL: sel_ar flipped with the host stalled")

    slave.release_r = True
    await ar5_task
    await wait_until(dut, lambda: len(mon.r_events) == DEMUX_MAX_R + 1, 60,
                     "all five R responses")
    for i in range(DEMUX_MAX_R):
        ev = mon.r_events[i]
        assert ev["resp"] == AXI_RESP_OKAY, f"r_events={mon.r_events}"
        assert ev["data"] == rdata_for(0x0000_1000 + 0x100 * i), f"r_events={mon.r_events}"
    assert mon.r_events[DEMUX_MAX_R]["resp"] == AXI_RESP_DECERR, f"r_events={mon.r_events}"
    assert mon.r_events[DEMUX_MAX_R]["data"] == DECERR_DATA
    assert slave.ar_count == DEMUX_MAX_R, f"stalled read leaked; {dbg(dut)}"
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 20, "isolation after drain")
    dut._log.info("CHK-STALLED-READ-TERMINATED: gate-stalled read DECERRed after the drain")

    tracker_task.kill()
    assert tracker.peak < INNER_PENDING, (
        f"pending_ar reached {tracker.peak}, inner threshold {INNER_PENDING} violated"
    )
    assert tracker.peak == DEMUX_MAX_R, (
        f"expected the demux ceiling {DEMUX_MAX_R}, saw peak {tracker.peak}"
    )
    dut._log.info(f"CHK-INNER-NEVER-SATURATES: peak pending_ar={tracker.peak} "
                  f"< InnerPending={INNER_PENDING}")

    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_read(dut, 0x0000_3000)
    await wait_until(dut, lambda: len(mon.r_events) == DEMUX_MAX_R + 2, 30,
                     "R after recovery")
    assert mon.r_events[-1]["resp"] == AXI_RESP_OKAY
