"""AI 任务完成提醒：Claude Code 和 Codex 的钩子调用本脚本，由它决定是否往飞书发提醒。

用法（由钩子自动调用，不需要手动运行）：
    python3 task_reminder.py <claude|codex> <turn-start|turn-end|needs-approval>
"""
import configparser
import os
import re
import subprocess
import time

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(INSTALL_DIR, "配置.ini")
LOG_PATH = os.path.join(INSTALL_DIR, "日志.txt")
STATE_DIR = os.path.join(INSTALL_DIR, "状态")

LOG_SIZE_LIMIT_BYTES = 1_000_000

SETTINGS_SECTION = "提醒"
WEBHOOK_URL_KEY = "飞书机器人地址"
# 配置文件里的中文键名 -> 程序里用的名字（这三项都必须是整数）
NUMBER_KEYS = {
    "任务时长门槛秒": "task_seconds_threshold",
    "离开门槛秒": "idle_seconds_threshold",
    "摘要字数": "summary_char_limit",
}


class ReminderSettingsError(Exception):
    """配置文件缺失、缺键或数值不合法。"""


def read_settings(ini_path):
    """读取配置文件，返回 webhook_url 和三个整数门槛。"""
    if not os.path.exists(ini_path):
        raise ReminderSettingsError("找不到配置文件：%s（请重新双击「安装.command」）" % ini_path)
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    if not parser.has_section(SETTINGS_SECTION):
        raise ReminderSettingsError("配置文件 %s 里缺少 [%s] 这一段" % (ini_path, SETTINGS_SECTION))
    section = parser[SETTINGS_SECTION]
    for key in [WEBHOOK_URL_KEY] + list(NUMBER_KEYS):
        if key not in section:
            raise ReminderSettingsError("配置文件 %s 里缺少「%s」这一项" % (ini_path, key))
    settings = {"webhook_url": section[WEBHOOK_URL_KEY].strip()}
    for key, name in NUMBER_KEYS.items():
        raw_value = section[key].strip()
        if not raw_value.isdigit():
            raise ReminderSettingsError(
                "配置文件 %s 里「%s」应为不带小数的非负整数，实际是：%s" % (ini_path, key, raw_value))
        settings[name] = int(raw_value)
    return settings


def write_log_line(log_path, text):
    """往日志追加一行；日志超过 1 MB 时先清空。"""
    if os.path.exists(log_path) and os.path.getsize(log_path) > LOG_SIZE_LIMIT_BYTES:
        open_mode = "w"
    else:
        open_mode = "a"
    with open(log_path, open_mode, encoding="utf-8") as log_file:
        log_file.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + text.replace("\n", " ") + "\n")


def show_mac_alert(text):
    """在 Mac 右上角弹一条系统通知。"""
    quoted_text = text.replace("\\", "\\\\").replace('"', '\\"')
    script = 'display notification "%s" with title "AI 任务提醒" sound name "Basso"' % quoted_text
    subprocess.run(["/usr/bin/osascript", "-e", script], check=True)


def turn_start_file_path(state_dir, session_id):
    """每个会话一个小文件，文件名里只保留字母、数字、下划线和短横线。"""
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return os.path.join(state_dir, safe_name + ".txt")


def record_turn_start(state_dir, session_id, now_timestamp):
    """记下这一轮的开始时间（同一会话再次记录会覆盖）。"""
    os.makedirs(state_dir, exist_ok=True)
    with open(turn_start_file_path(state_dir, session_id), "w", encoding="utf-8") as start_file:
        start_file.write(repr(now_timestamp))


def turn_elapsed_seconds(state_dir, session_id, now_timestamp):
    """这一轮已经跑了多少秒；没有开始记录时返回 None。"""
    start_path = turn_start_file_path(state_dir, session_id)
    if not os.path.exists(start_path):
        return None
    with open(start_path, encoding="utf-8") as start_file:
        return now_timestamp - float(start_file.read())


def parse_hid_idle_seconds(ioreg_output):
    """从 ioreg 的输出里取出键盘鼠标空闲时间（原始单位是纳秒）。"""
    found = re.search(r'"HIDIdleTime" = (\d+)', ioreg_output)
    if found is None:
        raise ValueError("ioreg 输出里找不到 HIDIdleTime，输出开头是：%s" % ioreg_output[:200])
    return int(found.group(1)) / 1_000_000_000


def keyboard_mouse_idle_seconds():
    """键盘鼠标已经多少秒没动过。"""
    ioreg = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                           capture_output=True, text=True, check=True)
    return parse_hid_idle_seconds(ioreg.stdout)


def should_notify(elapsed_seconds, idle_seconds, settings):
    """任务跑得久，或者人不在电脑前，满足其一就提醒。"""
    ran_long = elapsed_seconds is not None and elapsed_seconds > settings["task_seconds_threshold"]
    user_away = idle_seconds > settings["idle_seconds_threshold"]
    return ran_long or user_away


def format_duration(seconds):
    if seconds < 60:
        return "%d 秒" % seconds
    return "%d 分钟" % (seconds // 60)


def one_line_summary(source_text, char_limit):
    """把一段文字压成一行并截断；没有文字或字数上限为 0 时返回空串。"""
    if not source_text or char_limit == 0:
        return ""
    single_line = " ".join(source_text.split())
    if len(single_line) > char_limit:
        return single_line[:char_limit] + "…"
    return single_line


# 事件 -> (图标, 状态说法, 时长前的说法, 摘要前缀)
EVENT_WORDING = {
    "turn-end": ("✅", "跑完了", "用时", ""),
    "needs-approval": ("⏸", "等你批准", "已运行", "想执行："),
    "asks-question": ("❓", "在问你", "已运行", ""),
}


def build_message(tool_label, event_kind, cwd, elapsed_seconds, summary):
    """拼出发到飞书的提醒文字。"""
    icon, status_words, duration_words, summary_prefix = EVENT_WORDING[event_kind]
    folder_name = os.path.basename(cwd.rstrip("/"))
    first_line = "%s %s %s｜%s" % (icon, tool_label, status_words, folder_name)
    if elapsed_seconds is not None:
        first_line += "｜%s %s" % (duration_words, format_duration(elapsed_seconds))
    if summary:
        return first_line + "\n" + summary_prefix + summary
    return first_line
