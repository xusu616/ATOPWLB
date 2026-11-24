#!/system/bin/sh

# The root manager sources this script; do not start monitoring during install.
SKIPMOUNT=true
PROPFILE=false
POSTFSDATA=false
LATESTARTSERVICE=true

atop_install_abort() {
    abort "! $1"
}

if [ "$BOOTMODE" != true ]; then
    atop_install_abort "请开机后在 Magisk / KernelSU / KernelSU Next 管理器中安装；不支持 Recovery 刷入。"
fi
if [ "$(id -u)" != 0 ]; then
    atop_install_abort "安装进程没有 root 权限，无法读取电池及输入设备。"
fi

if [ ! -r "$MODPATH/common.sh" ]; then
    atop_install_abort "模块文件不完整：缺少 common.sh。"
fi
. "$MODPATH/common.sh"

if ! command -v timeout >/dev/null 2>&1 ||
   ! timeout 2 /system/bin/sh -c 'exit 0' >/dev/null 2>&1; then
    atop_install_abort "当前安装环境缺少可用的 timeout，无法安全检测设备。"
fi

ui_print "- 检测当前设备的电池与供电节点……"
if ! atop_detect_battery || ! atop_read_sample; then
    atop_install_abort "电池检测失败：$ATOP_ERROR。模块不会安装。"
fi
ui_print "- 电量节点：$ATOP_CAPACITY_FILE"
ui_print "- 当前电量：$ATOP_LEVEL%"

if ! atop_probe_shutdown; then
    atop_install_abort "正常关机接口检测失败：$ATOP_ERROR。模块不会安装。"
fi
ui_print "- 接口已声明支持；安装不执行真实关机测试。"

ATOP_GETEVENT=/system/bin/getevent
if [ ! -x "$ATOP_GETEVENT" ]; then
    atop_install_abort "系统没有可用的 getevent，无法读取音量键。"
fi
ATOP_KEY_CAPS=$(timeout 10 "$ATOP_GETEVENT" -pl 2>&1)
ATOP_KEY_CAPS_RESULT=$?
if [ "$ATOP_KEY_CAPS_RESULT" -ne 0 ]; then
    atop_install_abort "无法读取输入设备信息；请确认管理器拥有 root 权限。"
fi
case "$ATOP_KEY_CAPS" in
    *KEY_VOLUMEUP*) ;;
    *) atop_install_abort "没有检测到音量上键，拒绝使用默认阈值。" ;;
esac
case "$ATOP_KEY_CAPS" in
    *KEY_VOLUMEDOWN*) ;;
    *) atop_install_abort "没有检测到音量下键，拒绝使用默认阈值。" ;;
esac
unset ATOP_KEY_CAPS

ui_print ""
ui_print "- 请在 60 秒内按一次音量键选择自动关机阈值："
ui_print "  音量上键 (+)：20%"
ui_print "  音量下键 (-)：10%"
ui_print "- 达到或低于阈值且未充电时，才会请求正常关机。"
ui_print "- 充电或接入外部电源时不关机。"

ATOP_EVENT_FILE="$MODPATH/.volume-events-$$"
ATOP_KEY_ERROR_FILE="$MODPATH/.volume-errors-$$"
ATOP_CHOICE_FILE="$MODPATH/.volume-choice-$$"

