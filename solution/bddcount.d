// Sign-off counter: for every output of the block the synthesis server exports, the exact number
// of primary-input assignments that drive it to 1, at every checkpoint revision.
//
// The netlist and the ECO lines come off the server's export port (SYNTH_HOST, SYNTH_PORT). For
// each checkpoint the circuit is rebuilt (base netlist, then per net and per output name the ECO
// line with the highest revision at or below the checkpoint) and every output is turned into a
// reduced ordered BDD in one shared manager. The variable order is read off that checkpoint's
// circuit: outputs are walked from the smallest fan-in cone to the largest, each depth first
// through its gates' inputs in the order the netlist lists them, and a primary input takes the
// next level the first time the walk reaches it. Operand bits that meet in the same adder or
// comparator cell are therefore reached one after the other, which keeps ripple structures
// linear in size; the declaration order of the port list is never used. Counts are exact
// (std.bigint): a node's count is its children's counts scaled by two to the number of levels
// they skip, and the root's count is scaled by the levels above it.
import std.algorithm : canFind, filter, min, sort, SwapStrategy;
import std.array : array, split;
import std.bigint : BigInt;
import std.conv : to;
import std.process : environment;
import std.socket;
import std.stdio : stderr, stdout, writeln;
import std.string : indexOf, strip;
import core.thread : Thread;
import core.time : msecs;

enum Op : ubyte { AND, OR, XOR, NAND, NOR, XNOR, NOT, BUF, MUX }

Op opOf(string s) {
    switch (s) {
        case "AND": return Op.AND;
        case "OR": return Op.OR;
        case "XOR": return Op.XOR;
        case "NAND": return Op.NAND;
        case "NOR": return Op.NOR;
        case "XNOR": return Op.XNOR;
        case "NOT": return Op.NOT;
        case "BUF": return Op.BUF;
        case "MUX": return Op.MUX;
        default: throw new Exception("unknown gate type " ~ s);
    }
}

struct Gate { Op op; string[] ins; }

// ------------------------------------------------------------------ the export port
final class Port {
    string host;
    ushort port;
    Socket sock;
    char[] buf;
    size_t pos;
    string[] greeting;

    this(string h, ushort p) { host = h; port = p; }

    void open() {
        close();
        auto addrs = getAddress(host, port);
        Exception last;
        foreach (a; addrs) {
            try {
                sock = new Socket(a.addressFamily, SocketType.STREAM, ProtocolType.TCP);
                sock.connect(a);
                buf.length = 0;
                pos = 0;
                auto g = readLine();
                if (g is null) throw new Exception("connection closed before the greeting");
                greeting = g.split;
                return;
            } catch (Exception e) {
                last = e;
                close();
            }
        }
        throw last is null ? new Exception("no address for " ~ host) : last;
    }

    void close() {
        if (sock !is null) {
            try { sock.shutdown(SocketShutdown.BOTH); sock.close(); } catch (Exception) {}
            sock = null;
        }
    }

    // One line without its LF, or null when the peer closed first.
    string readLine() {
        while (true) {
            auto nl = indexOf(buf[pos .. $], '\n');
            if (nl >= 0) {
                auto line = buf[pos .. pos + nl].idup;
                pos += nl + 1;
                if (pos > 65536) { buf = buf[pos .. $].dup; pos = 0; }
                return line;
            }
            char[65536] tmp;
            auto n = sock.receive(tmp[]);
            if (n == 0 || n == Socket.ERROR) return null;
            buf ~= tmp[0 .. n];
        }
    }

    // Sends a command until it is answered, reconnecting and waiting as the server asks.
    string[] fetch(string kind, int k) {
        auto want = kind ~ " " ~ k.to!string;
        while (true) {
            if (sock is null) open();
            if (sock.send(want ~ "\n") != want.length + 1) { close(); continue; }
            auto head = readLine();
            if (head is null) { close(); continue; }
            auto w = head.split;
            if (w.length == 2 && w[0] == "BUSY") {
                Thread.sleep(msecs(w[1].to!long + 5));
                continue;
            }
            if (w.length >= 1 && w[0] == "BYE") { close(); continue; }
            if (w.length == 3 && w[0] == kind && w[1] == k.to!string) {
                auto n = w[2].to!size_t;
                string[] lines;
                foreach (i; 0 .. n) {
                    auto l = readLine();
                    if (l is null) throw new Exception("page cut short");
                    lines ~= l;
                }
                return lines;
            }
            throw new Exception("unexpected answer to " ~ want ~ ": " ~ head);
        }
    }
}

