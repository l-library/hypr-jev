"""意图识别基准测试:中英文准确率 + 延迟 + fan-out 对比(真实 API,~40 次调用)。

用法: .venv/bin/python tests/test_intents.py
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypr_jev.router import JevRouter  # noqa: E402

# (utterance, expected_intent)
CASES = [
    # ---- 中文 ----
    ("打开终端", "launch_app"),
    ("帮我把音量调大一点", "volume_up"),
    ("音量小一点", "volume_down"),
    ("切换到工作区3", "switch_workspace"),
    ("把这个窗口变成浮动的", "toggle_floating"),
    ("关闭当前窗口", "close_window"),
    ("截个屏", "screenshot"),
    ("锁屏", "lock_screen"),
    ("今天天气怎么样", "other"),
    ("帮我写一首诗", "other"),
    # ---- 英文 ----
    ("open a terminal", "launch_app"),
    ("turn the volume up", "volume_up"),
    ("volume down a bit", "volume_down"),
    ("go to workspace 3", "switch_workspace"),
    ("make this window float", "toggle_floating"),
    ("close the current window", "close_window"),
    ("take a screenshot", "screenshot"),
    ("lock the screen", "lock_screen"),
    ("what's the weather like", "other"),
    ("tell me a joke", "other"),
    # ---- 中英混合 / 口语化 ----
    ("把 terminal 打开一下", "launch_app"),
    ("could you close this window please", "close_window"),
]


def pctl(xs, p):
    xs = sorted(xs)
    return xs[min(int(len(xs) * p), len(xs) - 1)]


def main() -> int:
    router = JevRouter()
    print(f"冷启动(含 TLS/代理握手): {router.warmup():.0f} ms\n")

    rows, lat = [], []
    for utt, expected in CASES:
        d = router.route(utt)
        ok = d.intent == expected
        lat.append(d.latency_ms)
        rows.append((utt, expected, d, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {utt!r:42s} -> {d.intent:16s}"
              f" conf={d.confidence:.2f} confirm={d.needs_confirm:.2f}"
              f" strength={d.strength:.2f} {d.latency_ms:.0f}ms")

    acc = lambda rs: sum(r[3] for r in rs) / len(rs)  # noqa: E731
    print("\n========== 汇总 ==========")
    print(f"总体准确率: {acc(rows):.0%} ({sum(r[3] for r in rows)}/{len(rows)})")
    print(f"延迟 ms: p50={pctl(lat, .5):.0f}  p95={pctl(lat, .95):.0f}  "
          f"min={min(lat):.0f}  max={max(lat):.0f}")
    for utt, exp, d, _ in [r for r in rows if not r[3]]:
        print(f"  失败: {utt!r} 期望 {exp}, 实际 {d.intent} "
              f"(P({exp})={d.probabilities.get(exp, 0):.2f})")

    print("\n========== fan-out 对比(加问题几乎不加延迟?) ==========")
    probes = ["帮我把音量调大一点", "go to workspace 3", "关闭当前窗口"]
    for label, fanout in (("单问题", False), ("三问题", True)):
        ts = []
        for u in probes:
            for _ in range(2):
                ts.append(router.route(u, fanout=fanout).latency_ms)
                time.sleep(0.1)
        print(f"{label}: 均值 {statistics.mean(ts):.0f}ms  p50 {pctl(ts, .5):.0f}ms  "
              f"min {min(ts):.0f}ms  max {max(ts):.0f}ms")

    router.close()
    return 0 if all(r[3] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
