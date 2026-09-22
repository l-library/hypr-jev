"""daemon 协议离线回归:socket 请求/响应、confirm/choose 挂起与应答、
模式切换、双开保护、退出清理。

不触网、不依赖 Hyprland:Session 用替身;ASR 不加载;
HYPR_JEV_RUNTIME_DIR 指到临时目录,避免撞上真实 daemon。
用法: .venv/bin/python tests/test_daemon.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HYPR_JEV_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="hypr-jev-test-")

from hypr_jev.client import DaemonUnavailable, request  # noqa: E402
from hypr_jev.daemon import Daemon, parse_answer  # noqa: E402
from hypr_jev.executor import Action  # noqa: E402
from hypr_jev.runtime import socket_path  # noqa: E402
from hypr_jev.session import Outcome  # noqa: E402


class FakeSession:
    """Session 替身:按输入文本返回固定 Outcome,记录调用。"""

    def __init__(self):
        self.live = False
        self.handled, self.executed, self.chosen = [], [], []
        self.closed = False

    def handle(self, text):
        self.handled.append(text)
        if text == "CONFIRM":
            return Outcome(None, Action("confirm", "破坏性操作", command=["true"],
                                        desc="危险操作"))
        if text == "CHOOSE":
            return Outcome(None, Action(
                "choose", "请选择",
                choices=[("a", "甲", ["true"]), ("b", "乙", ["true"])], pick_id="b"))
        return Outcome(None, Action("execute", "通过门控", command=["true"],
                                    desc="打开终端"))

    def execute(self, action, decision=None):
        self.executed.append(action.desc or action.reason)
        return Outcome(decision, action, executed=True, ok=True, detail="fake")

    def choose(self, action, index):
        self.chosen.append(index)
        _cid, label, cmd = action.choices[index]
        return Outcome(None, Action("execute", label, command=cmd),
                       executed=True, ok=True, detail="fake")

    def warmup(self):
        return 1.0

    def close(self):
        self.closed = True


def wait_ready(deadline=5.0) -> None:
    end = time.time() + deadline
    last = None
    while time.time() < end:
        try:
            r = request({"cmd": "ping"}, timeout=2)
            if r.get("ok"):
                return
            last = r
        except DaemonUnavailable as e:
            last = e
        time.sleep(0.05)
    raise AssertionError(f"daemon 未就绪: {last}")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✅ {msg}")


def main() -> int:
    results = []
    session = FakeSession()
    daemon = Daemon(session, preload_asr=False)
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        wait_ready()

        # 1. ping
        r = request({"cmd": "ping"})
        expect(r["live"] is False and r["asr"] is False and r["pending"] is None,
               "ping 返回状态(live=False, asr=False, 无挂起)")

        # 2. handle:免确认命令,dry-run 只规划
        r = request({"cmd": "handle", "text": "打开终端"})
        expect(r["ok"] and not r["executed"] and r["pending"] is None
               and "将执行" in r["output"],
               "handle 正常出 Outcome(dry-run 规划,不执行)")

        # 3. confirm 挂起 → 拒绝应答
        r = request({"cmd": "handle", "text": "CONFIRM"})
        expect(r["pending"] and r["pending"]["type"] == "confirm"
               and r["pending"]["desc"] == "危险操作",
               "confirm 结果挂起,摘要返回给客户端")
        expect(session.executed == [], "挂起期间未执行任何命令")
        r = request({"cmd": "answer", "value": "n"})
        expect(r["ok"] and session.executed == [], "answer=n 取消,未执行")

        # 4. answer 无挂起时报错
        r = request({"cmd": "answer", "value": "y"})
        expect(not r["ok"] and "没有挂起" in r["error"], "无挂起时 answer 报错")

        # 5. confirm 挂起 → 确认执行
        request({"cmd": "handle", "text": "CONFIRM"})
        r = request({"cmd": "answer", "value": "y"})
        expect(r["ok"] and r["executed"] and r["exec_ok"]
               and session.executed == ["危险操作"],
               "answer=y 确认后执行成功")

        # 6. choose 挂起 → 编号应答(Jev 首选在下发摘要里)
        r = request({"cmd": "handle", "text": "CHOOSE"})
        expect(r["pending"]["choices"] == ["甲", "乙"]
               and r["pending"]["pick_id_index"] == 1,
               "choose 挂起返回候选与首选下标")
        r = request({"cmd": "answer", "value": "1"})
        expect(r["executed"] and session.chosen == [1], "answer=1 选中候选执行")

        # 7. choose 越界编号 → 取消
        request({"cmd": "handle", "text": "CHOOSE"})
        r = request({"cmd": "answer", "value": "9"})
        expect(r["ok"] and not r["executed"] and session.chosen == [1],
               "choose 越界编号安全取消")

        # 8. --yes 代答:confirm 直接执行
        r = request({"cmd": "handle", "text": "CONFIRM", "yes": True})
        expect(r["executed"] and r["exec_ok"] and r["pending"] is None,
               "yes=true 代答确认,直接执行")

        # 9. 模式切换
        r = request({"cmd": "mode", "live": True})
        expect(r["ok"] and session.live is True, "mode 切到 live")
        request({"cmd": "mode", "live": False})
        expect(session.live is False, "mode 切回 dry-run")

        # 10. 坏请求不杀 daemon
        import socket as _s
        with _s.socket(_s.AF_UNIX, _s.SOCK_STREAM) as s:
            s.connect(socket_path())
            s.sendall(b"not-json\n")
            r = s.makefile("r").readline()
        expect("ok" in r and '"ok": false' in r, "非 JSON 请求返回错误响应")
        expect(request({"cmd": "ping"})["ok"], "坏请求后 daemon 仍存活")

        # 11. 挂起 TTL 到期自动回收
        request({"cmd": "handle", "text": "CONFIRM"})
        expect(request({"cmd": "pending"})["pending"] is not None,
               "TTL 前:挂起存在")
        daemon.pending["expires"] = time.time() - 0.1
        time.sleep(0.8)   # > select 循环 tick(0.5s)
        expect(request({"cmd": "pending"})["pending"] is None,
               "TTL 过期后挂起被自动回收")

        # 12. 语音应答解析(纯函数):yes / no / 编号 / 非应答
        yes_no = [("是的", "yes"), ("好的", "yes"), ("确认执行", "yes"),
                  ("OK", "yes"), ("Yes.", "yes"), ("行吧", "yes"),
                  ("取消", "no"), ("算了", "no"), ("不要", "no"), ("先不要", "no")]
        expect(all(parse_answer(t) == w for t, w in yes_no), "yes/no 语音应答全命中")
        idx = [("第一个", 0), ("第2个", 1), ("选择第三个", 2), ("第十个", 9),
               ("2", 2), ("0", 0), ("2号", 2), ("选2", 2), ("选三", 3)]
        expect(all(parse_answer(t) == w for t, w in idx), "编号语音应答全命中")
        noans = ["打开终端", "关闭终端", "音量大一点", "十一", "三个", "", "继续"]
        expect(all(parse_answer(t) is None for t in noans),
               "普通命令/噪声不会被当成应答")

        # 13. 语音确认流程:挂起后 _voice_answer 命中/不命中
        request({"cmd": "handle", "text": "CONFIRM"})
        expect(not daemon._voice_answer("打开计算器") and daemon.pending is not None,
               "非应答语音返回 False,保持挂起走正常处理")
        expect(daemon._voice_answer("好的") and session.executed[-1] == "危险操作",
               "语音「好的」确认并执行")
        expect(daemon.pending is None, "语音应答后挂起清除")

        n_exec = len(session.executed)
        request({"cmd": "handle", "text": "CONFIRM"})
        expect(daemon._voice_answer("算了") and len(session.executed) == n_exec
               and not daemon.pending, "语音「算了」取消,不执行")

        request({"cmd": "handle", "text": "CONFIRM"})
        expect(not daemon._voice_answer("第一个") and daemon.pending is not None,
               "confirm 不把编号当应答,保持挂起")
        daemon.pending = None   # 清理后继续

        request({"cmd": "handle", "text": "CHOOSE"})
        expect(daemon._voice_answer("第一个") and session.chosen == [1, 0],
               "语音「第一个」选中 0 号候选")
        request({"cmd": "handle", "text": "CHOOSE"})
        expect(daemon._voice_answer("第二个") and session.chosen == [1, 0, 1],
               "语音「第二个」选中 1 号候选")
        request({"cmd": "handle", "text": "CHOOSE"})
        expect(daemon._voice_answer("好的") and session.chosen == [1, 0, 1, 1],
               "choose 挂起时「好的」= 选 Jev 首选")
        request({"cmd": "handle", "text": "CHOOSE"})
        expect(daemon._voice_answer("取消") and not daemon.pending
               and session.chosen == [1, 0, 1, 1], "choose 语音取消,未选择")

        # 14. 双开保护
        try:
            Daemon(FakeSession(), preload_asr=False)._open_socket(socket_path())
            raise AssertionError("双开未被拒绝")
        except SystemExit:
            expect(True, "已有活 daemon 时拒绝二次启动")

        # 15. quit 清理
        r = request({"cmd": "quit"})
        expect(r["ok"], "quit 命令被接受")
        thread.join(timeout=5)
        expect(not thread.is_alive(), "daemon 线程退出")
        expect(session.closed, "Session.close 被调用")
        expect(not os.path.exists(socket_path()), "socket 文件已清理")
        results.append(True)
    except AssertionError as e:
        print(f"  ❌ {e}")
        results.append(False)
    finally:
        daemon.stop()
        thread.join(timeout=3)

    ok = all(results)
    print(f"\n========== daemon 回归: {'全部通过' if ok else '存在失败'} ==========")
    shutil.rmtree(os.environ["HYPR_JEV_RUNTIME_DIR"], ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
