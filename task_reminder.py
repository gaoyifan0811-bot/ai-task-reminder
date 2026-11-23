"""AI 任务完成提醒：Claude Code 和 Codex 的钩子调用本脚本，由它决定是否往飞书发提醒。

用法（由钩子自动调用，不需要手动运行）：
    python3 task_reminder.py <claude|codex> <turn-start|turn-end|needs-approval|tool-finished>
"""
import configparser
import http.client
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

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
    subprocess.run(["/usr/bin/osascript", "-e", script], check=True, timeout=5)


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


def forget_turn_start(state_dir, session_id):
    """整件事做完后删掉开始记录，下一件事重新计时。"""
    try:
        os.remove(turn_start_file_path(state_dir, session_id))
    except FileNotFoundError:
        return  # 本来就没有记录（比如安装前就开始的会话）


def parse_hid_idle_seconds(ioreg_output):
    """从 ioreg 的输出里取出键盘鼠标空闲时间（原始单位是纳秒）。"""
    found = re.search(r'"HIDIdleTime" = (\d+)', ioreg_output)
    if found is None:
        raise ValueError("ioreg 输出里找不到 HIDIdleTime，输出开头是：%s" % ioreg_output[:200])
    return int(found.group(1)) / 1_000_000_000


def keyboard_mouse_idle_seconds():
    """键盘鼠标已经多少秒没动过。"""
    ioreg = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                           capture_output=True, text=True, check=True, timeout=2)
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


class FeishuSendError(Exception):
    """飞书消息没发出去：网络不通、HTTP 出错，或飞书返回了错误码。"""


def send_feishu_text(webhook_url, text):
    """往飞书群机器人发一条文字消息；不成功就抛 FeishuSendError。"""
    request_body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url, data=request_body, headers={"Content-Type": "application/json"})
    # 这台机器走代理而飞书在国内：空的 ProxyHandler 表示不用任何代理，直连
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with direct_opener.open(request, timeout=10) as response:
            reply_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as http_error:
        error_text = http_error.read().decode("utf-8", errors="replace")[:200]
        raise FeishuSendError("飞书返回 HTTP %d：%s" % (http_error.code, error_text))
    except (OSError, http.client.HTTPException) as network_error:
        # 断网、超时、连接中途被挂断都算在这里，交给外层重试
        raise FeishuSendError("连不上飞书：%r" % network_error)
    try:
        reply = json.loads(reply_text)
    except ValueError:
        raise FeishuSendError("飞书的回复不是 JSON：%s" % reply_text[:200])
    if not isinstance(reply, dict) or reply.get("code") != 0:
        raise FeishuSendError("飞书拒收：%s" % reply_text[:200])


def send_with_retry(webhook_url, text, attempts=3, wait_seconds=5):
    """发飞书，失败就隔几秒重试；全部失败后写日志并在 Mac 上弹窗。返回是否发成功。"""
    for attempt_number in range(1, attempts + 1):
        try:
            send_feishu_text(webhook_url, text)
            return True
        except FeishuSendError as send_error:
            last_reason = str(send_error)
            if attempt_number < attempts:
                time.sleep(wait_seconds)
    write_log_line(LOG_PATH, "飞书提醒发送失败（已试 %d 次）：%s｜消息：%s" % (attempts, last_reason, text))
    show_mac_alert("飞书提醒发送失败：%s" % last_reason)
    return False


def background_task_still_running(hook_input):
    """AI 这次停下时还有后台任务在跑：它只是先歇着，后台任务做完会被自动叫醒，整件事还没做完。

    只有 Claude Code 的「停下」钩子带 background_tasks；没有这个字段（比如 Codex）就当作没有后台任务。
    监视器（monitor）是一直开着盯某样东西的，不会「做完」，不算在内。
    """
    return any(task.get("status") == "running" and task.get("type") != "monitor"
               for task in hook_input.get("background_tasks") or [])


def background_wait_mark_file_path(state_dir, session_id):
    return turn_start_file_path(state_dir, session_id)[:-len(".txt")] + ".等后台任务"


def mark_waiting_for_background_task(state_dir, session_id):
    """记下「这个会话停下只是在等后台任务」，下次被叫醒时不重新计时。"""
    os.makedirs(state_dir, exist_ok=True)
    with open(background_wait_mark_file_path(state_dir, session_id), "w", encoding="utf-8"):
        pass


