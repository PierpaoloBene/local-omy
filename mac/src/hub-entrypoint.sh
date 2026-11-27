#!/bin/sh
# Two processes in the hub container: the receiver (iPhone API + compute API) and the hub-worker (scheduler).
# If one dies the container exits and docker restarts both.
omy hub-worker &
worker=$!
omy-receiver &
receiver=$!
trap 'kill $worker $receiver 2>/dev/null' TERM INT
wait -n $worker $receiver
status=$?
kill $worker $receiver 2>/dev/null
wait
exit $status
