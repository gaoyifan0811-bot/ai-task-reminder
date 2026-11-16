"""安装 / 卸载 AI 任务完成提醒。由「安装.command」「卸载.command」双击调用。

    python3 install_hooks.py install
    python3 install_hooks.py uninstall
"""
import copy
import json
import os
import re
import shlex
import shutil
import sys
import time

import task_reminder

CLAUDE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
CODEX_HOOKS_PATH = os.path.expanduser("~/.codex/hooks.json")
# 两个工具的钩子配置文件：结构相同，钩子都放在文件里的 "hooks" 键下
HOOK_FILES = {"claude": CLAUDE_SETTINGS_PATH, "codex": CODEX_HOOKS_PATH}

REMINDER_SCRIPT_NAME = "task_reminder.py"
REMINDER_SCRIPT_PATH = os.path.join(task_reminder.INSTALL_DIR, REMINDER_SCRIPT_NAME)

# 工具的钩子事件名 -> 传给提醒脚本的事件名
HOOK_EVENT_TO_REMINDER_EVENT = {
    "UserPromptSubmit": "turn-start",
    "Stop": "turn-end",
    "PermissionRequest": "needs-approval",
    "PostToolUse": "tool-finished",
}

FEISHU_WEBHOOK_PREFIXES = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/",
    "https://open.larksuite.com/open-apis/bot/v2/hook/",
)

SETTINGS_FILE_TEMPLATE = """# AI 任务完成提醒 的配置。用文本编辑器改完保存即可，下一次提醒就按新的来。
[提醒]

# 飞书群机器人的地址（相当于密码，不要发给别人）
飞书机器人地址 = {webhook_url}

# AI 这一轮跑了超过多少秒，就算「跑得久」，值得提醒
任务时长门槛秒 = 120

# 键盘鼠标超过多少秒没动，就算「人不在电脑前」
# AI 等你批准时，也是等这么多秒还没人理才补发提醒
离开门槛秒 = 180

# 提醒里附带 AI 回复或待执行命令的前多少个字。填 0 表示完全不带内容
摘要字数 = 100
"""


def validate_webhook_url(url):
    """检查粘贴进来的是不是飞书机器人地址，返回去掉首尾空白后的地址。"""
    cleaned_url = url.strip()
    for prefix in FEISHU_WEBHOOK_PREFIXES:
        token = cleaned_url[len(prefix):]
        if cleaned_url.startswith(prefix) and re.fullmatch(r"[A-Za-z0-9-]+", token):
            return cleaned_url
    raise ValueError(
        "这不是飞书机器人地址（应以 %s 开头，后面跟一串字母数字）。收到的是：%s"
        % (FEISHU_WEBHOOK_PREFIXES[0], url.strip()))


MOVED_FOLDER_ALERT_SCRIPT = (
    'display notification "找不到提醒脚本（文件夹是不是挪过位置？）请重新双击「安装.command」" '
    'with title "AI 任务提醒" sound name "Basso"')


def reminder_hook_command(script_path, tool, event_kind):
    """钩子里要运行的那条命令。

    结尾的 || true 很要紧：文件夹被挪走或删掉后，python 会因为找不到脚本而以 2 退出，
    而退出码 2 在两个工具里都表示「拦下这一步」，会把 AI 卡死。
    只在「用户发出指令」这一个事件上弹窗提示重装，其他事件不弹，免得一直刷屏。
    """
    command = "/usr/bin/python3 %s %s %s" % (shlex.quote(script_path), tool, event_kind)
    if event_kind == "turn-start":
        command += " || /usr/bin/osascript -e %s" % shlex.quote(MOVED_FOLDER_ALERT_SCRIPT)
    return command + " || true"


def remove_reminder_hooks(hooks_config):
    """去掉本工具加的钩子，别人的原样保留。返回新的字典，不改传进来的。"""
    cleaned_config = {}
    for event_name, groups in hooks_config.items():
        kept_groups = []
        for group in groups:
            kept_hooks = [hook for hook in group.get("hooks", [])
                          if REMINDER_SCRIPT_NAME not in hook.get("command", "")]
            if kept_hooks:
                kept_group = copy.deepcopy(group)
                kept_group["hooks"] = copy.deepcopy(kept_hooks)
                kept_groups.append(kept_group)
        if kept_groups:
            cleaned_config[event_name] = kept_groups
    return cleaned_config


