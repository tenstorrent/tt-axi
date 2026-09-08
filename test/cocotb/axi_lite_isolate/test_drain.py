# Copyright 2026 Tenstorrent Inc.
"""Drain-window behavior: the original deadlock scenario, as a regression.

New requests arriving while the inner FSM drains must be terminated with
SLVERR by the error slave - never left split across demux ports (the
select-stability deadlock this bench exists for).

Key AXI4-Lite difference from the full-AXI bench: with no transaction IDs
the DUT has ONE strictly ordered response stream per direction (the demux's
B/R select FIFOs), so a drain-window SLVERR is queued BEHIND the withheld
in-flight response instead of overtaking it. The termination handshakes
still complete during the drain; only the response is deferred.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_SLVERR,
    AXI_RESP_OKAY,
    ISOLATE_ERROR_DATA,
    ST_DRAIN,
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
    state_ar,
    state_aw,
    wait_until,
)


@cocotb.test()
async def test_slverr_during_drain(dut):
    """A write and a read arriving during Drain are captured by the error
    slave while the in-flight transactions complete untouched downstream.
    Their SLVERR responses are ordered behind the in-flight responses."""
    slave, mon = await setup(dut)

    slave.release_b = False
    slave.release_r = False
    await issue_write(dut, 0x0000_1000, 0xD00D_0001)
    await issue_read(dut, 0x0000_2000)
    await wait_until(
        dut, lambda: slave.aw_count == 1 and slave.ar_count == 1, 20, "downstream acceptance"
    )

    dut.isolate_i.value = 1
    await wait_until(
        dut,
        lambda: state_aw(dut) == ST_DRAIN and state_ar(dut) == ST_DRAIN,
        10,
        "both channels in Drain",
    )
    assert dut.isolated_o.value == 0, f"isolated during loaded drain; {dbg(dut)}"
    assert sel_aw(dut) == 1 and sel_ar(dut) == 1, dbg(dut)
    dut._log.info(f"CHK-DRAIN-ENTERED: loaded drain open; {dbg(dut)}")

    # Write and read during the drain: both handshake at the error slave
    # (the issue_* calls complete), but nothing may leak downstream and -
    # unlike full AXI with distinct IDs - their SLVERR responses stay queued
    # behind the withheld in-flight B/R for the duration of the window.
    await issue_write(dut, 0x0000_3000, 0xD00D_0002)
    await issue_read(dut, 0x0000_4000)
    for cycle in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert len(mon.b_events) == 0 and len(mon.r_events) == 0, (
            f"drain-window response overtook the in-flight one at cycle {cycle}: "
            f"b={mon.b_events} r={mon.r_events}; {dbg(dut)}"
        )
    assert slave.aw_count == 1 and slave.w_count == 1, (
        f"drain-window write leaked downstream; {dbg(dut)}"
    )
    assert slave.ar_count == 1, f"drain-window read leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-DRAIN-CAPTURE-ORDERED: drain-window requests captured, "
                  "responses held behind the in-flight ones, nothing leaked")

    slave.release_b = True
    slave.release_r = True
    await wait_until(
        dut,
        lambda: len(mon.b_events) == 2 and len(mon.r_events) == 2,
        30,
        "all responses after release",
    )
    assert [e["resp"] for e in mon.b_events] == [AXI_RESP_OKAY, AXI_RESP_SLVERR], (
        f"b_events={mon.b_events}"
    )
    assert [e["resp"] for e in mon.r_events] == [AXI_RESP_OKAY, AXI_RESP_SLVERR], (
        f"r_events={mon.r_events}"
    )
    assert mon.r_events[0]["data"] == rdata_for(0x0000_2000)
    assert mon.r_events[1]["data"] == ISOLATE_ERROR_DATA
    dut._log.info("CHK-ORDERED-RESPONSES: in-flight OKAY first, drain-window SLVERR second")

    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")
    dut._log.info("CHK-ISOLATED-AFTER-DRAIN: isolated_o asserted once drain completed")

    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_write(dut, 0x0000_5000, 0xD00D_0003)
    await wait_until(dut, lambda: len(mon.b_events) == 3, 20, "B after reopen")
    assert mon.b_events[2]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 2
    dut._log.info("CHK-REOPEN-AFTER-DRAIN: write OKAY via downstream")


@cocotb.test()
async def test_w_lags_aw_into_drain(dut):
    """Isolate lands with a write's W beat still owed: the owed beat drains
    through to the downstream port, while a second write offered during the
    drain routes to the error slave and its W beat - queued behind the owed
    one on the single W channel - follows its own AW's routing there."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_aw(dut, 0x0000_1000)
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "w1 AW downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    assert dut.isolated_o.value == 0, f"isolated with a W beat owed; {dbg(dut)}"

    # The second write's AW routes to the error slave; the W route for each
    # write was committed at its AW's presentation, so the two W beats must
    # land on different ports in order.
    await issue_aw(dut, 0x0000_2000)
    await issue_w(dut, 0x1B00_0001)
    await issue_w(dut, 0x1B00_0002)
    await wait_until(dut, lambda: slave.w_count == 1, 20, "owed W beat downstream")
    assert slave.w_beats == [0x1B00_0001], (
        f"drain-window W leaked or owed W lost: {slave.w_beats}; {dbg(dut)}"
    )
    assert slave.aw_count == 1, f"drain-window AW leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-OWED-W-DRAINS: owed beat completed downstream, "
                  "drain-window beat followed its AW to the error slave")

    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_events) == 2, 30, "both B responses")
    assert [e["resp"] for e in mon.b_events] == [AXI_RESP_OKAY, AXI_RESP_SLVERR], (
        f"b_events={mon.b_events}"
    )
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")
    dut._log.info("CHK-LAGGING-W-INFLIGHT-OKAY: w1 completed OKAY, then isolation")


@cocotb.test()
async def test_w_before_aw_during_drain(dut):
    """W data offered before its AW (legal in AXI4-Lite): with no committed
    W route the demux must stall the beat at the slave port, and when the AW
    then arrives mid-drain and routes to the error slave, the waiting beat
    must follow that late routing decision there - nothing may leak to the
    downstream port."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_write(dut, 0x0000_1000, 0xD00D_0010)
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "w1 downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")

    # Offer the W beat with no AW anywhere in flight: the demux has no route
    # for it (W-select FIFO empty), so the beat must stall.
    w2_task = cocotb.start_soon(issue_w(dut, 0xD00D_0011))
    for cycle in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert dut.slv_w_valid_i.value == 1 and dut.slv_w_ready_o.value == 0, (
            f"orphan W beat handshook with no AW at cycle {cycle}; {dbg(dut)}"
        )
    assert slave.w_count == 1, f"orphan W leaked downstream: {slave.w_beats}"
    dut._log.info("CHK-ORPHAN-W-STALLS: W beat held with no committed route")

    # The late AW arrives during the drain, routes to the error slave, and
    # the waiting beat follows it.
    await issue_aw(dut, 0x0000_2000)
    await w2_task
    assert slave.aw_count == 1 and slave.w_count == 1, (
        f"W-first write leaked downstream; {dbg(dut)}"
    )
    dut._log.info("CHK-W-FOLLOWS-LATE-AW: beat terminated at the error slave during drain")

    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_events) == 2, 30, "both B responses")
    assert [e["resp"] for e in mon.b_events] == [AXI_RESP_OKAY, AXI_RESP_SLVERR], (
        f"b_events={mon.b_events}"
    )
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")