def clear_background_wait_mark(state_dir, session_id):
    """清掉「在等后台任务」的标记；返回清之前有没有这个标记。"""
    try:
        os.remove(background_wait_mark_file_path(state_dir, session_id))
    except FileNotFoundError:
        return False
    return True


def waiting_mark_file_path(state_dir, session_id):
    return turn_start_file_path(state_dir, session_id)[:-len(".txt")] + ".等待批准.json"


def read_waiting_mark(state_dir, session_id):
    """读「正在等批准」的标记：{"since": 开始等的时间, "tool_name": 等批准的工具}；没在等返回 None。"""
    try:
        with open(waiting_mark_file_path(state_dir, session_id), encoding="utf-8") as mark_file:
            return json.load(mark_file)
    except FileNotFoundError:
        return None


def mark_waiting_for_approval(state_dir, session_id, now_timestamp, tool_name):
    """记下「这个会话正卡在等批准上」：从什么时候开始、等的是哪个工具。"""
    os.makedirs(state_dir, exist_ok=True)
    with open(waiting_mark_file_path(state_dir, session_id), "w", encoding="utf-8") as mark_file:
        json.dump({"since": now_timestamp, "tool_name": tool_name}, mark_file, ensure_ascii=False)


def clear_waiting_for_approval(state_dir, session_id, finished_tool_name=None):
    """会话继续往下走了，不再算等批准。

    给了 finished_tool_name 时，只有等批准的正是这个工具才清掉——
    否则同时在跑的别的工具（比如自动放行的读文件）一完成，就会把标记误清。
    """
    if finished_tool_name is not None:
        mark = read_waiting_mark(state_dir, session_id)
        if mark is not None and mark["tool_name"] not in (None, finished_tool_name):
            return
    try:
        os.remove(waiting_mark_file_path(state_dir, session_id))
    except FileNotFoundError:
        return  # 本来就没在等，或者刚被另一个钩子清掉


def waiting_for_approval_since(state_dir, session_id):
    """这个会话从什么时候开始等批准；没在等则返回 None。"""
    mark = read_waiting_mark(state_dir, session_id)
    if mark is None:
        return None
    return mark["since"]


def summary_source_from_hook(event_kind, hook_input):
    """从钩子给的信息里挑出要放进提醒的那段文字；没有可用的就返回 None。"""
    if event_kind == "turn-end":
        source = hook_input.get("last_assistant_message")
    elif event_kind == "asks-question":
        questions = (hook_input.get("tool_input") or {}).get("questions") or []
        source = questions[0].get("question") if questions else None
    else:
        source = (hook_input.get("tool_input") or {}).get("command") or hook_input.get("tool_name")
    if source is None or isinstance(source, str):
        return source
    # 有的工具把命令给成列表而不是一段文字
    return json.dumps(source, ensure_ascii=False)


def decide_delivery(event_kind, elapsed_seconds, idle_seconds, settings):
    """返回 0 表示立刻发；正数表示过这么多秒再看一次；None 表示不发。"""
    if should_notify(elapsed_seconds, idle_seconds, settings):
        return 0
    if event_kind in ("needs-approval", "asks-question"):
        return settings["idle_seconds_threshold"]
    return None


def start_background_delivery(text, recheck_after_seconds, session_id, waiting_since):
    """另起一个脱离钩子的后台进程去发送，钩子本身立刻返回，不拖慢 AI。"""
    delivery_order = json.dumps({
        "text": text, "recheck_after_seconds": recheck_after_seconds,
        "session_id": session_id, "waiting_since": waiting_since})
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "deliver", delivery_order],
        start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# 等批准的补查最多盯这么久；人一直在电脑前却不理，Mac 自带的弹窗已经提醒过了
RECHECK_GIVE_UP_SECONDS = 1800


def deliver(text, recheck_after_seconds, session_id, waiting_since):
    """后台进程做的事：立刻发；或者是等批准的补查——那次请求还卡着、人也确实不在，才发。"""
    if recheck_after_seconds > 0:
        give_up_at = time.time() + RECHECK_GIVE_UP_SECONDS
        seconds_to_wait = recheck_after_seconds
        while True:
            time.sleep(seconds_to_wait)
            if waiting_for_approval_since(STATE_DIR, session_id) != waiting_since:
                return  # 已经批准或拒绝了，或者换成了更新的一次请求
            idle_seconds = keyboard_mouse_idle_seconds()
            if idle_seconds >= recheck_after_seconds:
                break
            if time.time() > give_up_at:
                return
            # 人刚才还在动键鼠：等到「如果从此不动」正好满门槛的那一刻再看
            seconds_to_wait = max(recheck_after_seconds - idle_seconds, 10.0)
    send_with_retry(read_settings(SETTINGS_PATH)["webhook_url"], text)


