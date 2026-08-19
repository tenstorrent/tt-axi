# Copyright 2026 Tenstorrent Inc.
"""Drain-window behavior: the original deadlock scenario, as a regression.

New requests arriving while the inner FSM drains must be terminated with
DECERR by the error slave — never left split across demux ports (the
select-stability deadlock this bench exists for). The demux's W and ID
interlocks may stall such a request conservatively, but it must always
resolve once the older traffic retires.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import RisingEdge  # pyright: ignore[reportMissingImports]

from helpers import (
    AXI_RESP_DECERR,
    AXI_RESP_OKAY,
    DECERR_DATA,
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
async def test_decerr_during_drain(dut):
    """A write and a read arriving during Drain are DECERRed by the error
    slave while the in-flight transactions complete untouched downstream.
    With distinct IDs the DECERR responses are observable during the drain."""
    slave, mon = await setup(dut)

    slave.release_b = False
    slave.release_r = False
    await issue_write(dut, 0x0000_1000, [0xD00D_0001], txn_id=1)
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=2)
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

    # Write during drain. The ID must fall in the other AxiLookBits=1 hash
    # bucket than the in-flight id=1, otherwise the demux ID interlock holds
    # it behind the withheld B. With a distinct hash its DECERR B is not
    # ordered behind the in-flight B and must arrive while the drain is open.
    await issue_write(dut, 0x0000_3000, [0xD00D_0002], txn_id=4)
    await wait_until(dut, lambda: len(mon.b_of(4)) == 1, 20, "DECERR B during drain")
    b4 = mon.b_of(4)[0]
    assert b4["resp"] == AXI_RESP_DECERR and b4["isolated"] == 0, f"b_events={mon.b_events}"
    assert slave.aw_count == 1, f"drain-window write leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-DECERR-WRITE-DURING-DRAIN: DECERR B delivered while draining")

    await issue_read(dut, 0x0000_4000, txn_id=5, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(5)) == 2, 20, "DECERR R burst during drain")
    beats = mon.r_of(5)
    assert all(b["resp"] == AXI_RESP_DECERR and b["data"] == DECERR_DATA for b in beats)
    assert all(b["isolated"] == 0 for b in beats), f"r beats={beats}"
    assert slave.ar_count == 1, f"drain-window read leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-DECERR-READ-DURING-DRAIN: DECERR R burst delivered while draining")

    slave.release_b = True
    slave.release_r = True
    await wait_until(
        dut,
        lambda: len(mon.b_of(1)) == 1 and len(mon.r_of(2)) == 2,
        30,
        "in-flight responses after release",
    )
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    assert all(b["resp"] == AXI_RESP_OKAY for b in mon.r_of(2))
    assert [b["data"] for b in mon.r_of(2)] == [rdata_for(0x0000_2000, i) for i in range(2)]
    dut._log.info("CHK-INFLIGHT-COMPLETES-OKAY: drained write/read finished with OKAY")

    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")
    dut._log.info("CHK-ISOLATED-AFTER-DRAIN: isolated_o asserted once drain completed")

    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "re-opening")
    await issue_write(dut, 0x0000_5000, [0xD00D_0003], txn_id=6)
    await wait_until(dut, lambda: len(mon.b_of(6)) == 1, 20, "B after reopen")
    assert mon.b_of(6)[0]["resp"] == AXI_RESP_OKAY
    assert slave.aw_count == 2
    dut._log.info("CHK-REOPEN-AFTER-DRAIN: write OKAY via downstream")


@cocotb.test()
async def test_w_interlock_midburst_drain(dut):
    """Isolate lands mid-way through a write burst: the remaining W beats
    drain through, and a write offered during the drain is held by the demux
    W interlock until the open burst closes, then cleanly DECERRed."""
    slave, mon = await setup(dut)

    slave.release_b = False
    wdata = [0x1B00_0000 + i for i in range(4)]
    w1_task = cocotb.start_soon(issue_write(dut, 0x0000_1000, wdata, txn_id=1, w_gap=3))
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "w1 AW downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    assert dut.isolated_o.value == 0, f"isolated with a burst open; {dbg(dut)}"
    assert slave.w_last_count == 0, (
        f"w1 finished before the drain-window write could be offered; {dbg(dut)}"
    )

    # Offer a second write during the drain (opposite ID hash bucket so only
    # the W interlock holds it, not the ID interlock). Its W burst must wait
    # until w1's burst closes, so only the AW is offered here.
    aw2_task = cocotb.start_soon(issue_aw(dut, 0x0000_2000, 4, 1))
    # The interlock window: every cycle w1's burst is still open, no AW
    # handshake may complete at the slave port. Checked per cycle so an
    # early acceptance fails in the cycle it happens, with state attached.
    for cycle in range(40):
        if slave.w_last_count == 1:
            break
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert not (dut.slv_aw_valid_i.value and dut.slv_aw_ready_o.value), (
            f"w2 AW accepted at window cycle {cycle} with w1's burst open; {dbg(dut)}"
        )
    else:
        raise AssertionError(f"w1 burst never drained through; {dbg(dut)}")
    await w1_task
    dut._log.info("CHK-MIDBURST-W-DRAINS: remaining W beats completed during Drain")

    await aw2_task
    dut._log.info(f"CHK-W-INTERLOCK-HELD: no AW handshake during w1's open burst "
                  f"({cycle} window cycles)")

    await issue_w(dut, [0x1B00_00FF])
    await wait_until(dut, lambda: len(mon.b_of(4)) == 1, 20,
                     "DECERR B for the drain-window write")
    b4 = mon.b_of(4)[0]
    assert b4["resp"] == AXI_RESP_DECERR and b4["isolated"] == 0, f"b_events={mon.b_events}"
    assert slave.aw_count == 1, f"drain-window write leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-INTERLOCK-DECERR: held write terminated during drain, none leaked")

    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1, 20, "w1 B")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")
    dut._log.info("CHK-MIDBURST-INFLIGHT-OKAY: w1 completed OKAY, then isolation")


@cocotb.test()
async def test_same_id_hash_write_during_drain(dut):
    """A drain-window write whose ID collides with the in-flight write in the
    demux's AxiLookBits hash is held by the ID interlock until the in-flight
    B returns, then terminated - conservative stall, never a deadlock."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_write(dut, 0x0000_1000, [0x1DC0_0001], txn_id=1)
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "w1 downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")

    # id=3 hashes to the same AxiLookBits=1 bucket as the in-flight id=1, so
    # the demux must hold it (a same-hash write may not switch ports while
    # one is outstanding). The select-stability assertions guard the stall.
    wc_task = cocotb.start_soon(issue_write(dut, 0x0000_2000, [0x1DC0_0002], txn_id=3))
    for cycle in range(10):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert dut.slv_aw_valid_i.value == 1 and dut.slv_aw_ready_o.value == 0, (
            f"colliding-ID AW not held at the gate at cycle {cycle}; {dbg(dut)}"
        )
        assert len(mon.b_of(3)) == 0, (
            f"colliding-ID write terminated while its hash was occupied; {dbg(dut)}"
        )
    assert slave.aw_count == 1, f"colliding-ID write leaked downstream; {dbg(dut)}"
    dut._log.info("CHK-IDHASH-COLLISION-STALLS: same-hash write held while B outstanding")

    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1, 20, "w1 B")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    assert len(mon.b_of(3)) == 0, (
        f"B3 arrived before B1 - the hold did not order the responses; {dbg(dut)}"
    )
    await wc_task
    await wait_until(dut, lambda: len(mon.b_of(3)) == 1, 30,
                     "DECERR B for the colliding-ID write")
    assert mon.b_of(3)[0]["resp"] == AXI_RESP_DECERR
    assert slave.aw_count == 1
    dut._log.info("CHK-IDHASH-COLLISION-RESOLVES: held write DECERRed after B, no deadlock")

    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")


