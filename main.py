"""
Ecloud Cloud Computer V3.8.2 keepalive tool - CLI entry.

Usage:
  python main.py login          interactive login, save cloud_pc.json
  python main.py keepalive      keepalive loop (default 300s, unlimited)
  python main.py keepalive --interval 300 --rounds 100
  python main.py status         check token validity
  python main.py logout         logout and clear config

cloud_pc.json schema:
{
  "username": "...",
  "password": "...",
  "device_uid": "...",
  "access_token": "...",
  "device_info": {...}
}
"""
import argparse
import getpass
import json
import logging
import os
import sys
import traceback

import config
import device
import keepalive
import login
from ecloud_client import EcloudHttpUtil, EcloudError

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_pc.json")

log = logging.getLogger("cloudpc")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def build_client(cfg: dict) -> EcloudHttpUtil:
    """Build a client injected with device fingerprint and token from config."""
    dev_info = cfg.get("device_info")
    if dev_info:
        from dataclasses import fields
        dev = device.DeviceInfo(**{
            f.name: dev_info[f.name] for f in fields(device.DeviceInfo)
        })
    else:
        dev = device.detect(device_uid=cfg.get("device_uid"))
        cfg["device_info"] = {
            "device_uid": dev.device_uid,
            "device_name": dev.device_name,
            "client_type": dev.client_type,
            "client_version": dev.client_version,
            "device_company": dev.device_company,
            "device_model": dev.device_model,
            "operating_system": dev.operating_system,
            "device_system": dev.device_system,
            "operating_version": dev.operating_version,
            "cores": dev.cores,
            "processor": dev.processor,
            "system_architecture": dev.system_architecture,
            "disk_total": dev.disk_total,
            "disk_used": dev.disk_used,
            "ram": dev.ram,
            "ip_address": dev.ip_address,
            "mac_address": dev.mac_address,
        }
        cfg["device_uid"] = dev.device_uid

    client = EcloudHttpUtil(dev.to_common_params())
    if cfg.get("access_token"):
        client.set_token(cfg["access_token"])
    return client


def do_login(cfg: dict):
    """Run full login flow (with SMS branch interaction). Returns access_token or None."""
    client = build_client(cfg)

    username = cfg.get("username") or input("account: ").strip()
    password = cfg.get("password")
    if not password:
        password = getpass.getpass("password: ")
    cfg["username"], cfg["password"] = username, password

    log.info("login %s ...", username)
    result = login.login_with_password(client, username, password)

    if result["status"] == login.LoginResult.SUCCESS:
        log.info("OK: login success")
        token = result["access_token"]
        cfg["access_token"] = token
        save_config(cfg)
        try:
            info = login.get_user_info(client)
            log.info("user info: %s", info)
        except EcloudError as e:
            log.warning("get user info failed: %s", e)
        return token

    status = result["status"]
    if status == login.LoginResult.NEED_DEVICE_TRUST:
        log.info("need device trust. mobile: %s", result.get("mobile"))
        mobile = result.get("mobile") or input("mobile: ").strip()
        login.send_sms(client, mobile)
        code = input("sms code: ").strip()
        r = login.complete_device_trust(client, mobile, code, username)
        if r["status"] == login.LoginResult.SUCCESS:
            cfg["access_token"] = r["access_token"]
            save_config(cfg)
            log.info("OK: device trusted, login success")
            return r["access_token"]
    elif status == login.LoginResult.NEED_TWO_FACTOR:
        log.info("need two-factor. mobile: %s", result.get("mobile"))
        mobile = result.get("mobile") or input("mobile: ").strip()
        code = input("two-factor sms code: ").strip()
        r = login.complete_two_factor(client, mobile, username, password, code)
        if r["status"] == login.LoginResult.SUCCESS:
            cfg["access_token"] = r["access_token"]
            save_config(cfg)
            log.info("OK: two-factor passed, login success")
            return r["access_token"]
    elif status == login.LoginResult.NEED_ENHANCED_SMS:
        log.info("need enhanced-strategy sms. mobile: %s", result.get("mobile"))
        mobile = result.get("mobile") or input("mobile: ").strip()
        code = input("enhanced sms code: ").strip()
        r = login.complete_enhanced_sms(client, mobile, username, code)
        if r["status"] == login.LoginResult.SUCCESS:
            cfg["access_token"] = r["access_token"]
            save_config(cfg)
            log.info("OK: enhanced sms passed, login success")
            return r["access_token"]
    elif status == login.LoginResult.NEED_4A:
        log.error("need 4A MFA (userId=%s), not supported, use another method",
                  result.get("user_id"))
    else:
        log.error("login failed: %s", result.get("error", result.get("raw")))

    return None


