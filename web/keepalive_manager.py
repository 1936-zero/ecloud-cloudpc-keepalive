"""
保活后台线程管理器。

用 threading.Event 实现可控启停，用 collections.deque 缓存最近日志供前端轮询。
不改动 desktop_session.py 原有代码，而是直接调 DesktopSession.keepalive_once()。
"""
import threading
import time
import logging
from collections import deque
from datetime import datetime

import config
import keepalive as account_keepalive
import desktop_session
from ecloud_client import EcloudHttpUtil, EcloudError


class AccountKeepaliveManager:
    """管理账号登录态保活线程，对应 `python main.py keepalive`。"""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._interval = 300
        self._rounds = 0
        self._last_error = ""
        self._started_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._consecutive_errors = 0
        self._log_seq = 0
        self._logs: deque[dict] = deque(maxlen=200)

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict:
        with self._lock:
            if not self._running:
                health = "stopped"
            elif self._last_error:
                health = "error"
            elif self._last_success_at:
                health = "ok"
            else:
                health = "starting"
            return {
                "running": self._running,
                "health": health,
                "interval": self._interval,
                "rounds": self._rounds,
                "last_error": self._last_error,
                "consecutive_errors": self._consecutive_errors,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            }

    def get_logs(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [log for log in self._logs if log["seq"] > since]

    def _log(self, level: str, msg: str):
        with self._lock:
            self._log_seq = max(self._log_seq + 1, int(time.time() * 1000))
            entry = {
                "seq": self._log_seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "msg": msg,
            }
            self._logs.append(entry)

    def _record_success(self):
        with self._lock:
            self._last_error = ""
            self._last_success_at = datetime.now()
            self._consecutive_errors = 0

    def _record_error(self, msg: str):
        with self._lock:
            self._last_error = msg
            self._consecutive_errors += 1

    def start(self, http: EcloudHttpUtil, interval: int = 300, relogin_fn=None) -> bool:
        """启动账号登录态保活线程。已在运行则返回 False。"""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._interval = interval
            self._rounds = 0
            self._last_error = ""
            self._last_success_at = None
            self._consecutive_errors = 0
            self._started_at = datetime.now()
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run,
            args=(http, interval, relogin_fn),
            daemon=True,
            name="account-keepalive",
        )
        self._thread.start()
        self._log("INFO", f"账号保活已启动: interval={interval}s")
        return True

    def stop(self) -> bool:
        """停止账号登录态保活线程。未运行则返回 False。"""
        if not self._running:
            return False
        self._stop_event.set()
        self._log("INFO", "正在停止账号保活...")
        if self._thread:
            self._thread.join(timeout=10)
        with self._lock:
            self._running = False
        self._log("INFO", "账号保活已停止")
        return True

    def _run(self, http: EcloudHttpUtil, interval: int, relogin_fn):
        """保活线程主循环，复用 keepalive.keepalive_once()。"""
        while not self._stop_event.is_set():
            with self._lock:
                self._rounds += 1
                current_round = self._rounds
            try:
                alive = account_keepalive.keepalive_once(http)
                if alive:
                    self._record_success()
                    self._log("INFO", f"[{current_round}] 账号保活成功")
                else:
                    self._record_error("账号保活失败，可能 token 失效")
                    self._log("WARN", f"[{current_round}] 账号保活失败，尝试重新登录")
                    if relogin_fn:
                        token = relogin_fn()
                        if token:
                            http.set_token(token)
                            self._log("INFO", f"[{current_round}] 已重新登录，立即重试账号保活")
                            if account_keepalive.keepalive_once(http):
                                self._record_success()
                                self._log("INFO", f"[{current_round}] 账号保活成功")
                            else:
                                self._record_error("重登后账号保活仍失败")
                                self._log("WARN", f"[{current_round}] 重登后账号保活仍失败")
                        else:
                            self._log("ERROR", f"[{current_round}] 重新登录失败，停止账号保活")
                            break
            except EcloudError as e:
                self._record_error(f"[{e.code}] {e.message}")
                self._log("WARN", f"[{current_round}] 账号保活失败: {e.message}")
                if relogin_fn and _token_maybe_expired(e):
                    self._log("INFO", "token 可能失效，尝试重新登录...")
                    try:
                        token = relogin_fn()
                        if token:
                            http.set_token(token)
                            self._log("INFO", "重新登录成功，立即重试账号保活")
                            if account_keepalive.keepalive_once(http):
                                self._record_success()
                                self._log("INFO", f"[{current_round}] 账号保活成功")
                            else:
                                self._record_error("重登后账号保活仍失败")
                                self._log("WARN", f"[{current_round}] 重登后账号保活仍失败")
                        else:
                            self._log("ERROR", "重新登录失败")
                    except Exception as ex:
                        self._log("ERROR", f"重新登录异常: {ex}")
            except Exception as e:
                self._record_error(str(e))
                self._log("ERROR", f"[{current_round}] 账号保活异常: {e}")

            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        with self._lock:
            self._running = False


