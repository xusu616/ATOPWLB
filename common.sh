#!/system/bin/sh
# Shared POSIX-shell functions. Reading sysfs never changes battery hardware.

atop_read_percentage() {
  ATOP_VALUE=$(cat "$1" 2>/dev/null) || {
    ATOP_ERROR="Cannot read battery capacity: $1"
    return 1
  }
  case "$ATOP_VALUE" in
    0|[1-9]|[1-9][0-9]|100) return 0 ;;
    *) ATOP_ERROR="Invalid battery capacity at $1: $ATOP_VALUE"; return 1 ;;
  esac
}

atop_validate_battery() {
  [ "$(cat "$1/type" 2>/dev/null)" = Battery ] || {
    ATOP_ERROR="Not a Battery power supply: $1"
    return 1
  }
  if [ -e "$1/present" ]; then
    [ "$(cat "$1/present" 2>/dev/null)" = 1 ] || {
      ATOP_ERROR="Battery is absent or present is unreadable: $1"
      return 1
    }
  fi
  ATOP_STATUS=$(cat "$1/status" 2>/dev/null) || {
    ATOP_ERROR="Cannot read battery status: $1/status"
    return 1
  }
  case "$ATOP_STATUS" in
    Charging|Discharging|Full|'Not charging') return 0 ;;
    *) ATOP_ERROR="Unsupported battery status at $1/status: $ATOP_STATUS"; return 1 ;;
  esac
}

