// Copyright 2026 Tenstorrent Inc.
/**
 * @file tb_axi_isolate.sv
 * @brief Block-level harness that flattens the full-AXI isolate's ports for
 * Cocotb and mirrors the demux-select registers and inner FSM state for
 * checking.
 */

`include "axi/typedef.svh"

/** @brief Exposes a simulator-friendly isolate harness for Cocotb tests. */
module tb_axi_isolate #(
    parameter int unsigned NUM_PENDING = 4
);

    localparam int unsigned ADDR_WIDTH = 32;
    localparam int unsigned DATA_WIDTH = 32;
    localparam int unsigned ID_WIDTH   = 4;
    localparam int unsigned USER_WIDTH = 1;

    typedef logic [ADDR_WIDTH-1:0]   addr_t;
    typedef logic [DATA_WIDTH-1:0]   data_t;
    typedef logic [DATA_WIDTH/8-1:0] strb_t;
    typedef logic [ID_WIDTH-1:0]     id_t;
    typedef logic [USER_WIDTH-1:0]   user_t;

    `AXI_TYPEDEF_ALL(axi, addr_t, id_t, data_t, strb_t, user_t)

    logic clk_i;
    logic rst_ni;
    logic isolate_i;
    logic isolated_o;

    // Slave port: driven by the Cocotb master.
    logic        slv_aw_valid_i;
    logic [3:0]  slv_aw_id_i;
    logic [31:0] slv_aw_addr_i;
    logic [7:0]  slv_aw_len_i;
    logic [2:0]  slv_aw_size_i;
    logic [1:0]  slv_aw_burst_i;
    logic [5:0]  slv_aw_atop_i;
    logic        slv_aw_ready_o;
    logic        slv_w_valid_i;
    logic [31:0] slv_w_data_i;
    logic [3:0]  slv_w_strb_i;
    logic        slv_w_last_i;
    logic        slv_w_ready_o;
    logic        slv_b_ready_i;
    logic        slv_b_valid_o;
    logic [3:0]  slv_b_id_o;
    logic [1:0]  slv_b_resp_o;
    logic        slv_ar_valid_i;
    logic [3:0]  slv_ar_id_i;
    logic [31:0] slv_ar_addr_i;
    logic [7:0]  slv_ar_len_i;
    logic [2:0]  slv_ar_size_i;
    logic [1:0]  slv_ar_burst_i;
    logic        slv_ar_ready_o;
    logic        slv_r_ready_i;
    logic        slv_r_valid_o;
    logic [3:0]  slv_r_id_o;
    logic [31:0] slv_r_data_o;
    logic [1:0]  slv_r_resp_o;
    logic        slv_r_last_o;

    // Master port: the Cocotb downstream slave model responds here.
    logic        mst_aw_valid_o;
    logic [3:0]  mst_aw_id_o;
    logic [31:0] mst_aw_addr_o;
    logic [7:0]  mst_aw_len_o;
    logic [5:0]  mst_aw_atop_o;
    logic        mst_aw_ready_i;
    logic        mst_w_valid_o;
    logic [31:0] mst_w_data_o;
    logic        mst_w_last_o;
    logic        mst_w_ready_i;
    logic        mst_b_ready_o;
    logic        mst_b_valid_i;
    logic [3:0]  mst_b_id_i;
    logic [1:0]  mst_b_resp_i;
    logic        mst_ar_valid_o;
    logic [3:0]  mst_ar_id_o;
    logic [31:0] mst_ar_addr_o;
    logic [7:0]  mst_ar_len_o;
    logic        mst_ar_ready_i;
    logic        mst_r_ready_o;
    logic        mst_r_valid_i;
    logic [3:0]  mst_r_id_i;
    logic [31:0] mst_r_data_i;
    logic [1:0]  mst_r_resp_i;
    logic        mst_r_last_i;

    axi_req_t  slv_req, mst_req;
    axi_resp_t slv_resp, mst_resp;

    always_comb begin
        slv_req          = '0;
        slv_req.aw.id    = slv_aw_id_i;
        slv_req.aw.addr  = slv_aw_addr_i;
        slv_req.aw.len   = slv_aw_len_i;
        slv_req.aw.size  = slv_aw_size_i;
        slv_req.aw.burst = slv_aw_burst_i;
        slv_req.aw.atop  = slv_aw_atop_i;
        slv_req.aw_valid = slv_aw_valid_i;
        slv_req.w.data   = slv_w_data_i;
        slv_req.w.strb   = slv_w_strb_i;
        slv_req.w.last   = slv_w_last_i;
        slv_req.w_valid  = slv_w_valid_i;
        slv_req.b_ready  = slv_b_ready_i;
        slv_req.ar.id    = slv_ar_id_i;
        slv_req.ar.addr  = slv_ar_addr_i;
        slv_req.ar.len   = slv_ar_len_i;
        slv_req.ar.size  = slv_ar_size_i;
        slv_req.ar.burst = slv_ar_burst_i;
        slv_req.ar_valid = slv_ar_valid_i;
        slv_req.r_ready  = slv_r_ready_i;
    end

    assign slv_aw_ready_o = slv_resp.aw_ready;
    assign slv_w_ready_o  = slv_resp.w_ready;
    assign slv_b_valid_o  = slv_resp.b_valid;
    assign slv_b_id_o     = slv_resp.b.id;
    assign slv_b_resp_o   = slv_resp.b.resp;
    assign slv_ar_ready_o = slv_resp.ar_ready;
    assign slv_r_valid_o  = slv_resp.r_valid;
    assign slv_r_id_o     = slv_resp.r.id;
    assign slv_r_data_o   = slv_resp.r.data;
    assign slv_r_resp_o   = slv_resp.r.resp;
    assign slv_r_last_o   = slv_resp.r.last;

    assign mst_aw_valid_o = mst_req.aw_valid;
    assign mst_aw_id_o    = mst_req.aw.id;
    assign mst_aw_addr_o  = mst_req.aw.addr;
    assign mst_aw_len_o   = mst_req.aw.len;
    assign mst_aw_atop_o  = mst_req.aw.atop;
    assign mst_w_valid_o  = mst_req.w_valid;
    assign mst_w_data_o   = mst_req.w.data;
    assign mst_w_last_o   = mst_req.w.last;
    assign mst_b_ready_o  = mst_req.b_ready;
    assign mst_ar_valid_o = mst_req.ar_valid;
    assign mst_ar_id_o    = mst_req.ar.id;
    assign mst_ar_addr_o  = mst_req.ar.addr;
    assign mst_ar_len_o   = mst_req.ar.len;
    assign mst_r_ready_o  = mst_req.r_ready;

    always_comb begin
        mst_resp          = '0;
        mst_resp.aw_ready = mst_aw_ready_i;
        mst_resp.w_ready  = mst_w_ready_i;
        mst_resp.b_valid  = mst_b_valid_i;
        mst_resp.b.id     = mst_b_id_i;
        mst_resp.b.resp   = mst_b_resp_i;
        mst_resp.ar_ready = mst_ar_ready_i;
        mst_resp.r_valid  = mst_r_valid_i;
        mst_resp.r.id     = mst_r_id_i;
        mst_resp.r.data   = mst_r_data_i;
        mst_resp.r.resp   = mst_r_resp_i;
        mst_resp.r.last   = mst_r_last_i;
    end

    axi_isolate #(
        .NumPending           ( NUM_PENDING ),
        .TerminateTransaction ( 1'b1        ),
        .AtopSupport          ( 1'b1        ),
        .AxiAddrWidth         ( ADDR_WIDTH  ),
        .AxiDataWidth         ( DATA_WIDTH  ),
        .AxiIdWidth           ( ID_WIDTH    ),
        .AxiUserWidth         ( USER_WIDTH  ),
        .axi_req_t            ( axi_req_t   ),
        .axi_resp_t           ( axi_resp_t  )
    ) u_dut (
        .clk_i,
        .rst_ni,
        .slv_req_i  ( slv_req  ),
        .slv_resp_o ( slv_resp ),
        .mst_req_o  ( mst_req  ),
        .mst_resp_i ( mst_resp ),
        .isolate_i,
        .isolated_o
    );

    // Observation mirrors for Cocotb: demux select registers and inner
    // FSM/counter state for failure diagnostics.
    logic       obs_sel_aw;
    logic       obs_sel_ar;
    logic [1:0] obs_state_aw;
    logic [1:0] obs_state_ar;
    logic [3:0] obs_pending_aw;
    logic [3:0] obs_pending_w;
    logic [3:0] obs_pending_ar;

    assign obs_sel_aw     = u_dut.g_terminate.sel_aw_q;
    assign obs_sel_ar     = u_dut.g_terminate.sel_ar_q;
    assign obs_state_aw   = u_dut.i_axi_isolate.state_aw_q;
    assign obs_state_ar   = u_dut.i_axi_isolate.state_ar_q;
    assign obs_pending_aw = u_dut.i_axi_isolate.pending_aw_q;
    assign obs_pending_w  = u_dut.i_axi_isolate.pending_w_q;
    assign obs_pending_ar = u_dut.i_axi_isolate.pending_ar_q;

    // The demux-INTERNAL presented-and-unaccepted condition: the exact
    // antecedent of the demux's own select-stability requirement.  This is
    // deliberately NOT the host-level `slv_*_valid_i && !slv_*_ready_o`: the
    // demux may stall a host request at its ID/W-ordering/counter gates
    // without presenting it, and in that window the select is free to move
    // (test_saturation provokes exactly that, so a host-level antecedent
    // here would be a false failure).
    logic obs_demux_aw_unaccepted;
    logic obs_demux_ar_unaccepted;

    assign obs_demux_aw_unaccepted =
        (u_dut.demux_req[0].aw_valid | u_dut.demux_req[1].aw_valid) & ~slv_aw_ready_o;
    assign obs_demux_ar_unaccepted =
        (u_dut.demux_req[0].ar_valid | u_dut.demux_req[1].ar_valid) & ~slv_ar_ready_o;

`ifndef VERILATOR
    // The demux select must hold its value while a request is presented at a
    // master port and not yet accepted; it may only take a new value after
    // the handshake.  Mirrors the demux's internal `slv_*_select_stable`.
    sel_aw_stable_a: assert property (@(posedge clk_i) disable iff (!rst_ni)
        obs_demux_aw_unaccepted |=> $stable(obs_sel_aw)) else
        $fatal(1, "sel_aw_q changed while an AW was presented and not accepted");
    sel_ar_stable_a: assert property (@(posedge clk_i) disable iff (!rst_ni)
        obs_demux_ar_unaccepted |=> $stable(obs_sel_ar)) else
        $fatal(1, "sel_ar_q changed while an AR was presented and not accepted");
`endif

    // Guard the InnerPending derivation: for NumPending = 4 the demux admits
    // at most 5 outstanding AWs/ARs (two ID buckets, gate closed once one
    // holds 3), so the inner's threshold must sit above that: 2*3 + 1 = 7.
    // The numeric checks in test_saturation assume these exact values.
    initial begin
        if (NUM_PENDING != 32'd4) begin
            $fatal(1, "tests assume NUM_PENDING = 4, got %0d", NUM_PENDING);
        end
        if (u_dut.InnerPending != 32'd7) begin
            $fatal(1, "InnerPending derivation changed: expected 7 for NumPending = 4, got %0d",
                   u_dut.InnerPending);
        end
    end

    initial begin
        clk_i = 1'b0;
        rst_ni = 1'b0;
        isolate_i = 1'b1;

        slv_aw_valid_i = 1'b0;
        slv_aw_id_i = '0;
        slv_aw_addr_i = '0;
        slv_aw_len_i = '0;
        slv_aw_size_i = '0;
        slv_aw_burst_i = '0;
        slv_aw_atop_i = '0;
        slv_w_valid_i = 1'b0;
        slv_w_data_i = '0;
        slv_w_strb_i = '0;
        slv_w_last_i = 1'b0;
        slv_b_ready_i = 1'b0;
        slv_ar_valid_i = 1'b0;
        slv_ar_id_i = '0;
        slv_ar_addr_i = '0;
        slv_ar_len_i = '0;
        slv_ar_size_i = '0;
        slv_ar_burst_i = '0;
        slv_r_ready_i = 1'b0;

        mst_aw_ready_i = 1'b0;
        mst_w_ready_i = 1'b0;
        mst_b_valid_i = 1'b0;
        mst_b_id_i = '0;
        mst_b_resp_i = '0;
        mst_ar_ready_i = 1'b0;
        mst_r_valid_i = 1'b0;
        mst_r_id_i = '0;
        mst_r_data_i = '0;
        mst_r_resp_i = '0;
        mst_r_last_i = 1'b0;
    end

    initial begin
        forever #5 clk_i = ~clk_i;
    end

`ifndef VERILATOR
    initial begin
        if ($test$plusargs("waves")) begin
            $dumpfile("tb_axi_isolate.vcd");
            $dumpvars(0, tb_axi_isolate);
        end
    end
`endif

endmodule : tb_axi_isolate
