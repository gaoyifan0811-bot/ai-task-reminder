import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import install_hooks
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
                         ["/usr/bin/python3", "/Users/x/开发/AI 任务提醒/task_reminder.py", "codex", "turn-end",
                          "||", "true"])

    def test_hook_command_never_fails_when_script_is_gone(self):
        for event_kind in ["turn-end", "turn-start"]:
            command = reminder_hook_command("/已经挪走/task_reminder.py", "claude", event_kind)
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                finished = subprocess.run(command.replace("/usr/bin/osascript", "/usr/bin/true"),
                                          shell=True, capture_output=True)
            self.assertEqual((finished.returncode, finished.stdout), (0, b""), event_kind)

    def test_turn_start_command_tells_user_to_reinstall_when_script_is_gone(self):
        command = reminder_hook_command("/已经挪走/task_reminder.py", "claude", "turn-start")
        self.assertIn("osascript", command)
        self.assertIn("重新双击", command)

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
            self.assertEqual(all_commands(updated, "Stop"),
                             ["/usr/bin/python3 /p/task_reminder.py %s turn-end || true" % tool])
            self.assertEqual(all_commands(updated, "PostToolUse"),
                             ["/usr/bin/python3 /p/task_reminder.py %s tool-finished || true" % tool])

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
        self.assertEqual(all_commands(moved, "Stop"), ["/usr/bin/python3 /p/task_reminder.py codex turn-end || true"])

    def test_hooks_with_matcher_or_other_type_are_kept(self):
        others = {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "格式化.sh"}]},
                                  {"hooks": [{"type": "prompt", "prompt": "检查一下"}]}]}
        self.assertEqual(remove_reminder_hooks(add_reminder_hooks(others, "claude", SCRIPT)), others)


class HookFilesTest(unittest.TestCase):
    """安装、卸载对真实配置文件做的事（文件都指到临时文件夹）。"""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.claude_path = os.path.join(self.home, ".claude", "settings.json")
        self.codex_path = os.path.join(self.home, ".codex", "hooks.json")
        os.makedirs(os.path.dirname(self.claude_path))
        self.install_folder = tempfile.mkdtemp()
        self.ini_path = os.path.join(self.install_folder, "配置.ini")
        replacements = [
            (install_hooks, "HOOK_FILES", {"claude": self.claude_path, "codex": self.codex_path}),
            (install_hooks.task_reminder, "SETTINGS_PATH", self.ini_path),
            (install_hooks.task_reminder, "send_feishu_text", mock.Mock()),
            (install_hooks, "print", mock.Mock()),
        ]
        for target, name, replacement in replacements:
            patcher = mock.patch.object(target, name, replacement, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mock_send = install_hooks.task_reminder.send_feishu_text
        with open(self.claude_path, "w", encoding="utf-8") as claude_file:
            json.dump({"theme": "dark", "hooks": OTHERS_STOP_HOOK}, claude_file)

    def read_json(self, path):
        with open(path, encoding="utf-8") as json_file:
            return json.load(json_file)

    def install_with_answers(self, *answers):
        with mock.patch("builtins.input", side_effect=list(answers)):
            return install_hooks.install()

    URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"

    def test_install_then_uninstall_restores_both_files(self):
        self.assertEqual(self.install_with_answers(self.URL, "y"), 0)
        claude_settings = self.read_json(self.claude_path)
        self.assertEqual(claude_settings["theme"], "dark")
        self.assertIn("别人的脚本.sh", all_commands(claude_settings["hooks"], "Stop"))
        self.assertEqual(len(self.read_json(self.codex_path)["hooks"]), 4)
        self.assertTrue(any(".bak-提醒工具-" in name for name in os.listdir(os.path.dirname(self.claude_path))))

        self.assertEqual(install_hooks.uninstall(), 0)
        self.assertEqual(self.read_json(self.claude_path), {"theme": "dark", "hooks": OTHERS_STOP_HOOK})
        self.assertFalse(os.path.exists(self.codex_path))

    def test_failed_test_message_changes_nothing_and_keeps_old_address(self):
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/old")
        self.mock_send.side_effect = install_hooks.task_reminder.FeishuSendError("Key Words Not Found")
        self.assertEqual(self.install_with_answers(self.URL), 1)
        self.assertEqual(read_settings(self.ini_path)["webhook_url"],
                         "https://open.feishu.cn/open-apis/bot/v2/hook/old")
        self.assertEqual(self.read_json(self.claude_path), {"theme": "dark", "hooks": OTHERS_STOP_HOOK})
        self.assertFalse(os.path.exists(self.codex_path))

    def test_answering_no_changes_nothing(self):
        self.assertEqual(self.install_with_answers(self.URL, "n"), 1)
        self.assertFalse(os.path.exists(self.ini_path))
        self.assertFalse(os.path.exists(self.codex_path))

    def test_broken_codex_file_stops_install_before_touching_claude_file(self):
        os.makedirs(os.path.dirname(self.codex_path))
        with open(self.codex_path, "w", encoding="utf-8") as codex_file:
            codex_file.write("{这不是合法的 JSON")
        with self.assertRaises(install_hooks.HookFileError) as caught:
            self.install_with_answers(self.URL, "y")
        self.assertIn(self.codex_path, str(caught.exception))
        self.assertEqual(self.read_json(self.claude_path), {"theme": "dark", "hooks": OTHERS_STOP_HOOK})

    def test_hooks_of_unexpected_shape_are_reported_in_chinese(self):
        with open(self.claude_path, "w", encoding="utf-8") as claude_file:
            json.dump({"hooks": ["不该是列表"]}, claude_file)
        with self.assertRaises(install_hooks.HookFileError) as caught:
            self.install_with_answers(self.URL, "y")
        self.assertIn(self.claude_path, str(caught.exception))


class WriteSettingsFileTest(unittest.TestCase):
    def setUp(self):
        self.ini_path = os.path.join(tempfile.mkdtemp(), "配置.ini")

    def test_new_file_has_defaults_and_is_private(self):
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        self.assertEqual(read_settings(self.ini_path), {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            "task_seconds_threshold": 300, "idle_seconds_threshold": 180, "summary_char_limit": 100})
        self.assertEqual(stat.S_IMODE(os.stat(self.ini_path).st_mode), 0o600)

    def test_write_settings_file_keeps_user_numbers_and_comments(self):
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        with open(self.ini_path, encoding="utf-8") as ini_file:
            edited = ini_file.read().replace("任务时长门槛秒 = 300", "任务时长门槛秒 = 600")
        with open(self.ini_path, "w", encoding="utf-8") as ini_file:
            ini_file.write(edited)
        write_settings_file(self.ini_path, "https://open.feishu.cn/open-apis/bot/v2/hook/new")
        settings = read_settings(self.ini_path)
        self.assertEqual(settings["task_seconds_threshold"], 600)
        self.assertEqual(settings["webhook_url"], "https://open.feishu.cn/open-apis/bot/v2/hook/new")
        self.assertEqual(stat.S_IMODE(os.stat(self.ini_path).st_mode), 0o600)
        with open(self.ini_path, encoding="utf-8") as ini_file:
            self.assertIn("#", ini_file.read())


if __name__ == "__main__":
    unittest.main()
