"""文字输入 REPL / ASR 常驻入口。

默认 dry-run(只规划不执行);--live 才真正执行。
管道输入(非 TTY)时无提示符,confirm 一律拒绝(除非 --yes),
以便脚本/ASR 直接管道接入:echo "打开终端" | hypr-jev --live --yes
"""

import argparse
import sys

try:
    import readline  # noqa: F401  上/下箭头历史
except ImportError:
    readline = None

from .feedback import interact, render
from .session import Session

BANNER = """hypr-jev 文字控制 demo(默认 dry-run)
  输入自然语言命令,如:打开终端 / 切换到工作区3 / 音量大点
  /live 切换 live  /dry 切回 dry-run  /q 退出"""


def repl(session: Session, interactive: bool, auto_yes: bool) -> None:
    if interactive:
        print(BANNER)
    while True:
        try:
            text = input("🗣  ") if interactive else sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            break  # 管道 EOF
        text = text.strip()
        if not text:
            continue
        if text in ("/q", "/quit", "/exit"):
            break
        if text == "/live":
            session.live = True
            print("  · 已切换 live 模式(真实执行)")
            continue
        if text == "/dry":
            session.live = False
            print("  · 已切回 dry-run")
            continue

        outcome = session.handle(text)
        render(outcome)
        interact(session, outcome, interactive, auto_yes)
        if not interactive:
            sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(prog="hypr-jev", description="Jev 语音/文字桌面控制")
    ap.add_argument("--live", action="store_true", help="真实执行(默认 dry-run)")
    ap.add_argument("--yes", action="store_true", help="非交互模式下自动确认(谨慎)")
    ap.add_argument("--once", metavar="TEXT", help="处理单句后退出(脚本/管道用)")
    ap.add_argument("--asr", action="store_true", help="ASR 常驻模式(PTT FIFO 触发)")
    args = ap.parse_args()

    if args.asr:
        from .asr import run_asr
        interactive = sys.stdin.isatty()
        session = Session(live=args.live)
        try:
            session.warmup()
            run_asr(session, interactive=interactive, auto_yes=args.yes)
        finally:
            session.close()
        return 0

    interactive = sys.stdin.isatty() and args.once is None
    session = Session(live=args.live)
    try:
        session.warmup()
        if args.once is not None:
            outcome = session.handle(args.once)
            render(outcome)
            interact(session, outcome, interactive, args.yes)
            return 0 if not outcome.executed or outcome.ok else 1
        repl(session, interactive, args.yes)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
