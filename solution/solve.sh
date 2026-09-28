#!/bin/bash
# Oracle. Puts the reference counter in place and compiles it the way the grader does.
#
# bddcount.d reads the netlist and the ECO lines off the export port (waiting out BUSY answers
# and reconnecting when a connection reaches its command limit), rebuilds the block at every
# checkpoint with the highest revision at or below it winning for each net and output, and counts
# every output exactly on reduced ordered BDDs. The variable order comes from the checkpoint's
# circuit: outputs from the smallest fan-in cone up, each walked depth first, an input placed the
# first time the walk reaches it, so operand bits that meet in one adder or comparator cell sit
# next to each other. The port list's declaration order is never used.
# generate_block.py builds every block and its exact counts.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f /app/bdd/src/*.d
cp "$HERE/bddcount.d" /app/bdd/src/bddcount.d
cd /app/bdd
ldc2 -O2 -of=/app/bdd/bddcount /app/bdd/src/*.d
