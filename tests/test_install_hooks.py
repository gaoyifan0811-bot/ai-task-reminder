import os
import shlex
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from install_hooks import (
    add_reminder_hooks, reminder_hook_command, remove_reminder_hooks,
    validate_webhook_url, write_settings_file,
)
from task_reminder import read_settings

SCRIPT = "/p/task_reminder.py"
OTHERS_STOP_HOOK = {"Stop": [{"hooks": [{"type": "command", "command": "别人的脚本.sh"}]}]}


def all_commands(hooks_config, event):
    return [hook["command"] for group in hooks_config[event] for hook in group["hooks"]]


class ValidateWebhookUrlTest(unittest.TestCase):
    def test_validate_webhook_url_accepts_feishu_and_strips(self):
        self.assertEqual(validate_webhook_url("  https://open.feishu.cn/open-apis/bot/v2/hook/abc\n"),
                         "https://open.feishu.cn/open-apis/bot/v2/hook/abc")

    def test_validate_webhook_url_accepts_lark(self):
        url = "https://open.larksuite.com/open-apis/bot/v2/hook/abc"
        self.assertEqual(validate_webhook_url(url), url)

    def test_validate_webhook_url_rejects_other_host_and_shows_value(self):
        with self.assertRaises(ValueError) as caught:
            validate_webhook_url("https://example.com/hook/abc")
        self.assertIn("https://example.com/hook/abc", str(caught.exception))

    def test_validate_webhook_url_rejects_prefix_without_token(self):
        with self.assertRaises(ValueError):
            validate_webhook_url("https://open.feishu.cn/open-apis/bot/v2/hook/")

    def test_validate_webhook_url_rejects_text_with_spaces_inside(self):
        with self.assertRaises(ValueError):
            validate_webhook_url("https://open.feishu.cn/open-apis/bot/v2/hook/abc 复制多了")


class HookConfigTest(unittest.TestCase):
    def test_hook_command_quotes_chinese_path_with_space(self):
        command = reminder_hook_command("/Users/x/开发/AI 任务提醒/task_reminder.py", "codex", "turn-end")
        self.assertEqual(shlex.split(command),
                         ["/usr/bin/python3", "/Users/x/开发/AI 任务提醒/task_reminder.py", "codex", "turn-end"])

    def test_add_keeps_existing_hooks_of_others(self):
        existing = {"Stop": [{"hooks": [{"type": "command", "command": "别人的脚本.sh"}]}]}
        updated = add_reminder_hooks(existing, "claude", SCRIPT)
        commands = all_commands(updated, "Stop")
        self.assertIn("别人的脚本.sh", commands)
        self.assertEqual(sum("task_reminder.py" in command for command in commands), 1)
        self.assertEqual(existing, OTHERS_STOP_HOOK)

    def test_add_twice_does_not_duplicate(self):
        once = add_reminder_hooks({}, "codex", SCRIPT)
        twice = add_reminder_hooks(once, "codex", SCRIPT)
        self.assertEqual(once, twice)

    def test_both_tools_get_the_same_four_events(self):
        for tool in ["claude", "codex"]:
            updated = add_reminder_hooks({}, tool, SCRIPT)
            self.assertEqual(set(updated), {"UserPromptSubmit", "Stop", "PermissionRequest", "PostToolUse"})
            self.assertEqual(all_commands(updated, "Stop"), ["/usr/bin/python3 /p/task_reminder.py %s turn-end" % tool])
            self.assertEqual(all_commands(updated, "PostToolUse"),
                             ["/usr/bin/python3 /p/task_reminder.py %s tool-finished" % tool])

    def test_remove_restores_original(self):
        self.assertEqual(remove_reminder_hooks(add_reminder_hooks(OTHERS_STOP_HOOK, "claude", SCRIPT)),
                         OTHERS_STOP_HOOK)
        self.assertEqual(remove_reminder_hooks(add_reminder_hooks({}, "codex", SCRIPT)), {})

    def test_remove_keeps_other_hook_inside_a_shared_group(self):
        shared_group = {"Stop": [{"hooks": [
            {"type": "command", "command": "别人的脚本.sh"},
            {"type": "command", "command": "/usr/bin/python3 /p/task_reminder.py claude turn-end"}]}]}
        self.assertEqual(remove_reminder_hooks(shared_group), OTHERS_STOP_HOOK)

    def test_add_after_folder_moved_replaces_old_path(self):
        moved = add_reminder_hooks(add_reminder_hooks({}, "codex", "/旧位置/task_reminder.py"), "codex", SCRIPT)
        self.assertEqual(all_commands(moved, "Stop"), ["/usr/bin/python3 /p/task_reminder.py codex turn-end"])


class WriteSettingsFileTest(unittest.TestCase):
    def setUp(self):
        self.ini_path = os.path.join(tempfile.mkdtemp(), "配置.ini")

    def test_new_file_has_defaults_and_is_private(self):
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        self.assertEqual(read_settings(self.ini_path), {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            "task_seconds_threshold": 120, "idle_seconds_threshold": 180, "summary_char_limit": 100})
        self.assertEqual(stat.S_IMODE(os.stat(self.ini_path).st_mode), 0o600)

    def test_write_settings_file_keeps_user_numbers_and_comments(self):
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        with open(self.ini_path, encoding="utf-8") as ini_file:
            edited = ini_file.read().replace("任务时长门槛秒 = 120", "任务时长门槛秒 = 300")
        with open(self.ini_path, "w", encoding="utf-8") as ini_file:
            ini_file.write(edited)
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/new")
        settings = read_settings(self.ini_path)
        self.assertEqual(settings["task_seconds_threshold"], 300)
        self.assertEqual(settings["webhook_url"], "https://open.feishu.cn/open-apis/bot/v2/hook/new")
        self.assertEqual(stat.S_IMODE(os.stat(self.ini_path).st_mode), 0o600)
        with open(self.ini_path, encoding="utf-8") as ini_file:
            self.assertIn("#", ini_file.read())


if __name__ == "__main__":
    unittest.main()
