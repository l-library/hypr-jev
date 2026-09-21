"""文字输入 REPL / ASR 常驻入口 / 常驻 daemon 与其客户端。

默认 dry-run(只规划不执行);--live 才真正执行。
管道输入(非 TTY)时无提示符,confirm 一律拒绝(除非 --yes),
以便脚本/ASR 直接管道接入:echo "打开终端" | hypr-jev --live --yes

客户端命令(--send/--pending/--status/--stop)只连 socket,不导入
重型依赖;daemon 进程常驻预热连接,消除每次调用的冷启动。
"""

import argparse
import sys

try:
    import readline  # noqa: F401  上/下箭头历史
except ImportError:
    readline = None

BANNER = """hypr-jev 文字控制 demo(默认 dry-run)
  输入自然语言命令,如:打开终端 / 切换到工作区3 / 音量大点
  /live 切换 live  /dry 切回 dry-run  /q 退出"""


def repl(session, interactive: bool, auto_yes: bool) -> None:
    from .feedback import interact, render

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


# ---- 常驻 daemon ----

def _cmd_daemon(args) -> int:
    from .daemon import Daemon
    from .session import Session

    try:
        sys.stdout.reconfigure(line_buffering=True)  # journald 实时可见
    except (AttributeError, ValueError):
        pass
    session = Session(live=args.live)
    daemon = Daemon(session, preload_asr=not args.no_asr, auto_yes=args.yes)
    try:
        daemon.start()   # 预热 Jev 连接 + 预载 ASR 模型(冷启动只付一次)
        daemon.run()
    finally:
        session.close()
    return 0


# ---- daemon 客户端 ----

def _present(resp: dict) -> int:
    """打印响应;confirm/choose 挂起且终端可交互时追问一次并回传应答。"""
    from .client import DaemonUnavailable, request

    print(resp.get("output", ""), end="")
    pend = resp.get("pending")
    if not pend:
        return 1 if resp.get("executed") and not resp.get("exec_ok") else 0
    if not sys.stdin.isatty():
        return 0  # 非交互:保持挂起,用 --pending 或 --yes 处理
    try:
        if pend["type"] == "confirm":
            ans = input("     执行? [y/N] ").strip().lower()
        else:
            n = len(pend["choices"])
            ans = input(f"     选择编号 [0-{n - 1}](回车取消): ").strip()
        if not ans:
            return 0
        resp = request({"cmd": "answer", "value": ans})
    except DaemonUnavailable as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(resp.get("output", ""), end="")
    return 1 if resp.get("executed") and not resp.get("exec_ok") else 0


def _cmd_send(args) -> int:
    from .client import DaemonUnavailable, request

    try:
        if args.live or args.dry:   # 先切 daemon 模式再发话
            r = request({"cmd": "mode", "live": bool(args.live)})
            if not r.get("ok"):
                print(f"✖ {r.get('error')}", file=sys.stderr)
                return 1
            print(r.get("output", ""), end="")
        resp = request({"cmd": "handle", "text": args.send, "yes": args.yes})
    except DaemonUnavailable as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    if not resp.get("ok"):
        print(f"✖ {resp.get('error')}", file=sys.stderr)
        return 1
    return _present(resp)


def _cmd_pending() -> int:
    from .client import DaemonUnavailable, request

    try:
        resp = request({"cmd": "pending"})
    except DaemonUnavailable as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    pend = resp.get("pending")
    if not pend:
        print(resp.get("output", "无挂起操作"))
        return 0
    if pend["type"] == "confirm":
        print(f"  ⏸ 待确认: {pend.get('desc', '')}  (剩余 {pend['expires_in']:.0f}s)")
        default = ""
        prompt = "     执行? [y/N] "
    else:
        print("  ⏸ 待选择:")
        for i, label in enumerate(pend["choices"]):
            star = "   ★ Jev 首选" if i == pend.get("pick_id_index") else ""
            print(f"     {i}) {label}{star}")
        default = ""
        prompt = f"     选择编号 [0-{len(pend['choices']) - 1}](回车取消): "
    if not sys.stdin.isatty():
        pass  # 管道应答:--pending 就是为应答而生,echo y | hypr-jev --pending 可脚本化
    try:
        ans = input(prompt).strip() or default
        if not ans:
            print("  · (空应答,保持挂起)")
            return 0
        resp = request({"cmd": "answer", "value": ans})
    except EOFError:
        print("  · (无应答输入,保持挂起)")
        return 0
    except DaemonUnavailable as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(resp.get("output", ""), end="")
    return 1 if resp.get("executed") and not resp.get("exec_ok") else 0


