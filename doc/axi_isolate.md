# AXI Isolate Recovery Flush

`axi_isolate` normally waits for accepted transactions to finish before
asserting `isolated_o`. If either connected endpoint is force-reset with a
transaction in progress, the isolate can wait forever.

The `flush_i` input provides an explicit recovery path for that reset
sequence. It is only legal while `isolate_i` is asserted. A pulse opens a
latched flush window that remains active until `isolate_i` deasserts.

During this window, the isolate:

- clears pending-transaction counts and moves each safe channel to the
  isolated state;
- accepts and hides late B/R responses;
- prevents a late response from taking a cleared count below zero; and
- accepts late W beats for an AW accepted before reset, without forwarding
  those beats, so upstream write routing can finish the burst.

The flush never retracts an AW, AR, or W beat already presented to the
downstream interface with `valid=1` and `ready=0`. That channel defers its
clear until the beat is accepted. Consequently, a permanently unresponsive
downstream can leave one of these documented residual states:

- an AW or AR held forever prevents that channel from completing isolation;
- a held W beat does not prevent `isolated_o`, but its burst remains wedged;
- a transaction whose response never arrives can leave its internal demux ID
  bucket occupied until cold reset.

These limits follow from AXI's rule that a presented request cannot be
withdrawn. The stuck endpoint must respond, or the recovery must reset that
endpoint and the fabric path that still holds the transaction.

With `TerminateTransaction=1`, traffic newly routed to the error slave while
isolated still receives `SLVERR` and data marker `0x1501A7ED`. The legacy
`axi_isolate_intf` wrapper has no flush port and ties `flush_i` low.
