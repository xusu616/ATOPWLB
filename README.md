# ATOPWLB：低电量正常关机模块

电量达到或低于安装时选择的 **20% 或 10%**，并且没有接入外部电源时，向 Android 请求正常关机。充电或接入 USB、交流电、无线充电时继续运行。使用共同的 Shell 模块接口，支持 Magisk 20.4+、KernelSU、KernelSU Next 管理器安装；不需要系统挂载、Zygisk、metamodule、APK 或额外 SELinux 规则。

## 安装

1. 用 `python build.py` 构建，或使用 `dist/atopwlb-1.0.0.zip`。
2. 手机正常开机后，在 Magisk、KernelSU 或 KernelSU Next 的模块页面从本地安装 ZIP。**不支持 Recovery 安装**，因为安装时必须读取当前 Android 电池与输入设备。
3. 在安装日志提示后，60 秒内按一次 **音量上键：20%**，或 **音量下键：10%**。上下键可以来自不同输入设备。松键、长按重复、触摸输入不会被当成选择。
4. 安装日志会显示实际电量节点、选定阈值、配置和日志路径。重启后开始监测；安装期间不会关机。

ZIP 保留官方 Magisk 安装入口及 `#MAGISK` 标记，以兼容旧版管理器；KernelSU/Next 使用其内置安装器。运行脚本继承管理器的 BusyBox ash 环境，避免旧 Android 缺少系统 `timeout` 命令时安装与运行能力不一致。

读取电池/供电状态失败、系统没有声明支持正常关机命令、没有可读音量键、没有选择或选择超时，均会拒绝安装，不偷偷使用默认阈值。安装成功后再原子保存配置；更新安装也会重新要求音量键选择。

## 配置修改路径

需要 root 权限：**`/data/adb/atopwlb/config.conf`**。

```ini
ENABLED=1
THRESHOLD=20
INTERVAL=30
```

- `ENABLED=1` 启用；`ENABLED=0` 暂停关机判断。
- `THRESHOLD=10` 或 `THRESHOLD=20`，表示电量百分比，包含等于阈值的情况。
- `INTERVAL=30` 表示每 30 秒检查一次；可设为 1～3600 的整数秒。短间隔会增加唤醒后运行时的检查次数，长间隔会延迟发现低电量。

只使用 `KEY=value`，不加引号、空格或行尾注释；空行和以 `#` 开头的整行注释可以保留。支持 LF、CRLF。未知项、重复项、非法数字和缺少必需项会停止本次启动的监测，并记录原因。配置按文本解析，不作为 Shell 代码执行。

**运行中的监测器每轮重新读取配置**，修改通常会在下一次检查时生效。建议用临时文件写好后原子替换，避免监测器读到编辑期间的不完整文件。如果已经因错误停止，修正后需要重启恢复。配置 `ENABLED=0` 时监测仍运行，因此可以通过改回 `1` 恢复判断。

在管理器禁用或卸载模块，运行中的监测器也会在下一轮停止。配置与诊断目录保留，便于重新安装和排查；模块卸载后没有独立启动项继续运行。

## 自动检测与当前手机

通过 `/sys/class/power_supply` 统一接口检测，而不是对所有包含 `battery` 的文件尝试写入：

1. 优先选择 `battery` 中有效的 `capacity` 和 `status`；然后查找其他 `type=Battery` 的可用供电设备。
2. Qualcomm 的 `bms/capacity` 仅作为补充来源，且必须同时保留主 `battery` 设备的有效身份和充电状态。只有 BMS、没有可用主电池状态时拒绝使用。
3. 电量必须为 0～100 的整数；如果存在 `present`，必须为 `1`。状态必须是 `Charging`、`Discharging`、`Full` 或 `Not charging`。
4. 每轮重新枚举外部供电设备的 `online` 和 `present`，任意一个为 `1` 均禁止关机，即使电池报告 `Discharging` 或暂停充电。当前 Qualcomm 驱动在弱充电器或暂停 USB 供电时可能 `online=0`、`present=1`，所以必须同时检查。排除 `Battery`/`BMS` 的这些属性，避免把燃料计误当成充电器。`Not charging` 又没有任何可读的外部 `online`/`present` 时无法可靠判断是否接电，停止监测。
5. 每次开机重新探测路径；运行中节点不可读、值非法或电池不在位，立即停止监测，本次启动不再发起关机。