TOOL_LABELS = {"claude": "Claude Code", "codex": "Codex"}
HOOK_EVENTS = ("turn-start", "turn-end", "needs-approval", "tool-finished")


def report_problem(text):
    """工具自己出了问题：写日志并在 Mac 上弹窗，绝不悄悄失败。"""
    write_log_line(LOG_PATH, text)
    show_mac_alert(text)


def main(argv, stdin_text):
    """钩子入口。无论如何都返回 0，不能让提醒工具的问题打断 AI。"""
    if len(argv) != 2 or argv[0] not in TOOL_LABELS or argv[1] not in HOOK_EVENTS:
        report_problem("提醒脚本收到了不认识的参数：%s" % " ".join(argv))
        return 0
    tool, event = argv
    try:
        hook_input = json.loads(stdin_text)
    except ValueError:
        report_problem("钩子传来的内容不是 JSON：%s" % stdin_text[:100])
        return 0
    if not isinstance(hook_input, dict) or not hook_input.get("session_id"):
        report_problem("钩子传来的内容里缺少 session_id：%s" % stdin_text[:100])
        return 0
    session_id = hook_input["session_id"]
    now_timestamp = time.time()

    if event == "tool-finished":
        clear_waiting_for_approval(STATE_DIR, session_id, hook_input.get("tool_name"))
        return 0
    if event == "turn-start":
        clear_waiting_for_approval(STATE_DIR, session_id)
        was_waiting_for_background_task = clear_background_wait_mark(STATE_DIR, session_id)
        # 后台任务做完叫醒 AI 也算「一轮开始」，但整件事还是同一件，用时接着最初那条指令算
        if not (was_waiting_for_background_task and turn_elapsed_seconds(STATE_DIR, session_id, now_timestamp) is not None):
            record_turn_start(STATE_DIR, session_id, now_timestamp)
        return 0

    if not hook_input.get("cwd"):
        report_problem("钩子传来的内容里缺少 cwd：%s" % stdin_text[:100])
        return 0
    try:
        settings = read_settings(SETTINGS_PATH)
    except ReminderSettingsError as settings_error:
        report_problem(str(settings_error))
        return 0
    elapsed_seconds = turn_elapsed_seconds(STATE_DIR, session_id, now_timestamp)
    if event == "turn-end":
        clear_waiting_for_approval(STATE_DIR, session_id)
        if background_task_still_running(hook_input):
            mark_waiting_for_background_task(STATE_DIR, session_id)
            return 0  # 只做完了一小步，等整件事做完再提醒
        clear_background_wait_mark(STATE_DIR, session_id)
        forget_turn_start(STATE_DIR, session_id)
        waiting_since = None
    else:
        mark_waiting_for_approval(STATE_DIR, session_id, now_timestamp, hook_input.get("tool_name"))
        waiting_since = now_timestamp
        if hook_input.get("tool_name") == "AskUserQuestion":
            event = "asks-question"
    recheck_after_seconds = decide_delivery(event, elapsed_seconds, keyboard_mouse_idle_seconds(), settings)
    if recheck_after_seconds is None:
        return 0
    summary = one_line_summary(summary_source_from_hook(event, hook_input), settings["summary_char_limit"])
    text = build_message(TOOL_LABELS[tool], event, hook_input["cwd"], elapsed_seconds, summary)
    start_background_delivery(text, recheck_after_seconds, session_id, waiting_since)
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "deliver":
            delivery_order = json.loads(sys.argv[2])
            deliver(delivery_order["text"], delivery_order["recheck_after_seconds"],
                    delivery_order["session_id"], delivery_order["waiting_since"])
        else:
            main(sys.argv[1:], sys.stdin.read())
    except Exception:
        # 唯一的兜底：钩子不能让 AI 那一轮失败，但错误必须留痕并让人看见
        try:
            report_problem("提醒脚本出错：%s" % traceback.format_exc()[-300:])
        except Exception:
            # 连日志和弹窗都不行了（磁盘满、弹窗超时等）：写到标准错误，那里允许输出
            sys.stderr.write(traceback.format_exc())
    sys.exit(0)
