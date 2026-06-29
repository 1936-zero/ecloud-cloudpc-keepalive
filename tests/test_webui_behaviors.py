import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import desktop_session
import login
from ecloud_client import EcloudError
from web import server
from web.keepalive_manager import KeepaliveManager


class WebUiStatusTests(unittest.TestCase):
    def test_status_relogs_in_when_saved_token_is_invalid_and_credentials_exist(self):
        app = server.create_app()
        fake_http = Mock()
        server._app_state["cfg"] = {
            "access_token": "expired-token",
            "username": "user",
            "password": "password",
        }
        server._app_state["http"] = fake_http

        with patch("web.server.login.get_user_info", side_effect=EcloudError({
            "errorCode": "401",
            "errorMessage": "token失效",
        })), patch("web.server.login.login_with_password", return_value={
            "status": login.LoginResult.SUCCESS,
            "access_token": "fresh-token",
        }), patch("web.server._save_cfg"):
            data = app.test_client().get("/api/status").get_json()

        self.assertTrue(data["logged_in"])
        self.assertNotIn("error", data)
        self.assertEqual(server._app_state["cfg"]["access_token"], "fresh-token")
        fake_http.clear_token.assert_called_once()
        fake_http.set_token.assert_called_once_with("fresh-token")

    def test_status_does_not_report_logged_in_when_saved_token_is_invalid_without_credentials(self):
        app = server.create_app()
        server._app_state["cfg"] = {"access_token": "expired-token"}
        server._app_state["http"] = object()

        with patch("web.server.login.get_user_info", side_effect=EcloudError({
            "errorCode": "401",
            "errorMessage": "token失效",
        })):
            data = app.test_client().get("/api/status").get_json()

        self.assertFalse(data["logged_in"])
        self.assertEqual(data["error"], "token失效")


class KeepaliveManagerTests(unittest.TestCase):
    def test_log_sequence_is_monotonic_even_in_same_millisecond(self):
        manager = KeepaliveManager()

        with patch("web.keepalive_manager.time.time", return_value=1000.0):
            manager._log("INFO", "first")
            manager._log("INFO", "second")

        logs = manager.get_logs(0)
        self.assertEqual(len(logs), 2)
        self.assertLess(logs[0]["seq"], logs[1]["seq"])

    def test_status_reports_health_error_when_thread_is_running_but_last_probe_failed(self):
        manager = KeepaliveManager()
        manager._running = True
        manager._last_error = "[NO_UPTIME] desktopUptime 未返回运行时长"

        status = manager.get_status()

        self.assertEqual(status["health"], "error")
        self.assertTrue(status["running"])

    def test_relogin_retries_keepalive_and_clears_stale_token_error(self):
        manager = KeepaliveManager()
        manager._running = True
        fake_http = Mock()
        relogin = Mock(return_value="fresh-token")
        session = Mock()
        session.keepalive_once.side_effect = [False, True]
        session.last_uptime = "1小时2分3秒"

        with patch("web.keepalive_manager.desktop_session.DesktopSession", return_value=session), \
             patch("web.keepalive_manager.time.sleep", side_effect=lambda _seconds: manager._stop_event.set()):
            manager._run(fake_http, "CCA-test", "", "", 1, relogin)

        self.assertEqual(session.keepalive_once.call_count, 2)
        relogin.assert_called_once()
        fake_http.set_token.assert_called_once_with("fresh-token")
        self.assertEqual(manager._last_error, "")
        self.assertEqual(manager._consecutive_errors, 0)
        self.assertEqual(manager._last_uptime, "1小时2分3秒")

    def test_run_uses_desktop_keepalive_once(self):
        manager = KeepaliveManager()
        manager._running = True
        fake_http = Mock()
        session = Mock()
        session.keepalive_once.return_value = True
        session.last_uptime = "1小时2分3秒"

        with patch("web.keepalive_manager.desktop_session.DesktopSession", return_value=session) as session_cls, \
             patch("web.keepalive_manager.time.sleep", side_effect=lambda _seconds: manager._stop_event.set()):
            manager._run(fake_http, "CCA-test", "MID-test", "ticket-test", 1, relogin_fn=None)

        session_cls.assert_called_once_with(fake_http, "CCA-test", "MID-test", ticket="ticket-test")
        session.register_session.assert_called_once()
        session.keepalive_once.assert_called_once()
        self.assertEqual(manager._last_uptime, "1小时2分3秒")

    def test_desktop_keepalive_failure_relogs_in_and_retries(self):
        manager = KeepaliveManager()
        manager._running = True
        fake_http = Mock()
        relogin = Mock(return_value="fresh-token")
        session = Mock()
        session.keepalive_once.side_effect = [False, True]
        session.last_uptime = "1小时2分3秒"

        with patch("web.keepalive_manager.desktop_session.DesktopSession", return_value=session), \
             patch("web.keepalive_manager.time.sleep", side_effect=lambda _seconds: manager._stop_event.set()):
            manager._run(fake_http, "CCA-test", "", "", 1, relogin)

        relogin.assert_called_once()
        fake_http.set_token.assert_called_once_with("fresh-token")
        self.assertEqual(session.keepalive_once.call_count, 2)
        self.assertEqual(manager._last_error, "")
        self.assertEqual(manager._last_uptime, "1小时2分3秒")


