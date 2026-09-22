# hypr-jev

自然语言 / 语音控制的 Hyprland 桌面助手。
[TypeSafe Jev](https://docs.typesafe.ai) 做意图决策与置信度门控,
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) 做本地离线语音识别。

```
"打开计算器"  →  意图路由 + 应用语义匹配  →  gtk-launch  →  ✅
"音量大一点"  →  意图 + 强度评分(步长)    →  wpctl +15%  →  ✅
"关闭终端"    →  窗口定向(语义匹配窗口)  →  按地址精确关闭 →  确认后 ✅
"今天天气怎么样" →  非命令拒识(other)  →  静默忽略,零副作用
```

## 特性

- **中英混合自然语言**——同一个 intent 表,中英文、口语化、中英混说均可
- **廉价快速的结构化决策**——每次控制一次调用、≈0.4–0.5K token 载荷、无思维链;
  单次成本 ≈$0.0001–0.0002(输入 $0.25–0.42/M,输出免费),$1 ≈ 5k–10k 次决策;
  对比常见大模型 Agent 的多轮工具循环,延迟与开销低 1–2 个数量级(见下文)
- **置信度门控**——高置信度自动执行,中置信度请求确认,低置信度/非命令直接忽略,
  宁可不执行,绝不误执行
- **启动任意已安装应用**——本地 `.desktop` 索引检索 + Jev 语义精排两级架构,
  支持中文泛称("计算器"→Calculator)与昵称("B站"→bilibili)
- **定向关窗**——枚举打开窗口交给 Jev 语义匹配,按地址精确关闭,绝不误关聚焦窗口
- **ASR 语音输入**——按住 `Super+F9` 说话,本地 faster-whisper 转写,全程离线
- **语音确认**——破坏性操作/低置信选择挂起后,直接说「是 / 否 / 编号」应答,免键盘
- **常驻 daemon**——systemd 用户服务托管,预热连接 + 预载 ASR 模型,
  消除每次调用的冷启动(`--send` 全程 <1s)
- **端到端 ~1.1s**——ASR 600–800ms + Jev 决策 ~400ms(实测见下)
- **执行安全**——默认 dry-run;破坏性操作(关窗/锁屏)强制确认;测试自带现场恢复

## 架构

```
输入源                      业务核心                             反馈端
┌───────────────────┐   ┌───────────────────────────┐   ┌──────────────┐
│ cli.py  文字 REPL  │──▶│ Session.handle(text)       │──▶│ feedback.py   │
│ asr.py  PTT 录音   │   │  ├ router.py   Jev 决策    │   │ (终端 + OSD)  │
│        (faster-    │   │  │  Choice/Noul/Score 并行 │   │ 未来:GUI/TTS  │
│         whisper)   │   │  ├ executor.py 门控+执行   │   └──────────────┘
│ daemon.py 常驻服务 │   │  └ choose/confirm 确认通道 │
│  socket + PTT FIFO │   └───────────────────────────┘
│ client.py 轻客户端 │                │
└───────────────────┘   Hyprland (hyprctl / Lua dispatch) / wpctl / gtk-launch
```

- 输入源只调 `Session.handle()`,业务核心输出纯数据 `Outcome`,反馈端只消费渲染——
  接 GUI/TTS 不动核心
- 一次 Jev 调用并行携带三个问题:`intent`(Choice)、`needs_confirm`(Noul)、
  `strength`(Score),加问题几乎不加延迟

## 为什么是 Jev,而不是大模型 Agent

控制桌面的大模型 Agent 常见形态:数 KB 系统提示 + 工具 schema → 思维链生成 →
解析工具调用 → 观察 → 再规划,一个任务多轮循环。每一步都是文本生成,
安全靠提示词叮嘱。本项目把同一件事拆成三个**原子判断**,让决策模型直出结构化答案:

