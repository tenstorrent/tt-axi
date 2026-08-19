# Copyright 2026 Tenstorrent Inc.
"""Shared utilities for the axi_isolate Cocotb tests.

Provides constants, DUT state probes, AXI channel drivers, the reactive
downstream slave model, the response monitor, and common setup. Every
test_*.py module in this directory imports from here.

Timing convention (matches hw/ip/axi_snoop/tb_vcs): everything the test
drives changes at, or 1 ps after, a rising clock edge - never on the
falling edge. Test code samples settled post-edge state via settle(),
which unlike ReadOnly leaves the sim in a writable phase; ReadOnly appears
only inside the background samplers (slave model, monitors) and the
drivers' same-cycle ready checks, which never deposit afterwards.
"""

import cocotb  # pyright: ignore[reportMissingImports]
from cocotb.triggers import (  # pyright: ignore[reportMissingImports]
    ClockCycles,
    FallingEdge,
    ReadOnly,
    RisingEdge,
    Timer,
)

AXI_RESP_OKAY = 0b00
AXI_RESP_SLVERR = 0b10
AXI_RESP_DECERR = 0b11
DECERR_DATA = 0x1501A7ED
BURST_INCR = 0b01
ATOP_LOAD_ADD = 0x20  # atomic load, little-endian, ADD: returns both B and R
ATOP_STORE_ADD = 0x10  # atomic store, little-endian, ADD: returns only a B

STATE_NAMES = {0: "Normal", 1: "Hold", 2: "Drain", 3: "Isolate"}
ST_NORMAL = 0
ST_HOLD = 1
ST_DRAIN = 2
ST_ISOLATE = 3

# For NUM_PENDING = 4: two ID-LSB buckets, each full at 3, demux gate closes
# once ANY bucket fills, so at most 5 AWs (or ARs) are admitted before the
# gate closes; the inner's derived threshold is 7 (checked at elaboration in
# tb_axi_isolate.sv). test_saturation leans on these numbers.
DEMUX_MAX_ADMITTED = 5
INNER_PENDING = 7


def rdata_for(addr: int, beat: int = 0) -> int:
    """Deterministic read payload the downstream slave returns per beat."""
    return ((addr + 4 * beat) ^ 0xA5A5_A5A5) & 0xFFFF_FFFF


# ----------------------------------------------------------------------------
# DUT state probes (observation mirrors in tb_axi_isolate.sv)
# ----------------------------------------------------------------------------

def sel_aw(dut) -> int:
    return int(dut.obs_sel_aw.value)


def sel_ar(dut) -> int:
    return int(dut.obs_sel_ar.value)


def state_aw(dut) -> int:
    return int(dut.obs_state_aw.value)


def state_ar(dut) -> int:
    return int(dut.obs_state_ar.value)


def pending_aw(dut) -> int:
    return int(dut.obs_pending_aw.value)


def pending_ar(dut) -> int:
    return int(dut.obs_pending_ar.value)


def aw_unaccepted(dut) -> int:
    """Demux-internal AW presented-and-unaccepted (the D4 antecedent)."""
    return int(dut.obs_demux_aw_unaccepted.value)


def ar_unaccepted(dut) -> int:
    """Demux-internal AR presented-and-unaccepted (the D4 antecedent)."""
    return int(dut.obs_demux_ar_unaccepted.value)


def dbg(dut) -> str:
    """Format the DUT observation mirrors for failure diagnostics."""
    return (
        f"state_aw={STATE_NAMES[state_aw(dut)]} "
        f"state_ar={STATE_NAMES[state_ar(dut)]} "
        f"pending(aw,w,ar)=({pending_aw(dut)},"
        f"{int(dut.obs_pending_w.value)},{pending_ar(dut)}) "
        f"sel(aw,ar)=({sel_aw(dut)},{sel_ar(dut)}) "
        f"unacc(aw,ar)=({aw_unaccepted(dut)},{ar_unaccepted(dut)}) "
        f"isolated={int(dut.isolated_o.value)}"
    )


# ----------------------------------------------------------------------------
# Downstream slave model and response monitor
# ----------------------------------------------------------------------------

