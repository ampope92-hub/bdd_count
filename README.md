# BDD Count: Exact Model Counting of Gate-Level Netlists in D

A chip sign-off flow needs a counter that pulls a mapped block from a synthesis server over TCP and reports, for every primary output, how many input assignments drive it to 1. The server speaks a small line protocol (greeting, paged netlist and ECO fetches, `BUSY` back-off, per-connection command limits and `BYE limit`), described in `/app/bdd/README.md`. For each checkpoint revision the server announces, the agent's program has to apply the ECO (engineering change order) lines that are in effect at that revision and print `<revision> <output> <count>` with exact big-integer counts. It must be written in D, built with ldc2 1.41.0 (`std.numeric` removed from Phobos), and run within 90 seconds and 4 GiB of address space.

## What makes it hard

Model counting is #P-hard, and on arithmetic logic the method you choose decides whether the count finishes at all.

- **The shipped block can't tell methods apart.** It has 20 inputs, so a truth table, a BDD in port-list order and a BDD in name order all return correct counts in under a second.
- **The held-back block can.** The README states the bound the flow must handle: up to 236 inputs and 3188 gates as synthesized (3196 after ECOs), made of adders, comparators and encoders. The held-back block is a 100-bit ALU slice with an 8-bit comparator at exactly that size. Enumeration would need 2^230 passes. A BDD in port-list or sorted-name order puts every bit of one operand ahead of the other, so the adder BDD grows exponentially and runs out of 4 GiB after about 12 seconds. A variable order derived from the circuit, with bits that meet in one cell kept together, holds the block at about 252,000 nodes and finishes in about 10 seconds.
- **ECO semantics.** At each checkpoint, every net and output takes the ECO line of the highest revision at or below that checkpoint. The change database lists lines out of revision order, an output can be dropped and added back, and inputs that no gate reads still double the count. Applying lines in page order, or all at once, gives wrong counts on both blocks.
- **A real network client.** The program must survive `BUSY` waits of up to 1000 ms, connection limits as low as 1 command, and a host name instead of a numeric address.

The skill being tested is choosing a variable order and testing against the stated bound rather than against the block at hand.

## How it's graded

Grading runs in a separate container. It copies only the regular `.d` files from `/app/bdd/src` and compiles them as an unprivileged user, with interpreters closed. It then runs the binary against its own copy of the export server twice:

- **Shipped block** on `127.0.0.3:7521`, limit 4 commands, `BUSY` every third command for 250 ms.
- **Held-back block** (never present in the agent's container) reached through the host name `synth2.fab.internal`, limit 1 command per connection, `BUSY` every fifth command for 1000 ms.

Each run gets a clean environment, a 4 GiB address-space limit set by `prlimit`, a seccomp launcher (`tests/launch.c`) that blocks any further `exec`, and a KILL at 90 seconds. `tests/test_outputs.py` checks that the build succeeded, that every (checkpoint, output) pair is reported exactly once with nothing extra, and that every count equals the exact decimal value. Expected counts come from the generator's own Python BDD package and are cross-checked against a separately written D reference (`tests/golden_ref`). `test_two_references_agree` requires the two derivations to match. Answer keys, blocks and the server sit in root-only directories, and the verifier confirms the build user cannot list them before any candidate code runs.

## Repository layout

- `instruction.md` - the task prompt given to the agent
- `task.toml` - metadata, limits, and the difficulty, solution and verification write-ups
- `environment/` - multi-container setup: `docker-compose.yaml`, agent `Dockerfile`, `Dockerfile.synth` and `synth/` (the export server and shipped block), `app/bdd/` (protocol README and stub `src/app.d`)
- `solution/` - `solve.sh`, reference `bddcount.d`, `generate_block.py` (block and ECO generator)
- `tests/` - verifier `Dockerfile`, `test.sh`, `test_outputs.py`, seccomp `launch.c`, `synth/` server copy, `data/` (shipped and hidden blocks), `golden/` and `golden_ref/` expected reports

## Running it

Requires Docker and Harbor (`uv tool install harbor` or `pip install harbor`). From the repo root:

```
harbor run -p . -a oracle
```

runs the reference solution through the verifier (should score reward 1). To try an agent:

```
harbor run -p . -a <agent> -m <model>
```

for example `-a claude-code`.

---

Task design, data generators, reference solution and verifier by Audrey Pope.
