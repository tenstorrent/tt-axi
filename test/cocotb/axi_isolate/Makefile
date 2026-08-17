# Block-level Cocotb bench for axi_isolate (TerminateTransaction mode).
#
#   make sim-vcs          # regenerate filelist, compile, run all test modules
#   make sim-vcs WAVES=1  # same, with VCD waves
#   make sim-verilator    # alternative simulator flow
#
# Requires: VCS, a python env with cocotb, and bender (resolves common_cells).

AXI_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null)
ifeq ($(AXI_ROOT),)
  $(error Not inside the axi git repository)
endif
CC_ROOT := $(shell cd $(AXI_ROOT) && bender path common_cells)
ifeq ($(CC_ROOT),)
  $(error bender could not resolve common_cells; run 'bender checkout' in $(AXI_ROOT))
endif

SIM ?= vcs
WAVES ?= 0
TOPLEVEL_LANG = verilog
TOPLEVEL = tb_axi_isolate
# One module per topic; helpers.py is shared. Cocotb runs them in list order.
MODULE = test_sanity,test_drain,test_select_freeze,test_deisolate,test_saturation,test_atop,test_randomized
FILELIST := $(CURDIR)/axi_isolate.f
VERILOG_SOURCES += $(CURDIR)/tb_axi_isolate.sv

COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps
export PYTHONPATH := $(abspath .):$(PYTHONPATH)
export COCOTB_RESOLVE_X ?= ZEROS

ifeq ($(SIM),vcs)
  COMPILE_ARGS += -f $(FILELIST)
  COMPILE_ARGS += -sverilog -timescale=1ns/1ps -full64 +vcs+lic+wait
  COMPILE_ARGS += -lca +rad -kdb +nospecify -debug_access+all
  ifeq ($(WAVES),1)
    SIM_ARGS += +waves
  endif
else ifeq ($(SIM),verilator)
  VERILOG_SOURCES :=
  EXTRA_ARGS += -f $(FILELIST)
  EXTRA_ARGS += --timing --trace --trace-structs
  EXTRA_ARGS += --Wno-UNUSEDSIGNAL --Wno-WIDTH --Wno-PINCONNECTEMPTY --Wno-CASEINCOMPLETE
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: filelist
filelist:
	@rm -f $(FILELIST)
	@printf "%s\n" \
		"+incdir+$(AXI_ROOT)/include" \
		"+incdir+$(CC_ROOT)/include" \
		"$(CURDIR)/ocah_assert_compat.svh" \
		"$(AXI_ROOT)/src/axi_pkg.sv" \
		"$(CC_ROOT)/src/cf_math_pkg.sv" \
		"$(CC_ROOT)/src/spill_register_flushable.sv" \
		"$(CC_ROOT)/src/spill_register.sv" \
		"$(CC_ROOT)/src/fifo_v3.sv" \
		"$(CC_ROOT)/src/delta_counter.sv" \
		"$(CC_ROOT)/src/counter.sv" \
		"$(CC_ROOT)/src/lzc.sv" \
		"$(CC_ROOT)/src/rr_arb_tree.sv" \
		"$(CC_ROOT)/src/stream_register.sv" \
		"$(AXI_ROOT)/src/axi_demux_id_counters.sv" \
		"$(AXI_ROOT)/src/axi_demux_simple.sv" \
		"$(AXI_ROOT)/src/axi_demux.sv" \
		"$(AXI_ROOT)/src/axi_atop_filter.sv" \
		"$(AXI_ROOT)/src/axi_err_slv.sv" \
		"$(AXI_ROOT)/src/axi_isolate.sv" \
		"$(CURDIR)/tb_axi_isolate.sv" \
		> $(FILELIST)

.PHONY: sim-vcs
sim-vcs: filelist
	$(MAKE) SIM=vcs

.PHONY: sim-verilator
sim-verilator: filelist
	$(MAKE) SIM=verilator

.PHONY: clean
clean::
	rm -rf __pycache__ sim_build simv simv.daidir csrc ucli.key vc_hdrs.h
	rm -rf *.vcd *.vpd *.fst *.fsdb $(FILELIST) results.xml sim_vcs.log
