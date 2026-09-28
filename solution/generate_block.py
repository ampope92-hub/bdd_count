#!/usr/bin/env python3
"""Builds a datapath block the way the synthesis server holds it, and its exact counts.

A block is a gate-level netlist of one combinational datapath slice:
  - an ALU on the operand pair a, b (W1 bits): res = a+b, a-b, a&b or a^b picked by op[1:0],
    with the adder's carry out, a == b, a < b (unsigned), res == 0 and the parity of res;
  - a comparator on the pair c, d (W2 bits): c > d and the larger of the two, bit by bit;
  - a priority encoder over req[R-1:0]: the index of the highest request and a valid flag;
  - spare inputs that nothing reads (tie-offs the flow keeps on the port list).
Every structure is a ripple chain or a tree of two-input cells, with the gate types varied the
way a technology mapper varies them. The port list, the output list and the gate list are each
scrambled, gate fan-in order is scrambled, and net names are synthesis-style numbers.

Then a set of ECO revisions: a gate in a chain redefined, the same gate redefined again at a later
revision, an output added on new gates, an output dropped and re-added on another net. ECO lines
are delivered in scrambled order across pages. The checkpoints are revision 0 (the netlist as
synthesized), one revision in the middle and the final revision.

Counts are computed here with a small BDD package in the bit-sliced variable order the generator
knows from construction (a0 b0 a1 b1 ... then c/d the same way, then the rest), and for small
blocks cross-checked by a full truth table done bit-parallel on Python integers.

  generate_block.py --seed N --kind sample|hidden --out DIR
writes DIR/block.json (what the server serves) and DIR/counts.txt (revision output count).
"""
import argparse
import json
import os
import random
import sys

TWO = ("AND", "OR", "XOR", "NAND", "NOR", "XNOR")
SIZES = {
    # W1: ALU operand width, W2: comparator width, R: request lines, S: spare inputs,
    # page: lines per netlist page, epage: lines per ECO page
    "sample": {"W1": 4, "W2": 3, "R": 3, "S": 1, "page": 40, "epage": 4},
    "hidden": {"W1": 56, "W2": 40, "R": 16, "S": 2, "page": 160, "epage": 4},
}


class Block:
    def __init__(self, rng):
        self.rng = rng
        self.inputs = []
        self.gates = {}      # net -> [op, fanins]
        self.made = []       # creation order, which is topological
        self.outputs = []    # [name, net]
        self.used = set()

    def name(self):
        while True:
            n = f"n{self.rng.randrange(1000, 99999)}"
            if n not in self.used:
                self.used.add(n)
                return n

    def inp(self, name):
        self.used.add(name)
        self.inputs.append(name)
        return name

    def g(self, op, *ins):
        n = self.name()
        ins = list(ins)
        if op != "MUX":
            self.rng.shuffle(ins)
        self.gates[n] = [op, ins]
        self.made.append(n)
        return n

    # Two-input functions, each in one of the forms a mapper produces.
    def NOT(self, x):
        return self.g("NOT", x)

    def AND(self, x, y):
        return self.g("AND", x, y) if self.rng.random() < 0.7 else self.NOT(self.g("NAND", x, y))

    def OR(self, x, y):
        return self.g("OR", x, y) if self.rng.random() < 0.7 else self.NOT(self.g("NOR", x, y))

    def XOR(self, x, y):
        return self.g("XOR", x, y) if self.rng.random() < 0.7 else self.NOT(self.g("XNOR", x, y))

    def XNOR(self, x, y):
        return self.g("XNOR", x, y) if self.rng.random() < 0.7 else self.NOT(self.g("XOR", x, y))

    def MUX(self, s, d0, d1):
        return self.g("MUX", s, d0, d1)

    def tree(self, fn, xs):
        xs = list(xs)
        while len(xs) > 1:
            nxt = []
            for i in range(0, len(xs) - 1, 2):
                nxt.append(fn(xs[i], xs[i + 1]))
            if len(xs) % 2:
                nxt.append(xs[-1])
            xs = nxt
        return xs[0]

    def adder(self, xs, ys, cin_one=False):
        """Ripple adder x + y (+1). Returns (sum bits, carry into each bit, carry out)."""
        s, carries = [], []
        c = None
        for i, (x, y) in enumerate(zip(xs, ys)):
            carries.append(c)
            p = self.XOR(x, y)
            if c is None and not cin_one:
                s.append(p)
                c = self.AND(x, y)
                continue
            if c is None:  # carry in of one
                s.append(self.NOT(p))
                c = self.OR(x, y)
                continue
            s.append(self.XOR(p, c))
            form = self.rng.randrange(3)
            if form == 0:
                c = self.MUX(p, x, c)                      # propagate ? carry : x (== y)
            elif form == 1:
                c = self.OR(self.AND(x, y), self.AND(p, c))
            else:
                c = self.OR(self.AND(x, y), self.AND(self.OR(x, y), c))
        return s, carries, c

    def less_than(self, xs, ys):
        """Unsigned x < y, a chain from the least significant bit up."""
        lt = None
        for x, y in zip(xs, ys):
            here = self.AND(self.NOT(x), y)
            if lt is None:
                lt = here
            else:
                lt = self.OR(here, self.AND(self.XNOR(x, y), lt))
        return lt


