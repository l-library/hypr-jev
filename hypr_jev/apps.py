"""已安装桌面应用索引(.desktop 文件)与本地检索。

两级启动方案的第一级:零成本字符串匹配出 top-K 候选,
语义兜底(中文昵称、泛指)交给 router.pick_app() 精排。
"""

import re
from dataclasses import dataclass
from pathlib import Path

# 本地条目优先(同名 id 覆盖系统条目)
APP_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
]

# 从用户语句中剥离的启动动词/客套词(检索用)
_VERBS = re.compile(
    r"(帮我|请您?|给我|麻烦|把|一下|打开|开启|启动|运行|调出|"
    r"please|could you|would you|start|run|launch|open)\s*", re.I)
_PUNCT = re.compile(r"[，。！？、,.!?]")


@dataclass(frozen=True)
class App:
    id: str           # desktop 文件名(去 .desktop),gtk-launch 用
    name: str         # Name=
    zh_name: str      # Name[zh_CN]=,可能为空
    path: str

    @property
    def label(self) -> str:
        return self.zh_name or self.name


def _parse(path: Path) -> App | None:
    try:
        head = path.read_text(encoding="utf-8", errors="replace") \
                   .split("[Desktop Action", 1)[0]
    except OSError:
        return None
    if re.search(r"^(NoDisplay|Hidden)\s*=\s*true\s*$", head, re.M | re.I):
        return None

    def grab(pat: str) -> str:
        m = re.search(pat, head, re.M)
        return m.group(1).strip() if m else ""

    name = grab(r"^Name=(.+)$")
    if not name:
        return None
    return App(path.stem, name, grab(r"^Name\[zh_CN\]=(.+)$"), str(path))


def load_apps() -> list[App]:
    seen: set[str] = set()
    apps: list[App] = []
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            if f.stem in seen:
                continue
            app = _parse(f)
            if app:
                seen.add(app.id)
                apps.append(app)
    return apps


def search(apps: list[App], query: str, k: int = 5) -> list[App]:
    """归一化子串匹配,返回按分数排序的 top-K。空列表 = 本地未命中。"""
    q = _PUNCT.sub(" ", _VERBS.sub(" ", query)).strip().lower()
    if not q:
        return []
    scored: list[tuple[int, App]] = []
    for a in apps:
        best = 0
        for h in {a.id.lower(), a.name.lower(), a.zh_name.lower()} - {""}:
            if h == q:
                best = max(best, 100)
            elif len(q) >= 2 and (h.startswith(q) or (len(h) >= 2 and q.startswith(h))):
                best = max(best, 80)
            elif len(h) >= 2 and h in q:          # "firefox浏览器" 包含 firefox
                best = max(best, 70)
            elif len(q) >= 2 and q in h:
                best = max(best, 60)
        if best:
            scored.append((best, a))
    scored.sort(key=lambda t: (-t[0], t[1].label))
    return [a for _, a in scored[:k]]
