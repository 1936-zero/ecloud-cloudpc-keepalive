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
import desktop_session
from ecloud_client import EcloudHttpUtil, EcloudError


class KeepaliveManager:
    """单例：管理一个保活后台线程。"""

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

    def start(self, http: EcloudHttpUtil, instance_id: str,
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
            target=self._run, args=(http, instance_id, interval, relogin_fn),
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
             interval: int, relogin_fn):
        """保活线程主循环。"""
        session = desktop_session.DesktopSession(http, instance_id)
        while not self._stop_event.is_set():
            with self._lock:
                self._rounds += 1
                current_round = self._rounds
            try:
                uptime = session.report_uptime()
                with self._lock:
                    self._last_uptime = uptime
                    self._last_error = ""
                    self._last_success_at = datetime.now()
                    self._consecutive_errors = 0
                self._log("INFO", f"[{current_round}] 保活成功: {uptime}")
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
                            self._log("INFO", "重新登录成功，继续保活")
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