def cmd_login(args):
    cfg = load_config()
    token = do_login(cfg)
    if token:
        log.info("token: %s...%s", token[:8], token[-6:])
    sys.exit(0 if token else 1)


def cmd_keepalive(args):
    cfg = load_config()
    if not cfg.get("access_token"):
        log.info("no saved token, login first")
        if not do_login(cfg):
            sys.exit(1)

    client = build_client(cfg)

    def _relogin():
        log.info("relogin %s", cfg.get("username"))
        t = do_login(cfg)
        if t:
            client.set_token(t)
        return t

    keepalive.run_keepalive_loop(
        client,
        relogin_fn=_relogin,
        interval=args.interval,
        max_rounds=args.rounds,
    )


def cmd_status(args):
    cfg = load_config()
    if not cfg.get("access_token"):
        log.info("not logged in (no token)")
        sys.exit(1)
    client = build_client(cfg)
    try:
        info = login.get_user_info(client)
        log.info("OK: token valid. user info: %s", info)
    except EcloudError as e:
        log.error("FAIL: token invalid: %s", e)
        sys.exit(1)


def cmd_logout(args):
    cfg = load_config()
    if cfg.get("access_token"):
        try:
            client = build_client(cfg)
            login.logout(client)
        except Exception as e:
            log.warning("logout request failed (ignored): %s", e)
    cfg.pop("access_token", None)
    save_config(cfg)
    log.info("logged out, token cleared")


def cmd_desktop_keepalive(args):
    """桌面会话保活（基于抓包逆向的 desktopUptime 接口）。"""
    import desktop_session
    import desktop_list

    cfg = load_config()
    # 确保已登录
    if not cfg.get("access_token"):
        log.info("no saved token, login first")
        if not do_login(cfg):
            sys.exit(1)

    client = build_client(cfg)

    def _relogin():
        log.info("relogin %s", cfg.get("username"))
        t = do_login(cfg)
        if t:
            client.set_token(t)
        return t

    # 桌面选择：优先命令行参数，其次配置缓存，最后自动拉列表（全自动）
    instance_id = args.instance_id
    machine_id = args.machine_id or ""
    ticket = args.ticket or ""

    if not instance_id:
        instance_id = cfg.get("instance_id")
        machine_id = machine_id or cfg.get("machine_id", "")
        ticket = ticket or cfg.get("ticket", "")

    if not instance_id and not args.no_auto_select:
        # 全自动：拉桌面列表，选一个
        log.info("auto-selecting desktop from /user/getDeviceInfo ...")
        try:
            desktop = desktop_list.select_running_desktop(client)
        except EcloudError as e:
            log.error("拉取桌面列表失败: %s", e)
            if _token_maybe_expired(e):
                log.info("token 可能失效，尝试重新登录...")
                if _relogin():
                    desktop = desktop_list.select_running_desktop(client)
                else:
                    sys.exit(1)
            else:
                sys.exit(1)
        if desktop is None:
            log.error("账号下没有可用桌面。请先在官方客户端创建/开机桌面。")
            sys.exit(1)
        instance_id = desktop.instance_id
        machine_id = desktop.machine_id
        log.info("auto-selected: %s", desktop)

    if not instance_id:
        log.error("need instance_id. Options:")
        log.error("  1. python main.py desktop-keepalive --instance-id CCA-xxxx")
        log.error("  2. python main.py desktop-keepalive  (auto-select, needs valid token)")
        sys.exit(1)

    log.info("desktop keepalive: instance=%s", instance_id)
    if machine_id:
        log.info("  machine_id=%s", machine_id)
    if ticket:
        log.info("  ticket=%s...", ticket[:30])

    # 保存桌面凭证供下次使用
    cfg["instance_id"] = instance_id
    if machine_id:
        cfg["machine_id"] = machine_id
    if ticket:
        cfg["ticket"] = ticket
    save_config(cfg)

    desktop_session.run_desktop_keepalive(
        client,
        instance_id=instance_id,
        machine_id=machine_id,
        ticket=ticket,
        interval=args.interval,
        max_rounds=args.rounds,
        relogin_fn=_relogin,
    )


