# AXI Isolate Recovery Flush

`axi_isolate` normally waits for all accepted transactions to complete before
asserting `isolated_o`. If the connected master or slave is force-reset with
transactions in flight, those completions may never arrive and the normal
drain can remain wedged.

The `flush_i` input provides an explicit recovery path for that reset
sequence. It is only legal while `isolate_i` is asserted. A pulse opens a
latched flush window that remains active until `isolate_i` deasserts.

During the window, the isolate:

- clears pending-transaction bookkeeping and forces each channel toward the
  isolated state;
- masks B/R responses from the reset master while accepting stale downstream
  responses internally;
- uses saturating response-counter updates so a late response cannot
  underflow a cleared counter; and
- accepts W beats that arrive for a burst whose AW was accepted before the
  reset, allowing upstream W routing to unwind without forwarding those beats.

The flush never retracts an AW, AR, or W beat already presented to the
downstream interface with `valid=1` and `ready=0`. That channel defers its
clear until the beat is accepted. Consequently, a permanently unresponsive
downstream can leave one of these documented residual states:

- an AW or AR held forever prevents that channel from completing isolation;
- a held W beat does not prevent `isolated_o`, but its burst remains wedged;
- a transaction whose response never arrives can leave its internal demux ID
  bucket occupied until cold reset.

These residuals follow from AXI's no-retraction requirement. The downstream
endpoint involved in a torn transaction must itself be reset as part of the
recovery sequence.

With `TerminateTransaction=1`, traffic newly routed to the error slave while
isolated still receives `SLVERR` and data marker `0x1501A7ED`. The legacy
`axi_isolate_intf` wrapper has no flush port and ties `flush_i` low.