class DownstreamSlave:
    """Reactive AXI slave on the DUT master port with stall controls.

    accept_aw/accept_w/accept_ar gate the ready lines; release_b/release_r
    hold owed responses so a drain window can be stretched indefinitely.
    Handles bursts (R beats per ar.len, B after w.last) and atomic loads
    (an AW with the R-response ATOP bit owes an R burst as well as a B).
    """

    def __init__(self, dut):
        self.dut = dut
        self.accept_aw = True
        self.accept_w = True
        self.accept_ar = True
        self.release_b = True
        self.release_r = True
        self.aw_records = []
        self.aw_count = 0
        self.w_beats = []  # (data, last) per accepted W beat
        self.w_last_count = 0
        self.ar_records = []
        self.ar_count = 0
        self.b_queue = []
        self.b_sent = 0
        self.r_queue = []

    async def run(self) -> None:
        d = self.dut
        while True:
            await RisingEdge(d.clk_i)
            d.mst_aw_ready_i.value = 1 if self.accept_aw else 0
            d.mst_w_ready_i.value = 1 if self.accept_w else 0
            d.mst_ar_ready_i.value = 1 if self.accept_ar else 0
            b_show = self.release_b and (len(self.b_queue) > self.b_sent)
            d.mst_b_valid_i.value = 1 if b_show else 0
            d.mst_b_id_i.value = self.b_queue[self.b_sent] if b_show else 0
            d.mst_b_resp_i.value = AXI_RESP_OKAY
            r_show = self.release_r and len(self.r_queue) > 0
            head = self.r_queue[0] if r_show else None
            d.mst_r_valid_i.value = 1 if r_show else 0
            d.mst_r_id_i.value = head["id"] if r_show else 0
            d.mst_r_data_i.value = rdata_for(head["addr"], head["beat"]) if r_show else 0
            d.mst_r_resp_i.value = AXI_RESP_OKAY
            d.mst_r_last_i.value = 1 if (r_show and head["beat"] == head["len"]) else 0
            await ReadOnly()
            if d.mst_aw_ready_i.value and d.mst_aw_valid_o.value:
                rec = {
                    "id": int(d.mst_aw_id_o.value),
                    "addr": int(d.mst_aw_addr_o.value),
                    "len": int(d.mst_aw_len_o.value),
                    "atop": int(d.mst_aw_atop_o.value),
                }
                self.aw_records.append(rec)
                self.aw_count += 1
                if rec["atop"] & 0x20:
                    self.r_queue.append(
                        {"id": rec["id"], "addr": rec["addr"], "len": rec["len"], "beat": 0}
                    )
            if d.mst_w_ready_i.value and d.mst_w_valid_o.value:
                self.w_beats.append((int(d.mst_w_data_o.value), int(d.mst_w_last_o.value)))
                if d.mst_w_last_o.value:
                    self.w_last_count += 1
            while len(self.b_queue) < min(self.aw_count, self.w_last_count):
                self.b_queue.append(self.aw_records[len(self.b_queue)]["id"])
            if d.mst_ar_ready_i.value and d.mst_ar_valid_o.value:
                rec = {
                    "id": int(d.mst_ar_id_o.value),
                    "addr": int(d.mst_ar_addr_o.value),
                    "len": int(d.mst_ar_len_o.value),
                }
                self.ar_records.append(rec)
                self.r_queue.append(dict(rec, beat=0))
                self.ar_count += 1
            if d.mst_b_valid_i.value and d.mst_b_ready_o.value:
                self.b_sent += 1
            if d.mst_r_valid_i.value and d.mst_r_ready_o.value:
                self.r_queue[0]["beat"] += 1
                if self.r_queue[0]["beat"] > self.r_queue[0]["len"]:
                    self.r_queue.pop(0)


class RespMonitor:
    """Collects B and R handshakes at the slave port in arrival order.

    Records only actual handshakes (valid && ready), so tests may stall
    slv_b_ready_i / slv_r_ready_i without duplicating events. Each event
    records the isolate state it completed in.

    Samples at the FALLING edge, settled to ReadOnly: test deposits land
    just after the RISING edge (see settle()), so by the falling edge they
    are stable and half a timestep away - a post-rising-edge sample could
    race the very deposit that enables a handshake on the next edge. The
    ReadOnly settle is kept as a guard in case a write ever lands on the
    falling-edge timestep again.
    """

    def __init__(self, dut):
        self.dut = dut
        self.b_events = []
        self.r_events = []

    def b_of(self, txn_id: int):
        return [e for e in self.b_events if e["id"] == txn_id]

    def r_of(self, txn_id: int):
        return [e for e in self.r_events if e["id"] == txn_id]

    async def run(self) -> None:
        d = self.dut
        while True:
            await FallingEdge(d.clk_i)
            await ReadOnly()
            if d.slv_b_valid_o.value and d.slv_b_ready_i.value:
                self.b_events.append(
                    {
                        "id": int(d.slv_b_id_o.value),
                        "resp": int(d.slv_b_resp_o.value),
                        "isolated": int(d.isolated_o.value),
                    }
                )
            if d.slv_r_valid_o.value and d.slv_r_ready_i.value:
                self.r_events.append(
                    {
                        "id": int(d.slv_r_id_o.value),
                        "data": int(d.slv_r_data_o.value),
                        "resp": int(d.slv_r_resp_o.value),
                        "last": int(d.slv_r_last_o.value),
                        "isolated": int(d.isolated_o.value),
                    }
                )


# ----------------------------------------------------------------------------
# Waiting and channel drivers
# ----------------------------------------------------------------------------

async def settle(dut):
    """Advance 1 ps past the current edge so non-blocking (flop) updates
    become visible, then return in a WRITABLE phase: unlike ReadOnly, the
    caller may deposit values immediately. Deposits made after a settle
    land right after the rising edge, where the DUT first samples them on
    the next edge and no falling-edge transitions appear in the wave."""
    await Timer(1, "ps")


