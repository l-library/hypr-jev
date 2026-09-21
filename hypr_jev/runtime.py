"""运行期 IPC 端点的唯一定义:daemon 与所有客户端必须取同一路径。

可移植性:XDG_RUNTIME_DIR 优先(每用户隔离、tmpfs、随会话清理),
可用 HYPR_JEV_RUNTIME_DIR 覆盖(测试/多实例);都没有则退回 /tmp。
代码不依赖仓库位置或 cwd,systemd 用户服务下同样成立。
"""

import os


def runtime_dir() -> str:
    return (os.environ.get("HYPR_JEV_RUNTIME_DIR")
            or os.environ.get("XDG_RUNTIME_DIR")
            or "/tmp")


def socket_path() -> str:
    """daemon 的 Unix socket(文本请求入口)。"""
    return os.path.join(runtime_dir(), "hypr-jev.sock")


def ptt_fifo_path() -> str:
    """PTT 触发 FIFO(与 --asr 模式共用,键位绑定不用改)。"""
    return os.path.join(runtime_dir(), "hypr-jev.ptt")
