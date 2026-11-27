#!/bin/bash
# Two processes in the hub container: the receiver (iPhone API + compute API) and the hub-worker (scheduler).
# OMY_HUB_SCHEDULER=off (the parallel phase, before the cutover): receiver only, nothing is queued or scanned.
# If one process dies the container exits and docker restarts both.
omy-receiver &
receiver=$!
if [ "${OMY_HUB_SCHEDULER:-on}" = "off" ]; then
  echo "hub-entrypoint: scheduler OFF (OMY_HUB_SCHEDULER=off)" >&2
  trap 'kill $receiver 2>/dev/null' TERM INT
  wait $receiver
  exit $?
fi
omy hub-worker &
worker=$!
trap 'kill $worker $receiver 2>/dev/null' TERM INT
wait -n $worker $receiver
status=$?
kill $worker $receiver 2>/dev/null
wait
exit $status
