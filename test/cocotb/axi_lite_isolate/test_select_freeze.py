# Copyright 2026 Tenstorrent Inc.
"""The select registers freeze while a request is presented and unaccepted.

sel_aw_q / sel_ar_q must hold their value from the cycle the demux presents
a request at a master port until the cycle it is accepted, and only then
take the current isolate value. The two channels are independent: a frozen
AW select must not stop the AR select from switching, and vice versa.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_SLVERR,
    AXI_RESP_OKAY,
    ISOLATE_ERROR_DATA,
    ar_unaccepted,
    aw_unaccepted,
    dbg,
    issue_read,
    issue_write,
    rdata_for,
    sel_ar,
    sel_aw,
    settle,
    setup,
    wait_until,
)


@cocotb.test()
async def test_sel_aw_frozen_while_unaccepted(dut):
    """sel_aw_q holds while an AW is offered and not accepted, updates only
    after the handshake, and the stalled write still lands downstream.
    Meanwhile sel_ar diverges to the error slave - a read SLVERRs at once."""
    slave, mon = await setup(dut)

    slave.accept_aw = False
    slave.accept_w = False
    wr_task = cocotb.start_soon(issue_write(dut, 0x0000_1000, 0xF00D_0001))
    await wait_until(
        dut,
        lambda: dut.slv_aw_valid_i.value == 1 and dut.slv_aw_ready_o.value == 0,
        20,
        "AW offered and stalled",
    )

    dut.isolate_i.value = 1
    for cycle in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert sel_aw(dut) == 0, (
            f"sel_aw_q moved at stall cycle {cycle} with the AW unaccepted; {dbg(dut)}"
        )
        assert dut.slv_aw_valid_i.value == 1 and dut.slv_aw_ready_o.value == 0, (
            f"AW stall broke unexpectedly at cycle {cycle}; {dbg(dut)}"
        )
        assert aw_unaccepted(dut) == 1, (
            f"AW not presented-unaccepted at cycle {cycle} - this is a gate "
            f"stall, not the committed freeze under test; {dbg(dut)}"
        )
    assert slave.aw_count == 0
    dut._log.info("CHK-SEL-AW-FROZEN: sel_aw_q held 0 for 8 cycles of isolate_i=1 stall")

    # The AR select is independent and had nothing unaccepted, so it has
    # already switched: a read issued now SLVERRs while the write is frozen.
    assert sel_ar(dut) == 1, f"sel_ar_q did not diverge; {dbg(dut)}"
    await issue_read(dut, 0x0000_2000)
    await wait_until(dut, lambda: len(mon.r_events) == 1, 20, "SLVERR R during AW freeze")
    r0 = mon.r_events[0]
    assert r0["resp"] == AXI_RESP_SLVERR and r0["data"] == ISOLATE_ERROR_DATA
    assert r0["isolated"] == 0 and slave.ar_count == 0
    dut._log.info("CHK-SEL-DIVERGENCE: read SLVERRed while the AW select stayed frozen")

    slave.accept_aw = True
    slave.accept_w = True
    await wait_until(
        dut,
        lambda: dut.slv_aw_valid_i.value == 1 and dut.slv_aw_ready_o.value == 1,
        20,
        "stalled AW acceptance",
    )
    await RisingEdge(dut.clk_i)
    await settle(dut)
    assert sel_aw(dut) == 1, f"sel_aw_q did not update after accept; {dbg(dut)}"
    dut._log.info("CHK-SEL-AW-UPDATES-AFTER-ACCEPT: sel_aw_q took isolate value post-handshake")

    await wr_task
    await wait_until(dut, lambda: len(mon.b_events) == 1, 20, "B for the stalled write")
    assert mon.b_events[0]["resp"] == AXI_RESP_OKAY, f"b_events={mon.b_events}"
    assert slave.aw_count == 1 and slave.w_count == 1
    dut._log.info("CHK-FROZEN-WRITE-ROUTED-DOWNSTREAM: stalled write completed OKAY at port 0")

    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after freeze drain")
    await issue_write(dut, 0x0000_3000, 0xF00D_0002)
    await wait_until(dut, lambda: len(mon.b_events) == 2, 20, "B for post-freeze write")
    assert mon.b_events[1]["resp"] == AXI_RESP_SLVERR
    assert slave.aw_count == 1
    dut._log.info("CHK-POST-FREEZE-SLVERR: next write SLVERRed once select took effect")


@cocotb.test()
async def test_sel_ar_frozen_while_unaccepted(dut):
    """AR-side mirror of the freeze check, including the divergence check in
    the opposite direction: a frozen AR select must not stop the AW select
    from switching (catches sel_aw_q wrongly gated on the AR channel)."""
    slave, mon = await setup(dut)

    slave.accept_ar = False
    rd_task = cocotb.start_soon(issue_read(dut, 0x0000_1000))
    await wait_until(
        dut,
        lambda: dut.slv_ar_valid_i.value == 1 and dut.slv_ar_ready_o.value == 0,
        20,
        "AR offered and stalled",
    )

    dut.isolate_i.value = 1
    for cycle in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert sel_ar(dut) == 0, (
            f"sel_ar_q moved at stall cycle {cycle} with the AR unaccepted; {dbg(dut)}"
        )
        assert dut.slv_ar_valid_i.value == 1 and dut.slv_ar_ready_o.value == 0, (
            f"AR stall broke unexpectedly at cycle {cycle}; {dbg(dut)}"
        )
        assert ar_unaccepted(dut) == 1, (
            f"AR not presented-unaccepted at cycle {cycle} - this is a gate "
            f"stall, not the committed freeze under test; {dbg(dut)}"
        )
    assert slave.ar_count == 0
    dut._log.info("CHK-SEL-AR-FROZEN: sel_ar_q held 0 for 8 cycles of isolate_i=1 stall")

    # The AW select is independent and had nothing unaccepted, so it has
    # already switched: a write issued now SLVERRs while the read is frozen.
    assert sel_aw(dut) == 1, f"sel_aw_q did not diverge; {dbg(dut)}"
    await issue_write(dut, 0x0000_4000, 0xF00D_0007)
    await wait_until(dut, lambda: len(mon.b_events) == 1, 20, "SLVERR B during AR freeze")
    b0 = mon.b_events[0]
    assert b0["resp"] == AXI_RESP_SLVERR
    assert b0["isolated"] == 0 and slave.aw_count == 0
    dut._log.info("CHK-SEL-DIVERGENCE-AR: write SLVERRed while the AR select stayed frozen")

    slave.accept_ar = True
    await wait_until(
        dut,
        lambda: dut.slv_ar_valid_i.value == 1 and dut.slv_ar_ready_o.value == 1,
        20,
        "stalled AR acceptance",
    )
    await RisingEdge(dut.clk_i)
    await settle(dut)
    assert sel_ar(dut) == 1, f"sel_ar_q did not update after accept; {dbg(dut)}"
    dut._log.info("CHK-SEL-AR-UPDATES-AFTER-ACCEPT: sel_ar_q took isolate value post-handshake")

    await rd_task
    await wait_until(dut, lambda: len(mon.r_events) == 1, 20, "R for the stalled read")
    r0 = mon.r_events[0]
    assert r0["resp"] == AXI_RESP_OKAY and r0["data"] == rdata_for(0x0000_1000), f"r={r0}"
    assert slave.ar_count == 1
    dut._log.info("CHK-FROZEN-READ-ROUTED-DOWNSTREAM: stalled read completed OKAY at port 0")

    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after freeze drain")
    await issue_read(dut, 0x0000_2000)
    await wait_until(dut, lambda: len(mon.r_events) == 2, 20, "R for post-freeze read")
    assert mon.r_events[1]["resp"] == AXI_RESP_SLVERR
    assert mon.r_events[1]["data"] == ISOLATE_ERROR_DATA
    assert slave.ar_count == 1
    dut._log.info("CHK-POST-FREEZE-SLVERR-READ: next read SLVERRed once select took effect")
