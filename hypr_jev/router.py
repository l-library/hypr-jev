"""Jev 路由层:state( utterance + 活动窗口 ) → 结构化决策。

一次 API 调用并行携带 intent(Choice)/ needs_confirm(Noul)/ strength(Score)。
与输入源、执行方式完全解耦。
"""

import json
import subprocess
import time
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from .config import INTENT_ONLY, MODEL, QUESTIONS, WIDE_MAX


@dataclass
class Decision:
    utterance: str
    intent: str
    confidence: float
    probabilities: dict
    needs_confirm: float
    strength: float          # volume 步长等级 0~2,-1 表示该问题未携带
    latency_ms: float
    raw_probs: dict = field(default_factory=dict)


def get_active_window() -> dict | None:
    """hyprctl -j activewindow,失败返回 None(不阻塞路由)。"""
    try:
        out = subprocess.run(
            ["hyprctl", "-j", "activewindow"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if not out or out == "Invalid":
            return None
        w = json.loads(out)
        return {"class": w.get("class", ""), "title": w.get("title", "")}
    except Exception:
        return None


def build_state(utterance: str) -> dict:
    state: dict = {"utterance": utterance}
    win = get_active_window()
    if win:
        state["active_window"] = win
    return state


class JevRouter:
    def __init__(self, api_key: str | None = None):
        # max_retries=0:测量真实单次延迟,不隐藏重试
        self.client = TypeSafeClient(api_key=api_key, model=MODEL) \
            if api_key else TypeSafeClient(model=MODEL)

    def route(self, utterance: str, fanout: bool = True) -> Decision:
        """fanout=False 时只问 intent 单问题。"""
        questions = QUESTIONS if fanout else INTENT_ONLY
        state = build_state(utterance)
        t0 = time.perf_counter()
        resp = self.client.system_one(state=state, questions=questions)
        latency_ms = (time.perf_counter() - t0) * 1000

        ans = resp.answers
        choice = ans["intent"]
        noul = ans.get("needs_confirm")
        score = ans.get("strength")
        return Decision(
            utterance=utterance,
            intent=choice.choice,
            confidence=choice.confidence,
            probabilities=dict(choice.probabilities),
            needs_confirm=noul.noul if noul else 0.0,
            strength=score.score if score else -1.0,
            latency_ms=latency_ms,
        )

    def warmup(self) -> float:
        """预热连接(TLS/代理握手),返回冷启动耗时 ms。"""
        return self.route("warmup ping", fanout=False).latency_ms

    def pick_app(self, utterance: str, candidates) -> "AppPick":
        """第二级:对检索候选(少量)做语义精排。"""
        return self._pick(
            utterance,
            {a.id: a.label for a in candidates},
            instructions=(
                "Which candidate application does `utterance` ask to launch? "
                "Match by meaning, including Chinese names, English names and "
                "nicknames (e.g. 浏览器 → a browser). "
                "If none of the candidates fits, answer `none_of_these`."
            ),
        )

    def pick_app_wide(self, utterance: str, apps) -> "AppPick":
        """回退:本地检索零命中时,对全量应用目录做一次语义 Choice。

        仅适合应用数 ≤ WIDE_MAX 的机器;超过则放弃(诚实提示)。
        """
        if len(apps) > WIDE_MAX:
            return AppPick(None, 0.0, {}, 0.0)
        return self._pick(
            utterance,
            {a.id: a.label for a in apps},
            instructions=(
                "The options are every installed desktop application. "
                "Which one does `utterance` ask to launch? Match by meaning, "
                "including Chinese names, English names, categories and common "
                "nicknames (e.g. 浏览器 → a browser, 计算器 → Calculator, "
                "B站/哔哩哔哩 → bilibili). "
                "If no option fits, answer `none_of_these`."
            ),
        )

    def pick_window(self, utterance: str, windows: list[dict]) -> "AppPick":
        """定向关闭:从当前打开的窗口中语义匹配目标。"""
        criteria = {
            w["address"]: f"{w['class']} — {w['title']}"
                          + (" (currently focused)" if w["focused"] else "")
            for w in windows
        }
        return self._pick(
            utterance, criteria,
            instructions=(
                "The options are currently open windows. Which one does `utterance` "
                "ask to close? Match by meaning, including Chinese names and app "
                "categories (e.g. 终端 → a terminal window). If `utterance` refers to "
                "the current/active window, pick the one marked focused. "
                "If no window fits (e.g. that app is not running), answer `none_of_these`."
            ),
        )

    def _pick(self, utterance: str, mapping: dict[str, str], instructions: str) -> "AppPick":
        criteria = dict(mapping)
        criteria["none_of_these"] = (
            "The utterance does not clearly ask to launch any of the options"
        )
        questions = {
            "app": {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            },
        }
        t0 = time.perf_counter()
        resp = self.client.system_one(state={"utterance": utterance}, questions=questions)
        latency_ms = (time.perf_counter() - t0) * 1000
        ans = resp.answers["app"]
        return AppPick(
            app_id=None if ans.choice == "none_of_these" else ans.choice,
            confidence=ans.confidence,
            probabilities=dict(ans.probabilities),
            latency_ms=latency_ms,
        )

    def close(self):
        self.client.close()


@dataclass
class AppPick:
    app_id: str | None      # None = none_of_these
    confidence: float
    probabilities: dict
    latency_ms: float
