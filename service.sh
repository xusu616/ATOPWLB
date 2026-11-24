#!/system/bin/sh
MODDIR=${0%/*}
# Inherit the manager's BusyBox ash standalone environment. Android's system
# shell may lack timeout on old ROMs even though installation can use it.
# Source in a child shell so the monitor's exit never exits the manager script.
(set --; . "$MODDIR/monitor.sh") >/dev/null 2>&1 &
