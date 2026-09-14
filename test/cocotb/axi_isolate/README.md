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

The nine flush tests prove that a one-cycle `flush_i` pulse:

- clears a drain left by the reset side;
- hides late B/R responses;
- accepts late W beats without forwarding them;
- keeps counts at zero after late responses; and
- stays active until `isolate_i` goes low.

They also prove that the flush does not withdraw AW, AR, or W beats that have
already been presented to the receiving side. Three tests record the resulting
limits as passing, bounded checks:

- a response that never arrives can leave one internal AXI ID route in use;
- an AW or AR that is never accepted can prevent isolation; and
- an unaccepted W beat can leave its burst blocked even though isolation
  completes.

See `../../../doc/axi_isolate.md` for the recovery contract.

## Files

- `tb_axi_isolate.sv`: test wrapper, internal observation points, and routing
  assertions.
- `helpers.py`: AXI drivers, responders, and monitors.
- `test_*.py`: the eight test groups listed above.

Each test emits a `CHK-*` line for the behavior it proves. The testbench stops
at elaboration if its expected request-limit configuration changes.