async def wait_until(dut, cond, cycles: int, what: str) -> None:
    """Wait for cond() sampled each cycle 1 ps after the rising edge,
    bounded by cycles. Returns settled and writable (see settle()), so the
    caller may deposit values immediately.
    """
    for _ in range(cycles):
        await RisingEdge(dut.clk_i)
        await settle(dut)
        if cond():
            return
    raise AssertionError(f"Timed out after {cycles} cycles waiting for {what}; {dbg(dut)}")


async def issue_aw(dut, addr: int, txn_id: int, num_beats: int, atop: int = 0,
                   cycles: int = 300) -> None:
    """Handshake one AW (payload held stable until accepted)."""
    for _ in range(cycles):
        await RisingEdge(dut.clk_i)
        dut.slv_aw_valid_i.value = 1
        dut.slv_aw_id_i.value = txn_id
        dut.slv_aw_addr_i.value = addr
        dut.slv_aw_len_i.value = num_beats - 1
        dut.slv_aw_size_i.value = 2
        dut.slv_aw_burst_i.value = BURST_INCR
        dut.slv_aw_atop_i.value = atop
        await ReadOnly()
        if dut.slv_aw_ready_o.value:
            break
    else:
        raise AssertionError(f"Timed out on AW handshake @0x{addr:08x} id={txn_id}; {dbg(dut)}")
    await RisingEdge(dut.clk_i)
    dut.slv_aw_valid_i.value = 0


async def issue_w(dut, data, w_gap: int = 0, cycles: int = 300) -> None:
    """Drive one W burst; w_gap idle cycles are inserted between beats."""
    idx = 0
    gap = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk_i)
        active = gap == 0
        dut.slv_w_valid_i.value = 1 if active else 0
        dut.slv_w_data_i.value = data[idx]
        dut.slv_w_strb_i.value = 0xF
        dut.slv_w_last_i.value = 1 if idx == len(data) - 1 else 0
        await ReadOnly()
        if active and dut.slv_w_ready_o.value:
            idx += 1
            if idx == len(data):
                break
            gap = w_gap
        elif gap:
            gap -= 1
    else:
        raise AssertionError(f"Timed out on W burst beat {idx}/{len(data)}; {dbg(dut)}")
    await RisingEdge(dut.clk_i)
    dut.slv_w_valid_i.value = 0
    dut.slv_w_last_i.value = 0


async def issue_write(dut, addr: int, data, txn_id: int = 0, atop: int = 0,
                      w_gap: int = 0, cycles: int = 300) -> None:
    """Issue AW and its W burst concurrently; B is collected by RespMonitor."""
    aw_task = cocotb.start_soon(issue_aw(dut, addr, txn_id, len(data), atop, cycles))
    w_task = cocotb.start_soon(issue_w(dut, data, w_gap, cycles))
    await aw_task
    await w_task


async def issue_read(dut, addr: int, txn_id: int = 0, num_beats: int = 1,
                     cycles: int = 300) -> None:
    """Handshake one AR; R beats are collected by RespMonitor."""
    for _ in range(cycles):
        await RisingEdge(dut.clk_i)
        dut.slv_ar_valid_i.value = 1
        dut.slv_ar_id_i.value = txn_id
        dut.slv_ar_addr_i.value = addr
        dut.slv_ar_len_i.value = num_beats - 1
        dut.slv_ar_size_i.value = 2
        dut.slv_ar_burst_i.value = BURST_INCR
        await ReadOnly()
        if dut.slv_ar_ready_o.value:
            break
    else:
        raise AssertionError(f"Timed out on AR handshake @0x{addr:08x} id={txn_id}; {dbg(dut)}")
    await RisingEdge(dut.clk_i)
    dut.slv_ar_valid_i.value = 0


# ----------------------------------------------------------------------------
# Common setup
# ----------------------------------------------------------------------------

async def setup(dut, deisolate: bool = True):
    """Reset the DUT, start the slave model and response monitor.

    Returns (slave, monitor). Leaves slv_b_ready_i / slv_r_ready_i high;
    tests that need host-side response backpressure lower them afterwards.
    """
    await settle(dut)
    dut.rst_ni.value = 0
    dut.isolate_i.value = 1
    dut.slv_aw_valid_i.value = 0
    dut.slv_w_valid_i.value = 0
    dut.slv_b_ready_i.value = 0
    dut.slv_ar_valid_i.value = 0
    dut.slv_r_ready_i.value = 0
    dut.mst_aw_ready_i.value = 0
    dut.mst_w_ready_i.value = 0
    dut.mst_b_valid_i.value = 0
    dut.mst_ar_ready_i.value = 0
    dut.mst_r_valid_i.value = 0
    await ClockCycles(dut.clk_i, 5)
    dut.rst_ni.value = 1
    await ClockCycles(dut.clk_i, 2)

    slave = DownstreamSlave(dut)
    monitor = RespMonitor(dut)
    cocotb.start_soon(slave.run())
    cocotb.start_soon(monitor.run())
    dut.slv_b_ready_i.value = 1
    dut.slv_r_ready_i.value = 1
    await ClockCycles(dut.clk_i, 2)

    if deisolate:
        dut.isolate_i.value = 0
        await wait_until(dut, lambda: dut.isolated_o.value == 0, 10, "de-isolation")
    return slave, monitor
