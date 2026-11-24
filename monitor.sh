#!/system/bin/sh
MODDIR=${0%/*}
. "$MODDIR/common.sh"

ATOP_CHECK=0
ATOP_ONCE=0
ATOP_DRY_RUN=0
ATOP_CUSTOM_PATHS=0
ATOP_SYSFS_ROOT=/sys/class/power_supply
ATOP_CONFIG=/data/adb/atopwlb/config.conf
ATOP_SVC=/system/bin/svc
ATOP_STATE_DIR=/data/adb/atopwlb

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) ATOP_CHECK=1 ;;
    --once) ATOP_ONCE=1 ;;
    --dry-run) ATOP_DRY_RUN=1 ;;
    --sysfs-root|--config|--svc)
      ATOP_OPTION=$1
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      shift
      case "$ATOP_OPTION" in
        --sysfs-root) ATOP_SYSFS_ROOT=$1 ;;
        --config) ATOP_CONFIG=$1 ;;
        --svc) ATOP_SVC=$1 ;;
      esac
      ATOP_CUSTOM_PATHS=1
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$ATOP_CUSTOM_PATHS" -eq 1 ] && [ "$ATOP_CHECK" -eq 0 ] && [ "$ATOP_DRY_RUN" -eq 0 ]; then
  echo "Custom paths require --check or --dry-run" >&2
  exit 2
fi
if [ "$ATOP_DRY_RUN" -eq 1 ]; then ATOP_ONCE=1; fi

atop_log() {
  if [ "$ATOP_CHECK" -eq 1 ] || [ "$ATOP_DRY_RUN" -eq 1 ]; then
    printf '%s\n' "$1"
    return
  fi
  if [ -f "$ATOP_STATE_DIR/monitor.log" ] &&
     [ "$(wc -c < "$ATOP_STATE_DIR/monitor.log")" -gt 65536 ]; then
    mv -f "$ATOP_STATE_DIR/monitor.log" "$ATOP_STATE_DIR/monitor.log.1" || return 1
  fi
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$ATOP_STATE_DIR/monitor.log"
}

atop_state() {
  if [ "$ATOP_CHECK" -eq 1 ] || [ "$ATOP_DRY_RUN" -eq 1 ]; then return 0; fi
  {
    printf 'STATE=%s\n' "$1"
    printf 'DETAIL=%s\n' "$2"
    printf 'CAPACITY_FILE=%s\n' "$ATOP_CAPACITY_FILE"
    printf 'STATUS_FILE=%s\n' "$ATOP_STATUS_FILE"
    printf 'LEVEL=%s\n' "$ATOP_LEVEL"
    printf 'THRESHOLD=%s\n' "$ATOP_THRESHOLD"
    printf 'PLUGGED=%s\n' "$ATOP_PLUGGED"
  } > "$ATOP_STATE_DIR/status.new" &&
    mv -f "$ATOP_STATE_DIR/status.new" "$ATOP_STATE_DIR/status"
}

atop_fail() {
  atop_log "Inactive: $ATOP_ERROR"
  atop_state inactive "$ATOP_ERROR"
  exit 1
}

atop_module_stopped() {
  if [ -e "$MODDIR/disable" ] || [ -e "$MODDIR/remove" ]; then
    atop_log "Stopped by root manager disable/remove flag"
    atop_state stopped "Root manager disable/remove flag"
    return 0
  fi
  return 1
}

if [ "$ATOP_CHECK" -eq 0 ] && [ "$ATOP_DRY_RUN" -eq 0 ]; then
  [ "$(id -u)" -eq 0 ] || { echo "Root is required" >&2; exit 1; }
  umask 077
  mkdir -p "$ATOP_STATE_DIR" && chmod 700 "$ATOP_STATE_DIR" || exit 1
  atop_module_stopped && exit 0
fi
atop_load_config "$ATOP_CONFIG" || atop_fail

if [ "$ATOP_CHECK" -eq 0 ] && [ "$ATOP_DRY_RUN" -eq 0 ]; then
  atop_log "Waiting for Android boot completion"
  atop_state waiting_boot "sys.boot_completed"
  while [ "$(getprop sys.boot_completed)" != 1 ]; do
    atop_module_stopped && exit 0
    sleep 5
  done
fi
atop_detect_battery "$ATOP_SYSFS_ROOT" || atop_fail
atop_read_sample "$ATOP_SYSFS_ROOT" || atop_fail
atop_probe_shutdown "$ATOP_SVC" || atop_fail
atop_log "Detected capacity=$ATOP_CAPACITY_FILE status=$ATOP_STATUS_FILE; normal svc shutdown advertised (not exercised)"

if [ "$ATOP_CHECK" -eq 1 ]; then
  printf 'CHECK=ok\nCAPACITY_FILE=%s\nSTATUS_FILE=%s\nLEVEL=%s\nPLUGGED=%s\nTHRESHOLD=%s\nENABLED=%s\nSHUTDOWN=svc power shutdown (not exercised)\n' \
    "$ATOP_CAPACITY_FILE" "$ATOP_STATUS_FILE" "$ATOP_LEVEL" "$ATOP_PLUGGED" "$ATOP_THRESHOLD" "$ATOP_ENABLED"
  exit 0
fi

ATOP_PREVIOUS_DECISION=
while :; do
  atop_module_stopped && exit 0
  atop_load_config "$ATOP_CONFIG" || atop_fail
  atop_read_sample "$ATOP_SYSFS_ROOT" || atop_fail
  if atop_should_shutdown; then
    # Charging/configuration can change after sampling. Re-read both directly
    # before requesting shutdown, so the decision owns a fresh hardware state.
    atop_load_config "$ATOP_CONFIG" || atop_fail
    atop_read_sample "$ATOP_SYSFS_ROOT" || atop_fail
    atop_module_stopped && exit 0
    if atop_should_shutdown; then
      if [ "$ATOP_DRY_RUN" -eq 1 ]; then
        printf 'DECISION=low_battery\nWOULD_SHUTDOWN=1\nLEVEL=%s\nTHRESHOLD=%s\n' "$ATOP_LEVEL" "$ATOP_THRESHOLD"
        exit 0
      fi
      atop_log "Requesting normal shutdown once: level=$ATOP_LEVEL threshold=$ATOP_THRESHOLD plugged=$ATOP_PLUGGED"
      atop_state shutdown_requested "Android PowerManager normal shutdown"
      # Bound only the Binder client's wait, never force the phone off. The
      # framework owns its shutdown after accepting the request.
      timeout 60 /system/bin/sh -c '. "$1"; atop_request_shutdown "$2"' \
        atopwlb "$MODDIR/common.sh" "$ATOP_SVC" > "$ATOP_STATE_DIR/shutdown.out" 2>&1
      ATOP_REQUEST_RESULT=$?
      if [ "$ATOP_REQUEST_RESULT" -ne 0 ] || [ -s "$ATOP_STATE_DIR/shutdown.out" ]; then
        ATOP_ERROR="Normal shutdown did not complete cleanly (exit=$ATOP_REQUEST_RESULT); see shutdown.out. No further request will be made."
        atop_fail
      fi
      # Old svc can return success even on a Binder exception. An accepted
      # asynchronous request also needs time to finish; never send it twice.
      sleep 60
      ATOP_ERROR="Android is still running after the normal shutdown request. No further request will be made."
      atop_fail
    fi
  fi
  if [ "$ATOP_DRY_RUN" -eq 1 ]; then
    printf 'DECISION=%s\nWOULD_SHUTDOWN=0\nLEVEL=%s\nTHRESHOLD=%s\n' "$ATOP_DECISION" "$ATOP_LEVEL" "$ATOP_THRESHOLD"
    exit 0
  fi
  atop_state monitoring "$ATOP_DECISION"
  if [ "$ATOP_DECISION" != "$ATOP_PREVIOUS_DECISION" ]; then
    atop_log "Monitoring: $ATOP_DECISION level=$ATOP_LEVEL threshold=$ATOP_THRESHOLD plugged=$ATOP_PLUGGED"
    ATOP_PREVIOUS_DECISION=$ATOP_DECISION
  fi
  [ "$ATOP_ONCE" -eq 1 ] && exit 0
  sleep "$ATOP_INTERVAL"
done
