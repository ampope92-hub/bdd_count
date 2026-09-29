# bdd sign-off counts

Sign-off wants, for every primary output of a block, the number of assignments of the block's
primary inputs that drive that output to 1. Every declared input counts, including ones no gate
reads (tie-offs stay on the port list), so a block with n inputs has 2^n assignments and a count
is a whole number from 0 to 2^n. Counts go out as plain decimal integers with no sign and no
leading zeros. They do not fit in 64 bits; std.bigint is part of Phobos and is fine to use.

## The export port

The synthesis server exports the block it has just mapped on a TCP port. Its address is in
`SYNTH_HOST` (a host name or an address) and its port in `SYNTH_PORT`. Every line either way is
ASCII and ends in a single LF.

On connect the server sends one line:

    SYNTH <block> PAGES <p> ECO <e> CHECKPOINTS <r1>,<r2>,...,<rk> LIMIT <l>

Commands and answers:

    PAGE <k>     PAGE <k> <n> followed by the n lines of netlist page k, 1 <= k <= p
    ECO <k>      ECO <k> <n> followed by the n lines of ECO page k, 1 <= k <= e
    QUIT         BYE, and the server closes the connection
    other        ERR <reason>

The server is shared with the rest of the flow. It can answer any command with `BUSY <ms>`
instead; that command was not carried out, and the server keeps answering BUSY (with the time
still left) until that many milliseconds have passed. One connection gets answers to at most l
commands (the LIMIT in its greeting), BUSY and ERR answers included. The command after that gets
`BYE limit` and the connection is closed. There is no limit on the number of connections. A page
reads the same on every connection.

## Netlist lines

    INPUT <name>
    OUTPUT <name> <net>
    GATE <net> <type> <in> [<in> [<in>]]

A net is a primary input or the output of exactly one gate. An output is driven by a net, which
can be a primary input. Names are runs of letters, digits, `_`, `[` and `]`. The gate types are
AND, OR, XOR, NAND, NOR and XNOR with two inputs, NOT with one, and MUX with three:
`MUX s d0 d1` is d1 when s is 1 and d0 when s is 0. The netlist has no loops. The mapper writes
the port list, the output list and the cells in its own hash order, and the lines are split into
pages in that order, so line order carries no meaning.

## ECO lines

After synthesis the block collects engineering change orders. Each ECO page line is one change,
tagged with the revision of the order it belongs to:

    REV <r> GATE <net> <type> <in> ...     the gate driving <net> is replaced, or added if new
    REV <r> OUTPUT <name> <net>            output <name> is driven by <net>, added if new
    REV <r> DROP <name>                    output <name> is removed

Revisions are positive integers below 2^63. One order never names the same net or output twice. Revision 0
is the netlist as synthesized. The block at revision R is the netlist with, for every net and
every output name, the line of the highest revision at or below R that names it applied on top;
lines from revisions above R do not apply. The ECO pages list lines in the order the change
database hands them out, which is not revision order. An output that a later line removes and a
still later line adds back exists again from that revision on. At every checkpoint every net
that a present output depends on has a driver and there are no loops.

## Checkpoints and the report

The CHECKPOINTS list in the greeting names the revisions sign-off is run at. It starts with 0 and
ends with the highest revision in the ECO pages. For every checkpoint the counter prints one line
per output present at that revision:

    <revision> <output> <count>

on standard output, one space between fields, in any order, nothing else on standard output.
Anything on standard error is ignored.

## Blocks

The shipped server holds a small block (20 inputs). The flow runs this counter on every block it
signs off, and those stay inside these bounds: at most 236 primary inputs, 3196 gates and 127
outputs at every checkpoint, and ECO lines from at most 14 revisions, 43 lines in all. Blocks
are datapath slices: adders and subtracters feeding result muxes, magnitude and equality
comparators, parity and zero trees, priority encoders.

On the sign-off hosts a run gets 2 cores, 90 seconds of wall time and 4 GiB of address space, and
it cannot start other programs.
