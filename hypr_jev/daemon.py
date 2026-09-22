"""常驻 daemon:预热 Jev 连接 + 预载 ASR 模型,消除每次调用的冷启动。

单次冷启动的两笔开销(TLS/代理握手 ~4.8s、whisper 模型加载数秒)
只在 daemon 启动时付一次;此后进程内连接池常驻复用。

对外入口(路径见 runtime.py):
  - Unix socket:文本请求(单行 JSON → 单行 JSON)
  - PTT FIFO:语音按住说话,令牌协议与 --asr 模式完全一致;
    confirm/choose 挂起后,下一句语音若整句恰好是「是/否/编号」则直接作为应答

socket 协议:
  {"cmd":"handle","text":"…","yes":false}   处理一句;confirm/choose 未应答时挂起
  {"cmd":"answer","value":"y"|"n"|"2"|""}   应答挂起的 confirm/choose
  {"cmd":"mode","live":true}                切换 live / dry-run
  {"cmd":"pending"}                          查询挂起状态
  {"cmd":"ping"}                             健康检查(状态/运行时长)
  {"cmd":"quit"}                             退出 daemon

响应统一信封:{"ok":true,"cmd":…,"output":"已渲染文本","executed":…,"exec_ok":…,
"pending":{…}|null};错误为 {"ok":false,"error":"…"}。

设计:单线程 select 循环,无锁。语音采集(FIFO 令牌)即时响应;
转写/决策短暂阻塞循环(亚秒级),文本请求在内核 backlog 排队。
"""

import contextlib
import io
import json
import os
import re
import select
import signal
import socket
import threading
import time

from . import runtime
from .asr import Recorder, Transcriber
from .config import ASR_RATE, ASR_TIMEOUT_S
from .feedback import render

PENDING_TTL_S = 120.0     # 挂起的 confirm/choose 超时自动取消
REQUEST_TIMEOUT_S = 2.0   # 客户端连接读超时(不发请求的连接直接丢弃)


