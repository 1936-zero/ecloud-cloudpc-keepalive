import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
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


class DesktopStartPreflightTests(unittest.TestCase):
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
             patch.object(server._ka, "start", return_value=True) as start:
            data = app.test_client().post(
                "/api/keepalive/start",
                json={"instance_id": "CCA-test", "interval": 60},
            ).get_json()

        self.assertTrue(data["ok"])
        start.assert_called_once()


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
