// The tt-axi assertion renaming (563cc14f) expects a common_cells fork that
// defines the OCAH_* assertion macros; Bender.yml still resolves the upstream
// pulp-platform common_cells, which does not. Until Bender.yml points at the
// fork, provide the one macro the modules compiled by this bench use, mapped
// to its upstream meaning. Compiled first in the filelist so the define is
// visible to every following file.
`ifndef OCAH_ASSERT_COMPAT_SVH
`define OCAH_ASSERT_COMPAT_SVH

`ifndef OCAH_PULP_ASSUME
`define OCAH_PULP_ASSUME(__name, __prop) \
    __name : assume property (@(posedge clk_i) (__prop)) \
        else $fatal(1, "OCAH_PULP_ASSUME failed");
`endif

`endif