| 维度     | 常见 LLM Agent 控制                             | 本项目(Jev 决策模型)                                                            |
| -------- | ----------------------------------------------- | ------------------------------------------------------------------------------- |
| 调用形态 | 多轮:规划→工具→观察→再规划                      | 单轮:state + 3 个原子问题 → 结构化答案                                          |
| 单次负载 | 通常 3–10K token 输入 + 数百 token 生成†        | 实测 ≈1.4K 字符(≈0.4–0.5K token)输入,几十 token 输出                            |
| 延迟     | 单轮 1–5s 起步,思维链更久†                      | **p50 ≈ 400ms**(含代理往返);直连热调用 291–402ms                                |
| 意图理解 | 强,但非确定,可能幻觉出不存在的工具调用          | 实测 22/22 中英混说 + 拒识;答案即枚举值,无解析失败面                            |
| 安全     | 提示词约束,行为不可预测                         | 校准置信度 + 代码门控:关窗 0.6–0.7 / 锁屏 0.85+ / 常规 <0.06,低于阈值宁可不执行 |
| 加新意图 | 提示词/工具 schema 膨胀,重推所有上下文          | 意图表加一行;决策负载几乎不变(fan-out 实测持平)                                 |
| 单次成本 | 多轮循环,每轮数百 token 思维链生成均按输出计费† | **≈$0.0001–0.0002**(输入 $0.25–0.42/M,输出免费);$1 ≈ 5k–10k 次                  |

† agent 侧为典型经验值;Jev 侧延迟/负载/准确率为本仓库实测(见下表),成本按实测载荷 × 官方定价折算。

**为什么能这么便宜**:模型只回答"这是什么意图 / 破坏吗 / 多大力道"这类几秒内
能拍板的快判断;其余全部留给本地代码——应用索引字符串匹配先行、窗口枚举交
hyprctl、命令构造是纯函数,语义精排只发生在少量候选上。上下文小到只有一句话
加活动窗口,没有思维链,没有多轮。官方对这类 System One 模型的定位也是
"No text generation, no parsing"——返回的类型化结果和概率分布直接给代码消费,
且问题并行评估,加问题几乎不加响应时间。定价上输入 $0.25–0.42/M、**输出免费**,
叠加单轮 ≈0.5K token 的载荷,每次决策 ≈$0.0001–0.0002;$1 能跑 5k–10k 次,
对个人桌面助手而言等于没有成本。

**这只是一种可能的应用方向,不是对未来的承诺**:Jev 目前是闭源模型,发布至今
不足一个月,本项目是对 "System One 式决策模型控制桌面" 这一方向的可行性测试。
但方向本身有想象的余地:路由层接口窄得刻意——输入一句话加活动窗口,输出
`{intent, confidence, needs_confirm, strength}`,本质是"分类 + 打分 + 校准",
不需要文本生成能力;而 Jev 体量足够小,CPU 即可运行。后续若出现开源版本,
换后端只需替换 `router.py`,业务核心不动——届时本地推理、零 API 费用、完全
离线,网络往返(当前延迟的大头)也随之消失。

## 环境要求

