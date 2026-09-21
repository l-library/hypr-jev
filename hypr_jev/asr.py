"""ASR 输入源:push-to-talk(FIFO 令牌)→ arecord 采集 → faster-whisper 转写。

触发方式(不改任何用户配置,任选其一):
  1. 终端:echo start > $FIFO; 说话; echo stop > $FIFO
  2. Hyprland 键位绑定:按下写 start、松开写 stop(README 附示例)

转写文本喂给 Session.handle(),与文字 REPL 走完全相同的处理链
(意图路由 / 门控 / 确认 / 执行 / 反馈)。
"""

import os
import select
import subprocess
import sys
import time

import numpy as np

from .config import ASR_LANGUAGE, ASR_MODEL, ASR_RATE, ASR_TIMEOUT_S
from .feedback import interact, render
from .runtime import ptt_fifo_path as fifo_path


class Recorder:
    """arecord 子进程采集 16k 单声道 s16le 原始流(经 PipeWire default 设备)。"""

    def __init__(self, rate: int = ASR_RATE):
        self.rate = rate
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-D", "default", "-f", "S16_LE",
             "-r", str(self.rate), "-c", "1", "-t", "raw", "/dev/stdout"],
            stdout=subprocess.PIPE)

    def stop(self) -> np.ndarray | None:
        proc, self.proc = self.proc, None
        if proc is None:
            return None
        proc.terminate()
        try:
            raw, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            raw, _ = proc.communicate()
        audio = np.frombuffer(raw, dtype=np.int16)
        return audio if audio.size else None


class Transcriber:
    """faster-whisper(int8 CPU)。首次 load 需从 HuggingFace 下载模型。"""

    def __init__(self, model_name: str = ASR_MODEL):
        self._model_name = model_name
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self._model_name, device="cpu", compute_type="int8", cpu_threads=8)

    def transcribe(self, audio: np.ndarray,
                   language: str | None = ASR_LANGUAGE) -> tuple[str, float]:
        """int16 PCM(16k 单声道)→ (文本, 耗时 ms)。"""
        t0 = time.perf_counter()
        if self._model is None:
            self.load()
        segments, _info = self._model.transcribe(
            audio.astype(np.float32) / 32768.0,
            language=language, beam_size=1, vad_filter=True)
        text = "".join(s.text for s in segments).strip()
        return text, (time.perf_counter() - t0) * 1000


def handle_audio(session, transcriber: Transcriber, audio, interactive: bool,
                 auto_yes: bool = False) -> None:
    """录音 → 转写 → 与文字输入完全相同的处理链。"""
    if audio is None or audio.size == 0:
        print("  · (空录音,忽略)")
        return
    text, ms = transcriber.transcribe(audio)
    dur = audio.size / ASR_RATE
    print(f"  🎙 {text!r}  (ASR {ms:.0f}ms / 录音 {dur:.1f}s)")
    if not text:
        return
    outcome = session.handle(text)
    render(outcome)
    interact(session, outcome, interactive, auto_yes)


def run_asr(session, interactive: bool = True, auto_yes: bool = False) -> None:
    """常驻 PTT 循环:select 同时监听 FIFO 令牌与终端 stdin。"""
    path = fifo_path()
    if not os.path.exists(path):
        os.mkfifo(path)

    print(f"ASR 模式:加载 faster-whisper `{ASR_MODEL}`(首次需下载)…")
    t0 = time.perf_counter()
    transcriber = Transcriber()
    transcriber.load()
    print(f"模型就绪 ({time.perf_counter() - t0:.1f}s)")
    print(f"PTT 触发: echo start > {path} … echo stop > {path}")
    print("终端内直接输入文字照常处理;/q 退出\n")

    # O_RDWR 打开避免 fifo 读端阻塞等待写者 / EOF 翻转
    fifo = os.fdopen(os.open(path, os.O_RDWR | os.O_NONBLOCK), "r")
    recorder = Recorder()
    rec_started: float | None = None
    fds = [fifo] + ([sys.stdin] if interactive else [])
    try:
        while True:
            timeout = 1.0 if recorder.proc else None   # 录音中轮询超时保护
            rlist, _, _ = select.select(fds, [], [], timeout)
            now = time.time()
            if recorder.proc and now - rec_started > ASR_TIMEOUT_S:
                print("  · 录音超时,自动截断")
                handle_audio(session, transcriber, recorder.stop(),
                             interactive, auto_yes)
                rec_started = None
            for r in rlist:
                if r is fifo:
                    token = fifo.readline().strip()
                    if token == "start" and not recorder.proc:
                        recorder.start()
                        rec_started = now
                        print("  ● 录音中…")
                    elif token == "stop" and recorder.proc:
                        handle_audio(session, transcriber, recorder.stop(),
                                     interactive, auto_yes)
                        rec_started = None
                    elif token == "quit":
                        return
                else:
                    line = sys.stdin.readline()
                    if not line:
                        fds.remove(sys.stdin)
                        continue
                    cmd = line.strip()
                    if cmd in ("/q", "/quit", "/exit"):
                        return
                    if not cmd:
                        continue
                    outcome = session.handle(cmd)
                    render(outcome)
                    interact(session, outcome, interactive, auto_yes)
    finally:
        fifo.close()
        if recorder.proc:
            recorder.stop()
