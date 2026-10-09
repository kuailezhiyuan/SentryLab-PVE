# SentryLab-PVE：Debian 温度监控版

将 PVE 宿主机的 CPU／硬盘温度通过 MQTT 推送到 Home Assistant。
这是 [CmPi/SentryLab-PVE](https://github.com/CmPi/SentryLab-PVE) 的 fork，
增加标准 `.deb` 包、可靠的安装/升级/卸载流程，以及温度专用采集器。

支持 Debian 12/13（PVE 8/9 的系统基础），包架构为 `all`，无需 pip、Docker
或额外 Web 面板。Docker 只用于开发测试，运行时不需要。

## 快速安装

在 **PVE 宿主机的 root 终端** 上操作。以下命令以 root 运行；非 root 用户需要加 `sudo`。Home Assistant 应已配置 MQTT 集成，并且宿主机能连接 MQTT broker。

从 [Releases](https://github.com/kuailezhiyuan/SentryLab-PVE/releases) 下载 `.deb`，或运行：

```sh
curl -fLO https://github.com/kuailezhiyuan/SentryLab-PVE/releases/download/v1.1.1/sentrylab-pve_1.1.1-1_all.deb
apt install ./sentrylab-pve_1.1.1-1_all.deb
sentrylab configure
```

配置向导询问 MQTT 地址、端口、用户名、密码和 TLS。密码输入不回显，保存为
`/etc/sentrylab/sentrylab.conf`，权限 `0600`。匿名 broker 可留空用户名/密码。
向导启用开机运行并立即采集一次；Home Assistant 会自动发现一个宿主机设备和温度实体。

安装器不询问密码，不自动加载内核模块。未完成 MQTT 配置时，服务条件会跳过采集，
不会向示例地址发送数据。

## 温度支持

| 来源 | 默认 | 采集方式 |
| --- | --- | --- |
| Intel CPU | 开启 | `coretemp` hwmon，包括多 CPU/多核心 |
| AMD CPU | 开启 | `k10temp` / `zenpower`，包括 Tctl/Tdie |
| NVMe | 开启 | hwmon 温度、硬件型号和序列号 |
| 有 `drivetemp` hwmon 的硬盘 | 开启 | 读取已有 hwmon 温度 |
| SATA/SAS/USB SMART 温度 | 关闭，可选 | `smartctl -j -A -n standby,0` |

硬盘实体名称包含硬件型号；SATA 同时标明设备号，例如
`Disk SAMSUNG MZ7KM480HMHQ-000MV (sdc)`。型号优先读取已有 SMART 数据或
`lsblk` 的缓存信息，无需增加磁盘查询。显示名称更新保留已有 MQTT 实体标识和历史。

只读取内核已经提供的传感器，不执行 `sensors-detect`、`modprobe`、硬盘自检、
ZFS scrub、PVE 配置修改或主机重启。某个传感器暂时读不到不会中断其他温度采集。
没有硬件传感器的虚拟机/LXC 不会凭空获得宿主机温度。

检查本机可读取的温度（不连接 MQTT）：

```sh
sentrylab check
```

需要 SATA SMART 温度时：

```sh
apt install smartmontools
sudoedit /etc/sentrylab/sentrylab.conf
# 在 [sensors] 下设置 smart = true
systemctl start sentrylab-pve.service
```

SMART 查询跳过 standby 状态硬盘；部分 USB 桥无法可靠报告电源状态，不能保证所有
桥接设备都不被唤醒。`smartmontools` 的安装可能另行启用其自带的 `smartd` 服务，
SentryLab 不管理该服务。仅需 CPU/NVMe/hwmon 温度时无需安装它。

## 服务、配置和日志

默认每分钟运行一次，开机约两分钟后开始。定时器和服务名称固定，不扫描或改动其他服务。

```sh
sentrylab status
sentrylab disable     # 停止当前采集，并关闭开机运行
sentrylab enable      # 恢复开机运行
systemctl start sentrylab-pve.service  # 手动采集并推送一次
journalctl -u sentrylab-pve.service -n 50 --no-pager
```

`/etc/sentrylab/sentrylab.conf` 是 INI 文件，不执行 shell。支持自定义 `host_id`、
MQTT topic/discovery 前缀、TLS 和私有 CA。多台 PVE 应使用不同且稳定的 `host_id`。
更改 broker 前可先运行 `sentrylab cleanup`，清理旧 broker 上的发现实体。

| 位置 | 用途 |
| --- | --- |
| `/usr/bin/sentrylab` | 命令行采集器 |
| `/etc/sentrylab/sentrylab.conf` | MQTT 和传感器配置 |
| `/var/lib/sentrylab/` | 本机已发布的发现 topic 清单和锁，目录权限 `0700` |
| systemd 的 vendor unit 目录 | `sentrylab-pve.service`、`sentrylab-pve.timer` |

服务以 root 运行，以便按需读取 SMART；使用只读系统目录、独立写入目录及 systemd
保护选项。运行时依赖 `python3`、`python3-paho-mqtt`、`lm-sensors`，不安装 MQTT broker。
`smartmontools` 仅列为可选依赖，默认不会被本包拉入。

## Home Assistant 与 MQTT

默认 discovery topic：

```text
homeassistant/sensor/sentrylab_<host>/<sensor_key>/config
```

每个传感器有独立状态 topic，CPU 和硬盘状态不会互相覆盖：

```text
sentrylab/sentrylab_<host>/<sensor_key>/state
```

发现配置保留（retained），温度状态不保留。每次采集重新发布发现配置，所以 HA 或
broker 重启后可恢复。默认五分钟没有新温度即显示不可用，避免长期显示旧数据。

此 `.deb` 专注 CPU/硬盘温度。上游 shell 采集器、ZFS/磨损/健康/CSV/ESPHome 示例
保留在仓库中供参考，但不包含在 `.deb` 中，也不随安装启动。
旧 ESPHome 示例使用上游 topic，不能直接读取此版本的温度 topic。
上游使用说明见 [docs/UPSTREAM.md](docs/UPSTREAM.md)。旧脚本安装不会自动迁移；
如曾安装上游版本，应先按上游流程停用它，避免重复采集。

## 升级和卸载

安装新 `.deb` 即升级，Debian 会保留编辑过的配置。手动关闭的定时器不会被升级重新启用。

```sh
apt install ./sentrylab-pve_新版_all.deb
apt remove sentrylab-pve   # 停止服务、删除程序；保留配置和运行数据
apt purge sentrylab-pve    # 同时删除配置和运行数据
```

卸载尝试删除本机记录的 retained discovery 配置，不使用 MQTT 通配符。
broker 离线、认证失败或不可达时，清理失败会给出提示，但不会阻止卸载；
这种情况下 broker 中的残留实体需要恢复连接后清理。
卸载不会删除其他软件依赖、其他服务或 PVE 配置，也不会替你删除下载的 `.deb`。

## 开发、打包和验证

在 Debian 上安装构建工具：

```sh
apt install build-essential debhelper dpkg-dev fakeroot python3 python3-paho-mqtt
sh tools/build-deb.sh
```

输出在 `dist/`，同时生成 `SHA256SUMS`。或在本机 Docker 中构建/测试：

```sh
docker build -t sentrylab-deb-test -f tools/Dockerfile .
docker run --rm -v "$PWD:/workspace" sentrylab-deb-test sh tools/build-deb.sh
docker run --rm -v "$PWD:/workspace:ro" sentrylab-deb-test \
  sh tests/package-lifecycle.sh /workspace/dist/sentrylab-pve_1.1.1-1_all.deb
```

测试覆盖 Intel/AMD/NVMe/SATA 读取、稳定实体 ID、MQTT 实际发送和保留消息清理、
密码特殊字符、并发锁、安装、升级保留配置、保留禁用状态、离线卸载、purge 清理及
其他服务文件权限不变。生命周期脚本只能在一次性测试容器中运行。

GitHub Actions 构建 `.deb`，并在 Debian 12 和 13 上测试；推送与包版本相符的
`v*` 标签时发布经过测试的 Release 及校验文件。上游 MIT 许可证保留。
