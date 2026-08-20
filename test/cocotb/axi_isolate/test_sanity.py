# Copyright 2026 Tenstorrent Inc.
"""Sanity: reset state, burst pass-through, DECERR while isolated, reopen.

The DUT resets isolated (inner FSMs in Isolate, selects pointing at the
error slave), passes bursts through transparently once de-isolated, DECERRs
everything while isolated, and recovers cleanly on de-isolation.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_DECERR,
    AXI_RESP_OKAY,
    DECERR_DATA,
    dbg,
    issue_read,
    issue_write,
    rdata_for,
    settle,
    setup,
    wait_until,
)


@cocotb.test()
async def test_passthrough_and_isolated_decerr(dut):
    """Sanity: burst pass-through with ID echo, DECERR while isolated, reopen."""
    await RisingEdge(dut.clk_i)
    await settle(dut)
    assert dut.isolated_o.value == 1, f"expected isolated out of reset; {dbg(dut)}"

    slave, mon = await setup(dut)

    wdata = [0xCAFE_0000 + i for i in range(4)]
    await issue_write(dut, 0x0000_1000, wdata, txn_id=1)
    await wait_until(dut, lambda: len(mon.b_events) == 1, 30, "B for pass-through write")
    assert mon.b_events[0]["resp"] == AXI_RESP_OKAY and mon.b_events[0]["id"] == 1
    assert slave.aw_count == 1 and slave.w_last_count == 1
    assert [d for d, _ in slave.w_beats] == wdata, f"w_beats={slave.w_beats}"
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=4)
    await wait_until(dut, lambda: len(mon.r_of(2)) == 4, 30, "R burst for pass-through read")
    beats = mon.r_of(2)
    assert [b["last"] for b in beats] == [0, 0, 0, 1], f"r beats={beats}"
    assert all(b["resp"] == AXI_RESP_OKAY for b in beats)
    assert [b["data"] for b in beats] == [rdata_for(0x0000_2000, i) for i in range(4)]
    dut._log.info("CHK-PASSTHROUGH: 4-beat write and read OKAY with ID echo and W data intact")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "empty-drain isolation")
    dut._log.info("CHK-EMPTY-DRAIN-ISOLATES: isolated_o asserted with no pending traffic")

    await issue_write(dut, 0x0000_3000, [0xCAFE_1000], txn_id=3)
    await wait_until(dut, lambda: len(mon.b_events) == 2, 20, "B for isolated write")
    assert mon.b_events[1]["resp"] == AXI_RESP_DECERR and mon.b_events[1]["id"] == 3
    assert slave.aw_count == 1, f"isolated write leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-ISOLATED-WRITE-DECERR: bresp=DECERR with ID echo, nothing leaked")

    await issue_read(dut, 0x0000_4000, txn_id=4, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(4)) == 2, 20, "R burst for isolated read")
    beats = mon.r_of(4)
    assert all(b["resp"] == AXI_RESP_DECERR for b in beats)
    assert all(b["data"] == DECERR_DATA for b in beats), f"r beats={beats}"
    assert [b["last"] for b in beats] == [0, 1], f"r beats={beats}"
    assert slave.ar_count == 1
    dut._log.info("CHK-ISOLATED-READ-DECERR: full-length DECERR burst, rdata=0x1501A7ED")

    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_write(dut, 0x0000_5000, [0xCAFE_2000], txn_id=5)
    await wait_until(dut, lambda: len(mon.b_events) == 3, 20, "B after reopen")
    assert mon.b_events[2]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 2
    dut._log.info("CHK-REOPEN: write OKAY via downstream after de-isolation")