atop_detect_battery() {
  ATOP_ROOT=${1:-/sys/class/power_supply}
  ATOP_CAPACITY_FILE=
  ATOP_STATUS_FILE=
  ATOP_BATTERY_DIR=
  ATOP_ERROR=
  # Prefer the charger driver's battery supply over the raw fuel gauge.
  for ATOP_CANDIDATE in "$ATOP_ROOT/battery" "$ATOP_ROOT"/*; do
    [ -d "$ATOP_CANDIDATE" ] || continue
    atop_validate_battery "$ATOP_CANDIDATE" || continue
    atop_read_percentage "$ATOP_CANDIDATE/capacity" || continue
    ATOP_BATTERY_DIR=$ATOP_CANDIDATE
    ATOP_CAPACITY_FILE=$ATOP_CANDIDATE/capacity
    ATOP_STATUS_FILE=$ATOP_CANDIDATE/status
    ATOP_ERROR=
    return 0
  done
  # Some Qualcomm kernels expose capacity only on BMS. The real battery
  # supply must still identify this battery and provide its charging status.
  if atop_validate_battery "$ATOP_ROOT/battery"; then
    for ATOP_CANDIDATE in "$ATOP_ROOT/bms" "$ATOP_ROOT"/*; do
      [ -d "$ATOP_CANDIDATE" ] || continue
      [ "$(cat "$ATOP_CANDIDATE/type" 2>/dev/null)" = BMS ] || continue
      atop_read_percentage "$ATOP_CANDIDATE/capacity" || continue
      ATOP_BATTERY_DIR=$ATOP_ROOT/battery
      ATOP_CAPACITY_FILE=$ATOP_CANDIDATE/capacity
      ATOP_STATUS_FILE=$ATOP_BATTERY_DIR/status
      ATOP_ERROR=
      return 0
    done
  fi
  ATOP_ERROR="No readable, valid battery capacity and charging status under $ATOP_ROOT"
  return 1
}

atop_read_sample() {
  ATOP_ROOT=${1:-/sys/class/power_supply}
  atop_validate_battery "$ATOP_BATTERY_DIR" || return 1
  atop_read_percentage "$ATOP_CAPACITY_FILE" || return 1
  ATOP_LEVEL=$ATOP_VALUE
  ATOP_PLUGGED=0
  ATOP_EXTERNAL_STATE_COUNT=0
  # Re-enumerate external supplies: wireless/USB supplies can appear later.
  # BMS online is a fuel-gauge state, not evidence of an attached charger.
  for ATOP_SUPPLY in "$ATOP_ROOT"/*; do
    [ -d "$ATOP_SUPPLY" ] || continue
    ATOP_TYPE=$(cat "$ATOP_SUPPLY/type" 2>/dev/null) || {
      ATOP_ERROR="Cannot identify power supply: $ATOP_SUPPLY/type"
      return 1
    }
    case "$ATOP_TYPE" in Battery|BMS) continue ;; esac
    # Qualcomm ONLINE can be zero when USB charging is paused or the charger
    # is weak. PRESENT still reports the attached supply in that situation.
    for ATOP_ATTRIBUTE in online present; do
      [ -e "$ATOP_SUPPLY/$ATOP_ATTRIBUTE" ] || continue
      ATOP_EXTERNAL_STATE=$(cat "$ATOP_SUPPLY/$ATOP_ATTRIBUTE" 2>/dev/null) || {
        ATOP_ERROR="Cannot read charger state: $ATOP_SUPPLY/$ATOP_ATTRIBUTE"
        return 1
      }
      case "$ATOP_EXTERNAL_STATE" in
        0) ;;
        1) ATOP_PLUGGED=1 ;;
        *) ATOP_ERROR="Invalid charger state: $ATOP_SUPPLY/$ATOP_ATTRIBUTE"; return 1 ;;
      esac
      ATOP_EXTERNAL_STATE_COUNT=$((ATOP_EXTERNAL_STATE_COUNT + 1))
    done
  done
  case "$ATOP_STATUS" in
    Charging|Full) ATOP_PLUGGED=1 ;;
    'Not charging')
      if [ "$ATOP_EXTERNAL_STATE_COUNT" -eq 0 ]; then
        ATOP_ERROR="Not charging without a readable external online/present node is ambiguous"
        return 1
      fi
      ;;
  esac
  ATOP_ERROR=
}

atop_probe_shutdown() {
  ATOP_SVC=${1:-/system/bin/svc}
  [ -x "$ATOP_SVC" ] || {
    ATOP_ERROR="Android svc is unavailable: $ATOP_SVC"
    return 1
  }
  command -v timeout >/dev/null 2>&1 || {
    ATOP_ERROR="timeout command is unavailable"
    return 1
  }
  # Help is intentionally the only probe; never test a real shutdown here.
  ATOP_HELP=$(timeout 10 "$ATOP_SVC" power 2>&1)
  ATOP_HELP_RESULT=$?
  case "$ATOP_HELP_RESULT" in
    0|1) ;;
    *) ATOP_ERROR="svc power help failed or timed out (exit=$ATOP_HELP_RESULT)"; return 1 ;;
  esac
  case "$ATOP_HELP" in
    *'svc power shutdown'*) ATOP_ERROR=; return 0 ;;
    *) ATOP_ERROR="svc power help does not advertise normal shutdown"; return 1 ;;
  esac
}

atop_load_config() {
  ATOP_ENABLED=
  ATOP_THRESHOLD=
  ATOP_INTERVAL=
  ATOP_ERROR=
  [ -r "$1" ] || {
    ATOP_ERROR="Cannot read config: $1"
    return 1
  }
  ATOP_CR=$(printf '\r')
  # Parse data rather than source executable shell code from a config file.
  while IFS= read -r ATOP_LINE || [ -n "$ATOP_LINE" ]; do
    ATOP_LINE=${ATOP_LINE%"$ATOP_CR"}
    case "$ATOP_LINE" in
      ''|\#*) continue ;;
      ENABLED=*)
        [ -z "$ATOP_ENABLED" ] || { ATOP_ERROR="Duplicate ENABLED"; return 1; }
        ATOP_ENABLED=${ATOP_LINE#ENABLED=}
        case "$ATOP_ENABLED" in 0|1) ;; *) ATOP_ERROR="ENABLED must be 0 or 1"; return 1 ;; esac
        ;;
      THRESHOLD=*)
        [ -z "$ATOP_THRESHOLD" ] || { ATOP_ERROR="Duplicate THRESHOLD"; return 1; }
        ATOP_THRESHOLD=${ATOP_LINE#THRESHOLD=}
        case "$ATOP_THRESHOLD" in 10|20) ;; *) ATOP_ERROR="THRESHOLD must be 10 or 20"; return 1 ;; esac
        ;;
      INTERVAL=*)
        [ -z "$ATOP_INTERVAL" ] || { ATOP_ERROR="Duplicate INTERVAL"; return 1; }
        ATOP_INTERVAL=${ATOP_LINE#INTERVAL=}
        case "$ATOP_INTERVAL" in ''|0*|*[!0-9]*|?????*) ATOP_ERROR="INTERVAL must be an integer from 1 to 3600"; return 1 ;; esac
        if [ "$ATOP_INTERVAL" -gt 3600 ]; then
          ATOP_ERROR="INTERVAL must be an integer from 1 to 3600"
          return 1
        fi
        ;;
      *) ATOP_ERROR="Unknown or malformed config line: $ATOP_LINE"; return 1 ;;
    esac
  done < "$1"
  if [ -z "$ATOP_ENABLED" ] || [ -z "$ATOP_THRESHOLD" ] || [ -z "$ATOP_INTERVAL" ]; then
    ATOP_ERROR="Config must contain ENABLED, THRESHOLD and INTERVAL"
    return 1
  fi
}

atop_should_shutdown() {
  if [ "$ATOP_ENABLED" -eq 0 ]; then
    ATOP_DECISION=disabled
  elif [ "$ATOP_PLUGGED" -eq 1 ]; then
    ATOP_DECISION=charging
  elif [ "$ATOP_LEVEL" -gt "$ATOP_THRESHOLD" ]; then
    ATOP_DECISION=above_threshold
  else
    ATOP_DECISION=low_battery
    return 0
  fi
  return 1
}

atop_request_shutdown() {
  # Android PowerManager -> ShutdownThread; no raw sysfs/property/reboot call.
  "${1:-/system/bin/svc}" power shutdown
}
