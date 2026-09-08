# Copyright 2026 Tenstorrent Inc.
"""De-isolation corner cases: the select's 1 -> 0 direction.

The error slave accepts one transaction per direction at a time, so a second
request pipelined behind a busy error slave sits presented-and-unaccepted at
demux port 1 with its W route already committed there. If isolate_i falls in
that window, the select must hold at 1 until the error slave accepts -
otherwise the locked AW would be steered to the downstream block while its W
data stays bound to the error slave (wrong-port data), and the AR mirror
would return real read data for a request issued while isolated.

Also covers deasserting isolate_i mid-drain (violating the "hold isolate_i
until isolated_o" convention): a request offered during the residual drain
parks against port 0 and is delivered downstream once the drain completes -
bounded stall, never a deadlock.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_SLVERR,
    AXI_RESP_OKAY,
    ISOLATE_ERROR_DATA,
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
    write still SLVERRs, and only the next write goes downstream - with its
    W data arriving at the downstream port, not left at the error slave.

    The error slave stays busy until its B response is accepted, so
    withholding host B-readiness holds it busy for a controlled window."""
    slave, mon = await setup(dut)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation")

    # Write 1 occupies the error slave: its SLVERR B stays undeliverable
    # while the host withholds b_ready.
    dut.slv_b_ready_i.value = 0
    await issue_write(dut, 0x0000_1000, 0xDE15_0001)

    # Write 2's AW is presented at port 1 and parks there unaccepted. Its W
    # beat is sent separately later - keeping the parked window clean.
    aw2_task = cocotb.start_soon(issue_aw(dut, 0x0000_2000))
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

    # Deliver write 1's B; the error slave frees and accepts the parked AW2,
    # whose W beat must also land at the error slave (its route was
    # committed at presentation).
    dut.slv_b_ready_i.value = 1
    await aw2_task
    await issue_w(dut, 0xDE15_0002)
    await wait_until(dut, lambda: len(mon.b_events) == 2, 30, "both SLVERR responses")
    assert [e["resp"] for e in mon.b_events] == [AXI_RESP_SLVERR, AXI_RESP_SLVERR], (
        f"b_events={mon.b_events}"
    )
    assert slave.aw_count == 0, f"parked write leaked downstream; {dbg(dut)}"
    assert slave.w_count == 0, f"W data leaked downstream: {slave.w_beats}"
    dut._log.info("CHK-PARKED-AW-SLVERRS: parked write terminated at the error slave")

    # Only after the parked handshake may the select fall; the next write
    # goes downstream with its W data intact.
    await wait_until(dut, lambda: sel_aw(dut) == 0, 10, "select release after acceptance")
    await issue_write(dut, 0x0000_3000, 0xDE15_0003)
    await wait_until(dut, lambda: len(mon.b_events) == 3, 30, "B for post-deisolate write")
    assert mon.b_events[2]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 1 and slave.w_beats == [0xDE15_0003], (
        f"downstream saw wrong W data: {slave.w_beats}; {dbg(dut)}"
    )
    dut._log.info("CHK-POST-DEISOLATE-WRITE-OK: next write downstream, W data on the right port")


@cocotb.test()
async def test_deisolate_parked_ar_at_err(dut):
    """AR mirror: isolate_i falls while an AR is parked unaccepted at the
    busy error slave. sel_ar_q must hold 1 until acceptance; the parked read
    returns SLVERR, never real downstream data."""
    slave, mon = await setup(dut)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation")

    # Read 1 occupies the error slave; withholding host R keeps it busy.
    dut.slv_r_ready_i.value = 0
    await issue_read(dut, 0x0000_1000)

    rd2_task = cocotb.start_soon(issue_read(dut, 0x0000_2000))
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
    await wait_until(dut, lambda: len(mon.r_events) == 2, 60, "both SLVERR reads")
    assert [e["resp"] for e in mon.r_events] == [AXI_RESP_SLVERR, AXI_RESP_SLVERR], (
        f"r_events={mon.r_events}"
    )
    assert mon.r_events[1]["data"] == ISOLATE_ERROR_DATA, (
        f"parked read returned non-SLVERR data: {mon.r_events}"
    )
    assert slave.ar_count == 0, f"parked read leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-PARKED-AR-SLVERRS: parked read terminated at the error slave")

    await wait_until(dut, lambda: sel_ar(dut) == 0, 10, "select release after acceptance")
    await issue_read(dut, 0x0000_3000)
    await wait_until(dut, lambda: len(mon.r_events) == 3, 30, "R for post-deisolate read")
    assert mon.r_events[2]["resp"] == AXI_RESP_OKAY
    assert mon.r_events[2]["data"] == rdata_for(0x0000_3000)
    assert slave.ar_count == 1
    dut._log.info("CHK-POST-DEISOLATE-READ-OK: next read served downstream with real data")


@cocotb.test()
async def test_isolate_pulse_mid_drain(dut):
    """isolate_i deasserts before the drain completes (violating the
    hold-until-isolated_o convention). A write offered during the residual
    drain parks against port 0 and is delivered downstream once the FSM
    walks Drain -> Isolate -> Normal: a bounded stall and a late delivery,
    never a deadlock or a mis-route."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_write(dut, 0x0000_1000, 0x9015_0001)
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
    # inner, which refuses new AWs until it reopens.
    wr2_task = cocotb.start_soon(issue_write(dut, 0x0000_2000, 0x9015_0002))
    await wait_until(dut, lambda: aw_unaccepted(dut) == 1, 20, "w2 parked against the drain")
    for _ in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert aw_unaccepted(dut) == 1, f"w2 park resolved mid-window; {dbg(dut)}"
        assert len(mon.b_events) == 0, f"parked write answered during drain; {dbg(dut)}"
    assert slave.aw_count == 1, f"parked write leaked into the drain; {dbg(dut)}"
    dut._log.info("CHK-PARKED-AGAINST-DRAIN: w2 held unaccepted at port 0, unanswered")

    # Complete the drain: w1's B releases, the FSM reaches Isolate for one
    # cycle, sees isolate_i low, reopens, and w2 is delivered downstream.
    slave.release_b = True
    await wr2_task
    await wait_until(dut, lambda: len(mon.b_events) == 2, 40,
                     "both writes answered after the drain")
    assert [e["resp"] for e in mon.b_events] == [AXI_RESP_OKAY, AXI_RESP_OKAY], (
        f"parked write was terminated instead of delivered: {mon.b_events}"
    )
    assert slave.aw_count == 2 and slave.w_beats == [0x9015_0001, 0x9015_0002]
    dut._log.info("CHK-PARK-THEN-DELIVER: w2 delivered downstream OKAY after the residual drain")
