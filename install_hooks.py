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


def reminder_hook_command(script_path, tool, event_kind):
    return "/usr/bin/python3 %s %s %s" % (shlex.quote(script_path), tool, event_kind)


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


def read_hook_file(hook_file_path):
    """读钩子配置文件；文件不存在时当作空的。"""
    if not os.path.exists(hook_file_path):
        return {}
    with open(hook_file_path, encoding="utf-8") as hook_file:
        return json.load(hook_file)


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
    webhook_url = ask_for_webhook_url()
    write_settings_file(task_reminder.SETTINGS_PATH, webhook_url)

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

    print("\n第三步：接到 Claude Code 和 Codex 上……")
    for tool, hook_file_path in HOOK_FILES.items():
        file_content = read_hook_file(hook_file_path)
        file_content["hooks"] = add_reminder_hooks(file_content.get("hooks", {}), tool, REMINDER_SCRIPT_PATH)
        back_up_then_write_hook_file(hook_file_path, file_content)
        print("  已写入：" + hook_file_path)

    print("""
✓ 安装完成。

还差最后一步，需要你手动做一次：
  打开 Codex 桌面 App →「设置 → Hooks」，把新出现的钩子点「信任」。
  （Codex 出于安全考虑，新钩子不点信任不会运行。Claude Code 不需要这一步。）

想调提醒的门槛：用文本编辑器打开本文件夹里的「配置.ini」。
想停用：双击「卸载.command」。
注意：这个文件夹挪了位置之后，要重新双击一次「安装.command」。
""")
    return 0


def uninstall():
    print("\n=== 卸载 AI 任务完成提醒 ===\n")
    for hook_file_path in HOOK_FILES.values():
        file_content = read_hook_file(hook_file_path)
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
    if sys.argv[1:] == ["install"]:
        sys.exit(install())
    if sys.argv[1:] == ["uninstall"]:
        sys.exit(uninstall())
    sys.exit("用法：python3 install_hooks.py install 或 uninstall（收到的参数：%s）" % " ".join(sys.argv[1:]))
