import http.client
import http.server
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import task_reminder
from task_reminder import FeishuSendError, send_feishu_text, send_with_retry


def start_local_feishu_stub(status_code, reply_body):
    """起一个本机临时服务冒充飞书，返回 (地址, 收到的请求体列表, 服务对象)。"""
    received_bodies = []

    class StubHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received_bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
            self.send_response(status_code)
            self.end_headers()
            self.wfile.write(reply_body.encode("utf-8"))

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/hook" % server.server_port, received_bodies, server


class SendFeishuTextTest(unittest.TestCase):
    def test_send_feishu_text_posts_expected_body(self):
        url, received_bodies, server = start_local_feishu_stub(200, '{"code": 0}')
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        send_feishu_text(url, "你好")
        self.assertEqual(received_bodies, [{"msg_type": "text", "content": {"text": "你好"}}])

    def test_send_feishu_text_raises_on_nonzero_code(self):
        url, _, server = start_local_feishu_stub(200, '{"code": 19024, "msg": "Key Words Not Found"}')
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(FeishuSendError) as caught:
            send_feishu_text(url, "你好")
        self.assertIn("Key Words Not Found", str(caught.exception))

    def test_send_feishu_text_raises_on_http_error_with_status(self):
        url, _, server = start_local_feishu_stub(500, "服务器坏了")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(FeishuSendError) as caught:
            send_feishu_text(url, "你好")
        self.assertIn("500", str(caught.exception))

    def test_send_feishu_text_raises_on_non_json_reply(self):
        url, _, server = start_local_feishu_stub(200, "<html>登录页</html>")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(FeishuSendError) as caught:
            send_feishu_text(url, "你好")
        self.assertIn("登录页", str(caught.exception))

    def test_send_feishu_text_ignores_proxy_settings(self):
        url, received_bodies, server = start_local_feishu_stub(200, '{"code": 0}')
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        dead_proxy = {"http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1", "no_proxy": ""}
        with mock.patch.dict(os.environ, dead_proxy):
            send_feishu_text(url, "你好")
        self.assertEqual(len(received_bodies), 1)

    def test_send_feishu_text_wraps_dropped_connection(self):
        dropped = mock.Mock()
        dropped.open.side_effect = http.client.RemoteDisconnected("对方挂断")
        with mock.patch.object(task_reminder.urllib.request, "build_opener", return_value=dropped):
            with self.assertRaises(FeishuSendError) as caught:
                send_feishu_text("http://127.0.0.1:9/hook", "你好")
        self.assertIn("对方挂断", str(caught.exception))

    def test_send_feishu_text_raises_when_nothing_listens(self):
        with self.assertRaises(FeishuSendError):
            send_feishu_text("http://127.0.0.1:1/hook", "你好")


class SendWithRetryTest(unittest.TestCase):
    def setUp(self):
        for name in ["send_feishu_text", "show_mac_alert", "write_log_line"]:
            patcher = mock.patch.object(task_reminder, name)
            setattr(self, "mock_" + name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_retry_succeeds_on_third_attempt(self):
        self.mock_send_feishu_text.side_effect = [FeishuSendError("超时"), FeishuSendError("超时"), None]
        self.assertTrue(send_with_retry("地址", "x", attempts=3, wait_seconds=0))
        self.assertEqual(self.mock_send_feishu_text.call_count, 3)
        self.mock_show_mac_alert.assert_not_called()

    def test_retry_alerts_after_all_attempts_fail(self):
        self.mock_send_feishu_text.side_effect = FeishuSendError("连接被拒绝")
        self.assertFalse(send_with_retry("地址", "x", attempts=3, wait_seconds=0))
        self.assertEqual(self.mock_send_feishu_text.call_count, 3)
        self.assertIn("连接被拒绝", self.mock_show_mac_alert.call_args[0][0])
        self.assertIn("连接被拒绝", self.mock_write_log_line.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