- Linux + [Hyprland](https://hyprland.org) **≥ 0.55**(Lua 配置体系)
- Python ≥ 3.10,`pip` venv
- [TypeSafe API Key](https://docs.typesafe.ai)(环境变量 `TYPESAFE_API_KEY`)
- ASR 需要麦克风(`arecord`,经 PipeWire)

## 快速开始

```sh
git clone <repo> && cd hypr-jev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export TYPESAFE_API_KEY=...        # 已导出则跳过
```

下载 ASR 模型(HF 官方源不可达时走镜像,约 461MB,存于 `models/`):

```sh
mkdir -p models/faster-whisper-small
for f in config.json tokenizer.json vocabulary.txt model.bin; do
  curl -sSL -C - -o models/faster-whisper-small/$f \
    https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/$f
done
```

> 走 socks5 代理访问 TypeSafe API 时需要 `socksio`(已含在 requirements)。

## 使用

```sh
# 文字 REPL(默认 dry-run,--live 真实执行)
.venv/bin/python -m hypr_jev
.venv/bin/python -m hypr_jev --live

# 单句模式(脚本/管道;--yes 自动代答确认)
.venv/bin/python -m hypr_jev --once "打开终端" --live

# ASR 语音模式(常驻;按住 Super+F9 说话,松开转写执行)
.venv/bin/python -m hypr_jev --asr --live
```

### 常驻 daemon(推荐,消除冷启动)

单次调用的两笔冷启动开销——Jev 连接 TLS/代理握手 ~4.8s、whisper 模型加载数秒——
只在 daemon 启动时付一次;之后 `--send` 全程 <1s。

```sh
# 一键生成并启用 systemd 用户服务(路径在安装时解析,仓库搬家后重跑即可)
.venv/bin/python -m hypr_jev --install-service --live   # 语音助手通常配 --live
.venv/bin/python -m hypr_jev --install-service           # 不加 --live 则为 dry-run 单元

# 发命令(终端可交互时,confirm/choose 会追问一次)
.venv/bin/python -m hypr_jev --send "打开终端"
.venv/bin/python -m hypr_jev --send "关闭终端" --yes      # 自动代答确认(谨慎)
echo n | .venv/bin/python -m hypr_jev --pending           # 脚本化应答挂起的确认/选择

# 状态 / 切模式 / 停止
.venv/bin/python -m hypr_jev --status
.venv/bin/python -m hypr_jev --send "音量大点" --live      # 顺手切 daemon 到 live
.venv/bin/python -m hypr_jev --stop

# 卸载服务(保留 ~/.config/hypr-jev/env,内含密钥,自行决定去留)
.venv/bin/python -m hypr_jev --uninstall-service
```

daemon 同时监听 `$XDG_RUNTIME_DIR/hypr-jev.sock`(文字请求,单行 JSON 协议)与
PTT FIFO(与 `--asr` 完全同路径,**键位绑定无需任何改动**);语音触发 confirm/choose
时先挂起,再说一句「是 / 否 / 编号」即可语音应答(只有整句恰好是应答才命中,
不会误吞普通命令;choose 挂起时说「好」= 选 Jev 首选)。编号规则:带「第」按
第几个(「第一个」→ 列表 0 号);不带「第」按屏幕印出的编号(「2」/「2号」/「选2」
→ 2 号)。也可照旧用 `hypr-jev --pending` 从终端应答。

<details>
<summary>手动启动(不用 systemd)</summary>

```sh
.venv/bin/python -m hypr_jev --daemon --live        # 前台常驻,Ctrl-C 退出
```

</details>

**systemd 服务的可移植性设计**:单元在安装时从当前 venv/仓库解析出绝对路径
(`ExecStart`/`WorkingDirectory`),密钥与代理写入 `~/.config/hypr-jev/env`
(权限 600)经 `EnvironmentFile` 注入,不硬编码任何机器信息;ASR 模型、socket、
FIFO 路径全部按包位置/XDG 解析,与 cwd 无关。若你的 Hyprland 未用 uwsm/DE 托管,
在 `hyprland.conf` 加上(已配置则忽略):

```ini
exec-once = dbus-update-activation-environment --systemd --all
exec-once = systemctl --user start graphical-session.target
```

REPL 内:`/live` `/dry` 切换执行模式,`/q` 退出;
应用/窗口匹配低置信度时列出候选让你选编号(Jev 首选标 ★);
破坏性命令先 `[y/N]` 确认。

### PTT 按键绑定(可选)

ASR 模式监听 FIFO(`$XDG_RUNTIME_DIR/hypr-jev.ptt`)。注册按住说话键:

```sh
hyprctl eval 'hl.bind("SUPER+F9", hl.dsp.exec_cmd("echo start > '"$XDG_RUNTIME_DIR"'/hypr-jev.ptt"))'
hyprctl eval 'hl.bind("SUPER+F9", hl.dsp.exec_cmd("echo stop > '"$XDG_RUNTIME_DIR"'/hypr-jev.ptt"), { release = true })'
```

> 运行时注册,重载配置即失效;永久化把两句 `hl.bind` 写进 Lua 配置即可。
> 也可任意方式写 FIFO:`echo start > $FIFO; …说话…; echo stop > $FIFO`

## 配置

所有参数集中在 `hypr_jev/config.py`:

| 参数                | 默认                          | 说明                                            |
| ------------------- | ----------------------------- | ----------------------------------------------- |
| `CONF_EXECUTE`      | 0.75                          | ≥ 此值且免确认 → 自动执行                       |
| `CONF_ASK`          | 0.50                          | < 此值 → 忽略(视作没听见)                       |
| `NEEDS_CONFIRM_MAX` | 0.40                          | Jev 判定破坏性 ≥ 此值 → 强制确认                |
| `INTENTS`           | —                             | 意图表(option → 英文评分说明),加命令改这里      |
| `WIDE_MAX`          | 240                           | 应用全量语义回退的目录规模上限(Choice 上限 255) |
| `ASR_MODEL`         | `models/faster-whisper-small` | 本地模型目录,可换 medium 提精度                 |
| `ASR_LANGUAGE`      | `None`                        | 自动检测(中英混说);或 `"zh"` / `"en"`           |

环境变量覆盖(可移植旋钮):

| 变量                   | 作用                                                    |
| ---------------------- | ------------------------------------------------------- |
| `HYPR_JEV_ASR_MODEL`   | 覆盖 ASR 模型目录(默认仓库内 `models/…`)                |
| `HYPR_JEV_RUNTIME_DIR` | 覆盖 socket/FIFO 所在目录(默认 `XDG_RUNTIME_DIR`)       |
| `TYPESAFE_API_KEY`     | Jev 决策密钥;systemd 服务从 `~/.config/hypr-jev/env` 读 |

> 代理(`HTTPS_PROXY`/`ALL_PROXY` 等)同样从 env 文件注入服务。

## 测试与验证

```sh
.venv/bin/python tests/test_intents.py   # 意图基准:22 例中英准确率 + 延迟 + fan-out(真实 API)
.venv/bin/python tests/test_exec.py      # 端到端集成:8 步可逆 live 动作,自动恢复现场
.venv/bin/python tests/test_exec.py --dry  # 只走链路不执行
```

实测结果(RT 3050 / i5-11400H,Hyprland 0.56.2,经本地代理):

| 项目                      | 结果                                                   |
| ------------------------- | ------------------------------------------------------ |
| 意图识别(22 例中英+混合)  | **22/22 全对**,负例拒识正确                            |
| Jev 决策延迟              | p50 ≈ 400ms(含代理往返);直连热调用 291–402ms           |
| fan-out(1 问题 vs 3 问题) | 基本持平(网络抖动下有波动,历史多次测量近零差)          |
| `needs_confirm` 区分度    | 关窗 0.6–0.7 / 锁屏 0.85+ / 常规 <0.06                 |
| ASR 转写                  | jfk 样本逐字正确,实时率 0.26x(2s 命令 ≈ 600–800ms)     |
| daemon 冷启动(只付一次)   | 连接 ≈0.9–1.3s + 模型 ≈1.2s;此后 `--send` 全程 <1s     |
| 端到端                    | 语音→执行 ≈ 1.1s;文字→执行 ≈ 0.5s                      |
| 集成测试                  | 8/8(通知/工作区/音量/浮动/启动/定向关窗/门控拦截/拒识) |

## 已知限制

- **Hyprland ≥0.55 的 Lua IPC 是主要坑源**(均已在本项目解决并记录):
  - `hyprctl dispatch` 旧字符串语法废弃,须用 `hl.dsp.*` Lua 表达式;
  - `clients` JSON 不再含 `focused` 字段,需从 `activewindow` 取;
  - 窗口关闭只有 `close({ window = "address:0x…" })` 精确有效,
    位置参数形式在目标非聚焦时静默失效/误关聚焦窗口;
  - `hyprctl keyword` 在 Lua 配置下不可用(键位须 `hl.bind` via eval);
  - `gtk-launch` 直跑会阻塞,须经 `hl.dsp.exec_cmd()` 托管
- 应用启动:名称无字符串重叠且语义罕见的可能未命中;无 `.desktop` 的
  AppImage 不在索引;"关闭所有XX"暂不支持(一次一个窗口)
- REPL / `--asr` 模式的确认/选择仍是终端输入(y/N 或编号);daemon 语音路径
  支持语音应答(是/否/编号),终端侧可用 `--pending` 应答
- ~~首次 API 冷启动 ~4.8s(TLS+代理握手),常驻进程复用连接后消失~~
  → 已由常驻 daemon + systemd 用户服务消除(见「使用」)

## 路线图

- [x] 语音确认(confirm/choose 挂起后用语音回答 yes/no 或编号;终端侧仍可用 `--pending` 应答)
- [x] 常驻 daemon + systemd 用户服务,消除冷启动(`--daemon` / `--install-service`)
- [ ] GUI / TTS 反馈端(替换 `feedback.py`)
- [ ] VAD 自动断句,免按键
- [ ] 若出现开源版 Jev: 替换 `router.py`(零费用、完全离线;取决于上游,仅一种可能性)
- [ ] 动作扩展:媒体控制、剪贴板、多窗口批量操作

## 贡献

Issue / PR 欢迎。改动后请跑:

```sh
.venv/bin/python tests/test_exec.py --dry   # 链路回归(无副作用)
.venv/bin/python tests/test_intents.py      # 意图基准(真实 API)
.venv/bin/python tests/test_daemon.py       # daemon 协议回归(离线)
.venv/bin/python tests/test_service.py      # systemd 安装回归(离线)
```

## 致谢

- [TypeSafe](https://typesafe.ai) — Jev / System One 决策模型
- [SYSTRAN](https://github.com/SYSTRAN) — faster-whisper
- [Hyprland](https://hyprland.org)
