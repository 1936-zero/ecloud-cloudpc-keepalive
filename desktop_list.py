"""
桌面列表拉取 —— 自动获取 instanceId/machineId，实现全自动保活。

逆向自渲染层 bundle (index-53f3f1a5.js):
  POST /user/getDeviceInfo {accessToken, companyCode:ECloud, allCompany:true, version:1.0.0}
    -> body.machineList[]  每项含 {machineId, instanceId, machineName, originCompanyCode, ...}

  POST /user/getDesktopStatus {accessToken, instanceIdList:[...]}
    -> body.machineStatusList[]  每项含 {machineId, instanceId, resourceStatus, ...}

保活只需 instanceId（desktopUptime 接口），ticket/machineConnect 只在模拟"新连接"时才需要，
纯保活场景无需 ticket。
"""
import logging
from dataclasses import dataclass

import config
from ecloud_client import EcloudHttpUtil, EcloudError

log = logging.getLogger("desktop_list")


@dataclass
class Desktop:
    """一个云电脑桌面。"""
    instance_id: str          # CCA-xxx，desktopUptime 必需
    machine_id: str           # UUID，machineConnect 用
    machine_name: str = ""
    origin_company_code: str = ""   # CMSSZTE / ZTE / H3C / Inspur
    resource_pool_uid: str = ""
    status: str = ""          # 来自 getDesktopStatus

    def __repr__(self):
        return (f"Desktop(instance={self.instance_id[:20]}..., "
                f"name={self.machine_name}, status={self.status})")


def get_desktop_list(http: EcloudHttpUtil) -> list[Desktop]:
    """
    拉取桌面列表 (复刻 index-53f3f1a5.js 的 St 函数)。
    返回 Desktop 列表。失败抛 EcloudError。
    """
    resp = http.post(config.Endpoint.GET_DEVICE_INFO, {
        "companyCode": config.COMPANY_CODE,
        "allCompany": True,
        "version": "1.0.0",
    })
    # resp 是已解密的 body（dict）
    machine_list = resp.get("machineList", []) if isinstance(resp, dict) else []
    desktops = []
    for m in machine_list:
        if not isinstance(m, dict):
            continue
        d = Desktop(
            instance_id=m.get("instanceId", ""),
            machine_id=m.get("machineId", ""),
            machine_name=m.get("machineName", ""),
            origin_company_code=m.get("originCompanyCode", ""),
            resource_pool_uid=m.get("resourcePoolUid", ""),
        )
        if d.instance_id or d.machine_id:
            desktops.append(d)
    log.info("拉取到 %d 个桌面", len(desktops))
    return desktops


def get_desktop_status(http: EcloudHttpUtil, desktops: list[Desktop]) -> dict[str, str]:
    """
    查询桌面运行状态 (复刻 getDesktopStatus 调用)。
    返回 {instanceId: resourceStatus} 映射。
    """
    if not desktops:
        return {}
    instance_ids = [d.instance_id for d in desktops if d.instance_id]
    if not instance_ids:
        return {}
    resp = http.post(config.Endpoint.GET_DESKTOP_STATUS, {
        "instanceIdList": instance_ids,
    })
    status_list = resp.get("machineStatusList", []) if isinstance(resp, dict) else []
    result = {}
    for s in status_list:
        if isinstance(s, dict):
            iid = s.get("instanceId", "")
            result[iid] = s.get("resourceStatus", "")
    log.info("桌面状态: %s", result)
    return result


def operate_desktop(http: EcloudHttpUtil, machine_id: str, machine_name: str,
                    operate: str, resource_pool_uid: str = "") -> dict:
    """
    桌面操作：开机/关机/重启。
    :param operate: "startup" | "shutdown" | "restart"
    复刻 Z2 函数 (index-53f3f1a5.js)。
    """
    import os
    sdk_type = 5 if os.uname().sysname == "Darwin" else 4
    return http.post(config.Endpoint.RESOURCE_OPERATE, {
        "machineId": machine_id,
        "machineName": machine_name,
        "operate": operate,
        "deviceUid": http.common_params.get("deviceUid", ""),
        "resourcePoolUid": resource_pool_uid,
        "sdkType": sdk_type,
    })


def select_running_desktop(http: EcloudHttpUtil) -> Desktop | None:
    """
    自动选一个正在运行的桌面用于保活。
    优先选 resourceStatus 表示"运行中"的桌面。
    """
    desktops = get_desktop_list(http)
    if not desktops:
        log.warning("没有可用桌面")
        return None
    if len(desktops) == 1:
        log.info("唯一桌面: %s", desktops[0])
        return desktops[0]

    # 多个桌面，查状态选运行中的
    try:
        statuses = get_desktop_status(http, desktops)
        for d in desktops:
            st = statuses.get(d.instance_id, "")
            d.status = st
            # resourceStatus 运行中的值（需抓包确认，常见: running/active/1）
            if st and st.lower() in ("running", "active", "1", "on", "up"):
                log.info("选中运行中的桌面: %s (status=%s)", d, st)
                return d
    except EcloudError as e:
        log.warning("查询桌面状态失败（忽略）: %s", e)

    # 找不到明确运行中的，返回第一个
    log.info("无法确定运行状态，默认选第一个: %s", desktops[0])
    return desktops[0]
