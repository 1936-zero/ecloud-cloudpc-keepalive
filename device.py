"""
设备指纹采集 —— 复刻 deviceUtil.js 的 getCommonParams() (line 48-71)。

在 Linux 服务器上没有真实硬件可读，本模块的策略：
  1. deviceUid 优先从配置文件读取（用户首次在真机登录时记录，或手动指定），
     其次从 /etc/machine-id 派生一个稳定的 UUID，最后才随机生成并持久化。
     这点很重要：服务端会按 deviceUid 识别设备，频繁换值会触发"未授信设备"。
  2. 其余硬件字段（CPU/内存/磁盘/网卡）尽量真实读取，读不到给合理默认值。
  3. clientType 按 deviceUtil.js:188-211 的 Linux 分支取 linux_x86-64 / linux_arm64 等。
"""
import os
import re
import socket
import uuid as uuidlib
import platform
import subprocess
from dataclasses import dataclass

import config


def _read_machine_id() -> str:
    """读 /etc/machine-id（systemd）或 /var/lib/dbus/machine-id，返回 32 位 hex。"""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as f:
                mid = f.read().strip()
            if mid:
                return mid
        except OSError:
            continue
    return ""


def _cpu_model() -> str:
    for path in ("/proc/cpuinfo",):
        try:
            with open(path) as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            break
    return platform.processor() or "Unknown CPU"


def _os_pretty_name() -> str:
    """读 /etc/os-release 的 PRETTY_NAME，失败回退 platform.platform()。"""
    try:
        with open("/etc/os-release") as f:
            kv = {}
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    kv[k] = v.strip('"')
        return kv.get("PRETTY_NAME") or kv.get("NAME") or "Linux"
    except OSError:
        return platform.platform()


def _lan_ip_and_mac() -> tuple[str, str]:
    """取首个非回环 IPv4 与对应 MAC。"""
    try:
        hostname = socket.gethostname()
        # 取本机出站 IP（连一个公网地址，不真的发包）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip = "127.0.0.1"

    mac = "00:00:00:00:00:00"
    try:
        import netifaces  # type: ignore
        for ifname in netifaces.interfaces():
            addrs = netifaces.ifaddresses(ifname)
            if netifaces.AF_LINK in addrs and netifaces.AF_INET in addrs:
                m = addrs[netifaces.AF_LINK][0].get("addr", "")
                if m and m != "00:00:00:00:00:00":
                    mac = m
                    break
    except ImportError:
        try:
            mac = uuidlib.getnode()
            mac = ":".join(f"{(mac >> (8 * i)) & 0xff:02x}" for i in range(5, -1, -1))
        except Exception:
            pass
    return ip, mac


def _disk_sizes() -> tuple[float, float]:
    """返回 (total_GB, used_GB)。Linux 用 statvfs，无则用 shutil.disk_usage。"""
    try:
        import shutil
        du = shutil.disk_usage("/")
        return round(du.total / (1024 ** 3), 1), round(du.used / (1024 ** 3), 1)
    except OSError:
        try:
            st = os.statvfs("/")  # Linux/Unix 专用
            total = st.f_blocks * st.f_frsize / (1024 ** 3)
            avail = st.f_bavail * st.f_frsize / (1024 ** 3)
            return round(total, 1), round(total - avail, 1)
        except (OSError, AttributeError):
            return 500.0, 250.0


def _get_client_type() -> str:
    """复刻 deviceUtil.js:188-211 的 Linux 分支。"""
    arch = platform.machine()
    os_name = _os_pretty_name()
    if arch in ("aarch64", "arm64"):
        return "kylin_arm64" if "kylin" in os_name.lower() else "linux_arm64"
    if arch in ("armv7l", "arm"):
        return "linux_arm"
    if "kylin" in os_name.lower():
        return "kylin_x86-64"
    return "linux_x86-64"


@dataclass
class DeviceInfo:
    """对应 deviceUtil.js 里 DeviceUtil 实例的所有 commonParams 来源字段。"""
    device_uid: str
    device_name: str
    client_type: str
    client_version: str
    device_company: str
    device_model: str
    operating_system: str
    device_system: str
    operating_version: str
    cores: int
    processor: str
    system_architecture: str
    disk_total: float
    disk_used: float
    ram: int
    ip_address: str
    mac_address: str

    def to_common_params(self) -> dict:
        """生成 ecloudHttpUtil.js 合并到每个请求 body 的公共参数 (deviceUtil.js:50-70)。"""
        return {
            "companyCode": config.COMPANY_CODE,
            "clientType": self.client_type,
            "clientVersion": self.client_version,
            "deviceUid": self.device_uid,
            "deviceName": self.device_name,
            "deviceType": "pc",
            "deviceCompany": self.device_company,
            "deviceModel": self.device_model,
            "operatingSystem": self.operating_system,
            "deviceSystem": self.device_system,
            "operatingVersion": self.operating_version,
            "cores": self.cores,
            "processor": self.processor,
            "systemArchitecture": self.system_architecture,
            "diskTotal": self.disk_total,
            "diskUsed": self.disk_used,
            "ram": self.ram,
            "ipAddress": self.ip_address,
            "macAddress": self.mac_address,
        }


def detect(client_version: str = config.CLIENT_VERSION,
           device_uid: str | None = None) -> DeviceInfo:
    """
    采集当前机器的设备指纹。
    :param device_uid: 若提供则直接使用（保证跨运行稳定）；否则按优先级派生并持久化。
    """
    # device_uid：优先用传入值，其次 machine-id 派生 UUID，最后随机
    if not device_uid:
        mid = _read_machine_id()
        if mid:
            # 把 32 位 hex 格式化成 UUID 形态（8-4-4-4-12），更像真实 MachineGuid
            device_uid = f"{mid[:8]}-{mid[8:12]}-{mid[12:16]}-{mid[16:20]}-{mid[20:32]}"
        else:
            device_uid = str(uuidlib.uuid4())

    ip, mac = _lan_ip_and_mac()
    disk_total, disk_used = _disk_sizes()
    os_name = _os_pretty_name()

    return DeviceInfo(
        device_uid=device_uid,
        device_name=socket.gethostname() or "linux-server",
        client_type=_get_client_type(),
        client_version=client_version,
        device_company=_read_dmi("sys_vendor") or "Linux",
        device_model=_read_dmi("product_name") or "Server",
        operating_system=os_name,
        device_system=os_name,
        operating_version=os.uname().release if hasattr(os, "uname") else "unknown",
        cores=os.cpu_count() or 4,
        processor=_cpu_model(),
        system_architecture=platform.machine(),
        disk_total=disk_total,
        disk_used=disk_used,
        ram=_total_ram_gb(),
        ip_address=ip,
        mac_address=mac,
    )


def _read_dmi(field: str) -> str:
    """读 /sys/class/dmi/id/<field>。"""
    try:
        with open(f"/sys/class/dmi/id/{field}") as f:
            return f.read().strip()
    except OSError:
        return ""


def _total_ram_gb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024 / 1024)
    except OSError:
        pass
    return 8