# A subshell confines its traps to key selection, preserving manager traps.
# Keep the getevent PID itself so cleanup never leaves a reader behind.
(
    ATOP_READER_PID=
    trap 'if [ -n "$ATOP_READER_PID" ]; then
              kill "$ATOP_READER_PID" 2>/dev/null
              wait "$ATOP_READER_PID" 2>/dev/null
          fi
          rm -f "$ATOP_EVENT_FILE" "$ATOP_KEY_ERROR_FILE"' 0
    trap 'exit 130' INT TERM
    umask 077
    : >"$ATOP_EVENT_FILE" || exit 1
    : >"$ATOP_KEY_ERROR_FILE" || exit 1
    "$ATOP_GETEVENT" -ql >"$ATOP_EVENT_FILE" 2>"$ATOP_KEY_ERROR_FILE" &
    ATOP_READER_PID=$!
    ATOP_SECONDS_WAITED=0
    while [ "$ATOP_SECONDS_WAITED" -lt 60 ]; do
        sleep 1 || exit 1
        ATOP_SECONDS_WAITED=$((ATOP_SECONDS_WAITED + 1))
        ATOP_KEY_CHOICE=$(awk '
            $NF == "DOWN" && /EV_KEY/ {
                for (i = 1; i <= NF; i++) {
                    if ($i == "KEY_VOLUMEUP") { print "20"; exit }
                    if ($i == "KEY_VOLUMEDOWN") { print "10"; exit }
                }
            }
        ' "$ATOP_EVENT_FILE") || exit 1
        if [ -n "$ATOP_KEY_CHOICE" ]; then
            printf '%s\n' "$ATOP_KEY_CHOICE" >"$ATOP_CHOICE_FILE" || exit 1
            exit 0
        fi
        if ! kill -0 "$ATOP_READER_PID" 2>/dev/null; then
            exit 1
        fi
    done
    exit 2
)
ATOP_KEY_RESULT=$?
if [ "$ATOP_KEY_RESULT" -ne 0 ]; then
    rm -f "$ATOP_CHOICE_FILE"
    if [ "$ATOP_KEY_RESULT" -eq 2 ]; then
        atop_install_abort "60 秒内没有收到音量键选择，安装已取消。"
    fi
    atop_install_abort "音量键读取失败或安装被中断，安装已取消。"
fi
IFS= read -r ATOP_THRESHOLD <"$ATOP_CHOICE_FILE"
ATOP_CHOICE_RESULT=$?
rm -f "$ATOP_CHOICE_FILE"
if [ "$ATOP_CHOICE_RESULT" -ne 0 ]; then
    atop_install_abort "无法读取音量键选择结果。"
fi
case "$ATOP_THRESHOLD" in
    10|20) ;;
    *) atop_install_abort "音量键选择结果无效。" ;;
esac

# Recheck hardware after the interactive pause, before committing any config.
if ! atop_read_sample; then
    atop_install_abort "电池状态在安装期间变为不可用：$ATOP_ERROR。"
fi

if ! touch "$MODPATH/skip_mount"; then
    atop_install_abort "无法写入模块文件。"
fi
if ! set_perm_recursive "$MODPATH" 0 0 0755 0644 ||
   ! set_perm "$MODPATH/customize.sh" 0 0 0755 ||
   ! set_perm "$MODPATH/service.sh" 0 0 0755 ||
   ! set_perm "$MODPATH/monitor.sh" 0 0 0755; then
    atop_install_abort "无法设置模块文件权限。"
fi

ATOP_STATE_DIR=/data/adb/atopwlb
ATOP_CONFIG_FILE="$ATOP_STATE_DIR/config.conf"
ATOP_CONFIG_TEMP="$ATOP_STATE_DIR/.config.conf-$$"
if ! mkdir -p "$ATOP_STATE_DIR" ||
   ! chown 0:0 "$ATOP_STATE_DIR" || ! chmod 0700 "$ATOP_STATE_DIR"; then
    atop_install_abort "无法创建配置目录：$ATOP_STATE_DIR。"
fi
if ! (
    umask 077
    printf '# ATOPWLB: 修改后约 30 秒内生效；充电时始终不关机。\nENABLED=1\nTHRESHOLD=%s\nINTERVAL=30\n' \
        "$ATOP_THRESHOLD" >"$ATOP_CONFIG_TEMP" &&
    chown 0:0 "$ATOP_CONFIG_TEMP" && chmod 0600 "$ATOP_CONFIG_TEMP" &&
    mv -f "$ATOP_CONFIG_TEMP" "$ATOP_CONFIG_FILE"
); then
    rm -f "$ATOP_CONFIG_TEMP"
    atop_install_abort "无法保存配置；已有配置不会被半写入的文件覆盖。"
fi

ui_print ""
ui_print "- 已选择：电量 ≤ $ATOP_THRESHOLD%，未充电时自动关机。"
ui_print "- 关机方式：Android 正常关机接口 svc power shutdown。"
ui_print "- 配置修改路径：$ATOP_CONFIG_FILE"
ui_print "  THRESHOLD=10 或 20；ENABLED=0 可停用。"
ui_print "- 日志路径：$ATOP_STATE_DIR/monitor.log"
ui_print "- 检测失败时停止监测，不触发关机。"
ui_print "- 重启手机后模块开始监测；安装过程不会关机。"
