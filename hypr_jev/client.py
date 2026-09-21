"""daemon 客户端:单连接单请求,单行 JSON 进出。

刻意保持轻量:只依赖标准库,不导入 session/router/asr,
让 `--send` 的进程级开销保持在几十毫秒级(冷启动全部留在 daemon 侧)。
"""

import json
import socket

from . import runtime


class DaemonUnavailable(RuntimeError):
    """daemon 未运行或 socket 不可达。"""


def request(payload: dict, timeout: float = 30.0) -> dict:
    """发送一个请求,返回解析后的响应 dict。失败抛 DaemonUnavailable。"""
    path = runtime.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            with s.makefile("r", encoding="utf-8") as f:
                line = f.readline()
    except OSError as e:
        raise DaemonUnavailable(
            f"daemon 不可达({e});先启动:systemctl --user start hypr-jev,"
            "或 python -m hypr_jev --install-service") from e
    if not line:
        raise DaemonUnavailable("daemon 关闭了连接(可能正在退出)")
    try:
        return json.loads(line)
    except ValueError as e:
        raise DaemonUnavailable(f"响应不是 JSON: {e}") from e