class KeepaliveManager:
    """单例：管理一个桌面会话保活后台线程。"""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 状态
        self._running = False
        self._instance_id = ""
        self._interval = 300
        self._rounds = 0
        self._last_uptime = ""
        self._last_error = ""
        self._started_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._consecutive_errors = 0
        self._log_seq = 0
        # 日志缓存（最近 200 条）
        self._logs: deque[dict] = deque(maxlen=200)

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict:
        with self._lock:
            if not self._running:
                health = "stopped"
            elif self._last_error:
                health = "error"
            elif self._last_uptime:
                health = "ok"
            else:
                health = "starting"
            return {
                "running": self._running,
                "health": health,
                "instance_id": self._instance_id,
                "interval": self._interval,
                "rounds": self._rounds,
                "last_uptime": self._last_uptime,
                "last_error": self._last_error,
                "consecutive_errors": self._consecutive_errors,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            }

    def get_logs(self, since: int = 0) -> list[dict]:
        """返回序号 > since 的日志。"""
        with self._lock:
            return [log for log in self._logs if log["seq"] > since]

    def _log(self, level: str, msg: str):
        with self._lock:
            self._log_seq = max(self._log_seq + 1, int(time.time() * 1000))
            entry = {
                "seq": self._log_seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "msg": msg,
            }
            self._logs.append(entry)

    def _record_success(self, uptime: str):
        with self._lock:
            self._last_uptime = uptime
            self._last_error = ""
            self._last_success_at = datetime.now()
            self._consecutive_errors = 0

    def start(self, http: EcloudHttpUtil, instance_id: str,
              machine_id: str = "", ticket: str = "",
              interval: int = 300, relogin_fn=None) -> bool:
        """启动保活线程。已在运行则返回 False。"""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._instance_id = instance_id
            self._interval = interval
            self._rounds = 0
            self._last_uptime = ""
            self._last_error = ""
            self._last_success_at = None
            self._consecutive_errors = 0
            self._started_at = datetime.now()
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run,
            args=(http, instance_id, machine_id, ticket, interval, relogin_fn),
            daemon=True, name="keepalive",
        )
        self._thread.start()
        self._log("INFO", f"保活已启动: instance={instance_id[:20]}, interval={interval}s")
        return True

    def stop(self) -> bool:
        """停止保活线程。未运行则返回 False。"""
        if not self._running:
            return False
        self._stop_event.set()
        self._log("INFO", "正在停止保活...")
        if self._thread:
            self._thread.join(timeout=10)
        with self._lock:
            self._running = False
        self._log("INFO", "保活已停止")
        return True

    def _run(self, http: EcloudHttpUtil, instance_id: str,
             machine_id: str, ticket: str, interval: int, relogin_fn):
        """保活线程主循环，复用 desktop-keepalive 的桌面保活语义。"""
        session = desktop_session.DesktopSession(http, instance_id, machine_id, ticket=ticket)
        if ticket:
            try:
                session.register_session()
            except EcloudError as e:
                self._log("WARN", f"初次 session 登记失败（忽略）: {e.message}")

        while not self._stop_event.is_set():
            with self._lock:
                self._rounds += 1
                current_round = self._rounds
            try:
                alive = session.keepalive_once()
                if alive:
                    uptime = session.last_uptime or ""
                    self._record_success(uptime)
                    self._log("INFO", f"[{current_round}] 桌面保活成功: {uptime or 'ok'}")
                else:
                    with self._lock:
                        self._last_error = "桌面保活失败，可能 token 失效或桌面已关机"
                        self._consecutive_errors += 1
                    self._log("WARN", f"[{current_round}] 桌面保活失败，可能 token 失效或桌面已关机")
                    if relogin_fn:
                        token = relogin_fn()
                        if token:
                            http.set_token(token)
                            self._log("INFO", f"[{current_round}] 已重新登录，立即重试桌面保活")
                            if session.keepalive_once():
                                uptime = session.last_uptime or ""
                                self._record_success(uptime)
                                self._log("INFO", f"[{current_round}] 桌面保活成功: {uptime or 'ok'}")
                            else:
                                self._log("WARN", f"[{current_round}] 重登后桌面保活仍失败")
                        else:
                            self._log("ERROR", f"[{current_round}] 重新登录失败，停止保活")
                            break
            except EcloudError as e:
                with self._lock:
                    self._last_error = f"[{e.code}] {e.message}"
                    self._consecutive_errors += 1
                self._log("WARN", f"[{current_round}] 保活失败: {e.message}")
                # token 失效尝试重登
                if relogin_fn and _token_maybe_expired(e):
                    self._log("INFO", "token 可能失效，尝试重新登录...")
                    try:
                        token = relogin_fn()
                        if token:
                            http.set_token(token)
                            self._log("INFO", "重新登录成功，立即重试保活")
                            try:
                                alive = session.keepalive_once()
                                if not alive:
                                    raise EcloudError({
                                        "errorCode": "DESKTOP_KEEPALIVE_FAILED",
                                        "errorMessage": "桌面保活失败，可能 token 失效或桌面已关机",
                                    })
                                uptime = session.last_uptime or ""
                                self._record_success(uptime)
                                self._log("INFO", f"[{current_round}] 桌面保活成功: {uptime or 'ok'}")
                            except EcloudError as retry_err:
                                with self._lock:
                                    self._last_error = f"[{retry_err.code}] {retry_err.message}"
                                self._log("WARN", f"[{current_round}] 重登后保活仍失败: {retry_err.message}")
                            except Exception as retry_ex:
                                with self._lock:
                                    self._last_error = str(retry_ex)
                                self._log("ERROR", f"[{current_round}] 重登后保活异常: {retry_ex}")
                        else:
                            self._log("ERROR", "重新登录失败")
                    except Exception as ex:
                        self._log("ERROR", f"重新登录异常: {ex}")
            except Exception as e:
                with self._lock:
                    self._last_error = str(e)
                    self._consecutive_errors += 1
                self._log("ERROR", f"[{current_round}] 保活异常: {e}")

            # 等待 interval 秒，但每秒检查 stop 信号（便于快速停止）
            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        with self._lock:
            self._running = False


def _token_maybe_expired(err: EcloudError) -> bool:
    msg = (err.message or "").lower()
    return any(h in msg for h in ["token", "失效", "未登录", "expire", "401", "授权"])
