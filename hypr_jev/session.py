"""会话核心:utterance → 决策 → 门控 → (执行) → Outcome。

对输入源(现 shell,未来 ASR)和反馈端(现终端,未来 GUI/TTS)暴露的
唯一业务接口。输入源只需调用 handle() / execute() / choose();反馈端只消费 Outcome。
"""

from dataclasses import dataclass, field

from .apps import App, load_apps, search
from .config import CONF_EXECUTE
from .executor import (Action, close_command, launch_action, launch_command,
                       list_windows, plan, run)
from .router import Decision, JevRouter


@dataclass
class Outcome:
    decision: Decision | None   # choose/confirm 通道执行时可能为 None
    action: Action
    executed: bool = False
    ok: bool = False
    detail: str = ""


class Session:
    def __init__(self, live: bool = False):
        self.router = JevRouter()
        self.live = live
        self._apps: list[App] | None = None

    def handle(self, text: str) -> Outcome:
        """处理一句输入。除 live 下的免确认命令外不产生副作用。"""
        text = text.strip()
        if not text:
            return Outcome(None, Action("ignore", "空输入"))
        decision = self.router.route(text)
        if decision.intent == "launch_app":
            return self._handle_launch(text, decision)
        if decision.intent == "close_window":
            return self._handle_close(text, decision)
        action = plan(text, decision)
        if action.kind == "execute" and self.live:
            return self.execute(action, decision)
        return Outcome(decision, action)

    # ---- 应用启动(两级:本地检索 → Jev 语义精排)----

    def _handle_launch(self, text: str, decision: Decision) -> Outcome:
        if self._apps is None:
            self._apps = load_apps()
        candidates = search(self._apps, text)
        if candidates:
            pick = self.router.pick_app(text, candidates)
            pool = candidates            # 选择列表只给检索候选(少量)
        else:
            # 零命中回退:全量目录一次语义 Choice(中文泛称/呢称场景)
            pick = self.router.pick_app_wide(text, self._apps)
            pool = []
        lookup = pool or self._apps
        chosen = next((a for a in lookup if a.id == pick.app_id), None) \
            if pick.app_id else None
        if chosen is None:
            reason = ("没有匹配的已安装应用" if candidates or pick.app_id is None
                      else "语义匹配置信度过低,请用更完整的应用名")
            return Outcome(decision, Action(
                "ignore", f"{reason} (语义置信度 {pick.confidence:.2f})"))
        if pick.confidence >= CONF_EXECUTE:
            action = launch_action(chosen)
            if self.live:
                return self.execute(action, decision)
            return Outcome(decision, action)
        if not candidates:
            return Outcome(decision, Action(
                "ignore", f"全量语义匹配置信度偏低 ({pick.confidence:.2f}),请用更完整的应用名"))
        # 置信度不足 → 交还用户选择(Jev 首选标记为 pick_id)
        return Outcome(decision, Action(
            "choose",
            f"应用匹配置信度偏低 ({pick.confidence:.2f}),请选择:",
            choices=[(a.id, a.label, launch_command(a.id)) for a in candidates],
            pick_id=chosen.id,
        ))

    # ---- 定向关窗(打开窗口列表 → Jev 语义选窗口)----

    def _handle_close(self, text: str, decision: Decision) -> Outcome:
        windows = list_windows()
        if not windows:
            return Outcome(decision, Action("ignore", "当前没有打开的窗口"))
        pick = self.router.pick_window(text, windows)
        if pick.app_id is None:
            return Outcome(decision, Action(
                "ignore", f"没有匹配的打开窗口 (语义置信度 {pick.confidence:.2f})"))
        w = next((w for w in windows if w["address"] == pick.app_id), None)
        if w is None:
            return Outcome(decision, Action("ignore", "目标窗口刚刚关闭了"))
        if pick.confidence < CONF_EXECUTE:
            return Outcome(decision, Action(
                "choose",
                f"窗口匹配置信度偏低 ({pick.confidence:.2f}),请选择:",
                choices=[(w2["address"],
                          f'{w2["class"]} — {w2["title"]}',
                          close_command(w2["address"])) for w2 in windows],
                pick_id=pick.app_id,
            ))
        return Outcome(decision, Action(
            "confirm", "关闭窗口属破坏性操作,请确认",
            command=close_command(pick.app_id),
            desc=f'关闭窗口 {w["class"]} — {w["title"]}'))

    def choose(self, action: Action, index: int) -> Outcome:
        """用户从 choose 候选中选定后执行(显式选择即确认)。"""
        choices = action.choices or []
        if not 0 <= index < len(choices):
            return Outcome(None, Action("ignore", "无效选择"))
        _cid, label, cmd = choices[index]
        return self.execute(Action("execute", f"{label}", command=cmd))

    # ---- 通用执行 ----

    def execute(self, action: Action, decision: Decision | None = None) -> Outcome:
        """真正执行(用户确认后或免确认命令在 live 模式下)。"""
        if not self.live:
            return Outcome(decision, action)  # dry-run:止步于规划
        ok, detail = run(action)
        return Outcome(decision, action, executed=True, ok=ok, detail=detail)

    def warmup(self) -> float:
        return self.router.warmup()

    def close(self):
        self.router.close()