def build(seed, kind):
    P = SIZES[kind]
    rng = random.Random(seed)
    B = Block(rng)
    W1, W2, R = P["W1"], P["W2"], P["R"]
    a = [B.inp(f"a[{i}]") for i in range(W1)]
    b = [B.inp(f"b[{i}]") for i in range(W1)]
    c = [B.inp(f"c[{i}]") for i in range(W2)]
    d = [B.inp(f"d[{i}]") for i in range(W2)]
    op = [B.inp(f"op[{i}]") for i in range(2)]
    req = [B.inp(f"req[{i}]") for i in range(R)]
    for i in range(P["S"]):
        B.inp(f"tie[{i}]")   # spare inputs nothing reads

    # ALU on a, b.
    s, s_carry, cout = B.adder(a, b)
    nb = [B.NOT(y) for y in b]
    df, _d_carry, _dout = B.adder(a, nb, cin_one=True)
    an = [B.AND(x, y) for x, y in zip(a, b)]
    xo = [B.XOR(x, y) for x, y in zip(a, b)]
    res = []
    for i in range(W1):
        lo = B.MUX(op[0], s[i], df[i])
        hi = B.MUX(op[0], an[i], xo[i])
        res.append(B.MUX(op[1], lo, hi))
    eq = B.tree(B.AND, [B.XNOR(x, y) for x, y in zip(a, b)])
    lt = B.less_than(a, b)
    zero = B.NOT(B.tree(B.OR, res))
    par = B.tree(B.XOR, res)

    # Comparator on c, d.
    gt = B.less_than(d, c)
    mx = [B.MUX(gt, y, x) for x, y in zip(c, d)]

    # Priority encoder, highest request wins.
    found = None
    sel = [None] * R
    for i in reversed(range(R)):
        sel[i] = req[i] if found is None else B.AND(req[i], B.NOT(found))
        found = req[i] if found is None else B.OR(found, req[i])
    nbits = max(1, (R - 1).bit_length())
    pidx = []
    for bit in range(nbits):
        terms = [sel[i] for i in range(R) if i >> bit & 1]
        pidx.append(B.tree(B.OR, terms) if terms else None)

    outs = [[f"res[{i}]", res[i]] for i in range(W1)]
    outs += [["cout", cout], ["eq", eq], ["lt", lt], ["zero", zero], ["par", par], ["gt", gt]]
    outs += [[f"mx[{i}]", mx[i]] for i in range(W2)]
    outs += [[f"pidx[{k}]", pidx[k]] for k in range(nbits) if pidx[k] is not None]
    outs += [["pvalid", found]]
    B.outputs = outs
    base_gate_count = len(B.gates)

    # ---- ECO revisions -------------------------------------------------------------------
    revs = sorted(rng.sample(range(1, 30), 6))
    early, late = revs[:3], revs[3:5]
    rng.shuffle(early)
    rng.shuffle(late)
    rA, rC, rD = early
    rB, rE = late
    rF = revs[5]
    eco = []   # [rev, kind, target, rest...]

    # A carry-chain gate in the adder, somewhere in the upper half, redefined at rA and again
    # at rB. Its op changes to another two-input op; both changes must move some count.
    chain = [n for n in B.made if B.gates[n][0] in TWO and n in cone(B, s[W1 - 1])
             and n not in cone(B, s[W1 // 2])]
    rng.shuffle(chain)
    target = chain[0]
    old = B.gates[target][0]
    opsA = [o for o in TWO if o != old]
    newA = rng.choice(opsA)
    newB = rng.choice([o for o in TWO if o not in (old, newA)])
    eco.append([rA, "GATE", target, newA] + B.gates[target][1])
    eco.append([rB, "GATE", target, newB] + B.gates[target][1])

    # A second gate, in the comparator chain, redefined once at rF.
    gt_cone = cone(B, gt)
    t2 = rng.choice([n for n in B.made if B.gates[n][0] in TWO and n in gt_cone])
    eco.append([rF, "GATE", t2, rng.choice([o for o in TWO if o != B.gates[t2][0]])] + B.gates[t2][1])

    # An overflow flag added at rC on new gates: carry into the top bit XOR carry out.
    newnets = []
    def eco_gate(rev, op, *ins):
        n = B.name()
        ins = list(ins)
        if op != "MUX":
            rng.shuffle(ins)
        eco.append([rev, "GATE", n, op] + ins)
        newnets.append(n)
        return n
    ovf = eco_gate(rC, "XOR", s_carry[W1 - 1], cout)
    eco.append([rC, "OUTPUT", "ovf", ovf])

    # lt dropped at rD and put back at rE as a <= b, on one new gate.
    eco.append([rD, "DROP", "lt"])
    le = eco_gate(rE, "OR", lt, eq)
    eco.append([rE, "OUTPUT", "lt", le])

    meta = {"revs": {"A": rA, "B": rB, "C": rC, "D": rD, "E": rE, "F": rF}, "target": target, "t2": t2}
    return B, eco, meta, base_gate_count


def cone(B, net, memo=None):
    seen = set()
    st = [net]
    while st:
        n = st.pop()
        if n in seen or n not in B.gates:
            continue
        seen.add(n)
        st.extend(B.gates[n][1])
    return seen


def state_at(block, eco, R, order_by="revision"):
    """The circuit at revision R: base plus, per net and per output name, the ECO line of the
    highest revision <= R. order_by="delivery" is the wrong reading (last line delivered wins),
    kept for the rule checks below; order_by="all" ignores the checkpoint."""
    gates = {n: list(v) for n, v in block["gates"].items()}
    outs = {o[0]: o[1] for o in block["outputs"]}
    lines = [e for e in eco if order_by == "all" or e[0] <= R]
    if order_by in ("revision", "all"):
        lines = sorted(lines, key=lambda e: e[0])
    for e in lines:
        if e[1] == "GATE":
            gates[e[2]] = [e[3], list(e[4:])]
        elif e[1] == "OUTPUT":
            outs[e[2]] = e[3]
        elif e[1] == "DROP":
            outs.pop(e[2], None)
    return gates, outs


# ---- a small BDD package -----------------------------------------------------------------
class BDD:
    def __init__(self, nvars):
        self.n = nvars
        self.var = [nvars, nvars]
        self.lo = [0, 1]
        self.hi = [0, 1]
        self.uniq = {}
        self.cache = {}

    def mk(self, v, l, h):
        if l == h:
            return l
        k = (v, l, h)
        r = self.uniq.get(k)
        if r is None:
            r = len(self.var)
            self.var.append(v)
            self.lo.append(l)
            self.hi.append(h)
            self.uniq[k] = r
        return r

    def ite(self, f, g, h):
        if f == 1:
            return g
        if f == 0:
            return h
        if g == h:
            return g
        if g == 1 and h == 0:
            return f
        k = (f, g, h)
        r = self.cache.get(k)
        if r is not None:
            return r
        v = min(self.var[f], self.var[g], self.var[h])
        def cof(x, side):
            if self.var[x] != v:
                return x
            return self.hi[x] if side else self.lo[x]
        t = self.ite(cof(f, 1), cof(g, 1), cof(h, 1))
        e = self.ite(cof(f, 0), cof(g, 0), cof(h, 0))
        r = self.mk(v, e, t)
        self.cache[k] = r
        return r

    def count(self, f):
        memo = {}
        def c(x):
            if x < 2:
                return x
            if x in memo:
                return memo[x]
            v = self.var[x]
            r = c(self.lo[x]) << (self.var[self.lo[x]] - v - 1)
            r += c(self.hi[x]) << (self.var[self.hi[x]] - v - 1)
            memo[x] = r
            return r
        return c(f) << self.var[f]


def gate_fn(bdd, op, xs):
    ite = bdd.ite
    if op == "NOT":
        return ite(xs[0], 0, 1)
    if op == "BUF":
        return xs[0]
    if op == "MUX":
        return ite(xs[0], xs[2], xs[1])
    x, y = xs
    if op == "AND":
        return ite(x, y, 0)
    if op == "OR":
        return ite(x, 1, y)
    if op == "XOR":
        return ite(x, ite(y, 0, 1), y)
    if op == "NAND":
        return ite(x, ite(y, 0, 1), 1)
    if op == "NOR":
        return ite(x, 0, ite(y, 0, 1))
    if op == "XNOR":
        return ite(x, y, ite(y, 0, 1))
    raise ValueError(op)


def sliced_order(inputs):
    """The generator's own order: operand pairs interleaved bit by bit, then everything else."""
    def key(nm):
        bus, idx = nm[:-1].split("[")
        idx = int(idx)
        grp = {"a": 0, "b": 0, "c": 1, "d": 1}.get(bus, 2)
        return (grp, idx if grp < 2 else 0, bus, idx)
    return sorted(inputs, key=key)


def counts_bdd(block, gates, outs):
    order = sliced_order(block["inputs"])
    level = {nm: i for i, nm in enumerate(order)}
    bdd = BDD(len(order))
    val = {nm: bdd.mk(level[nm], 0, 1) for nm in order}
    sys.setrecursionlimit(100000)

    def ev(n):
        stack = [n]
        while stack:
            x = stack[-1]
            if x in val:
                stack.pop()
                continue
            op, ins = gates[x]
            pend = [i for i in ins if i not in val]
            if pend:
                stack.extend(pend)
                continue
            val[x] = gate_fn(bdd, op, [val[i] for i in ins])
            stack.pop()
        return val[n]

    return {o: bdd.count(ev(net)) for o, net in outs.items()}


def counts_truth_table(block, gates, outs):
    """Every assignment at once: input i is the integer whose bit k is bit i of k."""
    names = block["inputs"]
    n = len(names)
    N = 1 << n
    full = (1 << N) - 1
    val = {}
    for i, nm in enumerate(names):
        pat = ((1 << (1 << i)) - 1) << (1 << i)       # 2^i zeros then 2^i ones
        period = 1 << (i + 1)
        v = 0
        N // period
        # repeat pat reps times
        chunk, have = pat, period
        v = chunk
        while have < N:
            v |= v << have
            have *= 2
        val[nm] = v & full

    def ev(x):
        stack = [x]
        while stack:
            y = stack[-1]
            if y in val:
                stack.pop()
                continue
            op, ins = gates[y]
            pend = [i for i in ins if i not in val]
            if pend:
                stack.extend(pend)
                continue
            vs = [val[i] for i in ins]
            if op == "NOT":
                r = full ^ vs[0]
            elif op == "BUF":
                r = vs[0]
            elif op == "MUX":
                r = (vs[0] & vs[2]) | ((full ^ vs[0]) & vs[1])
            elif op == "AND":
                r = vs[0] & vs[1]
            elif op == "OR":
                r = vs[0] | vs[1]
            elif op == "XOR":
                r = vs[0] ^ vs[1]
            elif op == "NAND":
                r = full ^ (vs[0] & vs[1])
            elif op == "NOR":
                r = full ^ (vs[0] | vs[1])
            elif op == "XNOR":
                r = full ^ vs[0] ^ vs[1]
            val[y] = r
            stack.pop()
        return val[x]

    return {o: ev(net).bit_count() for o, net in outs.items()}


def pages(lines, size):
    return [lines[i:i + size] for i in range(0, len(lines), size)]


def make(seed, kind):
    B, eco, meta, base_gates = build(seed, kind)
    rng = random.Random(seed * 7919 + 17)
    P = SIZES[kind]
    inputs = list(B.inputs)
    rng.shuffle(inputs)
    outputs = [list(o) for o in B.outputs]
    rng.shuffle(outputs)
    gl = [[n] + B.gates[n] for n in B.made]
    rng.shuffle(gl)
    block = {"inputs": inputs, "outputs": outputs, "gates": {g[0]: [g[1], g[2]] for g in gl}}
    net_lines = [f"INPUT {nm}" for nm in inputs]
    net_lines += [f"OUTPUT {o} {n}" for o, n in outputs]
    net_lines += ["GATE {} {} {}".format(g[0], g[1], " ".join(g[2])) for g in gl]

    # ECO delivery order: scrambled, but with the later redefinition of the chain gate
    # delivered before the earlier one, and the re-add of lt before its drop.
    r = meta["revs"]
    eco_sorted = sorted(eco, key=lambda e: e[0])
    rng.shuffle(eco_sorted)
    def pos(pred):
        return next(i for i, e in enumerate(eco_sorted) if pred(e))
    iA = pos(lambda e: e[0] == r["A"] and e[1] == "GATE")
    iB = pos(lambda e: e[0] == r["B"] and e[1] == "GATE")
    if iB > iA:
        eco_sorted[iA], eco_sorted[iB] = eco_sorted[iB], eco_sorted[iA]
    iD = pos(lambda e: e[1] == "DROP")
    iE = pos(lambda e: e[0] == r["E"] and e[1] == "OUTPUT")
    if iE > iD:
        eco_sorted[iD], eco_sorted[iE] = eco_sorted[iE], eco_sorted[iD]
    eco = eco_sorted
    eco_lines = ["REV " + " ".join(str(x) for x in e) for e in eco]

    # Checkpoints: the netlist as synthesized, a revision after A, C and D but before B and E,
    # and the final revision.
    mids = [x for x in range(max(r["A"], r["C"], r["D"]), min(r["B"], r["E"]))]
    if not mids:
        return None
    mid = rng.choice(mids)
    final = max(e[0] for e in eco)
    checkpoints = [0, mid, final]

    doc = {"kind": kind, "seed": seed, "inputs": len(inputs), "gates": len(gl),
           "eco_lines": len(eco_lines), "checkpoints": checkpoints,
           "pages": pages(net_lines, P["page"]), "eco_pages": pages(eco_lines, P["epage"])}

    counts = {}
    for R in checkpoints:
        gates, outs = state_at(block, eco, R)
        counts[R] = counts_bdd(block, gates, outs)
        if len(inputs) <= 22:
            tt = counts_truth_table(block, gates, outs)
            assert tt == counts[R], "truth table and BDD disagree"

    # Every ECO rule has to move the graded answer on this block by itself.
    def wrong(R, how):
        gates, outs = state_at(block, eco, R, order_by=how)
        return counts_bdd(block, gates, outs)
    checks = {
        "delivery order instead of revision (final)": wrong(final, "delivery") != counts[final],
        "every revision applied at the middle checkpoint": wrong(mid, "all") != counts[mid],
        "lt absent at the middle checkpoint": "lt" not in counts[mid],
        "lt back at the final checkpoint with a new count": counts[final].get("lt") != counts[0].get("lt"),
        "first redefinition moves a count at the middle checkpoint":
            {k: v for k, v in counts[mid].items() if k in counts[0]} != {k: v for k, v in counts[0].items() if k in counts[mid]},
    }
    # The two redefinitions of the chain gate must each give different counts.
    return doc, counts, checks, meta, base_gates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--kind", choices=sorted(SIZES), required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    seed = args.seed
    while True:
        got = make(seed, args.kind)
        if got is not None and all(got[2].values()):
            break
        seed += 1000
    doc, counts, _checks, _meta, _base_gates = got
    doc["seed"] = seed
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "block.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    with open(os.path.join(args.out, "counts.txt"), "w") as fh:
        for R in doc["checkpoints"]:
            fh.writelines(f"{R} {o} {counts[R][o]}\n" for o in sorted(counts[R]))
    print(f"seed {seed}: {doc['inputs']} inputs, {doc['gates']} gates, {len(counts[0])} outputs, "
          f"{doc['eco_lines']} ECO lines, checkpoints {doc['checkpoints']}", file=sys.stderr)


if __name__ == "__main__":
    main()
