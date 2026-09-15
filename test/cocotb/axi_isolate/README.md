# `axi_isolate` Block Tests

This cocotb testbench checks normal isolation and forced-reset recovery with
`TerminateTransaction=1`.

## Run

```bash
module load synopsys/vcs/W-2024.09-SP2-5
source <python-env-with-cocotb-1.x>/bin/activate

cd test/cocotb/axi_isolate
make sim-vcs
make sim-vcs MODULE=test_flush TESTCASE=test_flush_swallows_stranded_responses WAVES=1
```

The full suite has 24 tests. The first run uses Bender to fetch
`common_cells`. `WAVES=1` writes `tb_axi_isolate.vcd`.

## Coverage

- `test_sanity`: normal traffic and `SLVERR` responses while isolated.
- `test_drain`: writes, reads, and partial bursts already in progress.
- `test_select_freeze`: stable routing for requests waiting on `ready`.
- `test_deisolate`: safe routing when isolation is removed.
- `test_saturation`: request limits and counter sizing.
- `test_atop`: atomic requests that require one or two responses.
- `test_flush`: forced-reset recovery and its limits.
- `test_randomized`: seeded traffic with changing isolation and backpressure.

## Recovery contract

`flush_i` is valid only while `isolate_i` is high. A pulse stays active until
`isolate_i` goes low. The nine flush tests prove that the flush:

- clears pending counts and moves safe channels to the isolated state;
- accepts and hides late B/R responses without counter underflow; and
- accepts late W beats without forwarding them, allowing upstream routing to
  finish the burst.

The flush never withdraws AW, AR, or W when `valid` is high and `ready` is low.
That channel waits for acceptance. Therefore:

- an unaccepted AW or AR can prevent isolation;
- an unaccepted W can leave its burst blocked after isolation; and
- a missing response can leave an internal demux ID bucket occupied.

The stuck endpoint must respond, or recovery must reset it and the fabric path
holding the transaction. With `TerminateTransaction=1`, new isolated traffic
still receives `SLVERR` and data `0x1501A7ED`. The legacy `axi_isolate_intf`
wrapper ties `flush_i` low.

## Files

- `tb_axi_isolate.sv`: test wrapper, internal observation points, and routing
  assertions.
- `helpers.py`: AXI drivers, responders, and monitors.
- `test_*.py`: the eight test groups listed above.

Each test emits a `CHK-*` line for the behavior it proves. The testbench stops
at elaboration if its expected request-limit configuration changes.
