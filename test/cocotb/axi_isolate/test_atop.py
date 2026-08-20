# Copyright 2026 Tenstorrent Inc.
"""ATOP handling: injected AR drain credit, isolated ATOP filtering.

An atomic load owes an R burst in addition to its B, which the inner tracks
by injecting a count into the AR pending counter. The drain must therefore
stay open until the atomic's R returns. While isolated, ATOPs are absorbed
by the error path's atop_filter and answered with SLVERR.

The store flavor (atop[ATOP_R_RESP] clear) owes only a B: no credit may be
injected for it, or the drain would wait forever for an R that never comes.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import ClockCycles  # pyright: ignore[reportMissingImports]

from helpers import (
    ATOP_LOAD_ADD,
    ATOP_STORE_ADD,
    AXI_RESP_OKAY,
    AXI_RESP_SLVERR,
    ST_DRAIN,
    dbg,
    issue_write,
    pending_ar,
    settle,
    setup,
    state_ar,
    state_aw,
    wait_until,
)


@cocotb.test()
async def test_atop_drain_credit(dut):
    """An atomic load in flight injects an AR credit: the drain must stay
    open until its R returns, not just its B. Once isolated, a new ATOP is
    filtered with SLVERR instead of reaching the error slave."""
    slave, mon = await setup(dut)

    slave.release_b = False
    slave.release_r = False
    await issue_write(dut, 0x0000_1000, [0xA707_0001], txn_id=1, atop=ATOP_LOAD_ADD)
    await wait_until(
        dut, lambda: slave.aw_count == 1 and slave.w_last_count == 1, 20,
        "downstream accepting the atomic",
    )

    dut.isolate_i.value = 1
    await wait_until(
        dut,
        lambda: state_aw(dut) == ST_DRAIN and state_ar(dut) == ST_DRAIN,
        10,
        "both channels in Drain",
    )
    assert pending_ar(dut) == 1, f"atomic did not inject an AR credit; {dbg(dut)}"
    dut._log.info(f"CHK-ATOP-INJECTS-AR-CREDIT: {dbg(dut)}")

    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1, 20, "B for the atomic")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    await ClockCycles(dut.clk_i, 3)
    await settle(dut)
    assert dut.isolated_o.value == 0 and state_ar(dut) == ST_DRAIN, (
        f"drain closed before the atomic's R returned; {dbg(dut)}"
    )
    dut._log.info("CHK-ATOP-DRAIN-WAITS-FOR-R: drain held open after B, waiting on R")

    slave.release_r = True
    await wait_until(dut, lambda: len(mon.r_of(1)) == 1, 20, "R for the atomic")
    r1 = mon.r_of(1)[0]
    assert r1["resp"] == AXI_RESP_OKAY and r1["last"] == 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after atomic R")
    dut._log.info("CHK-ATOP-DRAIN-CLOSES-ON-R: isolated_o asserted after the atomic's R")

    # While isolated, an ATOP is absorbed by the error path's atop_filter,
    # which responds SLVERR (B and one R for a load), nothing leaks downstream.
    await issue_write(dut, 0x0000_2000, [0xA707_0002], txn_id=5, atop=ATOP_LOAD_ADD)
    await wait_until(
        dut,
        lambda: len(mon.b_of(5)) == 1 and len(mon.r_of(5)) == 1,
        30,
        "filtered ATOP responses",
    )
    assert mon.b_of(5)[0]["resp"] == AXI_RESP_SLVERR
    assert mon.r_of(5)[0]["resp"] == AXI_RESP_SLVERR and mon.r_of(5)[0]["last"] == 1
    assert slave.aw_count == 1
    dut._log.info("CHK-ISOLATED-ATOP-SLVERR: isolated atomic filtered with SLVERR B+R")


@cocotb.test()
async def test_atop_store_no_credit(dut):
    """An atomic store owes only a B: no AR credit may be injected for it
    (guards against crediting on any non-zero atop instead of on
    atop[ATOP_R_RESP]), and the drain must complete on the B alone. While
    isolated, a store is filtered with a SLVERR B and no R beat."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_write(dut, 0x0000_1000, [0xA707_0003], txn_id=1, atop=ATOP_STORE_ADD)
    await wait_until(
        dut, lambda: slave.aw_count == 1 and slave.w_last_count == 1, 20,
        "downstream accepting the atomic store",
    )
    assert pending_ar(dut) == 0, f"atomic store injected an AR credit; {dbg(dut)}"
    dut._log.info("CHK-ATOP-STORE-NO-CREDIT: pending_ar stayed 0 with a store in flight")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    assert pending_ar(dut) == 0, f"AR credit appeared during the drain; {dbg(dut)}"

    # The B alone must close the drain - a phantom credit would hold state_ar
    # in Drain forever and this wait_until would time out.
    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1, 20, "B for the atomic store")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation on the B alone")
    assert len(mon.r_events) == 0, f"a store produced an R beat: {mon.r_events}"
    dut._log.info("CHK-ATOP-STORE-DRAIN-ON-B: isolation completed on the B, no R owed")

    # While isolated, a store is absorbed by the atop_filter: SLVERR B, no R.
    await issue_write(dut, 0x0000_2000, [0xA707_0004], txn_id=5, atop=ATOP_STORE_ADD)
    await wait_until(dut, lambda: len(mon.b_of(5)) == 1, 30, "filtered store B")
    assert mon.b_of(5)[0]["resp"] == AXI_RESP_SLVERR
    await ClockCycles(dut.clk_i, 10)
    assert len(mon.r_events) == 0, f"filtered store produced an R beat: {mon.r_events}"
    assert slave.aw_count == 1
    dut._log.info("CHK-ISOLATED-STORE-SLVERR-B-ONLY: filtered with SLVERR B and no R")
