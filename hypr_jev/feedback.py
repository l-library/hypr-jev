"""反馈渲染:Outcome → 终端输出 + 桌面 OSD 通知。

未来 GUI/TTS 只需替换本模块,不动 Session/Router/Executor。
"""

from .executor import notify
from .session import Outcome


def render(outcome: Outcome) -> None:
    d, a = outcome.decision, outcome.action
    if d is not None:
        print(f"  · 意图 {d.intent}  置信度 {d.confidence:.2f}  "
              f"需确认 {d.needs_confirm:.2f}  强度 {d.strength:.2f}  "
              f"延迟 {d.latency_ms:.0f}ms")
        _desktop_note(outcome, prefix="heard")

    if a.kind == "ignore":
        print(f"  ⏭ {a.reason}")
        return
    if a.kind == "confirm":
        print(f"  ⏸ {a.reason}")
        if a.command:
            print(f"     待执行: {a.desc}")
        return
    if a.kind == "choose":
        print(f"  ⏸ {a.reason}")
        for i, (_cid, label, _cmd) in enumerate(a.choices or []):
            star = "   ★ Jev 首选" if _cid == a.pick_id else ""
            print(f"     {i}) {label}{star}")
        return

    # execute
    if not outcome.executed:
        print(f"  ⏸ [dry-run] 将执行: {a.desc}  (--live 生效)")
        return
    if outcome.ok:
        print(f"  ⚡ 已执行: {a.desc}")
        _desktop_note(outcome, prefix="done")
    else:
        print(f"  ✖ 执行失败: {outcome.detail}")


def _desktop_note(outcome: Outcome, prefix: str) -> None:
    d = outcome.decision
    if d is not None and outcome.executed:
        notify(f"{prefix}: {d.intent} ({d.confidence:.2f})")


def interact(session, outcome: Outcome, interactive: bool,
             auto_yes: bool = False) -> None:
    """confirm / choose 两类 Outcome 的交互处理(REPL / ASR / --once 共用)。"""
    a = outcome.action
    if a.kind == "confirm":
        if interactive:
            ans = input("     执行? [y/N] ")
        else:
            ans = "y" if auto_yes else "n"
            print(f"     执行? [y/N] {ans}")
        if ans.strip().lower() in ("y", "yes"):
            render(session.execute(a, outcome.decision))
    elif a.kind == "choose":
        n = len(a.choices or [])
        if interactive:
            raw = input(f"     选择编号 [0-{n - 1}](回车取消): ").strip()
        elif auto_yes and a.pick_id:
            idx = next((i for i, (aid, _l, _c) in enumerate(a.choices)
                        if aid == a.pick_id), 0)
            raw = str(idx)
            print(f"     选择编号 [0-{n - 1}](回车取消): {raw}  (--yes)")
        else:
            raw = ""
            print(f"     选择编号 [0-{n - 1}](回车取消): (未选择)")
        if raw.isdigit() and int(raw) < n:
            render(session.choose(a, int(raw)))
