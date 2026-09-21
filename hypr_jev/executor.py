"""Hyprland 执行层:Decision → 动作规划 + 执行。

plan() 只规划不执行;run() 才执行。门控策略在 plan() 中完成。
Hyprland >= 0.55:hyprctl dispatch 参数是 Lua 表达式(hl.dsp.*),旧字符串语法已废弃。
"""

import json
import re
import subprocess
from dataclasses import dataclass

from .config import CONF_ASK, CONF_EXECUTE, NEEDS_CONFIRM_MAX
from .apps import App


@dataclass
class Action:
    kind: str        # execute | confirm | ignore | choose
    reason: str
    command: list[str] | None = None
    desc: str = ""
    choices: list[tuple[str, str, list[str]]] | None = None   # choose:(id, label, command)
    pick_id: str | None = None                     # choose:Jev 精排首选


def launch_command(app_id: str) -> list[str]:
    # 经 hyprctl exec 托管:立即返回,应用独立存活;gtk-launch 直跑会阻塞等子进程
    return ["hyprctl", "dispatch", f"hl.dsp.exec_cmd('gtk-launch {app_id}')"]


def close_command(address: str) -> list[str]:
    # 0.56 窗口关闭语法:只有对象形式 { window = "address:0x..." } 能精确关闭。
    # 位置参数 "address:..." 在目标非聚焦时静默失效(实测),甚至有误关聚焦窗口的风险。
    return ["hyprctl", "dispatch",
            f'hl.dsp.window.close({{ window = "address:{address}" }})']


def launch_action(app: App) -> Action:
    return Action("execute", f"启动 {app.label}",
                  command=launch_command(app.id),
                  desc=f"gtk-launch {app.id}  ({app.label})")


def list_windows(limit: int = 50) -> list[dict]:
    """当前打开的窗口(聚焦的排最前),供 Jev 定向关闭。

    注:0.56 的 clients JSON 不含 focused 字段,需从 activewindow 取。
    """
    try:
        out = subprocess.run(["hyprctl", "-j", "clients"],
                             capture_output=True, text=True, timeout=3).stdout
        clients = json.loads(out)
    except Exception:
        return []
    clients = [c for c in clients if c.get("mapped") and c.get("class")]
    try:
        aw = json.loads(subprocess.run(["hyprctl", "-j", "activewindow"],
                                       capture_output=True, text=True,
                                       timeout=2).stdout)
        focused_addr = aw.get("address")
    except Exception:
        focused_addr = None
    clients.sort(key=lambda c: (c["address"] != focused_addr, c.get("class", "")))
    return [{"address": c["address"], "class": c["class"],
             "title": c.get("title", ""), "focused": c["address"] == focused_addr}
            for c in clients[:limit]]


def hyprctl(*args: str) -> tuple[bool, str]:
    r = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def notify(text: str) -> None:
    """桌面 OSD 反馈(未来 GUI/TTS 的现状替代)。

    常驻 daemon 里由渲染路径调用,任何失败都只降级、不上抛,
    避免反馈通道(如 hyprctl 短暂不可用)杀死主循环。
    """
    try:
        hyprctl("notify", "1", "3000", "0", f"fontsize(14) {text}")
    except Exception:
        pass


def _volume_pct(decision) -> int:
    """strength Score(0~2) → 步长百分比。"""
    return [5, 15, 30][min(max(round(decision.strength), 0), 2)]


def plan(utterance: str, decision) -> Action:
    """门控 + 命令规划。不执行任何东西。"""
    if decision.intent == "other":
        return Action("ignore", "非命令语句 (other)")
    if decision.confidence < CONF_ASK:
        return Action("ignore", f"置信度过低 ({decision.confidence:.2f} < {CONF_ASK}),忽略")
    if decision.confidence < CONF_EXECUTE or decision.needs_confirm >= NEEDS_CONFIRM_MAX:
        return Action(
            "confirm",
            f"置信度 {decision.confidence:.2f} 或需确认 {decision.needs_confirm:.2f} → 需用户确认",
        )

    cmd = _command_for(utterance, decision)
    if cmd is None:
        return Action("ignore", "命令参数解析失败(如未找到工作区编号)")
    return Action("execute", "通过门控", command=cmd, desc=" ".join(cmd))


def _command_for(utterance: str, decision) -> list[str] | None:
    # 终端启动走 launch_app、关窗走定向流程(session 内拦截),均不经过此处
    it = decision.intent
    if it == "switch_workspace":
        m = re.search(r"(\d+)", utterance)
        return ["hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{m.group(1)}" }})'] if m else None
    if it == "toggle_floating":
        return ["hyprctl", "dispatch", "hl.dsp.window.float()"]
    if it == "volume_up":
        return ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{_volume_pct(decision)}%+"]
    if it == "volume_down":
        return ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{_volume_pct(decision)}%-"]
    if it == "screenshot":      # 安全占位:notify 代替真实截图,避免副作用
        return ["hyprctl", "notify", "1", "3000", "0", "fontsize(14) hypr-jev: screenshot triggered"]
    if it == "lock_screen":     # 同上,不真锁屏
        return ["hyprctl", "notify", "1", "3000", "0", "fontsize(14) hypr-jev: lock_screen triggered"]
    return None


def run(action: Action) -> tuple[bool, str]:
    if not action.command:
        return False, "no command"
    try:
        r = subprocess.run(action.command, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "command timed out"
    return r.returncode == 0, (r.stdout or r.stderr).strip()