// ------------------------------------------------------------------ BDD manager
struct Manager {
    uint[] var_, lo, hi;
    uint[] table;
    uint mask;
    struct Ent { uint f, g, h, r; }
    Ent[] cache;
    uint cmask;

    void init(uint nvars) {
        var_ = [nvars, nvars];
        lo = [0, 1];
        hi = [0, 1];
        table = new uint[1 << 16];
        table[] = uint.max;
        mask = (1 << 16) - 1;
        cache = new Ent[1 << 16];
        foreach (ref e; cache) e.f = uint.max;
        cmask = (1 << 16) - 1;
    }

    static uint hash3(uint a, uint b, uint c) {
        ulong h = a * 0x9E3779B97F4A7C15UL ^ (b + 0x632BE59BD9B4E019UL) * 0xC2B2AE3D27D4EB4FUL
                  ^ c * 0x165667B19E3779F9UL;
        h ^= h >> 29;
        h *= 0xBF58476D1CE4E5B9UL;
        h ^= h >> 32;
        return cast(uint) h;
    }

    void grow() {
        auto nt = new uint[table.length * 2];
        nt[] = uint.max;
        auto nm = cast(uint)(nt.length - 1);
        foreach (id; 2 .. cast(uint) var_.length) {
            uint i = hash3(var_[id], lo[id], hi[id]) & nm;
            while (nt[i] != uint.max) i = (i + 1) & nm;
            nt[i] = id;
        }
        table = nt;
        mask = nm;
        if (cache.length < table.length) {
            auto nc = new Ent[cache.length * 2];
            foreach (ref e; nc) e.f = uint.max;
            cache = nc;
            cmask = cast(uint)(nc.length - 1);
        }
    }

    uint mk(uint v, uint l, uint h) {
        if (l == h) return l;
        uint i = hash3(v, l, h) & mask;
        while (true) {
            uint id = table[i];
            if (id == uint.max) break;
            if (var_[id] == v && lo[id] == l && hi[id] == h) return id;
            i = (i + 1) & mask;
        }
        uint id = cast(uint) var_.length;
        var_ ~= v;
        lo ~= l;
        hi ~= h;
        table[i] = id;
        if (var_.length * 2 > table.length) grow();
        return id;
    }

    uint ite(uint f, uint g, uint h) {
        if (f == 1) return g;
        if (f == 0) return h;
        if (g == h) return g;
        if (g == 1 && h == 0) return f;
        uint slot = hash3(f, g, h) & cmask;
        if (cache[slot].f == f && cache[slot].g == g && cache[slot].h == h) return cache[slot].r;
        uint v = min(var_[f], var_[g], var_[h]);
        uint t = ite(var_[f] == v ? hi[f] : f, var_[g] == v ? hi[g] : g, var_[h] == v ? hi[h] : h);
        uint e = ite(var_[f] == v ? lo[f] : f, var_[g] == v ? lo[g] : g, var_[h] == v ? lo[h] : h);
        uint r = mk(v, e, t);
        cache[hash3(f, g, h) & cmask] = Ent(f, g, h, r);
        return r;
    }

    uint not(uint f) { return ite(f, 0, 1); }

    BigInt count(uint root, ref BigInt[uint] memo) {
        BigInt c(uint x) {
            if (x < 2) return BigInt(x);
            if (auto p = x in memo) return *p;
            uint v = var_[x];
            BigInt r = c(lo[x]) << (var_[lo[x]] - v - 1);
            r += c(hi[x]) << (var_[hi[x]] - v - 1);
            memo[x] = r;
            return r;
        }
        return c(root) << var_[root];
    }
}

// ------------------------------------------------------------------ the block
string[] inputs;
string[] outNames;
string[string] outNet;
Gate[string] gates;
string[][] ecoLines;   // the words after REV: revision, kind, target, ...

struct State { Gate[string] g; string[] outs; string[string] net; }

State stateAt(long R) {
    State s;
    foreach (k, v; gates) s.g[k] = v;
    s.outs = outNames.dup;
    foreach (k, v; outNet) s.net[k] = v;
    auto lines = ecoLines.filter!(t => t[0].to!long <= R).array;
    lines.sort!((a, b) => a[0].to!long < b[0].to!long, SwapStrategy.stable);
    foreach (t; lines) {
        if (t[1] == "GATE") s.g[t[2]] = Gate(opOf(t[3]), t[4 .. $].dup);
        else if (t[1] == "OUTPUT") {
            if (!s.outs.canFind(t[2])) s.outs ~= t[2];
            s.net[t[2]] = t[3];
        } else if (t[1] == "DROP") {
            s.outs = s.outs.filter!(o => o != t[2]).array;
            s.net.remove(t[2]);
        } else throw new Exception("unknown ECO line kind " ~ t[1]);
    }
    return s;
}

