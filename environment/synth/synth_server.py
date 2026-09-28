#!/usr/bin/env python3
"""The synthesis server's netlist export port.

Serves one block: its netlist in numbered pages and its ECO lines in numbered pages, over a
line protocol on TCP. Every read-only thing (pages, greeting) is built once at startup before the
port opens. The only shared mutable state is the command counter and the busy window, and both
are touched under one lock everywhere.

Protocol (every line ends in LF):
  on connect      SYNTH <block> PAGES <p> ECO <e> CHECKPOINTS <r1,r2,...> LIMIT <l>
  PAGE <k>        PAGE <k> <n>, then the n lines of netlist page k (1 <= k <= p)
  ECO <k>         ECO <k> <n>, then the n lines of ECO page k (1 <= k <= e)
  QUIT            BYE
  anything else   ERR <reason>
Instead of an answer the server may say BUSY <ms>: the command was not carried out and the same
command is refused again until that many milliseconds have gone by. A connection answers at most
<l> commands, BUSY and ERR answers included; the next command gets BYE limit and the connection
is closed.
"""
import argparse
import json
import socketserver
import sys
import threading
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7300)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--busy-every", type=int, default=9)
    ap.add_argument("--busy-ms", type=int, default=400)
    ap.add_argument("--ready-file", default="")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    with open(args.data) as fh:
        doc = json.load(fh)
    # Read-only from here on.
    block_id = f"{doc['kind']}_{doc['seed']}"
    net_pages = [[ln.encode() + b"\n" for ln in pg] for pg in doc["pages"]]
    eco_pages = [[ln.encode() + b"\n" for ln in pg] for pg in doc["eco_pages"]]
    marks = ",".join(str(c) for c in doc["checkpoints"])
    greeting = (f"SYNTH {block_id} PAGES {len(net_pages)} ECO {len(eco_pages)} "
                f"CHECKPOINTS {marks} LIMIT {args.limit}\n").encode()
    limit = args.limit
    busy_every = args.busy_every
    busy_ms = args.busy_ms

    lock = threading.Lock()
    shared = {"commands": 0, "busy_until": 0.0, "conns": 0}

    def log(line):
        if args.log:
            with lock, open(args.log, "a") as fh:
                fh.write(f"{time.time():.3f} {line}\n")

    def busy_check():
        """Counts the command and says how many ms it must wait, or 0."""
        now = time.monotonic()
        with lock:
            shared["commands"] += 1
            n = shared["commands"]
            if now < shared["busy_until"]:
                return max(1, int((shared["busy_until"] - now) * 1000 + 0.999))
            if n == 1 or n % busy_every == 0:
                shared["busy_until"] = now + busy_ms / 1000.0
                return busy_ms
        return 0

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            with lock:
                shared["conns"] += 1
                conn = shared["conns"]
            log(f"{conn} OPEN")
            answered = 0
            try:
                self.wfile.write(greeting)
                while True:
                    raw = self.rfile.readline(512)
                    if not raw:
                        log(f"{conn} CLOSED-BY-CLIENT")
                        return
                    words = raw.decode("ascii", "replace").split()
                    if answered >= limit:
                        self.wfile.write(b"BYE limit\n")
                        log(f"{conn} BYE limit")
                        return
                    answered += 1
                    if words == ["QUIT"]:
                        self.wfile.write(b"BYE\n")
                        log(f"{conn} QUIT")
                        return
                    wait = busy_check()
                    if wait:
                        self.wfile.write(b"BUSY %d\n" % wait)
                        log(f"{conn} BUSY {wait} {' '.join(words)}")
                        continue
                    if len(words) == 2 and words[0] in ("PAGE", "ECO") and words[1].isdigit():
                        pages = net_pages if words[0] == "PAGE" else eco_pages
                        k = int(words[1])
                        if 1 <= k <= len(pages):
                            body = pages[k - 1]
                            self.wfile.write(b"%s %d %d\n" % (words[0].encode(), k, len(body)) + b"".join(body))
                            log(f"{conn} SERVED {words[0]} {k}")
                            continue
                        self.wfile.write(b"ERR no such page\n")
                        log(f"{conn} ERR page {' '.join(words)}")
                        continue
                    self.wfile.write(b"ERR unknown command\n")
                    log(f"{conn} ERR unknown")
            except (BrokenPipeError, ConnectionResetError):
                log(f"{conn} RESET")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server((args.host, args.port), Handler)
    if args.ready_file:
        with open(args.ready_file, "w") as fh:
            fh.write("ready\n")
    print(f"synth server for {block_id} on {args.host}:{args.port}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