def _cmd_status() -> int:
    from .client import DaemonUnavailable, request

    try:
        r = request({"cmd": "ping"})
    except DaemonUnavailable as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(f"  ✓ daemon 运行中 pid={r['pid']}  "
          f"模式={'live' if r['live'] else 'dry-run'}  "
          f"ASR={'已加载' if r['asr'] else '未加载'}  "
          f"挂起={r['pending'] or '无'}  运行 {r['uptime_s']:.0f}s")
    return 0


def _cmd_stop() -> int:
    from .client import DaemonUnavailable, request

    try:
        r = request({"cmd": "quit"})
    except DaemonUnavailable as e:
        print(f"· {e}", file=sys.stderr)
        return 2
    print(r.get("output", "daemon 退出"))
    return 0


# ---- systemd 用户服务 ----

def _cmd_install(args) -> int:
    from .service import install_service
    return install_service(live=args.live, asr=not args.no_asr)


def _cmd_uninstall() -> int:
    from .service import uninstall_service
    return uninstall_service()


def main() -> int:
    ap = argparse.ArgumentParser(prog="hypr-jev", description="Jev 语音/文字桌面控制")
    ap.add_argument("--live", action="store_true", help="真实执行(默认 dry-run)")
    ap.add_argument("--dry", action="store_true", help="强制 dry-run(与 --send 连用切 daemon 模式)")
    ap.add_argument("--yes", action="store_true", help="非交互模式下自动确认(谨慎)")
    ap.add_argument("--once", metavar="TEXT", help="处理单句后退出(脚本/管道用)")
    ap.add_argument("--asr", action="store_true", help="ASR 常驻模式(PTT FIFO 触发)")
    ap.add_argument("--daemon", action="store_true",
                    help="常驻 daemon(socket + PTT FIFO,预热连接与 ASR 模型)")
    ap.add_argument("--no-asr", action="store_true", help="daemon 不预载 ASR(纯文字)")
    ap.add_argument("--send", metavar="TEXT", help="把一句话发给常驻 daemon")
    ap.add_argument("--pending", action="store_true", help="查看/应答 daemon 挂起的确认或选择")
    ap.add_argument("--status", action="store_true", help="查看 daemon 状态")
    ap.add_argument("--stop", action="store_true", help="停止常驻 daemon")
    ap.add_argument("--install-service", action="store_true",
                    help="生成并启用 systemd 用户服务(加 --live 为真实执行)")
    ap.add_argument("--uninstall-service", action="store_true", help="停止并移除 systemd 用户服务")
    args = ap.parse_args()

    if args.install_service:
        return _cmd_install(args)
    if args.uninstall_service:
        return _cmd_uninstall()
    if args.status:
        return _cmd_status()
    if args.stop:
        return _cmd_stop()
    if args.send is not None:
        return _cmd_send(args)
    if args.pending:
        return _cmd_pending()
    if args.daemon:
        return _cmd_daemon(args)

    if args.asr:
        from .asr import run_asr
        from .session import Session
        interactive = sys.stdin.isatty()
        session = Session(live=args.live)
        try:
            session.warmup()
            run_asr(session, interactive=interactive, auto_yes=args.yes)
        finally:
            session.close()
        return 0

    from .feedback import interact, render
    from .session import Session
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