string[] structuralOrder(ref State s) {
    auto outs = s.outs.dup;
    size_t[string] cone;
    foreach (o; outs) {
        bool[string] seen;
        auto st = [s.net[o]];
        while (st.length) {
            auto x = st[$ - 1];
            st.length--;
            if (x in seen) continue;
            seen[x] = true;
            if (auto g = x in s.g) st ~= g.ins;
        }
        cone[o] = seen.length;
    }
    outs.sort!((a, b) => cone[a] < cone[b], SwapStrategy.stable);
    string[] ord;
    bool[string] seen, placed;
    foreach (o; outs) {
        auto st = [s.net[o]];
        while (st.length) {
            auto x = st[$ - 1];
            st.length--;
            if (x in seen) continue;
            seen[x] = true;
            if (auto g = x in s.g) {
                foreach_reverse (i; g.ins) st ~= i;
            } else if (x !in placed) {
                placed[x] = true;
                ord ~= x;
            }
        }
    }
    foreach (nm; inputs) if (nm !in placed) ord ~= nm;
    return ord;
}

void countAt(long R) {
    auto s = stateAt(R);
    auto ord = structuralOrder(s);
    Manager m;
    m.init(cast(uint) ord.length);
    uint[string] val;
    foreach (i, nm; ord) val[nm] = m.mk(cast(uint) i, 0, 1);
    uint ev(string n) {
        auto st = [n];
        while (st.length) {
            auto x = st[$ - 1];
            if (x in val) { st.length--; continue; }
            auto gp = x in s.g;
            if (gp is null) throw new Exception("net " ~ x ~ " has no driver at revision " ~ R.to!string);
            bool pend = false;
            foreach (i; gp.ins) if (i !in val) { st ~= i; pend = true; }
            if (pend) continue;
            uint[3] a;
            foreach (k, i; gp.ins) a[k] = val[i];
            uint r;
            final switch (gp.op) {
                case Op.AND: r = m.ite(a[0], a[1], 0); break;
                case Op.OR: r = m.ite(a[0], 1, a[1]); break;
                case Op.XOR: r = m.ite(a[0], m.not(a[1]), a[1]); break;
                case Op.NAND: r = m.ite(a[0], m.not(a[1]), 1); break;
                case Op.NOR: r = m.ite(a[0], 0, m.not(a[1])); break;
                case Op.XNOR: r = m.ite(a[0], a[1], m.not(a[1])); break;
                case Op.NOT: r = m.not(a[0]); break;
                case Op.BUF: r = a[0]; break;
                case Op.MUX: r = m.ite(a[0], a[2], a[1]); break;
            }
            val[x] = r;
            st.length--;
        }
        return val[n];
    }
    BigInt[uint] memo;
    foreach (o; s.outs) writeln(R, " ", o, " ", m.count(ev(s.net[o]), memo));
    stderr.writeln("revision ", R, ": ", s.outs.length, " outputs, ", m.var_.length, " BDD nodes");
}

void main() {
    auto port = new Port(environment.get("SYNTH_HOST", "127.0.0.1"),
                         environment.get("SYNTH_PORT", "7300").to!ushort);
    port.open();
    auto g = port.greeting;   // SYNTH <block> PAGES <p> ECO <e> CHECKPOINTS <list> LIMIT <l>
    if (g.length < 8 || g[0] != "SYNTH") throw new Exception("unexpected greeting");
    int npages = g[3].to!int, necos = g[5].to!int;
    long[] checkpoints;
    foreach (c; g[7].split(",")) checkpoints ~= c.to!long;

    foreach (k; 1 .. npages + 1) foreach (line; port.fetch("PAGE", k)) {
        auto t = line.split;
        if (t.length == 0) continue;
        if (t[0] == "INPUT") inputs ~= t[1];
        else if (t[0] == "OUTPUT") { outNames ~= t[1]; outNet[t[1]] = t[2]; }
        else if (t[0] == "GATE") gates[t[1]] = Gate(opOf(t[2]), t[3 .. $].dup);
        else throw new Exception("unexpected netlist line: " ~ line);
    }
    foreach (k; 1 .. necos + 1) foreach (line; port.fetch("ECO", k)) {
        auto t = line.split;
        if (t.length == 0) continue;
        if (t[0] != "REV") throw new Exception("unexpected ECO line: " ~ line);
        ecoLines ~= t[1 .. $];
    }
    port.close();
    stderr.writeln(inputs.length, " inputs, ", gates.length, " gates, ", ecoLines.length, " ECO lines");

    foreach (R; checkpoints) countAt(R);
    stdout.flush();
}
