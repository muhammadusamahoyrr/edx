#!/usr/bin/env bash
# Is the build actually transferring, or dead?
#
# Measured on the interface the default route actually uses. A previous version
# of this check sampled a hardcoded eth0 that did not exist, read zero bytes,
# and declared a perfectly healthy pull "stalled" — so the interface is derived,
# never assumed.
set -eu
IFACE=$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')
read_rx() { awk -v i="$IFACE:" '$1==i {print $2}' /proc/net/dev; }
A=$(read_rx); sleep 20; B=$(read_rx)
echo "iface=$IFACE  received ${A} -> ${B}  delta=$(( (B-A)/1024 )) KiB in 20s"
PID=$(pgrep -f 'tutor images build' | head -1 || true)
[ -n "$PID" ] && echo "build running for $(ps -o etimes= -p "$PID" | tr -d ' ')s" || echo "build not running"
