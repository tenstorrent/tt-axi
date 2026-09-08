# Copyright 2026 Tenstorrent Inc.
"""Sanity: reset state, pass-through, SLVERR while isolated, reopen.

The DUT resets isolated (inner FSMs in Isolate, selects pointing at the
error slave), passes writes and reads through transparently once
de-isolated, SLVERRs everything while isolated, and recovers cleanly on
de-isolation.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_SLVERR,
    AXI_RESP_OKAY,
    ISOLATE_ERROR_DATA,
    dbg,
    issue_read,
    issue_write,
    rdata_for,
    settle,
    setup,
    wait_until,
)


@cocotb.test()
async def test_passthrough_and_isolated_slverr(dut):
    """Sanity: pass-through write/read, SLVERR while isolated, reopen."""
    await RisingEdge(dut.clk_i)
    await settle(dut)
    assert dut.isolated_o.value == 1, f"expected isolated out of reset; {dbg(dut)}"

    slave, mon = await setup(dut)

    await issue_write(dut, 0x0000_1000, 0xCAFE_0000)
    await wait_until(dut, lambda: len(mon.b_events) == 1, 30, "B for pass-through write")
    assert mon.b_events[0]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 1 and slave.w_count == 1
    assert slave.aw_addrs == [0x0000_1000] and slave.w_beats == [0xCAFE_0000]
    await issue_read(dut, 0x0000_2000)
    await wait_until(dut, lambda: len(mon.r_events) == 1, 30, "R for pass-through read")
    r0 = mon.r_events[0]
    assert r0["resp"] == AXI_RESP_OKAY and r0["data"] == rdata_for(0x0000_2000), f"r={r0}"
    dut._log.info("CHK-PASSTHROUGH: write and read OKAY with address and W data intact")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "empty-drain isolation")
    dut._log.info("CHK-EMPTY-DRAIN-ISOLATES: isolated_o asserted with no pending traffic")

    await issue_write(dut, 0x0000_3000, 0xCAFE_1000)
    await wait_until(dut, lambda: len(mon.b_events) == 2, 20, "B for isolated write")
    assert mon.b_events[1]["resp"] == AXI_RESP_SLVERR
    assert slave.aw_count == 1, f"isolated write leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-ISOLATED-WRITE-SLVERR: bresp=SLVERR, nothing leaked")

    await issue_read(dut, 0x0000_4000)
    await wait_until(dut, lambda: len(mon.r_events) == 2, 20, "R for isolated read")
    r1 = mon.r_events[1]
    assert r1["resp"] == AXI_RESP_SLVERR and r1["data"] == ISOLATE_ERROR_DATA, f"r={r1}"
    assert slave.ar_count == 1
    dut._log.info("CHK-ISOLATED-READ-SLVERR: rresp=SLVERR, rdata=0x1501A7ED")

    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_write(dut, 0x0000_5000, 0xCAFE_2000)
    await wait_until(dut, lambda: len(mon.b_events) == 3, 20, "B after reopen")
    assert mon.b_events[2]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 2
    dut._log.info("CHK-REOPEN: write OKAY via downstream after de-isolation")