2026-11-24 在当前 **Redmi 4X / santoni / Android 11（API 30）** 上只读验证得到：

```text
电量：/sys/class/power_supply/battery/capacity
状态：/sys/class/power_supply/battery/status
外接电源：/sys/class/power_supply/usb/online 和 present
```

`capacity=100`、`status=Charging`、`usb/online=1`，诊断结果为接电状态，应继续运行。`battery` 类路径是你列出的 `qpnp-smbcharger/.../battery` 实体目录的符号链接。`battery_data`、设备树的 `battery-type`、`battery_charging_enabled` 都不作为关机开关；模块不会写入这些节点。

## 正常关机与检测边界

使用 `/system/bin/svc power shutdown`，经 Android PowerManager 的正常关机流程处理系统服务与存储。每轮触发前立即重读配置和硬件状态，只发起一次请求。没有强制关机备用命令；不写 `sys.powerctl`、电池控制节点或 SysRq。

安装与只读诊断仅校验 `svc power` 帮助中声明支持 `shutdown`，**不会执行真实关机验证**。ROM 权限、SELinux 或 framework 实现仍可能导致实际请求失败。捕获调用输出和退出码；调用出错、超时或请求后系统仍运行时记录为 `inactive` 并结束监测。旧版 `svc` 在部分异常下也可能返回 0，因此不会把退出码 0 当成“已成功关机”。

模块等待 `sys.boot_completed=1`，随后周期性读取状态，不持有唤醒锁。深度休眠期间的检查可能延后；这是正常关机保护模块，不能代替电池硬件欠压保护。

## 诊断与测试

日志：`/data/adb/atopwlb/monitor.log`，单个日志超过 64 KiB 时保留上一份为 `monitor.log.1`。

状态：`/data/adb/atopwlb/status`，包含实际节点、电量、阈值、接电状态和停止原因。

关机命令输出：`/data/adb/atopwlb/shutdown.out`。

已安装后执行**只读诊断**，不会触发关机：

```sh
su -c '/system/bin/sh /data/adb/modules/atopwlb/monitor.sh --check'
```

仅模拟一次当前决策，不关机、不修改配置或状态文件：

```sh
su -c '/system/bin/sh /data/adb/modules/atopwlb/monitor.sh --once --dry-run'
```

开发测试使用模拟 sysfs、模拟关机命令和临时配置，不消耗电池或修改系统的真实电量。`--sysfs-root`、`--config`、`--svc` 自定义路径只允许用于 `--check` 或 `--dry-run`。运行测试和构建：

```sh
python3 tests/test_module.py --shell /bin/sh
python3 tests/test_installer.py --shell /bin/sh
python3 build.py
```

## 一手资料

- [Magisk 模块指南](https://topjohnwu.github.io/Magisk/guides.html)
- [KernelSU 模块指南](https://kernelsu.org/guide/module.html)
- [KernelSU Next 安装器源码](https://github.com/KernelSU-Next/KernelSU-Next/blob/dev/userspace/ksud/src/installer.sh)
- [AOSP Android 11 PowerCommand](https://android.googlesource.com/platform/frameworks/base/+/android-11.0.0_r1/cmds/svc/src/com/android/commands/svc/PowerCommand.java)
- [AOSP Android 11 ShutdownThread](https://android.googlesource.com/platform/frameworks/base/+/android-11.0.0_r1/services/core/java/com/android/server/power/ShutdownThread.java)
- [LineageOS Qualcomm 充电驱动](https://github.com/LineageOS/android_kernel_xiaomi_msm8937/blob/lineage-19.1/drivers/power/supply/qcom/qpnp-smbcharger.c)

自行编写的模块代码采用 MIT License。随附的官方 Magisk 安装入口采用 GPLv3，许可证见 `LICENSE.magisk`，来源和原文校验信息见 `THIRD_PARTY_NOTICES.md`。
