# Copyright 2026 Tenstorrent Inc.
"""The demux backpressures before the inner counters can saturate.

For NumPending=4 the demux's ID-bucket counters close its acceptance gate
after 5 admitted transactions per direction, while the inner's derived
threshold (InnerPending) is 7. So the inner can never reach the
counter-saturation branch of its FSM - the one Normal -> Drain path that
skips Hold and historically moved the select under a committed request.

A host request stalled at the closed demux gate is NOT presented at a
master port (no W-route commitment, no select-stability obligation), so the
select is free to flip while it waits: raising isolate_i in that state must
switch the select immediately and the stalled request must terminate at the
error slave once the gate reopens. The host-level freeze of an earlier fix
attempt deadlocked in exactly this scenario.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import ReadOnly, RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_DECERR,
    AXI_RESP_OKAY,
    DEMUX_MAX_ADMITTED,
    INNER_PENDING,
    ST_DRAIN,
    ST_NORMAL,
    ar_unaccepted,
    aw_unaccepted,
    dbg,
    issue_read,
    issue_write,
    pending_ar,
    pending_aw,
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
    """Fill the write path to the demux ceiling with B withheld: the demux
    stops accepting at 5 outstanding, the stalled 6th AW is never presented
    internally, the inner stays in Normal below its threshold, and raising
    isolate_i flips the select immediately despite the host-level stall."""
    slave, mon = await setup(dut)
    tracker = PeakTracker(dut, pending_aw)
    tracker_task = cocotb.start_soon(tracker.run())

    # 5 single-beat writes, IDs alternating between the two AxiLookBits=1
    # buckets: buckets end at (3, 2), and 3 closes the gate globally.
    slave.release_b = False
    for i in range(DEMUX_MAX_ADMITTED):
        await issue_write(dut, 0x0000_1000 + 0x100 * i, [0x5A70_0000 + i], txn_id=i)
    await wait_until(dut, lambda: slave.aw_count == DEMUX_MAX_ADMITTED, 20,
                     "all admitted writes downstream")

    # The 6th write stalls at the demux gate: host sees ready low, but the
    # request is NOT presented at a master port (Group A stall).
    wr6_task = cocotb.start_soon(issue_write(dut, 0x0000_2000, [0x5A70_0005], txn_id=5))
    for _ in range(10):
        await RisingEdge(dut.clk_i)
    await settle(dut)
    assert dut.slv_aw_valid_i.value == 1 and dut.slv_aw_ready_o.value == 0, (
        f"6th AW not stalled at the gate; {dbg(dut)}"
    )
    assert aw_unaccepted(dut) == 0, (
        f"gate-stalled AW was presented internally; {dbg(dut)}"
    )
    assert state_aw(dut) == ST_NORMAL and pending_aw(dut) == DEMUX_MAX_ADMITTED, (
        f"inner not in Normal at the demux ceiling; {dbg(dut)}"
    )
    dut._log.info(f"CHK-GATE-CLOSED-FIRST: demux stalls at {DEMUX_MAX_ADMITTED}, "
                  f"inner idle below threshold {INNER_PENDING}; {dbg(dut)}")

    # Isolate while the host is stalled at the closed gate. Nothing is
    # committed, so the select must flip immediately - the scenario where
    # freezing on the HOST-level handshake deadlocked.
    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN and sel_aw(dut) == 1, 5,
                     "select flip under a gate-stalled host request")
    dut._log.info("CHK-SELECT-FREE-UNDER-GATE-STALL: sel_aw flipped with the host stalled")

    # Release B: the five in-flight writes drain OKAY, the gate reopens as
    # their buckets empty, and the stalled write terminates at the error
    # slave (its bucket's ID interlock resolves as the drain progresses).
    slave.release_b = True
    await wr6_task
    await wait_until(dut, lambda: len(mon.b_events) == DEMUX_MAX_ADMITTED + 1, 60,
                     "all six B responses")
    for i in range(DEMUX_MAX_ADMITTED):
        assert mon.b_of(i)[0]["resp"] == AXI_RESP_OKAY, f"b_events={mon.b_events}"
    assert mon.b_of(5)[0]["resp"] == AXI_RESP_DECERR, f"b_events={mon.b_events}"
    assert slave.aw_count == DEMUX_MAX_ADMITTED, f"stalled write leaked; {dbg(dut)}"
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 20, "isolation after drain")
    dut._log.info("CHK-STALLED-WRITE-TERMINATED: gate-stalled write DECERRed after the drain")

    tracker_task.kill()
    assert tracker.peak < INNER_PENDING, (
        f"pending_aw reached {tracker.peak}, inner threshold {INNER_PENDING} violated"
    )
    assert tracker.peak == DEMUX_MAX_ADMITTED, (
        f"expected the demux ceiling {DEMUX_MAX_ADMITTED}, saw peak {tracker.peak}"
    )
    dut._log.info(f"CHK-INNER-NEVER-SATURATES: peak pending_aw={tracker.peak} "
                  f"< InnerPending={INNER_PENDING}")

    # Recovery sanity.
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_write(dut, 0x0000_3000, [0x5A70_00FF], txn_id=0)
    await wait_until(dut, lambda: len(mon.b_of(0)) == 2, 30, "B after recovery")
    assert mon.b_of(0)[1]["resp"] == AXI_RESP_OKAY


@cocotb.test()
async def test_ar_gate_closes_before_inner_saturates(dut):
    """AR mirror: 5 reads with R withheld close the demux gate; the stalled
    6th AR is never presented, the inner stays below threshold, the select
    flips freely on isolate, and the stalled read DECERRs after the drain."""
    slave, mon = await setup(dut)
    tracker = PeakTracker(dut, pending_ar)
    tracker_task = cocotb.start_soon(tracker.run())

    slave.release_r = False
    for i in range(DEMUX_MAX_ADMITTED):
        await issue_read(dut, 0x0000_1000 + 0x100 * i, txn_id=i, num_beats=1)
    await wait_until(dut, lambda: slave.ar_count == DEMUX_MAX_ADMITTED, 20,
                     "all admitted reads downstream")

    rd6_task = cocotb.start_soon(issue_read(dut, 0x0000_2000, txn_id=5, num_beats=1))
    for _ in range(10):
        await RisingEdge(dut.clk_i)
    await settle(dut)
    assert dut.slv_ar_valid_i.value == 1 and dut.slv_ar_ready_o.value == 0, (
        f"6th AR not stalled at the gate; {dbg(dut)}"
    )
    assert ar_unaccepted(dut) == 0, (
        f"gate-stalled AR was presented internally; {dbg(dut)}"
    )
    assert state_ar(dut) == ST_NORMAL and pending_ar(dut) == DEMUX_MAX_ADMITTED, (
        f"inner not in Normal at the demux ceiling; {dbg(dut)}"
    )
    dut._log.info(f"CHK-AR-GATE-CLOSED-FIRST: demux stalls at {DEMUX_MAX_ADMITTED}; {dbg(dut)}")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_ar(dut) == ST_DRAIN and sel_ar(dut) == 1, 5,
                     "AR select flip under a gate-stalled host request")
    dut._log.info("CHK-AR-SELECT-FREE-UNDER-GATE-STALL: sel_ar flipped with the host stalled")

    slave.release_r = True
    await rd6_task
    await wait_until(dut, lambda: len(mon.r_events) == DEMUX_MAX_ADMITTED + 1, 60,
                     "all six R responses")
    for i in range(DEMUX_MAX_ADMITTED):
        assert mon.r_of(i)[0]["resp"] == AXI_RESP_OKAY, f"r_events={mon.r_events}"
    assert mon.r_of(5)[0]["resp"] == AXI_RESP_DECERR, f"r_events={mon.r_events}"
    assert slave.ar_count == DEMUX_MAX_ADMITTED, f"stalled read leaked; {dbg(dut)}"
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 20, "isolation after drain")
    dut._log.info("CHK-STALLED-READ-TERMINATED: gate-stalled read DECERRed after the drain")

    tracker_task.kill()
    assert tracker.peak < INNER_PENDING, (
        f"pending_ar reached {tracker.peak}, inner threshold {INNER_PENDING} violated"
    )
    assert tracker.peak == DEMUX_MAX_ADMITTED, (
        f"expected the demux ceiling {DEMUX_MAX_ADMITTED}, saw peak {tracker.peak}"
    )
    dut._log.info(f"CHK-INNER-NEVER-SATURATES: peak pending_ar={tracker.peak} "
                  f"< InnerPending={INNER_PENDING}")

    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_read(dut, 0x0000_3000, txn_id=0, num_beats=1)
    await wait_until(dut, lambda: len(mon.r_of(0)) == 2, 30, "R after recovery")
    assert mon.r_of(0)[1]["resp"] == AXI_RESP_OKAY