@cocotb.test()
async def test_w_before_aw_during_drain(dut):
    """W data offered before its AW (legal in AXI4): with no committed W
    route the demux must stall the beats at the slave port, and when the AW
    then arrives mid-drain and routes to the error slave, the waiting beats
    must follow that late routing decision there - nothing may leak to the
    downstream port."""
    slave, mon = await setup(dut)

    slave.release_b = False
    await issue_write(dut, 0x0000_1000, [0xD00D_0010], txn_id=1)
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "w1 downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")

    # Offer the W burst with no AW anywhere in flight: the demux has no
    # route for it (w_select_valid low), so the beats must stall.
    w2_data = [0xD00D_0011, 0xD00D_0012]
    w2_task = cocotb.start_soon(issue_w(dut, w2_data))
    for cycle in range(8):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert dut.slv_w_valid_i.value == 1 and dut.slv_w_ready_o.value == 0, (
            f"orphan W beat handshook with no AW at cycle {cycle}; {dbg(dut)}"
        )
    assert len(slave.w_beats) == 1, f"orphan W leaked downstream: {slave.w_beats}"
    dut._log.info("CHK-ORPHAN-W-STALLS: W beats held with no committed route")

    # The late AW (opposite ID bucket to the in-flight id=1) arrives during
    # the drain, routes to the error slave, and the waiting beats follow it.
    await issue_aw(dut, 0x0000_2000, 4, len(w2_data))
    await w2_task
    await wait_until(dut, lambda: len(mon.b_of(4)) == 1, 20, "DECERR B for the W-first write")
    b4 = mon.b_of(4)[0]
    assert b4["resp"] == AXI_RESP_DECERR and b4["isolated"] == 0, f"b_events={mon.b_events}"
    assert slave.aw_count == 1 and len(slave.w_beats) == 1, (
        f"W-first write leaked downstream; {dbg(dut)}"
    )
    dut._log.info("CHK-W-FOLLOWS-LATE-AW: beats terminated at the error slave during drain")

    slave.release_b = True
    await wait_until(dut, lambda: len(mon.b_of(1)) == 1, 20, "w1 B")
    assert mon.b_of(1)[0]["resp"] == AXI_RESP_OKAY
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after drain")
