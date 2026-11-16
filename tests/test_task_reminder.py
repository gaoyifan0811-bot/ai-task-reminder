import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from task_reminder import ReminderSettingsError, read_settings, write_log_line

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


if __name__ == "__main__":
    unittest.main()
