#!/bin/bash

# Verifier entrypoint. Runs in the verifier container, not the agent's, and sees only the paths
# declared in `artifacts`. Everything it needs is baked into tests/Dockerfile; nothing is fetched
# here. It compiles the collected D once and runs the counter twice, each time against an export
# server it starts from the copy in this image: the shipped block on another address and port
# with other limits, then a block the agent has never had access to.

set -uo pipefail
# The image's working directory is the tree the build below deletes and recreates.
cd /
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
exec 2>&1

# Root runs this script and later runs pytest, after agent-authored code has run as the build
# user. Every directory on PATH has to be one the build user cannot write to, so PATH is pinned
# and each entry is checked before anything untrusted runs.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
hash -r
for d in ${PATH//:/ }; do
  real="$(/usr/bin/readlink -f "$d" 2>/dev/null)" || continue
  [ -d "$real" ] || continue
  owner="$(/usr/bin/stat -c %u "$real")"
  mode="$(/usr/bin/stat -c %a "$real")"
  if [ "$owner" != "0" ] || [ $(( 8#$mode & 8#022 )) -ne 0 ]; then
    echo "FATAL: $d (-> $real) is on root's PATH but is owner $owner mode $mode."
    echo 0 > /logs/verifier/reward.txt
    exit 0
  fi
done
echo "every directory on root's PATH is root-owned and closed to writing by anyone else"

LDC=/opt/ldc/bin/ldc2
PY=/usr/local/bin/python3
if [ ! -x "$LDC" ] || [ ! -x "$PY" ]; then
  echo "FATAL: the D compiler or the server's interpreter is missing"
  echo 0 > /logs/verifier/reward.txt
  exit 0
fi

/usr/bin/install -d -m 700 -o root -g root /opt/run

as_runner() {
  setpriv --clear-groups --no-new-privs --reuid runner --regid runner "$@"
}

# Close the verifier's own code, the blocks, the counts, the server and the reward directory to
# the build user before anything untrusted runs, and prove it. `ls` needs read permission on the
# directory itself, which is the right probe.
chmod 700 /tests /logs/verifier /opt/truth /opt/synth /opt/run 2>/dev/null || true
if as_runner bash -c 'test -r /tests/test_outputs.py || ls /opt/truth || ls /opt/synth || ls /opt/run || ls /logs/verifier' >/dev/null 2>&1; then
  echo "FATAL: /tests, the blocks, the server, the run directory or the reward directory is reachable by the build user."
  echo 0 > /logs/verifier/reward.txt
  exit 0
fi
echo "verifier files closed to the unprivileged build user"

# What is compiled is D source and nothing else, by construction: the regular .d files directly
# under /app/bdd/src are copied into a root-owned pristine directory and the compiler gets those
# and the Phobos it ships with. A link, a subdirectory or anything that is not a .d file never
# reaches it.
rm -rf /opt/src_pristine
/usr/bin/install -d -m 700 -o root -g root /opt/src_pristine
for f in /app/bdd/src/*.d; do
  if [ -f "$f" ] && [ ! -L "$f" ]; then /usr/bin/cp --no-dereference "$f" /opt/src_pristine/; fi
done
echo "collected .d files: $(ls /opt/src_pristine | wc -l)"

rm -rf /app/bdd
/usr/bin/install -d -o runner -g runner -m 755 /app/bdd /app/bdd/src
cp /opt/src_pristine/*.d /app/bdd/src/ 2>/dev/null || true
chown -R runner:runner /app/bdd
/usr/bin/find /app/bdd -type f -exec /usr/bin/touch {} +

# Interpreters closed to the build user while the D compiles: the build has no business running
# them. Root is unaffected.
INTERP="$(for t in python3 python3.13 pip perl; do
            p="$(command -v "$t" 2>/dev/null)" && readlink -f "$p"; done | sort -u)"
close_interp() { for f in $INTERP; do /usr/bin/chmod 750 "$f" 2>/dev/null || true; done; }
open_interp()  { for f in $INTERP; do /usr/bin/chmod 755 "$f" 2>/dev/null || true; done; }
trap 'open_interp' EXIT

echo ok > /opt/run/build_check.txt
if [ "$(ls /app/bdd/src | wc -l)" -eq 0 ]; then
  echo "no .d files to compile"
  echo failed > /opt/run/build_check.txt
else
  close_interp
  setsid /usr/bin/timeout --signal=KILL 900 \
    setpriv --clear-groups --no-new-privs --reuid runner --regid runner \
    env -i -C /app/bdd HOME=/home/runner PATH=/opt/ldc/bin:/usr/bin:/bin \
      /bin/bash -c "exec $LDC -O2 -of=/app/bdd/bddcount \"\$@\"" ldc2 /app/bdd/src/*.d &
  bpid=$!
  wait "$bpid"
  bstatus=$?
  echo "ldc2 exit status: $bstatus"
  kill -KILL -- -"$bpid" 2>/dev/null || true
  open_interp
  as_runner bash -c 'kill -9 -1' 2>/dev/null || true
  [ "$bstatus" -eq 0 ] || echo failed > /opt/run/build_check.txt
fi
if [ -L /app/bdd/bddcount ] || [ ! -f /app/bdd/bddcount ]; then
  echo "no regular executable was produced at /app/bdd/bddcount"
  echo failed > /opt/run/build_check.txt
  /usr/bin/rm -f /app/bdd/bddcount
fi

SERVICE_PID=""
start_synth() {   # $1 block, $2 address, $3 port, $4 limit, $5 busy-every, $6 busy-ms
  /usr/bin/rm -f /opt/run/ready
  "$PY" /opt/synth/synth_server.py --data "/opt/truth/$1/block.json" --host "$2" --port "$3" \
      --limit "$4" --busy-every "$5" --busy-ms "$6" \
      --ready-file /opt/run/ready --log "/opt/run/synth_$1.log" >> "/opt/run/synth_$1.err" 2>&1 &
  SERVICE_PID=$!
  local i
  for i in $(/usr/bin/seq 1 300); do
    [ -f /opt/run/ready ] && return 0
    /usr/bin/sleep 0.1
  done
  echo "FATAL: the export server never came up on $2:$3"
  return 1
}

# `kill` is the shell builtin on purpose: a builtin cannot be shadowed by anything on PATH.
stop_synth() {
  [ -n "$SERVICE_PID" ] || return 0
  kill -TERM "$SERVICE_PID" 2>/dev/null || true
  wait "$SERVICE_PID" 2>/dev/null
  SERVICE_PID=""
}

# $1 block, $2 SYNTH_HOST, $3 SYNTH_PORT. Starts the binary the build produced from /app/bdd, as
# the build user with a clean environment, 4 GiB of address space, and through tool_launch
# (tests/launch.c) so neither it nor any child can start another program. Stdout is captured for
# grading; stderr goes to this log. The run is killed at the 90 seconds the instruction states;
# the wall time is recorded so a run is only called a timeout when it actually reached the limit.
run_tool() {
  local out="/opt/run/report_$1.txt" pid status t0 t1
  /usr/bin/find /tmp /var/tmp /dev/shm -mindepth 1 -user runner -delete 2>/dev/null || true
  t0="$(/usr/bin/date +%s.%N)"
  setsid /usr/bin/timeout --signal=KILL 90 \
    env -i -C /app/bdd HOME=/home/runner PATH=/usr/bin:/bin SYNTH_HOST="$2" SYNTH_PORT="$3" \
      /usr/bin/prlimit --as=4294967296 -- \
      setpriv --clear-groups --no-new-privs --reuid runner --regid runner \
      /usr/local/bin/tool_launch /app/bdd/bddcount > "$out" &
  pid=$!
  wait "$pid"
  status=$?
  t1="$(/usr/bin/date +%s.%N)"
  kill -KILL -- -"$pid" 2>/dev/null || true
  as_runner bash -c 'kill -9 -1' 2>/dev/null || true
  /usr/bin/chown root:root "$out" 2>/dev/null || true
  /usr/bin/chmod 600 "$out" 2>/dev/null || true
  echo "$status $t0 $t1" > "/opt/run/status_$1.txt"
  echo "run $1: exit status $status, server $2:$3, $(/usr/bin/awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b - a}') s"
}

if [ -x /app/bdd/bddcount ]; then
  echo "=== the shipped block, on another address and port, with other limits ==="
  if start_synth sample 127.0.0.3 7521 4 3 250; then
    run_tool sample 127.0.0.3 7521
  fi
  stop_synth

  echo "=== a block the agent has never seen ==="
  if start_synth hidden 127.0.0.5 7634 6 5 300; then
    run_tool hidden 127.0.0.5 7634
  fi
  stop_synth
fi
as_runner bash -c 'kill -9 -1' 2>/dev/null || true

for s in sample hidden; do
  if [ -f "/opt/run/synth_$s.log" ]; then
    echo "$s: $(/usr/bin/grep -c ' OPEN$' /opt/run/synth_$s.log) connections, $(/usr/bin/grep -c ' SERVED ' /opt/run/synth_$s.log) pages served, $(/usr/bin/grep -c ' BUSY ' /opt/run/synth_$s.log) busy answers, $(/usr/bin/grep -c 'BYE limit' /opt/run/synth_$s.log) limit closes"
  fi
done

# By absolute path, and -P keeps the working directory off the module path.
"$PY" -P -m pytest -o cache_dir=/tmp/pytest_cache -p no:cacheprovider \
  --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
if [ "$?" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
