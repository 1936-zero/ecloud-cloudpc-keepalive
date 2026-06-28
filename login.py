"""
登录流程 —— 复刻 service/user.js。

密码登录链路 (loginWithPassword line 277-356):
  POST /login/verify {username, password, timestamp, clientNeedTwoFactor:true}
    ├ 成功 → body.accessTicket → verifyAccessTicket
    ├ errorCode 30002009 (未授信设备) → 需短信 + trustDevice
    ├ errorCode 30002060 (二次验证) → 需短信 + verifyTwoFactorAuthSms
    ├ errorCode 30002063 (增强策略) → 需短信 + verifyLoginEnhanceSms
    └ userId 字段 → 4A MFA 流程

verifyAccessTicket (line 448-480):
  POST /login/verifyAccessTicket {accessTicket} → accessToken
"""
import time
import json

import config
from ecloud_client import EcloudHttpUtil, EcloudError


class LoginResult:
    SUCCESS = "success"
    NEED_DEVICE_TRUST = "need_device_trust"      # 30002009
    NEED_TWO_FACTOR = "need_two_factor"          # 30002060
    NEED_ENHANCED_SMS = "need_enhanced_sms"      # 30002063
    NEED_4A = "need_4a"
    FAILED = "failed"


def _extract_error(resp: dict) -> str:
    """Extract a useful server-side failure message from a decoded response."""
    if not isinstance(resp, dict):
        return str(resp)
    for key in (
        "errorMessage", "message", "msg", "resultMsg", "resultMessage",
        "returnMessage", "desc", "description",
    ):
        if resp.get(key):
            return str(resp[key])
    body = resp.get("body")
    if isinstance(body, dict):
        nested = _extract_error(body)
        if nested and nested != "{}":
            return nested
    return json.dumps(resp, ensure_ascii=False)[:500]


def login_with_password(http: EcloudHttpUtil, username: str, password: str) -> dict:
    """
    密码登录。返回 {"status": ..., "access_ticket": ..., "access_token": ..., ...}。

    若服务端要求短信验证/设备信任，返回 status=NEED_* 并带 mobile 字段，
    由调用方拿到短信后调 complete_login_with_sms() 继续流程。
    """
    try:
        resp = http.post(config.Endpoint.LOGIN_CHECK_USER_PASSWORD, {
            "username": username,
            "password": password,
            "timestamp": int(time.time() * 1000),
            "clientNeedTwoFactor": True,
        })
    except EcloudError as e:
        # 业务错误（未授信设备/二次验证等）—— errorCode 在异常里，需要解析分支
        return _classify_login_error(e, username)

    # 直接拿到 accessTicket（已授信设备 + 无需二次验证）
    ticket = resp.get("accessTicket") if isinstance(resp, dict) else None
    if ticket:
        token = _exchange_ticket(http, ticket)
        return {"status": LoginResult.SUCCESS, "access_ticket": ticket,
                "access_token": token, "user_info": resp}

    # 响应里没 ticket 也没异常，按错误分类处理
    if isinstance(resp, dict) and resp.get("errorCode"):
        return _classify_dict_error(resp, username)
    return {"status": LoginResult.SUCCESS, "access_token": None, "user_info": resp}


def _classify_login_error(e: EcloudError, username: str) -> dict:
    """把 EcloudError 转成分支状态。错误对象 resp 含 errorCode/errorMessage/body。"""
    resp = e.resp if isinstance(e.resp, dict) else {}
    code = str(resp.get("errorCode", "") or e.code or "")
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    if isinstance(body, str):
        try:
            import json
            body = json.loads(body)
        except Exception:
            body = {}
    mobile = body.get("mobile", "") if isinstance(body, dict) else ""
    login_code = body.get("code") if isinstance(body, dict) else None
    common = {
        "mobile": mobile,
        "login_code": login_code,
        "raw": resp,
        "error": e.message,
        "error_code": code,
    }

    if code == config.LoginError.UNTRUSTED_DEVICE:
        return {"status": LoginResult.NEED_DEVICE_TRUST, **common}
    if code == config.LoginError.TWO_FACTOR_AUTH:
        return {"status": LoginResult.NEED_TWO_FACTOR, **common}
    if code == config.LoginError.ENHANCED_STRATEGY:
        return {"status": LoginResult.NEED_ENHANCED_SMS, **common}
    if resp.get("userId"):
        return {"status": LoginResult.NEED_4A, "user_id": resp.get("userId"),
                "login_type": resp.get("loginType"), **common}
    return {"status": LoginResult.FAILED, **common}


def _classify_dict_error(resp: dict, username: str) -> dict:
    code = str(resp.get("errorCode", ""))
    body = resp.get("body", {}) if isinstance(resp.get("body"), dict) else {}
    mobile = body.get("mobile", "")
    common = {
        "mobile": mobile,
        "login_code": body.get("code"),
        "raw": resp,
        "error_code": code,
    }
    if code == config.LoginError.UNTRUSTED_DEVICE:
        return {"status": LoginResult.NEED_DEVICE_TRUST, **common}
    if code == config.LoginError.TWO_FACTOR_AUTH:
        return {"status": LoginResult.NEED_TWO_FACTOR, **common}
    if code == config.LoginError.ENHANCED_STRATEGY:
        return {"status": LoginResult.NEED_ENHANCED_SMS, **common}
    if resp.get("userId"):
        return {"status": LoginResult.NEED_4A, "user_id": resp.get("userId"),
                "login_type": resp.get("loginType"), **common}
    return {"status": LoginResult.FAILED, "error": resp.get("errorMessage"), **common}