def add_reminder_hooks(hooks_config, tool, script_path):
    """加上本工具的钩子（先去掉旧的，所以重复安装不会重复）。返回新的字典。"""
    updated_config = remove_reminder_hooks(hooks_config)
    for hook_event, reminder_event in HOOK_EVENT_TO_REMINDER_EVENT.items():
        command = reminder_hook_command(script_path, tool, reminder_event)
        updated_config.setdefault(hook_event, []).append(
            {"hooks": [{"type": "command", "command": command}]})
    return updated_config


def write_settings_file(ini_path, webhook_url):
    """生成配置文件；已有配置文件时只换机器人地址，保留用户改过的数字和注释。"""
    if os.path.exists(ini_path):
        with open(ini_path, encoding="utf-8") as ini_file:
            old_text = ini_file.read()
        new_text, replaced_count = re.subn(
            r"(?m)^飞书机器人地址\s*=.*$", lambda matched: "飞书机器人地址 = " + webhook_url, old_text)
        if replaced_count != 1:
            raise ValueError("配置文件 %s 里应当正好有一行「飞书机器人地址 = …」，实际有 %d 行" % (ini_path, replaced_count))
    else:
        new_text = SETTINGS_FILE_TEMPLATE.format(webhook_url=webhook_url)
    with open(ini_path, "w", encoding="utf-8") as ini_file:
        ini_file.write(new_text)
    os.chmod(ini_path, 0o600)


class HookFileError(Exception):
    """钩子配置文件读不懂，不敢改它。"""


def read_hook_file(hook_file_path):
    """读钩子配置文件并检查结构；文件不存在时当作空的。读不懂就报错，绝不硬改。"""
    if not os.path.exists(hook_file_path):
        return {}
    with open(hook_file_path, encoding="utf-8") as hook_file:
        try:
            file_content = json.load(hook_file)
        except json.JSONDecodeError as broken_json:
            raise HookFileError("配置文件 %s 不是合法的 JSON（%s），没有改动它。请先修好这个文件。"
                                % (hook_file_path, broken_json))
    hooks_config = file_content.get("hooks", {}) if isinstance(file_content, dict) else None
    shape_is_expected = isinstance(hooks_config, dict) and all(
        isinstance(groups, list) and all(
            isinstance(group, dict) and isinstance(group.get("hooks", []), list) and all(
                isinstance(hook, dict) and isinstance(hook.get("command", ""), str)
                for hook in group.get("hooks", []))
            for group in groups)
        for groups in hooks_config.values())
    if not shape_is_expected:
        raise HookFileError("配置文件 %s 里钩子部分的结构和预期不一样，没有改动它。" % hook_file_path)
    return file_content


def back_up_then_write_hook_file(hook_file_path, file_content):
    """先把原文件备份一份，再写入新内容。内容为空字典时删除文件（说明它原本就不存在）。"""
    if os.path.exists(hook_file_path):
        backup_path = hook_file_path + ".bak-提醒工具-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(hook_file_path, backup_path)
        print("  已备份：" + backup_path)
    if file_content == {}:
        if os.path.exists(hook_file_path):
            os.remove(hook_file_path)
        return
    os.makedirs(os.path.dirname(hook_file_path), exist_ok=True)
    with open(hook_file_path, "w", encoding="utf-8") as hook_file:
        json.dump(file_content, hook_file, ensure_ascii=False, indent=2)
        hook_file.write("\n")


def ask_for_webhook_url():
    """让用户粘贴机器人地址，格式不对就说明原因并重新问。已有配置时直接回车表示沿用。"""
    existing_url = None
    if os.path.exists(task_reminder.SETTINGS_PATH):
        existing_url = task_reminder.read_settings(task_reminder.SETTINGS_PATH)["webhook_url"]
    while True:
        if existing_url:
            typed_text = input("粘贴新的飞书机器人地址后回车（直接回车＝沿用现在的）：")
            if typed_text.strip() == "":
                return existing_url
        else:
            typed_text = input("粘贴飞书机器人地址后回车：")
        try:
            return validate_webhook_url(typed_text)
        except ValueError as invalid_url:
            print("✗ " + str(invalid_url))


