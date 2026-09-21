"""端到端集成测试:文字 → Jev 决策 → 门控 → Hyprland 执行 → 验证 → 恢复。

只做可逆/无副作用动作并自动恢复现场;破坏性命令只验证门控拦截。
用法: .venv/bin/python tests/test_exec.py            # live 执行(推荐)
      .venv/bin/python tests/test_exec.py --dry      # 只走链路不执行
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypr_jev.executor import hyprctl  # noqa: E402
from hypr_jev.session import Session  # noqa: E402


def cur_workspace() -> int:
    out = subprocess.run(["hyprctl", "-j", "activeworkspace"],
                         capture_output=True, text=True).stdout
    return json.loads(out)["id"]


def cur_volume() -> float:
    out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                         capture_output=True, text=True).stdout
    return float(out.strip().split()[-1])


def cur_floating() -> bool:
    out = subprocess.run(["hyprctl", "-j", "activewindow"],
                         capture_output=True, text=True).stdout
    return bool(json.loads(out).get("floating"))


def clients_with(cls: str) -> int:
    out = subprocess.run(["hyprctl", "-j", "clients"],
                         capture_output=True, text=True).stdout
    return sum(1 for c in json.loads(out)
               if c.get("mapped") and cls in c.get("class", "").lower())


def step(session, name, utt, expect_kind, verify=None, restore=None,
        settle=0.0) -> bool:
    """通用步骤:handle → 断言门控类型 → (live 下)验证 → 恢复。"""
    outcome = session.handle(utt)
    d, a = outcome.decision, outcome.action
    print(f"\n▶ [{name}] {utt!r}")
    print(f"  决策: intent={d.intent if d else '-'} conf={d.confidence if d else 0:.2f} "
          f"confirm={d.needs_confirm if d else 0:.2f} → {a.kind}")
    if a.kind != expect_kind:
        print(f"  ❌ 门控类型不符: 期望 {expect_kind}, 实际 {a.kind}")
        return False
    if expect_kind == "confirm":
        print("  ✅ 门控拦截(未执行)")
        return True
    if expect_kind == "ignore":
        print("  ✅ 已忽略")
        return True
    if not session.live:
        print("  ✅ [dry-run] 链路正常,未执行")
        return True
    if settle:
        import time
        time.sleep(settle)
    ok = outcome.executed and outcome.ok and (verify is None or verify())
    print(f"  {'✅ 执行+验证通过' if ok else '❌ ' + outcome.detail}")
    if restore:
        restore()
    return ok


def main() -> int:
    dry = "--dry" in sys.argv
    session = Session(live=not dry)
    session.warmup()
    results = []

    # 1. 通知类(无副作用)
    results.append(step(session, "通知", "截个屏", "execute",
                        verify=lambda: True))

    # 2. 工作区切换 → 验证 → 切回
    ws0 = cur_workspace()
    results.append(step(session, "工作区", "切换到工作区9", "execute",
                        verify=lambda: cur_workspace() == 9,
                        restore=lambda: hyprctl(
                            "dispatch", 'hl.dsp.focus({ workspace = "%d" })' % ws0)))

    # 3. 音量 → 验证 → 恢复
    vol0 = cur_volume()
    results.append(step(session, "音量", "帮我把音量调大一点", "execute",
                        verify=lambda: cur_volume() > vol0,
                        restore=lambda: subprocess.run(
                            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
                             f"{vol0:.2f}"], capture_output=True)))

    # 4. 浮动切换 → 验证 → 切回
    fl0 = cur_floating()
    results.append(step(session, "浮动", "把这个窗口变成浮动的", "execute",
                        verify=lambda: cur_floating() != fl0,
                        restore=lambda: hyprctl("dispatch",
                                                 "hl.dsp.window.float()")))

    # 5. 应用启动 → 验证 → 定向关窗(对象形式按地址精确关闭)→ 验证
    results.append(step(session, "启动应用", "打开计算器", "execute",
                        verify=lambda: clients_with("calculator") == 1,
                        settle=2.5))
    if clients_with("calculator"):
        outcome = session.handle("关闭计算器")
        a = outcome.action
        print(f"\n▶ [定向关窗] 关闭计算器 → {a.kind}")
        ok = a.kind == "confirm"
        if ok:
            from hypr_jev.executor import run
            rok, detail = run(a)
            import time as _t
            _t.sleep(1.5)
            ok = rok and clients_with("calculator") == 0
            print(f"  {'✅ 确认后按地址精确关闭,目标窗口已消失' if ok else '❌ ' + detail}")
        results.append(ok)

    # 6. 破坏性命令:门控应拦截
    results.append(step(session, "锁屏门控", "锁屏", "confirm"))
    # 7. 非命令:应忽略
    results.append(step(session, "非命令", "今天天气怎么样", "ignore"))

    print(f"\n========== 集成测试: {sum(results)}/{len(results)} 通过 ==========")
    session.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
