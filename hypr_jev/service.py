"""systemd 用户服务:生成、安装、卸载 hypr-jev.service。

可移植性设计(配置路线不绑定机器/仓库位置):
  - ExecStart 的 Python 解释器在【安装时】从当前 venv 解析为绝对路径,
    单元文件写到 ~/.config/systemd/user/,仓库搬到哪都能重新生成;
  - 代码内部路径(ASR 模型、socket、FIFO)全部按包位置/XDG 解析,
    与 WorkingDirectory 无关,故单元不设 WorkingDirectory;
  - 密钥与代理经 EnvironmentFile(~/.config/hypr-jev/env,权限 600)注入,
    不写死在单元里;systemd 用户管理器总有 XDG_RUNTIME_DIR;
  - After/PartOf=graphical-session.target:随图形会话启停,保证 daemon
    拿到 Hyprland/Wayland 运行时变量(需要会话导入,见 install 输出提示)。
"""

import os
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "hypr-jev.service"
ENV_FILE_REL = Path(".config/hypr-jev/env")

# 写入 env 文件的白名单:密钥 + SDK 选项 + 代理(socks5 场景)
ENV_KEYS = [
    "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
]


def unit_text(python_bin: str, repo_root: str, live: bool, asr: bool) -> str:
    flags = (" --live" if live else "") + ("" if asr else " --no-asr")
    return f"""\
# 本文件由 `python -m hypr_jev --install-service` 生成;重装会覆盖,勿手改。
# 改执行模式/ASR 选项请重新执行安装命令(--live / --no-asr)。

[Unit]
Description=hypr-jev 常驻 daemon(预热 Jev 连接 + 预载本地 ASR,消除冷启动)
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Type=simple
# WorkingDirectory = 仓库根(安装时解析):`python -m hypr_jev` 依赖它找包
WorkingDirectory={repo_root}
ExecStart={python_bin} -m hypr_jev --daemon{flags}
Environment=PYTHONUNBUFFERED=1
# 密钥/代理注入(前缀 '-' 表示文件不存在不报错)
EnvironmentFile=-%h{ENV_FILE_REL}
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=graphical-session.target
"""


def env_file_text(environ: dict | None = None) -> str | None:
    """从环境中提取白名单变量;一个都没有则返回 None(不生成文件)。"""
    env = os.environ if environ is None else environ
    lines = [f'{k}="{env[k]}"' for k in ENV_KEYS if env.get(k)]
    return "\n".join(lines) + "\n" if lines else None


def _systemctl(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, text=True).returncode


def _env_imported() -> bool:
    """systemd 用户管理器环境是否已导入 Wayland 变量(uwsm/DE 通常已导入)。"""
    try:
        p = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return True   # 探测不了就别误导用户
    return any(l.startswith("WAYLAND_DISPLAY=") for l in p.stdout.splitlines())


def install_service(live: bool, asr: bool, home: Path | None = None,
                    run=None) -> int:
    run = run or _systemctl
    home = Path(home) if home else Path.home()
    python = Path(sys.executable).resolve()

    if Path(sys.prefix) == Path(sys.base_prefix):
        print("⚠ 当前解释器不在 venv 内;单元将直接使用它,依赖可能缺失")

    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parent.parent
    unit_path = unit_dir / UNIT_NAME
    unit_path.write_text(unit_text(str(python), str(repo_root), live, asr),
                         encoding="utf-8")
    print(f"✓ 已写入 {unit_path}")

    env_text = env_file_text()
    env_path = home / ENV_FILE_REL
    if env_text and not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(env_text, encoding="utf-8")
        env_path.chmod(0o600)
        print(f"✓ 已写入 {env_path}(密钥/代理,权限 600)")
    elif env_text:
        print(f"· {env_path} 已存在,保留不动;需更新请手动编辑或删除后重装")
    else:
        print(f"· 环境中没有 TYPESAFE_API_KEY/代理变量,未生成 {env_path}")
        print("  daemon 需要密钥才能决策;请把 TYPESAFE_API_KEY 写入该文件后重启服务")

    print(f"· 执行模式: {'live(真实执行)' if live else 'dry-run(只规划不执行)'}")

    if os.environ.get("WAYLAND_DISPLAY") and not _env_imported():
        print("⚠ 检测到 Wayland 变量未导入 systemd 用户环境,daemon 将拿不到 Hyprland 状态。")
        print("  在 hyprland.conf 加上(uwsm/部分 DE 已自动处理,加了也无害):")
        print('    exec-once = dbus-update-activation-environment --systemd --all')
        print("    exec-once = systemctl --user start graphical-session.target")

    for argv in (["daemon-reload"], ["enable", "--now", UNIT_NAME]):
        if (rc := run(["systemctl", "--user", *argv])) != 0:
            print(f"✖ systemctl --user {' '.join(argv)} 失败(rc={rc});"
                  "单元已就位,可稍后手动重试")
            return rc or 1

    print(f"✓ {UNIT_NAME} 已启用并启动")
    print("  日志:   journalctl --user -u hypr-jev -f")
    print('  发命令: python -m hypr_jev --send "打开终端"')
    print("  语音:   PTT FIFO 与 --asr 相同,键位绑定无需改动")
    if not live:
        print("  ⚠ 当前单元为 dry-run;要真实执行请加 --live 重新安装")
    return 0


def uninstall_service(home: Path | None = None, run=None) -> int:
    run = run or _systemctl
    home = Path(home) if home else Path.home()
    unit_path = home / ".config" / "systemd" / "user" / UNIT_NAME

    run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
    if unit_path.exists():
        unit_path.unlink()
        print(f"✓ 已移除 {unit_path}")
    else:
        print(f"· {unit_path} 不存在(未安装?)")
    run(["systemctl", "--user", "daemon-reload"])
    env_path = home / ENV_FILE_REL
    if env_path.exists():
        print(f"· 保留 {env_path}(内含密钥,请自行决定删除)")
    print("✓ 卸载完成;正在运行的 daemon 随 disable --now 停止")
    return 0