def install():
    print("""
=== 安装 AI 任务完成提醒 ===

第一步：在飞书里准备一个机器人（已有就跳过）
  1. 手机或电脑飞书里新建一个群（只有你自己也行）
  2. 群设置 → 群机器人 → 添加机器人 → 自定义机器人
  3. 安全设置选「自定义关键词」，添加两个关键词：Claude 和 Codex
  4. 复制它给出的 Webhook 地址
""")
    # 先把两个工具的配置都读一遍：有读不懂的就在动手之前停下，不会装到一半
    new_file_contents = {}
    for tool, hook_file_path in HOOK_FILES.items():
        file_content = read_hook_file(hook_file_path)
        file_content["hooks"] = add_reminder_hooks(file_content.get("hooks", {}), tool, REMINDER_SCRIPT_PATH)
        new_file_contents[hook_file_path] = file_content

    webhook_url = ask_for_webhook_url()

    print("\n第二步：发一条测试消息……")
    try:
        task_reminder.send_feishu_text(webhook_url, "✅ Claude Code / Codex 任务提醒：安装测试消息，收到就说明通了")
    except task_reminder.FeishuSendError as send_error:
        print("✗ 测试消息没发出去：%s" % send_error)
        print("  没有改动 Claude Code 和 Codex 的任何配置。请检查机器人地址和关键词后重新双击安装。")
        return 1
    if input("手机飞书收到测试消息了吗？收到输入 y 回车，没收到输入 n 回车：").strip().lower() != "y":
        print("✗ 已停止，没有改动 Claude Code 和 Codex 的任何配置。")
        print("  请确认手机飞书的通知已打开、这个群没有设免打扰，然后重新双击安装。")
        return 1

    write_settings_file(task_reminder.SETTINGS_PATH, webhook_url)
    print("\n第三步：接到 Claude Code 和 Codex 上……")
    for hook_file_path, file_content in new_file_contents.items():
        back_up_then_write_hook_file(hook_file_path, file_content)
        print("  已写入：" + hook_file_path)

    print("""
✓ 安装完成。

还差最后一步，需要你手动做一次：
  打开 Codex 桌面 App →「设置 → Hooks」，把新出现的钩子点「信任」。
  （Codex 出于安全考虑，新钩子不点信任不会运行。Claude Code 不需要这一步。）

如果哪天飞书发不出去，Mac 右上角会弹「飞书提醒发送失败」的通知。
（这条通知是以「脚本编辑器」的名义弹的：系统设置 → 通知 里别把它关掉。）

想调提醒的门槛：用文本编辑器打开本文件夹里的「配置.ini」。
想停用：双击「卸载.command」。
注意：这个文件夹挪了位置之后，要重新双击一次「安装.command」。
""")
    return 0


def uninstall():
    print("\n=== 卸载 AI 任务完成提醒 ===\n")
    file_contents = {hook_file_path: read_hook_file(hook_file_path) for hook_file_path in HOOK_FILES.values()}
    for hook_file_path, file_content in file_contents.items():
        if "hooks" not in file_content:
            print("  本来就没有钩子，不用改：" + hook_file_path)
            continue
        remaining_hooks = remove_reminder_hooks(file_content["hooks"])
        if remaining_hooks:
            file_content["hooks"] = remaining_hooks
        else:
            del file_content["hooks"]
        back_up_then_write_hook_file(hook_file_path, file_content)
        print("  已移除提醒钩子：" + hook_file_path)
    print("\n✓ 卸载完成，Claude Code 和 Codex 不会再调用提醒脚本。")
    print("  「配置.ini」和「日志.txt」还留在本文件夹里；彻底不用了，把整个文件夹删掉即可。\n")
    return 0


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["install"]:
            sys.exit(install())
        if sys.argv[1:] == ["uninstall"]:
            sys.exit(uninstall())
    except (HookFileError, task_reminder.ReminderSettingsError) as stop_reason:
        sys.exit("✗ 已停止：%s" % stop_reason)
    sys.exit("用法：python3 install_hooks.py install 或 uninstall（收到的参数：%s）" % " ".join(sys.argv[1:]))