class DesktopStartPreflightTests(unittest.TestCase):
    def test_desktop_keepalive_once_records_last_uptime(self):
        fake_http = Mock()
        fake_http.post.return_value = "0小时1分2秒"
        session = desktop_session.DesktopSession(fake_http, "CCA-test")

        self.assertTrue(session.keepalive_once())
        self.assertEqual(session.last_uptime, "0小时1分2秒")

    def test_save_cfg_writes_valid_json_without_temp_file_leftovers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "cloud_pc.json"
            with patch("web.server.CONFIG_FILE", str(cfg_path)):
                server._save_cfg({"keepalive_autostart": True, "keepalive_interval": 300})

            self.assertEqual(
                json.loads(cfg_path.read_text(encoding="utf-8")),
                {"keepalive_autostart": True, "keepalive_interval": 300},
            )
            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])

    def test_preflight_uses_desktop_uptime_not_status_enum_guess(self):
        class FakeHttp:
            common_params = {}

            def post(self, endpoint, payload=None):
                if endpoint == config.Endpoint.DESKTOP_UPTIME:
                    return "0小时1分2秒"
                raise AssertionError(endpoint)

        self.assertEqual(
            server._preflight_uptime(FakeHttp(), "CCA-test"),
            "0小时1分2秒",
        )

    def test_start_does_not_block_on_unknown_status_when_uptime_succeeds(self):
        app = server.create_app()
        server._app_state["cfg"] = {"access_token": "valid-token"}
        server._app_state["http"] = object()

        with patch("web.server.desktop_list.get_desktop_status", return_value={"CCA-test": "mystery"}), \
             patch("web.server._preflight_uptime", return_value="0小时1分2秒"), \
             patch.object(server._ka, "start", return_value=True) as start, \
             patch("web.server._save_cfg") as save_cfg:
            data = app.test_client().post(
                "/api/keepalive/start",
                json={"instance_id": "CCA-test", "machine_id": "MID-test", "interval": 60},
            ).get_json()

        self.assertTrue(data["ok"])
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["machine_id"], "MID-test")
        self.assertEqual(server._app_state["cfg"]["instance_id"], "CCA-test")
        self.assertEqual(server._app_state["cfg"]["machine_id"], "MID-test")
        self.assertTrue(server._app_state["cfg"]["keepalive_autostart"])
        self.assertEqual(server._app_state["cfg"]["keepalive_interval"], 60)
        self.assertEqual(save_cfg.call_count, 2)

    def test_stop_disables_persisted_autostart(self):
        app = server.create_app()
        server._app_state["cfg"] = {"keepalive_autostart": True}
        events = []

        def save_cfg(cfg):
            events.append(("save", cfg["keepalive_autostart"]))

        def stop():
            events.append(("stop", None))
            return False

        with patch.object(server._ka, "stop", side_effect=stop) as stop_mock, \
             patch("web.server._save_cfg", side_effect=save_cfg) as save_cfg_mock:
            data = app.test_client().post("/api/keepalive/stop").get_json()

        self.assertFalse(data["ok"])
        stop_mock.assert_called_once()
        self.assertFalse(server._app_state["cfg"]["keepalive_autostart"])
        save_cfg_mock.assert_called_once()
        self.assertEqual(events, [("save", False), ("stop", None)])

    def test_autostart_starts_keepalive_from_config(self):
        server._app_state["cfg"] = {
            "access_token": "valid-token",
            "instance_id": "CCA-test",
            "machine_id": "MID-test",
            "ticket": "ticket-test",
            "keepalive_autostart": True,
            "keepalive_interval": 60,
        }
        fake_http = object()
        server._app_state["http"] = fake_http

        with patch.object(server._ka, "is_running", return_value=False), \
             patch.object(server._ka, "start", return_value=True) as start:
            self.assertTrue(server._ensure_keepalive_autostart("test"))

        start.assert_called_once()
        args, kwargs = start.call_args
        self.assertIs(args[0], fake_http)
        self.assertEqual(args[1], "CCA-test")
        self.assertEqual(kwargs["machine_id"], "MID-test")
        self.assertEqual(kwargs["ticket"], "ticket-test")
        self.assertEqual(kwargs["interval"], 60)

    def test_autostart_reloads_config_when_memory_state_is_stale(self):
        server._app_state["cfg"] = {"keepalive_autostart": False}
        fake_http = object()
        server._app_state["http"] = fake_http

        with patch("web.server._load_cfg", return_value={
            "access_token": "valid-token",
            "instance_id": "CCA-test",
            "machine_id": "MID-test",
            "keepalive_autostart": True,
            "keepalive_interval": 90,
        }), patch.object(server._ka, "is_running", return_value=False), \
             patch.object(server._ka, "start", return_value=True) as start:
            self.assertTrue(server._ensure_keepalive_autostart("test"))

        start.assert_called_once()
        self.assertTrue(server._app_state["cfg"]["keepalive_autostart"])
        self.assertEqual(start.call_args.kwargs["machine_id"], "MID-test")
        self.assertEqual(start.call_args.kwargs["interval"], 90)


class FrontendRegressionTests(unittest.TestCase):
    def test_action_buttons_restore_on_request_failure(self):
        html = Path("web/templates/index.html").read_text(encoding="utf-8")
        for button_id in (
            "btn-start",
            "btn-stop",
            "btn-logout",
            "btn-send-sms",
            "btn-verify",
        ):
            with self.subTest(button_id=button_id):
                handler = re.search(
                    r'document\.getElementById\("' + button_id + r'"\)\.onclick = function\([^)]*\) \{(?P<body>.*?)\n\};',
                    html,
                    re.S,
                )
                self.assertIsNotNone(handler)
                self.assertIn(".catch(", handler.group("body"))


if __name__ == "__main__":
    unittest.main()
