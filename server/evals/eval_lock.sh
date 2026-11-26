# Sourced by a script that runs server/evals/earth_eval.py: one eval build on this machine at a time. Agents' builds run side by side died of
# memory ("memory allocation of N bytes failed": the A5, Nanpu's upstream side, six of Vienna's A23 builds, 2026-11-18).
# The lock is a directory (atomic mkdir) holding the holder's pid. Run the build as `rr_run <command>`: a TERM, INT or HUP
# sent to the script stops the build with its processes and releases the lock at once (a plain foreground command would
# finish first, the trap waiting for it), and a build that ends, fails or times out (earth_eval --junction-timeout) releases
# it by the EXIT trap. A holder that is gone without it (killed hard: taskkill /F, a crash, a reboot) leaves the lock to
# the next waiter, which takes it over: a dead pid, or a lock with no pid for longer than RR_LOCK_STALE_S (the holder died
# between mkdir and writing it). One waiter at a time takes over (the lock's .take), so two never remove each other's new lock.
# EVAL_LOCK overrides the lock's path.
LOCK=${EVAL_LOCK:-${TMPDIR:-/tmp}/beamng-earth-eval.lock}
GUARD=$LOCK.take
RR_LOCK_STALE_S=${RR_LOCK_STALE_S:-60}
RR_CHILD=

rr_age() {      # seconds since $1 was last changed
  m=$(stat -c %Y "$1" 2>/dev/null) || return 1
  echo $(( $(date +%s) - m ))
}

rr_take_over() {        # 0 when it removed a stale lock
  holder=$(cat $LOCK/pid 2>/dev/null)
  if [ -n "$holder" ]; then
    kill -0 $holder 2>/dev/null && return 1
  else
    a=$(rr_age $LOCK) || return 1
    [ "$a" -gt "$RR_LOCK_STALE_S" ] || return 1
  fi
  if mkdir $GUARD 2>/dev/null; then
    if [ "$(cat $LOCK/pid 2>/dev/null)" = "$holder" ]; then     # (still that holder's, not one a quicker waiter just made)
      echo "eval lock: holder ${holder:-none} gone, taking it" >&2
      rm -rf $LOCK
      rmdir $GUARD
      return 0
    fi
    rmdir $GUARD
  else
    a=$(rr_age $GUARD) && [ "$a" -gt "$RR_LOCK_STALE_S" ] && rmdir $GUARD 2>/dev/null     # (a waiter died holding it)
  fi
  return 1
}

rr_release() {      # only a lock that is ours
  [ "$(cat $LOCK/pid 2>/dev/null)" = "$$" ] && rm -rf $LOCK
}

rr_kill_tree() {    # a background job and its processes (a Windows process tree: python starts earth_view, which starts Earth's server)
  w=$(cat /proc/$1/winpid 2>/dev/null)
  if [ -n "$w" ]; then taskkill //T //F //PID $w >/dev/null 2>&1; fi
  kill -9 $1 2>/dev/null
}

rr_stop() {
  [ -n "$RR_CHILD" ] && rr_kill_tree $RR_CHILD
  exit $1
}

rr_run() {      # the build, as a job that a signal can stop: rr_run python server/evals/earth_eval.py ... > log 2>&1; its status
  "$@" &
  RR_CHILD=$!
  wait $RR_CHILD
  rc=$?
  RR_CHILD=
  return $rc
}

while ! mkdir $LOCK 2>/dev/null; do
  rr_take_over || sleep ${RR_LOCK_WAIT_S:-20}
done
echo $$ > $LOCK/pid
trap rr_release EXIT
trap 'rr_stop 143' TERM
trap 'rr_stop 130' INT
trap 'rr_stop 129' HUP