def _exchange_ticket(http: EcloudHttpUtil, access_ticket: str) -> str | None:
    """
    accessTicket 换 accessToken (verifyAccessTicket line 448-480)。
    EcloudHttpUtil 内部会在 LOGIN_GET_TOKEN 分支自动 set_token。
    """
    body = http.post(config.Endpoint.LOGIN_GET_TOKEN, {"accessTicket": access_ticket})
    return body.get("accessToken") if isinstance(body, dict) else None


def send_sms(http: EcloudHttpUtil, mobile: str, code_type: str = "login") -> dict:
    """
    发送短信验证码 (sendSMSCode line 186-213)。
    code_type: "login" | "forget"
    """
    return http.post(config.Endpoint.LOGIN_SEND_SMS, {
        "mobile": mobile,
        "codeType": code_type,
    })


def verify_sms(http: EcloudHttpUtil, mobile: str, verification_code: str,
               code_type: str = "login") -> dict:
    """验证通用短信验证码，部分登录分支会返回后续接口需要的 code。"""
    return http.post(config.Endpoint.LOGIN_VERIFY_SMS, {
        "mobile": mobile,
        "verificationCode": verification_code,
        "codeType": code_type,
    })


def send_two_factor_sms(http: EcloudHttpUtil, mobile: str, username: str) -> dict:
    """发送二次验证短信。"""
    return http.post(config.Endpoint.LOGIN_AUTH_TWOFACTOR_GET, {
        "mobile": mobile,
        "userName": username,
    })


def complete_device_trust(http: EcloudHttpUtil, mobile: str,
                          verification_code: str, login_username: str = "",
                          is_temporary: bool = False, code: str | None = None) -> dict:
    """
    未授信设备 → 短信验证后信任/临时设备 (user.js loginTrustDevice line 647-697)。
    code 字段来自登录响应 body.code（line 296），trustDevice 需要带上它。
    """
    if not code:
        verify_resp = verify_sms(http, mobile, verification_code)
        if isinstance(verify_resp, dict):
            code = verify_resp.get("code") or verify_resp.get("verifyCode")
            ticket = verify_resp.get("accessTicket")
            if ticket:
                token = _exchange_ticket(http, ticket)
                return {"status": LoginResult.SUCCESS, "access_ticket": ticket,
                        "access_token": token}

    payload = {
        "mobile": mobile,
        "verificationCode": verification_code,
        "isNeedTemporaryDeviceSelection": True,
        "code": code,
    }
    if login_username:
        payload["loginUserName"] = login_username
    resp = http.post(config.Endpoint.LOGIN_TRUST_DEVICE, payload)
    ticket = resp.get("accessTicket") if isinstance(resp, dict) else None
    if not ticket:
        return {"status": LoginResult.FAILED, "raw": resp,
                "error": _extract_error(resp) or "信任设备后未返回 accessTicket"}

    is_temp_val = 1 if is_temporary else 0
    http.post(config.Endpoint.LOGIN_TEMPORARY_DEVICE, {
        "accessTicket": ticket, "isTemporary": is_temp_val,
    })
    token = _exchange_ticket(http, ticket)
    return {"status": LoginResult.SUCCESS, "access_ticket": ticket,
            "access_token": token}


def complete_two_factor(http: EcloudHttpUtil, mobile: str, username: str,
                        password: str, verification_code: str) -> dict:
    """
    二次验证短信验证。短信发送由调用方先调用 send_two_factor_sms 完成。
    """
    resp = http.post(config.Endpoint.LOGIN_AUTH_TWOFACTOR, {
        "mobile": mobile, "userName": username,
        "verificationCode": verification_code,
        "password": password,
    })
    ticket = resp.get("accessTicket") if isinstance(resp, dict) else None
    if ticket:
        token = _exchange_ticket(http, ticket)
        return {"status": LoginResult.SUCCESS, "access_ticket": ticket,
                "access_token": token}
    return {"status": LoginResult.FAILED, "raw": resp, "error": _extract_error(resp)}


def complete_enhanced_sms(http: EcloudHttpUtil, mobile: str, username: str,
                          verification_code: str) -> dict:
    """
    增强策略短信 (user.js:417 verifyLoginEnhanceSms)。
    """
    resp = http.post(config.Endpoint.LOGIN_ENHANCE_SMS, {
        "mobile": mobile,
        "verificationCode": verification_code,
        "userName": username,
    })
    ticket = resp.get("accessTicket") if isinstance(resp, dict) else None
    if ticket:
        token = _exchange_ticket(http, ticket)
        return {"status": LoginResult.SUCCESS, "access_ticket": ticket,
                "access_token": token}
    return {"status": LoginResult.FAILED, "raw": resp, "error": _extract_error(resp)}


def logout(http: EcloudHttpUtil) -> None:
    """登出 (user.js:235 logout)。"""
    try:
        http.post(config.Endpoint.LOGOUT)
    except EcloudError:
        pass
    finally:
        http.clear_token()


def get_user_info(http: EcloudHttpUtil) -> dict:
    """登录后拉取用户信息 (user.js:476 USER_GET_INFO)。"""
    return http.post(config.Endpoint.USER_GET_INFO)


def get_device_list(http: EcloudHttpUtil) -> dict:
    """
    获取云桌面列表 (USER_GET_DEVICE_INFO)。
    用于保活前确认有可用桌面，也用于阶段2拿到连接信息。
    """
    return http.post(config.Endpoint.USER_GET_DEVICE_INFO)
