"""配置:意图表、门控阈值、ASR 参数。

intent 是稳定 ID,中英文语音/文字都映射到同一 ID,
执行层只认 intent,不认原始语言。
"""

MODEL = "jev-latest"

# 置信度门控策略(语音场景核心:宁可不执行,不可误执行)
CONF_EXECUTE = 0.75   # >= 此值且无需确认 → 直接执行
CONF_ASK = 0.50       # < 此值 → 忽略(视作没听见)
NEEDS_CONFIRM_MAX = 0.40  # needs_confirm >= 此值 → 先请求确认,不执行

# Choice 候选:option → 评分说明(给模型的英文说明,与输入语言无关)
INTENTS = {
    "launch_app":       "Launch/open a specific installed desktop application by its name (terminals included)",
    "switch_workspace": "Switch to a numbered workspace, e.g. workspace 3",
    "toggle_floating":  "Toggle floating mode of the focused window",
    "close_window":     "Close a window: the focused one, or the window of a specific app by name",
    "volume_up":        "Increase the output volume",
    "volume_down":      "Decrease the output volume",
    "screenshot":       "Take a screenshot",
    "lock_screen":      "Lock the screen / session",
    "other":            "Small talk, questions, or anything that is not one of the desktop commands above",
}

# 三个问题在同一次调用中并行评估(测试 fan-out 几乎不加延迟的官方宣称)
QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": (
            "Which desktop command is the user asking for in `utterance`? "
            "Use `active_window` only as context. If it is not a command, answer `other`."
        ),
        "criteria": INTENTS,
    },
    "needs_confirm": {
        "type": "noul",
        "instructions": (
            "Would executing the command in `utterance` be destructive or hard to undo "
            "(closing/destroying windows, locking the session)? "
            "Routine navigation, volume or layout toggles are NOT destructive."
        ),
    },
    "strength": {
        "type": "score",
        "instructions": (
            "If `utterance` asks to change the volume, how large should the step be? "
            "Otherwise answer arbitrarily."
        ),
        "criteria": [
            "a small nudge (about 5%)",
            "a medium step (about 15%)",
            "a large jump (about 30%)",
        ],
    },
}

# 仅 intent 单问题(用于 fan-out 对比)
INTENT_ONLY = {"intent": QUESTIONS["intent"]}

# Choice 单次选项上限 255;全量回退预留 none_of_these + 安全余量
WIDE_MAX = 240

# ASR(faster-whisper,本地 CPU int8)
# HF 官方源不可达,模型经 hf-mirror 下载到本地目录(README 有说明)
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
ASR_MODEL = _os.path.join(_ROOT, "models", "faster-whisper-small")
ASR_RATE = 16000
ASR_LANGUAGE = None     # None = 自动检测(支持中英混说);或 "zh" / "en"
ASR_TIMEOUT_S = 12.0    # PTT 最长录音时长,超时自动截断
