import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import task_reminder
from task_reminder import (
    clear_waiting_for_approval, decide_delivery, deliver, main, mark_waiting_for_approval,
    summary_source_from_hook, waiting_for_approval_since,
)

SETTINGS = {
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
    "task_seconds_threshold": 120,
    "idle_seconds_threshold": 180,
    "summary_char_limit": 100,
}


class SummarySourceTest(unittest.TestCase):
    def test_summary_source_for_turn_end(self):
        self.assertEqual(summary_source_from_hook("turn-end", {"last_assistant_message": "完成"}), "完成")
        self.assertIsNone(summary_source_from_hook("turn-end", {"last_assistant_message": None}))
        self.assertIsNone(summary_source_from_hook("turn-end", {}))

    def test_summary_source_for_needs_approval_prefers_command(self):
        hook = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        self.assertEqual(summary_source_from_hook("needs-approval", hook), "ls")
        self.assertEqual(summary_source_from_hook("needs-approval", {"tool_name": "apply_patch"}), "apply_patch")
        self.assertEqual(
            summary_source_from_hook("needs-approval", {"tool_name": "Edit", "tool_input": {"file_path": "/a"}}),
            "Edit")
        self.assertIsNone(summary_source_from_hook("needs-approval", {}))

    def test_summary_source_for_question(self):
        hook = {"tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "用哪种形态？"}, {"question": "第二问"}]}}
        self.assertEqual(summary_source_from_hook("asks-question", hook), "用哪种形态？")
        self.assertIsNone(summary_source_from_hook("asks-question", {"tool_input": {"questions": []}}))


class DecideDeliveryTest(unittest.TestCase):
    def test_decide_delivery(self):
        self.assertEqual(decide_delivery("turn-end", 200, 5, SETTINGS), 0)
        self.assertIsNone(decide_delivery("turn-end", 30, 5, SETTINGS))
        self.assertEqual(decide_delivery("needs-approval", 30, 5, SETTINGS), 180)
        self.assertEqual(decide_delivery("needs-approval", 30, 200, SETTINGS), 0)
        self.assertEqual(decide_delivery("asks-question", 30, 5, SETTINGS), 180)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.patch("STATE_DIR", os.path.join(tempfile.mkdtemp(), "状态"))
        self.patch("LOG_PATH", os.path.join(tempfile.mkdtemp(), "日志.txt"))
        self.mock_read_settings = self.patch("read_settings", mock.Mock(return_value=dict(SETTINGS)))
        self.mock_idle = self.patch("keyboard_mouse_idle_seconds", mock.Mock(return_value=5.0))
        self.mock_start = self.patch("start_background_delivery", mock.Mock())
        self.mock_alert = self.patch("show_mac_alert", mock.Mock())

    def patch(self, name, replacement):
        patcher = mock.patch.object(task_reminder, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    def run_main_at(self, timestamp, argv, hook_input):
        with mock.patch.object(task_reminder.time, "time", return_value=timestamp):
            return main(argv, json.dumps(hook_input))

    def test_main_turn_start_then_long_turn_end_starts_delivery(self):
        hook = {"session_id": "s1", "cwd": "/Users/x/项目甲"}
        self.assertEqual(self.run_main_at(1000.0, ["claude", "turn-start"], hook), 0)
        self.mock_start.assert_not_called()
        self.assertEqual(self.run_main_at(1200.0, ["claude", "turn-end"],
                                          dict(hook, last_assistant_message="做完了")), 0)
        self.mock_start.assert_called_once_with("✅ Claude Code 跑完了｜项目甲｜用时 3 分钟\n做完了", 0, "s1")

    def test_main_short_turn_with_user_present_stays_silent(self):
        hook = {"session_id": "s1", "cwd": "/Users/x/项目甲"}
        self.run_main_at(1000.0, ["codex", "turn-start"], hook)
        self.run_main_at(1030.0, ["codex", "turn-end"], dict(hook, last_assistant_message="好"))
        self.mock_start.assert_not_called()

    def test_main_turn_end_without_start_record_and_user_away(self):
        self.mock_idle.return_value = 600.0
        self.run_main_at(1000.0, ["codex", "turn-end"],
                         {"session_id": "新会话", "cwd": "/Users/x/项目乙", "last_assistant_message": None})
        self.mock_start.assert_called_once_with("✅ Codex 跑完了｜项目乙", 0, "新会话")

    def test_main_needs_approval_with_user_present_schedules_recheck(self):
        hook = {"session_id": "s1", "cwd": "/Users/x/项目甲"}
        self.run_main_at(1000.0, ["codex", "turn-start"], hook)
        self.run_main_at(1030.0, ["codex", "needs-approval"],
                         dict(hook, tool_name="Bash", tool_input={"command": "pip install pandas"}))
        self.mock_start.assert_called_once_with(
            "⏸ Codex 等你批准｜项目甲｜已运行 30 秒\n想执行：pip install pandas", 180, "s1")

    def test_main_question_tool_uses_question_wording(self):
        hook = {"session_id": "s1", "cwd": "/Users/x/项目甲", "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "用哪种形态？"}]}}
        self.mock_idle.return_value = 600.0
        self.run_main_at(1000.0, ["claude", "needs-approval"], hook)
        self.mock_start.assert_called_once_with("❓ Claude Code 在问你｜项目甲\n用哪种形态？", 0, "s1")

    def test_main_summary_limit_zero_sends_no_summary(self):
        self.mock_read_settings.return_value = dict(SETTINGS, summary_char_limit=0)
        self.mock_idle.return_value = 600.0
        self.run_main_at(1000.0, ["claude", "turn-end"],
                         {"session_id": "s1", "cwd": "/Users/x/项目甲", "last_assistant_message": "机密内容"})
        self.mock_start.assert_called_once_with("✅ Claude Code 跑完了｜项目甲", 0, "s1")

    def test_main_returns_zero_and_alerts_on_invalid_json(self):
        self.assertEqual(main(["codex", "turn-end"], "这不是 JSON"), 0)
        self.mock_alert.assert_called_once()
        self.assertIn("这不是 JSON", self.mock_alert.call_args[0][0])
        self.mock_start.assert_not_called()

    def test_main_alerts_when_session_id_missing(self):
        self.assertEqual(main(["codex", "turn-end"], json.dumps({"cwd": "/a"})), 0)
        self.assertIn("session_id", self.mock_alert.call_args[0][0])

    def test_main_alerts_on_unknown_tool_or_event(self):
        self.assertEqual(main(["别的工具", "turn-end"], json.dumps({"session_id": "s", "cwd": "/a"})), 0)
        self.assertIn("别的工具", self.mock_alert.call_args[0][0])

    def test_main_alerts_when_settings_missing(self):
        self.mock_read_settings.side_effect = task_reminder.ReminderSettingsError("找不到配置文件：/x/配置.ini")
        self.assertEqual(main(["claude", "turn-end"], json.dumps({"session_id": "s", "cwd": "/a"})), 0)
        self.assertIn("/x/配置.ini", self.mock_alert.call_args[0][0])


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = os.path.join(tempfile.mkdtemp(), "状态")
        patcher = mock.patch.object(task_reminder, "STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, name in [(task_reminder, "send_with_retry"), (task_reminder.time, "sleep"),
                             (task_reminder, "read_settings")]:
            patcher = mock.patch.object(target, name)
            setattr(self, "mock_" + name, patcher.start())
            self.addCleanup(patcher.stop)
        self.mock_read_settings.return_value = dict(SETTINGS)

    def test_deliver_immediately(self):
        deliver("文本", 0, "s1")
        self.mock_sleep.assert_not_called()
        self.mock_send_with_retry.assert_called_once_with(SETTINGS["webhook_url"], "文本")

    def test_deliver_recheck_sends_when_still_waiting_for_approval(self):
        mark_waiting_for_approval(self.state_dir, "s1", 1030.0)
        deliver("文本", 180, "s1")
        self.mock_sleep.assert_called_once_with(180)
        self.mock_send_with_retry.assert_called_once_with(SETTINGS["webhook_url"], "文本")

    def test_deliver_recheck_skips_when_approval_was_answered(self):
        mark_waiting_for_approval(self.state_dir, "s1", 1030.0)
        self.mock_sleep.side_effect = lambda seconds: clear_waiting_for_approval(self.state_dir, "s1")
        deliver("文本", 180, "s1")
        self.mock_send_with_retry.assert_not_called()

    def test_deliver_recheck_skips_when_a_newer_request_replaced_it(self):
        mark_waiting_for_approval(self.state_dir, "s1", 1030.0)
        self.mock_sleep.side_effect = lambda seconds: mark_waiting_for_approval(self.state_dir, "s1", 1100.0)
        deliver("文本", 180, "s1")
        self.mock_send_with_retry.assert_not_called()


class WaitingMarkTest(MainTest):
    HOOK = {"session_id": "s1", "cwd": "/Users/x/项目甲", "tool_name": "Bash", "tool_input": {"command": "ls"}}

    def is_waiting(self):
        return waiting_for_approval_since(task_reminder.STATE_DIR, "s1") is not None

    def test_needs_approval_marks_session_as_waiting(self):
        self.run_main_at(1030.0, ["codex", "needs-approval"], self.HOOK)
        self.assertEqual(waiting_for_approval_since(task_reminder.STATE_DIR, "s1"), 1030.0)

    def test_tool_finished_clears_the_mark_and_sends_nothing(self):
        self.run_main_at(1030.0, ["codex", "needs-approval"], self.HOOK)
        self.mock_start.reset_mock()
        self.assertEqual(self.run_main_at(1040.0, ["codex", "tool-finished"], self.HOOK), 0)
        self.assertFalse(self.is_waiting())
        self.mock_start.assert_not_called()
        self.mock_read_settings.reset_mock()

    def test_turn_end_and_turn_start_clear_the_mark(self):
        for event in ["turn-end", "turn-start"]:
            self.run_main_at(1030.0, ["codex", "needs-approval"], self.HOOK)
            self.run_main_at(1040.0, ["codex", event], self.HOOK)
            self.assertFalse(self.is_waiting(), event)

    def test_tool_finished_without_mark_is_fine(self):
        self.assertEqual(self.run_main_at(1040.0, ["claude", "tool-finished"], self.HOOK), 0)
        self.mock_alert.assert_not_called()

if __name__ == "__main__":
    unittest.main()
