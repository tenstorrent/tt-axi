# Copyright 2026 Tenstorrent Inc.
"""De-isolation corner cases: the select's 1 -> 0 direction.

The error slave is MaxTrans=1, so a second request pipelined behind a busy
error slave sits presented-and-unaccepted at demux port 1 with its W route
already committed there. If isolate_i falls in that window, the select must
hold at 1 until the error slave accepts - otherwise the locked AW would be
steered to the downstream block while its W data stays bound to the error
slave (wrong-port data), and the AR mirror would return real read data for
a request issued while isolated.

Also covers deasserting isolate_i mid-drain (violating the "hold isolate_i
until isolated_o" convention): a request offered during the residual drain
parks against port 0 and is delivered downstream once the drain completes -
bounded stall, never a deadlock.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_DECERR,
    AXI_RESP_OKAY,
    DECERR_DATA,
    ST_DRAIN,
    ar_unaccepted,
    aw_unaccepted,
    dbg,
    issue_aw,
    issue_read,
    issue_w,
    issue_write,
    rdata_for,
    sel_ar,
    sel_aw,
    settle,
    setup,
    state_aw,
    wait_until,
)


@cocotb.test()
async def test_deisolate_parked_aw_at_err(dut):
    """isolate_i falls while an AW is parked unaccepted at the busy error
    slave: sel_aw_q must hold 1 until the error slave accepts, the parked
    write still DECERRs, and only the next write goes downstream - with its
    W data arriving at the downstream port, not left at the error slave.

    The error slave accepts a new AW only once the previous W burst is fully
    consumed (its W fifo is MaxTrans=1 deep and pops on w.last), so a slow
    first W burst holds it busy for a controlled window."""
    slave, mon = await setup(dut)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation")

    # Write 1 occupies the error slave: 4 beats trickled with a large gap
    # keep its W fifo full for tens of cycles. Its AW is awaited before AW2
    # is offered - the two AW drivers must not own the channel concurrently.
    w1_data = [0xDE15_0000 + i for i in range(4)]
    await issue_aw(dut, 0x0000_1000, 1, len(w1_data))
    w1_task = cocotb.start_soon(issue_w(dut, w1_data, w_gap=8))

    # Write 2's AW (same ID hash bucket, so only the busy error slave stalls
    # it) is presented at port 1 and parks there unaccepted. Its W burst is
    # sent separately later - the W channel is still owned by write 1.
    aw2_task = cocotb.start_soon(issue_aw(dut, 0x0000_2000, 3, 1))
    await wait_until(
        dut, lambda: aw_unaccepted(dut) == 1 and sel_aw(dut) == 1, 30,
        "AW2 parked at the busy error slave",
    )

    # De-isolate in the parked window. The select must not move.
    dut.isolate_i.value = 0
    for cycle in range(6):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert sel_aw(dut) == 1, (
            f"sel_aw_q fell at cycle {cycle} with an AW parked at the error slave; {dbg(dut)}"
        )
        assert aw_unaccepted(dut) == 1, (
            f"parked AW resolved unexpectedly at cycle {cycle}; {dbg(dut)}"
        )
    dut._log.info("CHK-SEL-AW-HELD-ON-DEISOLATE: sel_aw_q held 1 across isolate_i falling")

    # Write 1's burst completes; the error slave frees and accepts the
    # parked AW2, whose W burst must also land at the error slave (its
    # route was committed at presentation).
    await w1_task
    await aw2_task
    await issue_w(dut, [0xDE15_0010])
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1 and len(mon.b_of(3)) == 1, 30,
                     "both DECERR responses")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_DECERR
    assert mon.b_of(3)[0]["resp"] == AXI_RESP_DECERR
    assert slave.aw_count == 0, f"parked write leaked downstream; {dbg(dut)}"
    assert len(slave.w_beats) == 0, f"W data leaked downstream: {slave.w_beats}"
    dut._log.info("CHK-PARKED-AW-DECERRS: parked write terminated at the error slave")

    # Only after the parked handshake may the select fall; the next write
    # goes downstream with its W data intact.
    await wait_until(dut, lambda: sel_aw(dut) == 0, 10, "select release after acceptance")
    await issue_write(dut, 0x0000_3000, [0xDE15_0003], txn_id=1)
    await wait_until(dut, lambda: len(mon.b_of(1)) == 2, 30, "B for post-deisolate write")
    assert mon.b_of(1)[1]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 1 and slave.w_beats == [(0xDE15_0003, 1)], (
        f"downstream saw wrong W data: {slave.w_beats}; {dbg(dut)}"
    )
    dut._log.info("CHK-POST-DEISOLATE-WRITE-OK: next write downstream, W data on the right port")


@cocotb.test()
async def test_deisolate_parked_ar_at_err(dut):
    """AR mirror: isolate_i falls while an AR is parked unaccepted at the
    busy error slave. sel_ar_q must hold 1 until acceptance; the parked read
    returns DECERR, never real downstream data."""
    slave, mon = await setup(dut)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation")

    # Read 1 occupies the error slave; withholding host R keeps it busy.
    dut.slv_r_ready_i.value = 0
    await issue_read(dut, 0x0000_1000, txn_id=1, num_beats=1)

    rd2_task = cocotb.start_soon(issue_read(dut, 0x0000_2000, txn_id=3, num_beats=1))
    await wait_until(
        dut, lambda: ar_unaccepted(dut) == 1 and sel_ar(dut) == 1, 30,
        "AR2 parked at the busy error slave",
    )

    dut.isolate_i.value = 0
    for cycle in range(6):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert sel_ar(dut) == 1, (
            f"sel_ar_q fell at cycle {cycle} with an AR parked at the error slave; {dbg(dut)}"
        )
        assert ar_unaccepted(dut) == 1, (
            f"parked AR resolved unexpectedly at cycle {cycle}; {dbg(dut)}"
        )
    dut._log.info("CHK-SEL-AR-HELD-ON-DEISOLATE: sel_ar_q held 1 across isolate_i falling")

    dut.slv_r_ready_i.value = 1
    await rd2_task
    try:
        await wait_until(dut, lambda: len(mon.r_of(1)) == 1 and len(mon.r_of(3)) == 1, 60,
                         "both DECERR reads")
    except AssertionError:
        dut._log.error(f"r_events={mon.r_events} ar_count={slave.ar_count}")
        raise
    assert mon.r_of(1)[0]["resp"] == AXI_RESP_DECERR
    assert mon.r_of(3)[0]["resp"] == AXI_RESP_DECERR
    assert mon.r_of(3)[0]["data"] == DECERR_DATA, (
        f"parked read returned non-DECERR data: {mon.r_of(3)}"
    )
    assert slave.ar_count == 0, f"parked read leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-PARKED-AR-DECERRS: parked read terminated at the error slave")

    await wait_until(dut, lambda: sel_ar(dut) == 0, 10, "select release after acceptance")
    await issue_read(dut, 0x0000_3000, txn_id=1, num_beats=1)
    await wait_until(dut, lambda: len(mon.r_of(1)) == 2, 30, "R for post-deisolate read")
    assert mon.r_of(1)[1]["resp"] == AXI_RESP_OKAY
    assert mon.r_of(1)[1]["data"] == rdata_for(0x0000_3000)
    assert slave.ar_count == 1
    dut._log.info("CHK-POST-DEISOLATE-READ-OK: next read served downstream with real data")


@cocotb.test()
async def test_isolate_pulse_mid_drain(dut):
    """isolate_i deasserts before the drain completes (violating the
    hold-until-isolated_o convention the SEP/SMC sequencers follow). A write
    offered during the residual drain parks against port 0 and is delivered
    downstream once the FSM walks Drain -> Isolate -> Normal: a bounded
    stall and a late delivery, never a deadlock or a mis-route."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_write(dut, 0x0000_1000, [0x9015_0001], txn_id=1)
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "w1 downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")

    # End the pulse mid-drain: the FSM stays latched in Drain, but with
    # nothing presented-unaccepted the select follows isolate_i back to 0.
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: sel_aw(dut) == 0, 5, "select back to 0 mid-drain")
    assert state_aw(dut) == ST_DRAIN, f"drain aborted by the pulse end; {dbg(dut)}"
    dut._log.info("CHK-PULSE-ENDS-MID-DRAIN: select back at 0 while the FSM still drains")

    # A write offered now routes to port 0 and parks against the draining
    # inner (opposite ID bucket, so no demux interlock is involved).
    wr2_task = cocotb.start_soon(issue_write(dut, 0x0000_2000, [0x9015_0002], txn_id=2))
    await wait_until(dut, lambda: aw_unaccepted(dut) == 1, 20, "w2 parked against the drain")
    for _ in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert aw_unaccepted(dut) == 1, f"w2 park resolved mid-window; {dbg(dut)}"
        assert len(mon.b_of(2)) == 0, f"parked write answered during drain; {dbg(dut)}"
    assert slave.aw_count == 1, f"parked write leaked into the drain; {dbg(dut)}"
    dut._log.info("CHK-PARKED-AGAINST-DRAIN: w2 held unaccepted at port 0, unanswered")

    # Complete the drain: w1's B releases, the FSM reaches Isolate for one
    # cycle, sees isolate_i low, reopens, and w2 is delivered downstream.
    slave.release_b = True
    await wr2_task
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1 and len(mon.b_of(2)) == 1, 40,
                     "both writes answered after the drain")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    assert mon.b_of(2)[0]["resp"] == AXI_RESP_OKAY, (
        f"parked write was terminated instead of delivered: {mon.b_events}"
    )
    assert slave.aw_count == 2 and slave.w_last_count == 2
    dut._log.info("CHK-PARK-THEN-DELIVER: w2 delivered downstream OKAY after the residual drain")
