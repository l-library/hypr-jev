"""systemd 用户服务安装路径的离线回归:单元生成、env 文件提取、安装/卸载流程。

不触碰真实 systemctl(替身记录调用),HOME 指向临时目录;
不依赖网络/Hyprland。用法: .venv/bin/python tests/test_service.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypr_jev.service import (ENV_FILE_REL, UNIT_NAME, env_file_text,  # noqa: E402
                              install_service, uninstall_service, unit_text)


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✅ {msg}")


def fake_run(calls):
    def run(argv):
        calls.append(argv)
        return 0
    return run


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="hypr-jev-svc-"))
    try:
        # 1. 单元文本:路径安装时解析,无仓库位置硬编码
        t = unit_text("/opt/x/.venv/bin/python", "/opt/x", live=True, asr=False)
        expect("ExecStart=/opt/x/.venv/bin/python -m hypr_jev --daemon --live --no-asr" in t,
               "ExecStart 按安装时路径生成,携带 --live/--no-asr")
        expect("WorkingDirectory=/opt/x" in t, "WorkingDirectory 解析为仓库根(-m 找包依赖)")
        expect("EnvironmentFile=-%h" + str(ENV_FILE_REL) in t, "密钥走 EnvironmentFile(%h 家目录)")
        expect("WantedBy=graphical-session.target" in t and "PartOf=graphical-session.target" in t,
               "随图形会话启停")

        # 2. env 提取:白名单 + 引号包裹;无密钥时返回 None
        env = {"TYPESAFE_API_KEY": "sk-test", "ALL_PROXY": "socks5://x:1080",
               "PATH": "/usr/bin", "SOME_SECRET": "不收录"}
        text = env_file_text(env)
        expect('TYPESAFE_API_KEY="sk-test"' in text and 'ALL_PROXY="socks5://x:1080"' in text
               and "SOME_SECRET" not in text and "PATH" not in text,
               "env 只提取白名单变量")
        expect(env_file_text({}) is None, "空环境不生成 env 文件")

        # 3. 安装流程:写单元 + env(600)+ daemon-reload + enable --now
        calls = []
        rc = install_service(live=True, asr=False, home=home, run=fake_run(calls))
        unit = home / ".config/systemd/user" / UNIT_NAME
        expect(rc == 0 and unit.exists(), "install_service 成功写出单元")
        expect('TYPESAFE_API_KEY="' in (home / ENV_FILE_REL).read_text()
               and ((home / ENV_FILE_REL).stat().st_mode & 0o777) == 0o600,
               "env 文件从当前环境生成,权限 600")
        expect(calls[0] == ["systemctl", "--user", "daemon-reload"]
               and calls[1] == ["systemctl", "--user", "enable", "--now", UNIT_NAME],
               "安装后 daemon-reload + enable --now")

        # 4. 已有 env 文件不覆盖(避免刷掉用户手工改动)
        (home / ENV_FILE_REL).write_text('TYPESAFE_API_KEY="keep-me"\n')
        install_service(live=False, asr=True, home=home, run=fake_run([]))
        expect('keep-me' in (home / ENV_FILE_REL).read_text(), "已存在的 env 文件保留不动")

        # 5. 卸载:disable --now → 删单元 → reload;env 文件保留
        calls = []
        rc = uninstall_service(home=home, run=fake_run(calls))
        expect(rc == 0 and not unit.exists(), "卸载移除单元")
        expect(["systemctl", "--user", "disable", "--now", UNIT_NAME] == calls[0],
               "卸载先 disable --now")
        expect((home / ENV_FILE_REL).exists(), "env 文件(含密钥)由用户决定去留")
        return 0
    except AssertionError as e:
        print(f"  ❌ {e}")
        return 1
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
