# Copyright 2026 Tenstorrent Inc.
"""Recovery-flush (`flush_i`) behavior: escaping a wedged drain after a
forced reset of the master, without corrupting anything downstream.

Covers the cases from vendor/pulp-platform/axi/upstream/src/README_axi_isolate_flush.md:

* 1.a     - responses stranded at the slave port (master hangs, then is
            force-reset): masked from the very first flush cycle, swallowed,
            every counter pops (inner AND demux ID buckets), window closes
            clean.
* 1.b.1 / 2.b.2 - downstream accepted but never responds: flush clears the
            wedged drain. SAFETY NET: responses that do arrive later inside
            the window (a too-short timeout on a slow-but-live slave) are
            absorbed with no underflow and never shown to the master.
* 1.b.1 residual - downstream NEVER responds: the demux ID bucket of the
            dead transaction leaks (documented; cold reset clears it), other
            buckets stay live.
* 1.b.2 / 2.b.1 - a beat presented-unaccepted downstream (Hold): the flush
            must DEFER per channel (no retraction), then apply by itself
            once the beat is finally accepted.
* 1.b.2 true hang - the Hold beat is NEVER accepted: the flush can never
            apply, `isolated_o` never asserts; after software de-isolates,
            reads are live and writes are dead behind the held AW
            (documented; cold reset clears it).
* 2.b.3   - W burst interrupted: a parked W beat defers only the W clear
            (isolation is not hostage to it); beats arriving after the
            clear are absorbed at the slave port so upstream W routing
            unwinds on their `last`.
* 2.b.3 true hang - the parked W beat is NEVER accepted: isolation still
            completes, but the burst stays wedged through de-isolation
            (documented; downstream acceptance or cold reset clears it).

Which "late" cases are real scenarios and which are safety nets: late W
beats (2.b.3) come from a LIVE upstream master that already had its AW
accepted and is obliged to finish the burst - no timeout length changes
that, so they are primary scenarios. Late RESPONSES (1.b.1 / 2.b.2) are the
timeout-too-short class; they are tested only to prove the flush is harmless
when the timeout was not long enough, since no timeout is provably
sufficient. The true-hang tests cover what a longer timeout cannot fix.

The flush window is latched: every test drives `flush_i` for a single cycle
and relies on the DUT holding the window open until `isolate_i` deasserts.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import (  # pyright: ignore[reportMissingImports]
    ClockCycles,
    FallingEdge,
    ReadOnly,
    RisingEdge,
)

from helpers import (
    AXI_RESP_OKAY,
    AXI_RESP_SLVERR,
    SLVERR_DATA,
    ST_DRAIN,
    ST_HOLD,
    ST_ISOLATE,
    ST_NORMAL,
    dbg,
    flush_active,
    issue_aw,
    issue_read,
    issue_w,
    issue_write,
    pending_ar,
    pending_aw,
    pending_w,
    rdata_for,
    settle,
    setup,
    state_ar,
    state_aw,
    wait_until,
)


async def flush_pulse(dut, in_cycle=None):
    """Assert flush_i for exactly one cycle; the DUT latches the window.

    `in_cycle`, if given, is evaluated at the falling edge of the pulse
    cycle itself - before `flush_active_q` has latched - to check the
    combinational effects the pulse must have from its very first cycle."""
    dut.flush_i.value = 1
    if in_cycle is not None:
        await FallingEdge(dut.clk_i)
        await ReadOnly()
        assert in_cycle(), f"flush-cycle check failed before the window latched; {dbg(dut)}"
    await RisingEdge(dut.clk_i)
    await settle(dut)
    dut.flush_i.value = 0


async def assert_held(dut, cond, cycles: int, what: str) -> None:
    """cond() must hold on every one of the next `cycles` post-edge samples."""
    for cycle in range(cycles):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert cond(), f"{what} violated at cycle {cycle}; {dbg(dut)}"


def reset_downstream(slave):
    """Model the downstream's own force-reset (the flush's premise): a torn
    burst leaves dangling per-burst state in the downstream, and it is that
    reset - not the isolate - which clears it. Without this, the in-order
    slave model would pair later B responses with the dead AW."""
    slave.aw_records.clear()
    slave.aw_count = 0
    slave.w_beats.clear()
    slave.w_last_count = 0
    slave.ar_records.clear()
    slave.ar_count = 0
    slave.b_queue.clear()
    slave.b_sent = 0
    slave.r_queue.clear()


async def recover_and_probe(dut, slave, mon, wr_id: int, rd_id: int, base: int):
    """Deisolate, verify the window closed, and prove both directions live
    end to end (write B OKAY from downstream, read data exact)."""
    # The pulse was a single cycle many cycles ago: the window must still be
    # open here, i.e. the DUT latched it rather than tracking flush_i.
    assert flush_active(dut) == 1, f"flush window closed before de-isolation; {dbg(dut)}"
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "de-isolation")
    assert flush_active(dut) == 0, f"flush window survived de-isolation; {dbg(dut)}"

    aw_before, b_before = slave.aw_count, len(mon.b_of(wr_id))
    await issue_write(dut, base, [0x0EC0_0000 | wr_id], txn_id=wr_id)
    await wait_until(dut, lambda: len(mon.b_of(wr_id)) == b_before + 1, 30,
                     "recovery write B")
    assert mon.b_of(wr_id)[-1]["resp"] == AXI_RESP_OKAY, f"b={mon.b_of(wr_id)}"
    assert slave.aw_count == aw_before + 1, f"recovery write not downstream; {dbg(dut)}"

    r_before = len(mon.r_of(rd_id))
    await issue_read(dut, base + 0x100, txn_id=rd_id, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(rd_id)) == r_before + 2, 30,
                     "recovery read R")
    beats = mon.r_of(rd_id)[-2:]
    assert [b["data"] for b in beats] == [rdata_for(base + 0x100, i) for i in range(2)]
    assert all(b["resp"] == AXI_RESP_OKAY for b in beats), f"r={beats}"


async def assert_buckets_popped(dut, mon, wr_id: int, rd_id: int, base: int):
    """Prove the internal demux ID buckets of an earlier flushed transaction
    really popped: isolate again and issue a write/read whose IDs share the
    LookBits=1 bucket (same ID LSB) with the flushed ones. A leaked count
    would hold them at the demux gate forever; a clean bucket terminates
    them with SLVERR. Leaves the DUT de-isolated."""
    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "re-isolation")
    await issue_write(dut, base, [0xB0C0_0000 | wr_id], txn_id=wr_id)
    await wait_until(dut, lambda: len(mon.b_of(wr_id)) == 1, 30, "same-bucket SLVERR B")
    assert mon.b_of(wr_id)[0]["resp"] == AXI_RESP_SLVERR, f"b={mon.b_of(wr_id)}"
    await issue_read(dut, base + 0x100, txn_id=rd_id, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(rd_id)) == 2, 30, "same-bucket SLVERR R")
    assert all(b["resp"] == AXI_RESP_SLVERR and b["data"] == SLVERR_DATA
               for b in mon.r_of(rd_id)), f"r={mon.r_of(rd_id)}"
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "final reopen")


@cocotb.test()
async def test_flush_swallows_stranded_responses(dut):
    """Case 1.a: the master stops taking B/R (then is force-reset while its
    responses sit parked at the slave port). The flush must consume the
    parked responses internally (popping every counter on the way out),
    mask them from the master, isolate, and reopen clean."""
    slave, mon = await setup(dut)

    # Master hangs: stops accepting responses.
    dut.slv_b_ready_i.value = 0
    dut.slv_r_ready_i.value = 0
    await issue_write(dut, 0x0000_1000, [0xF1A5_0001], txn_id=1)
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=2)
    await wait_until(
        dut,
        lambda: dut.slv_b_valid_o.value == 1 and dut.slv_r_valid_o.value == 1,
        30,
        "responses parked at the slave port",
    )
    assert pending_aw(dut) == 1 and pending_ar(dut) == 1, dbg(dut)

    # Reset sequence starts: the drain can never finish.
    dut.isolate_i.value = 1
    await wait_until(
        dut,
        lambda: state_aw(dut) == ST_DRAIN and state_ar(dut) == ST_DRAIN,
        10,
        "both channels in Drain",
    )
    await assert_held(
        dut,
        lambda: dut.isolated_o.value == 0 and pending_aw(dut) == 1 and pending_ar(dut) == 1,
        20,
        "drain wedged on the hung master",
    )
    dut._log.info(f"CHK-FLUSH-WEDGE-FORMED: {dbg(dut)}")

    # The timeout-forced reset fires the (single-cycle, latched) flush. The
    # outward mask and the inward ready force are combinational on flush_i,
    # so they must already be in effect in the pulse cycle itself - before
    # the window has latched. This is the one deliberate "retraction" of
    # the design (a valid pulled from a master that is being reset anyway).
    await flush_pulse(
        dut,
        in_cycle=lambda: dut.slv_b_valid_o.value == 0
        and dut.slv_r_valid_o.value == 0
        and dut.mst_b_ready_o.value == 1
        and dut.mst_r_ready_o.value == 1,
    )

    # The parked responses must drain internally, never visibly: outward
    # valid stays masked for the rest of the window.
    for _ in range(20):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert dut.slv_b_valid_o.value == 0 and dut.slv_r_valid_o.value == 0, (
            f"stale response shown to the master during the flush window; {dbg(dut)}"
        )
        # b_sent / r_queue book the downstream model's mst-port handshakes:
        # B accepted and the R queue drained = the isolate consumed the whole
        # response stream, and the masked valids above prove none of it
        # reached the master.
        if slave.b_sent == 1 and len(slave.r_queue) == 0:
            break
    else:
        raise AssertionError(f"stranded responses were not absorbed; {dbg(dut)}")
    dut._log.info("CHK-FLUSH-SWALLOW: stranded B and R consumed internally, masked outward")

    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "isolation after flush")
    assert pending_aw(dut) == 0 and pending_ar(dut) == 0, dbg(dut)
    dut._log.info(f"CHK-FLUSH-ISOLATED: {dbg(dut)}")

    # Master "reboots" INSIDE the still-open window and is ready again. Only
    # now is the monitor a real leak detector (with ready low, no handshake
    # could have been recorded regardless): the mask must hold until
    # de-isolation, so nothing may complete toward the master.
    dut.slv_b_ready_i.value = 1
    dut.slv_r_ready_i.value = 1
    await assert_held(
        dut,
        lambda: dut.slv_b_valid_o.value == 0
        and dut.slv_r_valid_o.value == 0
        and len(mon.b_events) == 0
        and len(mon.r_events) == 0,
        10,
        "response mask held against a ready master inside the window",
    )
    dut._log.info("CHK-FLUSH-MASK-HELD: ready master saw no stale handshake in-window")

    await recover_and_probe(dut, slave, mon, wr_id=1, rd_id=2, base=0x0000_3000)
    dut._log.info("CHK-FLUSH-RECOVERY: both directions live after the window closed")

    # "Every counter pops": the swallowed B/R went through the internal demux,
    # so its ID buckets must be clean too (id=3 shares the bucket of the
    # stale id=1 write, id=4 that of the stale id=2 read).
    await assert_buckets_popped(dut, mon, wr_id=3, rd_id=4, base=0x0000_4000)
    dut._log.info("CHK-FLUSH-BUCKETS-POPPED: swallowed responses popped the demux buckets")


@cocotb.test()
async def test_flush_clears_wedge_and_absorbs_late_responses(dut):
    """Cases 1.b.1 / 2.b.2: downstream accepted the transactions but never
    responds, so the drain wedges with nothing parked anywhere. The flush
    must clear the bookkeeping and isolate immediately.

    The second half is a SAFETY NET, not the primary scenario: if the slave
    was merely slow (the timeout was too short), its responses arrive inside
    the window. No timeout is provably long enough for every slave, so the
    flush must be harmless when that happens: absorbed saturating-at-zero
    (no underflow, no re-count, master sees nothing). Because the absorbed
    responses drain through the internal demux, its ID buckets pop: after
    recovery the next isolation must still terminate same-bucket traffic
    (no latent wedge). The never-responds outcome is the leak test below."""
    slave, mon = await setup(dut)

    slave.release_b = False
    slave.release_r = False
    await issue_write(dut, 0x0000_1000, [0x1B10_0001], txn_id=1)
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=2)
    await wait_until(dut, lambda: slave.aw_count == 1 and slave.ar_count == 1, 20,
                     "downstream acceptance")

    dut.isolate_i.value = 1
    await wait_until(
        dut,
        lambda: state_aw(dut) == ST_DRAIN and state_ar(dut) == ST_DRAIN,
        10,
        "both channels in Drain",
    )
    # The vanilla deadlock: nothing can ever pop these counters.
    await assert_held(
        dut,
        lambda: dut.isolated_o.value == 0 and pending_aw(dut) == 1 and pending_ar(dut) == 1,
        30,
        "drain wedged on the silent downstream",
    )
    dut._log.info(f"CHK-FLUSH-WEDGE-FORMED: {dbg(dut)}")

    # Nothing is presented downstream, so nothing defers: the counters clear
    # and both FSMs are forced to Isolate on the flush edge itself.
    await flush_pulse(dut)
    assert dut.isolated_o.value == 1, f"flush did not isolate on its own edge; {dbg(dut)}"
    assert pending_aw(dut) == 0 and pending_ar(dut) == 0, dbg(dut)
    assert slave.b_sent == 0, "nothing should have been absorbed yet"
    dut._log.info(f"CHK-FLUSH-CLEARS-WEDGE: isolated with the burst still owed; {dbg(dut)}")

    # The stale responses arrive INSIDE the still-open window. The master is
    # ready (slv_b/r_ready high since setup), so any leak would be a real
    # handshake - the monitor must stay empty.
    slave.release_b = True
    slave.release_r = True
    for _ in range(20):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert dut.isolated_o.value == 1, f"flush window lost isolation; {dbg(dut)}"
        assert pending_aw(dut) == 0 and pending_ar(dut) == 0, (
            f"late response re-counted or underflowed; {dbg(dut)}"
        )
        # Same mst-port booking as the 1.a test = fully absorbed internally.
        if slave.b_sent == 1 and len(slave.r_queue) == 0:
            break
    else:
        raise AssertionError(f"late responses were not absorbed; {dbg(dut)}")
    assert len(mon.b_events) == 0 and len(mon.r_events) == 0, (
        f"stale response leaked to the master: b={mon.b_events} r={mon.r_events}"
    )
    dut._log.info("CHK-FLUSH-LATE-ABSORB: in-window responses eaten, counters pinned at 0")

    await recover_and_probe(dut, slave, mon, wr_id=1, rd_id=2, base=0x0000_3000)

    # The absorbed responses popped the internal demux ID buckets: a second
    # isolation must terminate same-bucket traffic instead of stalling on a
    # leaked count (id=3 shares the LookBits=1 bucket with the stale id=1
    # write, id=4 with the stale id=2 read).
    await assert_buckets_popped(dut, mon, wr_id=3, rd_id=4, base=0x0000_4000)
    dut._log.info("CHK-FLUSH-BUCKETS-POPPED: next isolation terminates same-bucket IDs")


@cocotb.test()
async def test_flush_never_responding_slave_leaks_one_bucket(dut):
    """Case 1.b.1 residual, documented behavior: if the dead slave NEVER
    responds, the flush still un-wedges the isolate, but the internal demux
    ID bucket of the dead write cannot pop. On the next isolation a
    same-bucket AW must stall at the demux gate (the latent wedge the
    README documents - bounded observation here), while the other bucket
    still terminates fine. Only a cold reset clears the leak."""
    slave, mon = await setup(dut)

    slave.release_b = False  # writes never answered; reads stay live
    await issue_write(dut, 0x0000_1000, [0xDEAD_0001], txn_id=1)
    await wait_until(dut, lambda: slave.aw_count == 1 and slave.w_last_count == 1, 20,
                     "dead write accepted downstream")

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    await assert_held(dut, lambda: dut.isolated_o.value == 0 and pending_aw(dut) == 1,
                      20, "drain wedged on the dead write")

    await flush_pulse(dut)
    assert dut.isolated_o.value == 1, f"flush did not isolate on its own edge; {dbg(dut)}"
    assert pending_aw(dut) == 0, dbg(dut)
    dut._log.info(f"CHK-FLUSH-UNWEDGED: isolate recovered from the dead write; {dbg(dut)}")

    # Reopen: read path fully live (the write B path downstream stays dead).
    assert flush_active(dut) == 1, f"flush window closed before de-isolation; {dbg(dut)}"
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "de-isolation")
    assert flush_active(dut) == 0, f"flush window survived de-isolation; {dbg(dut)}"
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(2)) == 2, 30, "recovery read")
    assert [b["data"] for b in mon.r_of(2)] == [rdata_for(0x0000_2000, i) for i in range(2)]
    dut._log.info("CHK-LEAK-READS-LIVE: read path unaffected by the write-bucket leak")

    # Second isolation: the clean bucket (id=2, LSB 0) still terminates...
    dut.isolate_i.value = 1
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 10, "second isolation")
    await issue_write(dut, 0x0000_3000, [0xDEAD_0002], txn_id=2)
    await wait_until(dut, lambda: len(mon.b_of(2)) == 1, 30, "clean-bucket SLVERR B")
    assert mon.b_of(2)[0]["resp"] == AXI_RESP_SLVERR, f"b={mon.b_of(2)}"
    dut._log.info("CHK-LEAK-CONFINED: clean bucket still terminates during isolation")

    # ...but the leaked bucket (id=3 shares LSB with the dead id=1) stalls at
    # the demux gate: its port-0 count can never drain. Bounded observation
    # of the documented latent wedge.
    await RisingEdge(dut.clk_i)
    dut.slv_aw_valid_i.value = 1
    dut.slv_aw_id_i.value = 3
    dut.slv_aw_addr_i.value = 0x0000_4000
    dut.slv_aw_len_i.value = 0
    dut.slv_aw_size_i.value = 2
    dut.slv_aw_burst_i.value = 1
    await assert_held(
        dut,
        lambda: dut.slv_aw_ready_o.value == 0 and len(mon.b_of(3)) == 0,
        30,
        "leaked-bucket AW stalled at the demux gate",
    )
    dut._log.info("CHK-LEAK-DOCUMENTED: same-bucket AW held forever (cold reset required)")

    # The stalled AW may not be retracted (demux stability SVAs); end the
    # test the way the real system ends this state - with a cold reset,
    # whose `disable iff` window also covers the deassertion.
    await RisingEdge(dut.clk_i)
    dut.rst_ni.value = 0
    dut.slv_aw_valid_i.value = 0
    dut.isolate_i.value = 1
    await ClockCycles(dut.clk_i, 4)


@cocotb.test()
async def test_flush_defers_in_hold_aw(dut):
    """Cases 1.b.2 / 2.b.1, write flavor: AW and W are presented-unaccepted
    downstream (Hold) when the flush fires. The flush must defer - no
    retraction, payload stable, counters kept - and `isolated_o` must stay
    low (this is the class the flush deliberately does not force). When the
    downstream finally accepts (e.g. the freshly-reset cluster), the write
    completes into it, the latched flush applies by itself, and its B is
    absorbed by the still-open window."""
    slave, mon = await setup(dut)

    slave.accept_aw = False
    slave.accept_w = False
    wr_task = cocotb.start_soon(
        issue_write(dut, 0x0000_1000, [0x1101_0001], txn_id=1, cycles=600)
    )
    await wait_until(
        dut,
        lambda: state_aw(dut) == ST_HOLD
        and dut.mst_aw_valid_o.value == 1
        and dut.mst_w_valid_o.value == 1,
        20,
        "AW+W parked downstream (Hold)",
    )
    held_id = int(dut.mst_aw_id_o.value)
    held_addr = int(dut.mst_aw_addr_o.value)
    held_wdata = int(dut.mst_w_data_o.value)

    dut.isolate_i.value = 1
    await ClockCycles(dut.clk_i, 3)
    await settle(dut)
    assert state_aw(dut) == ST_HOLD, f"Hold lost on isolate; {dbg(dut)}"

    await flush_pulse(dut)

    # Deferral window: the flush is latched and pending, but must not touch
    # the committed channel. The idle AR side flushes to Isolate.
    await assert_held(
        dut,
        lambda: state_aw(dut) == ST_HOLD
        and dut.mst_aw_valid_o.value == 1
        and int(dut.mst_aw_id_o.value) == held_id
        and int(dut.mst_aw_addr_o.value) == held_addr
        and dut.mst_w_valid_o.value == 1
        and int(dut.mst_w_data_o.value) == held_wdata
        and pending_aw(dut) == 1
        and pending_w(dut) == 1
        and dut.isolated_o.value == 0
        and state_ar(dut) == ST_ISOLATE
        and flush_active(dut) == 1,
        30,
        "flush deferred with no retraction (AW/W held)",
    )
    dut._log.info(f"CHK-FLUSH-HOLD-DEFERS-AW: {dbg(dut)}")

    # Downstream relents (the freshly-reset cluster starts accepting): the
    # stray write lands downstream - the documented 2.b.1 residual - and the
    # latched flush then applies without another pulse.
    slave.accept_aw = True
    slave.accept_w = True
    await wr_task
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 15,
                     "flush applied after the late acceptance")
    assert pending_aw(dut) == 0 and pending_w(dut) == 0, dbg(dut)
    assert slave.aw_count == 1 and slave.w_last_count == 1, (
        f"stray write did not land downstream intact; {dbg(dut)}"
    )
    # Its B is generated downstream and absorbed by the window: the master
    # is ready, so a leak would be a recorded handshake.
    await wait_until(dut, lambda: slave.b_sent == 1, 20, "stray B absorbed")
    assert len(mon.b_of(1)) == 0, f"stray B leaked to the master: {mon.b_events}"
    dut._log.info("CHK-FLUSH-APPLIES-AFTER-HOLD: late acceptance completed the flush")

    await recover_and_probe(dut, slave, mon, wr_id=1, rd_id=2, base=0x0000_3000)


@cocotb.test()
async def test_flush_defers_in_hold_ar(dut):
    """Cases 1.b.2 / 2.b.1, read flavor: mirror of the AW test. The AR in
    Hold defers the flush (no retraction, pending kept, not isolated); once
    accepted, the stray read lands downstream, the flush applies, and the R
    burst is absorbed by the window."""
    slave, mon = await setup(dut)

    slave.accept_ar = False
    rd_task = cocotb.start_soon(
        issue_read(dut, 0x0000_2000, txn_id=5, num_beats=2, cycles=600)
    )
    await wait_until(
        dut,
        lambda: state_ar(dut) == ST_HOLD and dut.mst_ar_valid_o.value == 1,
        20,
        "AR parked downstream (Hold)",
    )
    held_id = int(dut.mst_ar_id_o.value)
    held_addr = int(dut.mst_ar_addr_o.value)

    dut.isolate_i.value = 1
    await ClockCycles(dut.clk_i, 3)
    await settle(dut)
    assert state_ar(dut) == ST_HOLD, f"Hold lost on isolate; {dbg(dut)}"

    await flush_pulse(dut)

    await assert_held(
        dut,
        lambda: state_ar(dut) == ST_HOLD
        and dut.mst_ar_valid_o.value == 1
        and int(dut.mst_ar_id_o.value) == held_id
        and int(dut.mst_ar_addr_o.value) == held_addr
        and pending_ar(dut) == 1
        and dut.isolated_o.value == 0
        and state_aw(dut) == ST_ISOLATE
        and flush_active(dut) == 1,
        30,
        "flush deferred with no retraction (AR held)",
    )
    dut._log.info(f"CHK-FLUSH-HOLD-DEFERS-AR: {dbg(dut)}")

    slave.accept_ar = True
    await rd_task
    await wait_until(dut, lambda: dut.isolated_o.value == 1, 15,
                     "flush applied after the late acceptance")
    assert pending_ar(dut) == 0, dbg(dut)
    assert slave.ar_count == 1, f"stray read did not land downstream; {dbg(dut)}"
    await wait_until(dut, lambda: len(slave.r_queue) == 0, 20, "stray R burst absorbed")
    assert len(mon.r_of(5)) == 0, f"stray R leaked to the master: {mon.r_events}"
    dut._log.info("CHK-FLUSH-APPLIES-AFTER-HOLD: late acceptance completed the flush")

    await recover_and_probe(dut, slave, mon, wr_id=1, rd_id=5, base=0x0000_3000)


@cocotb.test()
async def test_flush_hold_never_accepted_defers_forever(dut):
    """Case 1.b.2 true hang, documented behavior: the downstream NEVER
    accepts the AW parked in Hold. Nothing here is "late" - the acceptor is
    dead - so a longer timeout cannot help, and the flush has no
    protocol-legal move: it must defer for as long as the beat is
    unaccepted, so `isolated_o` never asserts and the window stays latched.

    In the system the timeout has already forced the CPU reset regardless of
    `drained_i`, and software then releases the reset (de-isolates) with the
    AW still held. Bounded observation of what that leaves behind: the window
    closes, nothing is retracted, the read path is fully live again, and the
    write path is dead behind the held AW (Hold has no payload register, so
    the held valid keeps following the slave port). Only a cold reset clears
    it."""
    slave, mon = await setup(dut)

    slave.accept_aw = False
    slave.accept_w = False
    wr_task = cocotb.start_soon(
        issue_write(dut, 0x0000_1000, [0x1102_0001], txn_id=1, cycles=2000)
    )
    await wait_until(
        dut,
        lambda: state_aw(dut) == ST_HOLD and dut.mst_aw_valid_o.value == 1,
        20,
        "AW parked downstream (Hold)",
    )
    held_id = int(dut.mst_aw_id_o.value)
    held_addr = int(dut.mst_aw_addr_o.value)

    dut.isolate_i.value = 1
    await ClockCycles(dut.clk_i, 3)
    await settle(dut)
    await flush_pulse(dut)

    # Long bounded window: the flush never applies to the held channel.
    await assert_held(
        dut,
        lambda: state_aw(dut) == ST_HOLD
        and dut.mst_aw_valid_o.value == 1
        and int(dut.mst_aw_id_o.value) == held_id
        and int(dut.mst_aw_addr_o.value) == held_addr
        and pending_aw(dut) == 1
        and dut.isolated_o.value == 0
        and state_ar(dut) == ST_ISOLATE
        and flush_active(dut) == 1,
        200,
        "flush deferred indefinitely on the dead acceptor",
    )
    dut._log.info(f"CHK-HANG-HOLD-NEVER-APPLIES: {dbg(dut)}")

    # Software releases the reset (the force already reset the CPU): the
    # window closes with the AW still held and not retracted.
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: state_ar(dut) == ST_NORMAL, 10, "AR side reopened")
    assert flush_active(dut) == 0, f"flush window survived de-isolation; {dbg(dut)}"
    assert (
        state_aw(dut) == ST_HOLD
        and dut.mst_aw_valid_o.value == 1
        and int(dut.mst_aw_id_o.value) == held_id
        and int(dut.mst_aw_addr_o.value) == held_addr
    ), f"held AW disturbed by de-isolation; {dbg(dut)}"

    # Reads are fully live.
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(2)) == 2, 30, "read after the hang")
    assert [b["data"] for b in mon.r_of(2)] == [rdata_for(0x0000_2000, i) for i in range(2)]
    dut._log.info("CHK-HANG-READS-LIVE: read path unaffected by the held AW")

    # Writes are dead: the slave port's aw_ready follows the dead downstream,
    # so no new AW can enter and the held one never completes.
    await assert_held(
        dut,
        lambda: dut.slv_aw_ready_o.value == 0
        and state_aw(dut) == ST_HOLD
        and dut.mst_aw_valid_o.value == 1
        and len(mon.b_events) == 0,
        30,
        "write path dead behind the held AW",
    )
    dut._log.info("CHK-HANG-WRITES-DEAD: held AW blocks the write path (cold reset required)")

    # The held AW may not be retracted (demux stability SVAs); end the test
    # the way the real system ends this state - with a cold reset.
    wr_task.kill()
    await RisingEdge(dut.clk_i)
    dut.rst_ni.value = 0
    dut.slv_aw_valid_i.value = 0
    dut.slv_w_valid_i.value = 0
    dut.isolate_i.value = 1
    await ClockCycles(dut.clk_i, 4)


@cocotb.test()
async def test_flush_w_midburst_defers_then_unwinds(dut):
    """Case 2.b.3, parked-beat branch: the flush fires with a W beat
    presented-unaccepted downstream mid-burst. The AW/AR bookkeeping is
    flushed and `isolated_o` asserts (isolation is not hostage to W), but
    the W clear defers: the parked beat is never retracted. Once downstream
    accepts (the reset cluster's fresh w_ready), the burst unwinds: the
    parked beat completes downstream, and its acceptance cycle is exactly
    the cycle the deferred W clear applies (the window is latched), so
    pending_w drops to zero and EVERY later beat of the burst is absorbed
    at the slave port - the upstream routing still unwinds on `last`, and
    the downstream sees precisely one (torn, non-last) beat. Torn data
    toward the downstream is by-premise harmless there: the flush only ever
    fires when that downstream is being force-reset."""
    slave, mon = await setup(dut)

    slave.accept_w = False
    wdata = [0x2B30_0000 + i for i in range(4)]
    wr_task = cocotb.start_soon(
        issue_write(dut, 0x0000_1000, wdata, txn_id=1, cycles=600)
    )
    await wait_until(
        dut,
        lambda: slave.aw_count == 1 and dut.mst_w_valid_o.value == 1,
        20,
        "AW downstream, first W beat parked",
    )
    held_wdata = int(dut.mst_w_data_o.value)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    await assert_held(dut, lambda: dut.isolated_o.value == 0 and pending_aw(dut) == 1,
                      15, "drain wedged mid-burst")

    await flush_pulse(dut)

    # AW/AR flush; the parked W beat defers its clear and is not retracted.
    await assert_held(
        dut,
        lambda: dut.isolated_o.value == 1
        and pending_aw(dut) == 0
        and pending_w(dut) == 1
        and dut.mst_w_valid_o.value == 1
        and int(dut.mst_w_data_o.value) == held_wdata,
        20,
        "isolated with the parked W beat deferred, not retracted",
    )
    dut._log.info(f"CHK-FLUSH-NOT-HOSTAGE-TO-W: {dbg(dut)}")

    # Downstream relents: the burst unwinds. The parked beat completes
    # downstream, the latched flush clears pending_w on that same cycle, and
    # the remaining three beats are absorbed at the slave port. The host
    # burst finishes and the counters end at zero with no underflow.
    slave.accept_w = True
    await wr_task
    await wait_until(dut, lambda: pending_w(dut) == 0, 20, "pending_w cleared")
    assert dut.isolated_o.value == 1, dbg(dut)
    assert dut.mst_w_valid_o.value == 0, f"W still presented downstream after unwind; {dbg(dut)}"
    assert slave.w_beats == [(held_wdata, 0)], (
        f"expected exactly the parked (non-last) beat downstream, got {slave.w_beats}"
    )
    # No `last` ever reached the downstream, so it cannot answer; nothing may
    # surface toward the master either way.
    assert slave.b_sent == 0 and len(mon.b_of(1)) == 0, (
        f"response for the torn write: b_sent={slave.b_sent} events={mon.b_events}"
    )
    dut._log.info(
        f"CHK-FLUSH-W-UNWINDS: host burst completed "
        f"({len(slave.w_beats)}/4 beats downstream, rest absorbed)"
    )

    reset_downstream(slave)
    await recover_and_probe(dut, slave, mon, wr_id=2, rd_id=2, base=0x0000_3000)


@cocotb.test()
async def test_flush_w_parked_never_accepted_isolation_proceeds(dut):
    """Case 2.b.3 true hang, documented behavior: the W beat parked
    downstream mid-burst is NEVER accepted (the hung cluster's w_ready stays
    low, and stays low after its reset too). Isolation is not hostage to it:
    the flush isolates on its own edge, so in the system `drained_i` asserts
    and the reset handshake completes. But the W clear defers forever - the
    beat is never retracted, `pending_w` stays at 1, and the upstream master
    stays wedged mid-burst.

    Bounded observation of the residual after software de-isolates: the
    window closes, reads are live, the parked beat is still held intact and
    nothing of the burst has reached the downstream, and the torn write never
    produces a B. Only the downstream accepting (the unwind test) or a cold
    reset clears it."""
    slave, mon = await setup(dut)

    slave.accept_w = False
    wdata = [0x2B31_0000 + i for i in range(4)]
    wr_task = cocotb.start_soon(
        issue_write(dut, 0x0000_1000, wdata, txn_id=1, cycles=2000)
    )
    await wait_until(
        dut,
        lambda: slave.aw_count == 1 and dut.mst_w_valid_o.value == 1,
        20,
        "AW downstream, first W beat parked",
    )
    held_wdata = int(dut.mst_w_data_o.value)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    await flush_pulse(dut)
    assert dut.isolated_o.value == 1, f"isolation held hostage to the parked W; {dbg(dut)}"
    assert pending_aw(dut) == 0 and pending_w(dut) == 1, dbg(dut)

    # Long bounded window: isolation holds, the W clear never applies, the
    # parked beat is never retracted.
    await assert_held(
        dut,
        lambda: dut.isolated_o.value == 1
        and pending_w(dut) == 1
        and dut.mst_w_valid_o.value == 1
        and int(dut.mst_w_data_o.value) == held_wdata
        and flush_active(dut) == 1,
        200,
        "W clear deferred indefinitely with isolation held",
    )
    dut._log.info(f"CHK-HANG-W-ISOLATION-NOT-HOSTAGE: {dbg(dut)}")

    # Software releases the reset with the beat still parked.
    dut.isolate_i.value = 0
    await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "de-isolation")
    assert flush_active(dut) == 0, f"flush window survived de-isolation; {dbg(dut)}"
    assert pending_w(dut) == 1 and dut.mst_w_valid_o.value == 1, (
        f"parked beat disturbed by de-isolation; {dbg(dut)}"
    )

    # Reads are fully live.
    await issue_read(dut, 0x0000_2000, txn_id=2, num_beats=2)
    await wait_until(dut, lambda: len(mon.r_of(2)) == 2, 30, "read after the hang")
    assert [b["data"] for b in mon.r_of(2)] == [rdata_for(0x0000_2000, i) for i in range(2)]
    dut._log.info("CHK-HANG-READS-LIVE: read path unaffected by the wedged burst")

    # The torn burst stays wedged: beat held intact, nothing downstream, no B.
    await assert_held(
        dut,
        lambda: pending_w(dut) == 1
        and dut.mst_w_valid_o.value == 1
        and int(dut.mst_w_data_o.value) == held_wdata
        and len(slave.w_beats) == 0
        and len(mon.b_events) == 0,
        30,
        "torn burst still wedged after de-isolation",
    )
    dut._log.info("CHK-HANG-W-RESIDUAL: burst wedged until downstream accepts or cold reset")

    # The parked beat may not be retracted; end with a cold reset.
    wr_task.kill()
    await RisingEdge(dut.clk_i)
    dut.rst_ni.value = 0
    dut.slv_aw_valid_i.value = 0
    dut.slv_w_valid_i.value = 0
    dut.isolate_i.value = 1
    await ClockCycles(dut.clk_i, 4)


@cocotb.test()
async def test_flush_absorbs_late_w_at_slave_port(dut):
    """Case 2.b.3, late-beats branch (the chiplet write-wedge contract): the
    flush fires with an accepted AW whose W beats have not arrived at all.
    Everything clears (isolated with the burst incomplete), and when the
    master's W beats show up inside the window they are absorbed at the
    slave port - never forwarded downstream - so the upstream W routing can
    unwind on their `last`."""
    slave, mon = await setup(dut)

    await issue_aw(dut, 0x0000_1000, 1, 4)  # 4-beat write, no data offered
    await wait_until(dut, lambda: slave.aw_count == 1, 20, "AW accepted downstream")
    assert pending_aw(dut) == 1 and pending_w(dut) == 1, dbg(dut)

    dut.isolate_i.value = 1
    await wait_until(dut, lambda: state_aw(dut) == ST_DRAIN, 10, "AW Drain")
    await assert_held(dut, lambda: dut.isolated_o.value == 0, 15,
                      "drain wedged on the beat-less write")

    # No beat is presented downstream, so the W clear does not defer either:
    # everything clears on the flush edge.
    await flush_pulse(dut)
    assert dut.isolated_o.value == 1, f"flush did not isolate on its own edge; {dbg(dut)}"
    assert pending_aw(dut) == 0 and pending_w(dut) == 0, dbg(dut)
    dut._log.info(f"CHK-FLUSH-NOT-HOSTAGE-TO-W: {dbg(dut)}")

    # The owed beats arrive inside the window: absorbed at the slave port,
    # nothing downstream, and the master's burst completes (this is what
    # lets the upstream W routing close its tracking).
    absorb_task = cocotb.start_soon(issue_w(dut, [0x3B30_0000 + i for i in range(4)]))
    for _ in range(30):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        assert dut.mst_w_valid_o.value == 0, (
            f"late W beat leaked downstream during the flush window; {dbg(dut)}"
        )
        if absorb_task.done():
            break
    else:
        raise AssertionError(f"late W beats were not absorbed; {dbg(dut)}")
    assert len(slave.w_beats) == 0, f"beats leaked downstream: {slave.w_beats}"
    assert pending_w(dut) == 0 and pending_aw(dut) == 0, dbg(dut)
    assert slave.b_sent == 0 and len(mon.b_of(1)) == 0
    dut._log.info("CHK-FLUSH-LATE-W-ABSORBED: owed beats eaten at the slave port")

    # Recovery on the clean bucket (the dead write's bucket is leaked by
    # design here - the never-responding case is covered in the leak test).
    reset_downstream(slave)
    await recover_and_probe(dut, slave, mon, wr_id=2, rd_id=4, base=0x0000_3000)