def _token_maybe_expired(err: EcloudError) -> bool:
    msg = (err.message or "").lower()
    return any(h in msg for h in ["token", "失效", "未登录", "expire", "401", "授权"])


def cmd_list_desktops(args):
    """列出可用云电脑。"""
    cfg = load_config()
    if not cfg.get("access_token"):
        log.info("no saved token, login first")
        if not do_login(cfg):
            sys.exit(1)
    client = build_client(cfg)
    import desktop_list
    try:
        desktops = desktop_list.get_desktop_list(client)
        if not desktops:
            log.info("no desktops found")
            return
        # 查状态
        try:
            statuses = desktop_list.get_desktop_status(client, desktops)
            for d in desktops:
                d.status = statuses.get(d.instance_id, "?")
        except EcloudError:
            pass
        log.info("found %d desktop(s):", len(desktops))
        for i, d in enumerate(desktops):
            print(f"  [{i}] instance={d.instance_id}")
            print(f"      machine={d.machine_id}")
            print(f"      name={d.machine_name}, vendor={d.origin_company_code}, status={d.status}")
    except EcloudError as e:
        log.error("failed: %s", e)
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(
        description="Ecloud Cloud Computer V3.8.2 keepalive tool",
    )
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="verbose (-vv for debug)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="interactive login, save config")
    ka = sub.add_parser("keepalive", help="account keepalive loop (HTTP)")
    ka.add_argument("--interval", type=int, default=300,
                    help="interval seconds (default 300)")
    ka.add_argument("--rounds", type=int, default=None,
                    help="max rounds (default unlimited)")

    # 桌面会话保活（基于抓包逆向）
    dka = sub.add_parser("desktop-keepalive", help="desktop session keepalive (desktopUptime)")
    dka.add_argument("--instance-id", help="desktop instance ID (CCA-xxxx); omit to auto-select")
    dka.add_argument("--machine-id", help="desktop machine ID (UUID), optional")
    dka.add_argument("--ticket", help="session ticket (ticket:xxxx), optional")
    dka.add_argument("--interval", type=int, default=300,
                     help="interval seconds (default 300)")
    dka.add_argument("--rounds", type=int, default=None,
                     help="max rounds (default unlimited)")
    dka.add_argument("--no-auto-select", action="store_true",
                     help="disable auto desktop selection (require --instance-id)")

    sub.add_parser("list-desktops", help="try to fetch desktop list")
    sub.add_parser("status", help="check token validity")
    sub.add_parser("logout", help="logout and clear config")

    args = p.parse_args()
    level = logging.DEBUG if args.verbose >= 2 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        {"login": cmd_login, "keepalive": cmd_keepalive,
         "desktop-keepalive": cmd_desktop_keepalive,
         "list-desktops": cmd_list_desktops,
         "status": cmd_status, "logout": cmd_logout}[args.cmd](args)
    except KeyboardInterrupt:
        log.info("interrupted")
    except EcloudError as e:
        log.error("api error: %s", e)
        sys.exit(1)
    except Exception:
        log.error("unexpected error:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
