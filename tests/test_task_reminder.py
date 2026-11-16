import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from task_reminder import (
    ReminderSettingsError, build_message, format_duration, one_line_summary,
    parse_hid_idle_seconds, read_settings, record_turn_start, should_notify,
    turn_elapsed_seconds, write_log_line,
)

COMPLETE_INI = """[提醒]
飞书机器人地址 = https://open.feishu.cn/open-apis/bot/v2/hook/abc
任务时长门槛秒 = 120
离开门槛秒 = 180
摘要字数 = 100
"""


def write_text_file(folder, file_name, text):
    path = os.path.join(folder, file_name)
    with open(path, "w", encoding="utf-8") as text_file:
        text_file.write(text)
    return path


class ReadSettingsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def test_read_settings_returns_all_four_values(self):
        ini_path = write_text_file(self.folder, "配置.ini", COMPLETE_INI)
        self.assertEqual(read_settings(ini_path), {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            "task_seconds_threshold": 120,
            "idle_seconds_threshold": 180,
            "summary_char_limit": 100,
        })

    def test_read_settings_missing_file_names_the_path(self):
        with self.assertRaises(ReminderSettingsError) as caught:
            read_settings("/不存在/配置.ini")
        self.assertIn("/不存在/配置.ini", str(caught.exception))

    def test_read_settings_missing_key_names_the_key(self):
        ini_path = write_text_file(self.folder, "配置.ini", COMPLETE_INI.replace("离开门槛秒 = 180\n", ""))
        with self.assertRaises(ReminderSettingsError) as caught:
            read_settings(ini_path)
        self.assertIn("离开门槛秒", str(caught.exception))

    def test_read_settings_non_number_shows_the_value(self):
        ini_path = write_text_file(self.folder, "配置.ini", COMPLETE_INI.replace("= 120", "= 两分钟"))
        with self.assertRaises(ReminderSettingsError) as caught:
            read_settings(ini_path)
        self.assertIn("两分钟", str(caught.exception))


class WriteLogLineTest(unittest.TestCase):
    def test_write_log_line_appends(self):
        log_path = os.path.join(tempfile.mkdtemp(), "日志.txt")
        write_log_line(log_path, "第一行")
        write_log_line(log_path, "第二行")
        with open(log_path, encoding="utf-8") as log_file:
            lines = log_file.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("第二行"))

    def test_write_log_line_clears_file_over_1mb(self):
        log_path = write_text_file(tempfile.mkdtemp(), "日志.txt", "x" * 1_100_000)
        write_log_line(log_path, "新的一行")
        self.assertLess(os.path.getsize(log_path), 1000)
        with open(log_path, encoding="utf-8") as log_file:
            self.assertIn("新的一行", log_file.read())


class TurnElapsedTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = os.path.join(tempfile.mkdtemp(), "状态")

    def test_elapsed_is_none_without_start_record(self):
        self.assertIsNone(turn_elapsed_seconds(self.state_dir, "没记录的会话", 1000.0))

    def test_elapsed_after_recorded_start(self):
        record_turn_start(self.state_dir, "s1", 1000.0)
        self.assertEqual(turn_elapsed_seconds(self.state_dir, "s1", 1090.0), 90.0)

    def test_later_start_replaces_earlier_one(self):
        record_turn_start(self.state_dir, "s1", 1000.0)
        record_turn_start(self.state_dir, "s1", 1050.0)
        self.assertEqual(turn_elapsed_seconds(self.state_dir, "s1", 1090.0), 40.0)

    def test_session_id_with_slash_stays_inside_state_dir(self):
        record_turn_start(self.state_dir, "../逃出去", 1000.0)
        self.assertEqual(turn_elapsed_seconds(self.state_dir, "../逃出去", 1010.0), 10.0)
        self.assertEqual(len(os.listdir(self.state_dir)), 1)


class IdleAndDecisionTest(unittest.TestCase):
    def test_parse_hid_idle_seconds(self):
        self.assertEqual(parse_hid_idle_seconds('    | "HIDIdleTime" = 185000000000\n'), 185.0)

    def test_parse_hid_idle_seconds_without_field_shows_output(self):
        with self.assertRaises(ValueError) as caught:
            parse_hid_idle_seconds("这里没有那个字段")
        self.assertIn("这里没有那个字段", str(caught.exception))

    def test_should_notify_matrix(self):
        s = {"task_seconds_threshold": 120, "idle_seconds_threshold": 180}
        self.assertFalse(should_notify(30, 5, s))
        self.assertTrue(should_notify(121, 5, s))
        self.assertTrue(should_notify(30, 181, s))
        self.assertFalse(should_notify(None, 5, s))
        self.assertTrue(should_notify(None, 181, s))


class MessageTextTest(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(45), "45 秒")
        self.assertEqual(format_duration(1085), "18 分钟")

    def test_one_line_summary(self):
        self.assertEqual(one_line_summary("第一行\n\n  第二行", 100), "第一行 第二行")
        self.assertEqual(one_line_summary("甲" * 150, 100), "甲" * 100 + "…")
        self.assertEqual(one_line_summary(None, 100), "")
        self.assertEqual(one_line_summary("有内容", 0), "")

    def test_build_message_turn_end(self):
        self.assertEqual(
            build_message("Claude Code", "turn-end", "/Users/x/开发/泡沫固井模拟", 1085, "已合并进 main"),
            "✅ Claude Code 跑完了｜泡沫固井模拟｜用时 18 分钟\n已合并进 main")

    def test_build_message_needs_approval(self):
        self.assertEqual(
            build_message("Codex", "needs-approval", "/Users/x/学习/量化交易学习", 360, "pip install pandas"),
            "⏸ Codex 等你批准｜量化交易学习｜已运行 6 分钟\n想执行：pip install pandas")

    def test_build_message_without_elapsed_or_summary(self):
        self.assertEqual(build_message("Codex", "turn-end", "/Users/x/项目", None, ""),
                         "✅ Codex 跑完了｜项目")

    def test_build_message_asks_question(self):
        self.assertEqual(
            build_message("Claude Code", "asks-question", "/Users/x/项目", 90, "用哪种交付形态？"),
            "❓ Claude Code 在问你｜项目｜已运行 1 分钟\n用哪种交付形态？")

    def test_build_message_folder_name_ignores_trailing_slash(self):
        self.assertEqual(build_message("Codex", "turn-end", "/Users/x/项目/", None, ""),
                         "✅ Codex 跑完了｜项目")


if __name__ == "__main__":
    unittest.main()