def _capture(fn, *args) -> str:
    """把 render() 的终端输出捕获为字符串(反馈端复用,不改 feedback.py)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


# ---- 语音应答解析(纯函数,离线可测)----

_YES = {
    "y", "yes", "yeah", "yep", "ok", "okay", "sure", "confirm",
    "是", "是的", "是呀", "是啊", "对", "对的", "对呀", "对啊",
    "好", "好的", "好吧", "好呀", "好啊", "好嘞", "确认", "确定", "确认执行", "没问题",
    "可以", "行", "行的", "行吧", "行呀", "行啊", "执行", "执行吧", "就这么办",
}
_NO = {
    "n", "no", "nope", "cancel",
    "不", "不吧", "不要", "不要了", "不好", "不行", "不行的", "不用", "不用了",
    "不必", "别", "别吧", "别执行", "先别", "先不要", "算了", "算了吧",
    "取消", "取消吧", "否", "不执行",
}
_CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"([0-9]+|[零〇一二两三四五六七八九十]+)"


def _num(s: str) -> int | None:
    """阿拉伯或中文数字串(0–99)→ int;无法解析返回 None。"""
    if s.isdigit():
        return int(s)
    if "十" in s:
        tens, _, ones = s.partition("十")
        if (tens and tens not in _CN_NUM) or (ones and ones not in _CN_NUM):
            return None
        return ((_CN_NUM[tens] if tens else 1) * 10
                + (_CN_NUM[ones] if ones else 0))
    return _CN_NUM.get(s)


def parse_answer(text: str) -> str | int | None:
    """把一句转写解析为挂起 confirm/choose 的应答。

    返回 "yes" / "no" / 0 基编号(int)/ None(不是应答,按普通命令处理)。
    只有整句恰好是应答才命中——包含式匹配在语音场景太容易误执行。

    编号规则:带「第」是第几个(1 基,「第一个」→ 0);不带「第」
    (裸数字 /「N号」/「选N」)是屏幕上印出的编号(0 基)。
    """
    norm = re.sub(r"[\s，。、.,!！?？~～·]+", "", text).lower()
    if not norm:
        return None
    if norm in _YES:
        return "yes"
    if norm in _NO:
        return "no"
    m = re.fullmatch(rf"(?:选择|选)?第{_NUM}[个号项]?", norm)   # 第N个:1 基序数
    if m:
        n = _num(m.group(1))
        return n - 1 if n and n > 0 else None
    m = re.fullmatch(rf"(?:选择|选)?([0-9]+)(?:个|号|项|选项|号选项)?", norm)
    if m:                                                      # 屏显编号(数字)
        return int(m.group(1))
    m = re.fullmatch(
        rf"(?:选择|选)([零〇一二两三四五六七八九十]+)(?:个|号|项|选项|号选项)?"
        rf"|([零〇一二两三四五六七八九十]+)(?:号|项|选项|号选项)", norm)
    if m:                                                      # 屏显编号(中文数字)
        return _num(m.group(1) or m.group(2))
    return None


class Daemon:
    def __init__(self, session, preload_asr: bool = True, auto_yes: bool = False):
        self.session = session
        self.preload_asr = preload_asr
        self.auto_yes = auto_yes          # PTT/文本请求未显式给 yes 时的兜底
        self.transcriber: Transcriber | None = None
        self.pending: dict | None = None  # {"type","action","decision","expires"}
        self.started = time.time()
        self._stop = False

    # ---- 生命周期 ----

    def start(self) -> None:
        """预热:Jev 连接(TLS/代理握手)+ ASR 模型加载,只此一次。"""
        print("hypr-jev daemon:预热 Jev 连接(TLS/代理握手)…")
        ms = self.session.warmup()
        print(f"  ✓ 连接就绪(冷启动 {ms:.0f}ms,此后常驻复用)")
        if self.preload_asr:
            from .config import ASR_MODEL
            print(f"加载 faster-whisper `{ASR_MODEL}`…")
            t0 = time.perf_counter()
            self.transcriber = Transcriber()
            self.transcriber.load()
            print(f"  ✓ 模型就绪 ({time.perf_counter() - t0:.1f}s)")

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop())

        sock_path = runtime.socket_path()
        srv = self._open_socket(sock_path)
        fifo = self._open_fifo(runtime.ptt_fifo_path())
        recorder = Recorder()
        rec_started: float | None = None

        print(f"daemon 就绪  socket: {sock_path}  PTT FIFO: {runtime.ptt_fifo_path()}")
        print(f"模式: {'live(真实执行)' if self.session.live else 'dry-run(只规划不执行)'}")
        print("发送文字: python -m hypr_jev --send \"打开终端\"\n")
        try:
            while not self._stop:
                rlist, _, _ = select.select([srv, fifo], [], [], 0.5)
                now = time.time()

                if self.pending and now > self.pending["expires"]:
                    print("  · 挂起的待确认操作超时,已取消")
                    self.pending = None

                if recorder.proc and now - rec_started > ASR_TIMEOUT_S:
                    print("  · 录音超时,自动截断")
                    self._finish_voice(recorder.stop())
                    rec_started = None

                for r in rlist:
                    if r is fifo:
                        token = fifo.readline().strip()
                        if token == "start" and not recorder.proc:
                            recorder.start()
                            rec_started = now
                            print("  ● 录音中…")
                            if self.pending:
                                print("  ⏸ 有挂起待确认:可说「是 / 否 / 编号」")
                        elif token == "stop" and recorder.proc:
                            self._finish_voice(recorder.stop())
                            rec_started = None
                        elif token == "quit":
                            self._stop = True
                    elif r is srv:
                        self._serve_once(srv)
        finally:
            if recorder.proc:
                recorder.stop()
            fifo.close()
            srv.close()
            for p in (sock_path,):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self.session.close()
            print("daemon 已退出")

    # ---- 入口建立 ----

    @staticmethod
    def _open_socket(path: str) -> socket.socket:
        if os.path.exists(path):
            alive = False
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    probe.connect(path)
                    alive = True
            except OSError:
                pass
            if alive:
                raise SystemExit(
                    f"daemon 已在运行({path});"
                    "先 `--stop` 或 `systemctl --user stop hypr-jev` 再启动")
            os.unlink(path)  # 残留的死 socket
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)   # socket 能执行桌面命令,仅限本用户
        srv.listen(4)
        return srv

    @staticmethod
    def _open_fifo(path: str):
        if not os.path.exists(path):
            os.mkfifo(path)
        # O_RDWR:读端不因写者离开而翻转 EOF,select 也不会一直可读
        return os.fdopen(os.open(path, os.O_RDWR | os.O_NONBLOCK), "r")

    # ---- socket 请求 ----

    def _serve_once(self, srv: socket.socket) -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            try:
                conn.settimeout(REQUEST_TIMEOUT_S)
                line = conn.makefile("r", encoding="utf-8").readline()
            except OSError:
                return
            if not line:
                return
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("请求必须是 JSON 对象")
            except ValueError as e:
                resp = {"ok": False, "error": f"无效请求: {e}"}
            else:
                resp = self._dispatch(req)
            try:
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            except OSError:
                pass

    def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        try:
            if cmd == "handle":
                return self._cmd_handle(req)
            if cmd == "answer":
                return self._cmd_answer(req)
            if cmd == "mode":
                return self._cmd_mode(req)
            if cmd == "pending":
                return self._cmd_pending()
            if cmd == "ping":
                return self._status("ping")
            if cmd == "quit":
                self._stop = True
                return {"ok": True, "cmd": "quit", "output": "daemon 退出\n"}
            return {"ok": False, "error": f"未知命令: {cmd!r}"}
        except Exception as e:   # 任何单请求失败都不允许杀死常驻循环
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- 命令实现 ----

    def _cmd_handle(self, req: dict) -> dict:
        text = str(req.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "text 为空"}
        outcome = self.session.handle(text)
        auto_yes = bool(req.get("yes", self.auto_yes))
        out, final, pending = self._settle(outcome, auto_yes)
        return self._ok("handle", out, final, pending)

    def _cmd_answer(self, req: dict) -> dict:
        p = self.pending
        if p is None:
            return {"ok": False, "error": "没有挂起的待确认操作"}
        self.pending = None
        value = str(req.get("value", "")).strip().lower()

        if p["type"] == "confirm":
            if value in ("y", "yes"):
                o2 = self.session.execute(p["action"], p["decision"])
                out = "(已确认)\n" + _capture(render, o2)
                return self._ok("answer", out, o2, None)
            return self._ok("answer", "已取消,未执行\n", None, None)

        # choose:显式编号即确认;空值取消
        choices = p["action"].choices or []
        if value.isdigit() and int(value) < len(choices):
            o2 = self.session.choose(p["action"], int(value))
            out = f"(已选择 {value})\n" + _capture(render, o2)
            return self._ok("answer", out, o2, None)
        return self._ok("answer", "已取消,未选择\n", None, None)

    def _cmd_mode(self, req: dict) -> dict:
        live = bool(req.get("live"))
        self.session.live = live
        out = f"  · 已切换 {'live 模式(真实执行)' if live else 'dry-run'}\n"
        return self._ok("mode", out, None, None)

    def _cmd_pending(self) -> dict:
        p = self.pending
        if p is None:
            return self._ok("pending", "无挂起操作\n", None, None)
        info = {"type": p["type"],
                "expires_in": round(p["expires"] - time.time(), 1)}
        if p["type"] == "confirm":
            info["desc"] = p["action"].desc or p["action"].reason
        else:
            choices = p["action"].choices or []
            info["choices"] = [label for _cid, label, _cmd in choices]
            info["pick_id_index"] = next(
                (i for i, (cid, _l, _c) in enumerate(choices)
                 if cid == p["action"].pick_id), 0)
        return {"ok": True, "cmd": "pending", "output": "", "pending": info}

    def _status(self, cmd: str) -> dict:
        return {"ok": True, "cmd": cmd,
                "live": self.session.live,
                "asr": self.transcriber is not None,
                "pending": self.pending["type"] if self.pending else None,
                "uptime_s": round(time.time() - self.started, 1),
                "pid": os.getpid()}

    # ---- 语音应答:挂起的 confirm/choose 用下一句语音回答 ----

    def _voice_answer(self, text: str) -> bool:
        """挂起存在时,尝试把这句转写当作语音应答;命中并处理返回 True。"""
        p = self.pending
        if p is None:
            return False
        ans = parse_answer(text)
        if ans is None:
            return False
        if p["type"] == "confirm":
            if not isinstance(ans, str):
                return False   # confirm 不吃编号,整句按普通命令处理
            value = "y" if ans == "yes" else "n"
        else:   # choose:「好」= 选 Jev 首选(与 --yes 代答一致),「否」取消
            choices = p["action"].choices or []
            if ans == "yes":
                idx = next((i for i, (cid, _l, _c) in enumerate(choices)
                            if cid == p["action"].pick_id), 0)
                value = str(idx)
            elif ans == "no":
                value = "n"
            else:
                value = str(ans)
        print(f"  🗣 语音应答 {text!r}")
        resp = self._cmd_answer({"cmd": "answer", "value": value})
        print(resp.get("output", ""), end="")
        return True

    # ---- Outcome 结算:渲染 + 挂起记录 + auto_yes 代答 ----

    def _settle(self, outcome, auto_yes: bool):
        """渲染 outcome;confirm/choose 在未代答时记录为 pending。

        返回 (渲染文本, 最终 outcome 或 None, pending 摘要或 None)。
        auto_yes 语义与 feedback.interact 的非交互分支一致。
        """
        out = _capture(render, outcome)
        a = outcome.action
        if a.kind == "confirm":
            if auto_yes:
                o2 = self.session.execute(a, outcome.decision)
                return out + "(--yes 自动确认)\n" + _capture(render, o2), o2, None
            self.pending = {"type": "confirm", "action": a,
                            "decision": outcome.decision,
                            "expires": time.time() + PENDING_TTL_S}
            return out, outcome, {"type": "confirm", "desc": a.desc or a.reason}
        if a.kind == "choose":
            if auto_yes:
                idx = next((i for i, (cid, _l, _c) in enumerate(a.choices or [])
                            if cid == a.pick_id), 0)
                o2 = self.session.choose(a, idx)
                return out + "(--yes 自动选择)\n" + _capture(render, o2), o2, None
            self.pending = {"type": "choose", "action": a,
                            "decision": outcome.decision,
                            "expires": time.time() + PENDING_TTL_S}
            choices = a.choices or []
            labels = [label for _cid, label, _cmd in choices]
            pick_idx = next((i for i, (cid, _l, _c) in enumerate(choices)
                             if cid == a.pick_id), 0)
            return out, outcome, {"type": "choose", "choices": labels,
                                  "pick_id_index": pick_idx}
        return out, outcome, None

    @staticmethod
    def _ok(cmd: str, output: str, outcome, pending: dict | None) -> dict:
        return {"ok": True, "cmd": cmd, "output": output, "pending": pending,
                "executed": bool(outcome and outcome.executed),
                "exec_ok": bool(outcome and outcome.ok)}

    # ---- 语音路径(与 --asr 同一条业务链)----

    def _finish_voice(self, audio) -> None:
        try:
            if audio is None or audio.size == 0:
                print("  · (空录音,忽略)")
                return
            text, ms = self.transcriber.transcribe(audio)
            print(f"  🎙 {text!r}  (ASR {ms:.0f}ms / 录音 {audio.size / ASR_RATE:.1f}s)")
            if not text:
                return
            if self._voice_answer(text):
                return
            outcome = self.session.handle(text)
            out, _o, pending = self._settle(outcome, self.auto_yes)
            print(out, end="")
            if pending:
                print("  ⏸ 挂起待确认:再说一句「是 / 否 / 编号」即可语音应答," 
                      "或用 `python -m hypr_jev --pending` 从终端应答")
        except Exception as e:
            print(f"  ✖ 语音处理失败: {type(e).__name__}: {e}")
