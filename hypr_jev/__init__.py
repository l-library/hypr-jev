"""hypr-jev:Jev 驱动的 Hyprland 桌面控制(文字 REPL + ASR 语音输入)。

分层:
  cli.py / asr.py  输入源(文字 REPL / PTT 录音转写)
  session.py      业务核心:handle(text) -> Outcome(纯数据)
  router.py       Jev 决策    executor.py 门控+执行    feedback.py 渲染(未来 GUI)
"""

__version__ = "0.1.0"
