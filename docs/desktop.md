# 桌面听写

## 使用

下述 700 ms 分段适用于默认“稳听”。顶栏还可选择“随听”和“即听”流式模式，安装和行为见 [三模式说明](modes.md)。

1. 连接 DJI 接收器并开启麦克风发射器，将光标放入要输入文字的位置。
2. 按 **F8** 开始持续录音。停顿约 700 ms 时自动识别上一段，文字逐段追加。
3. 再按 **F8** 停止采集，剩余语音识别完毕后结束本轮。保持目标窗口及输入位置不变。

默认每轮录音最长 15 分钟、单段最长 15 秒。录音与识别并行；录音中按 F8 始终用于结束当前一轮。收尾期间再次按 F8 只提示正在处理，不会新开录音。首次推理需要加载模型，按 F8 开始时会提前预热。

窗口或 X11 焦点改变时，本轮后续结果都改为累积到剪贴板，并提示手动粘贴。自动输入期间，剪贴板通常保存最近一个片段；完整文本保存在 `runtime/last-transcript.txt`。同一窗口内的标签页或输入框变化未必改变 X11 焦点，听写时应保持输入位置。

所有转写合并为一行并移除控制字符，不发送 Enter。GNOME Terminal、Konsole 等使用 Ctrl+Shift+V，VS Code 使用 Shift+Insert（兼容编辑区和集成终端），常规输入框使用 Ctrl+V。tmux 在终端里接收粘贴内容，不自动执行命令。

## 命令

```bash
./bin/oneaxe-voice devices
./bin/oneaxe-voice toggle
./bin/oneaxe-voice desktop-status
./bin/oneaxe-voice cancel
```

`cancel` 会取消采集、队列和等待中的识别，阻止后续文字输入；已经输入的片段保留，已经发出的单次粘贴无法撤回。服务端若已开始 GPU 推理，可能继续完成本次推理，但取消后的桌面端不会接收并粘贴结果。

`desktop-status` 显示 idle、starting、recording、finishing 或 error；`capture_active`、`recognizing`、`warming` 分别表示采集、单段识别与预热。状态包含片段计数，不包含转写正文。完整结果保存为 `runtime/last-transcript.txt`，原始片段及切分原因保存在 `runtime/last-session.json`。

## 安装与移除

系统需有 `pactl`、`parec`（通常由 `pulseaudio-utils` 提供）、`xdotool`、`xprop`、`xclip`、`gsettings`、`notify-send`，以及第一阶段的模型和 Python 环境。当前实现面向 GNOME X11。

```bash
./bin/oneaxe-voice desktop-setup --shortcut F8
```

安装器按当前项目实际路径渲染三个用户级服务，并注册一个 GNOME 自定义快捷键。服务模板保存在仓库，生成的本机路径只出现在用户配置中。已有 API 服务软链接会被原子替换为生成的单元文件，不会写回模板。安装保留其他自定义快捷键，遇到已存在的 GNOME F8 绑定会报错；其他应用自行注册的全局快捷键仍可能冲突。

顶栏服务随图形会话启动，带起桌面控制和轻量 API，GPU 模型按需加载。F8 的命令行入口会按需启动桌面服务，桌面服务通过 `Wants` 启动 OneAxe Voice ASR。移动项目后重新运行安装命令以更新服务与快捷键路径。重新登录若显示环境变化，也可重新运行该命令。

```bash
systemctl --user status oneaxe-voice-desktop.service
journalctl --user -u oneaxe-voice-desktop.service -n 50 --no-pager
./bin/oneaxe-voice desktop-setup --remove
```

移除操作只删除本项目的快捷键、禁用顶栏自启并停止桌面控制器。需要停止 GPU 服务时另行运行 `systemctl --user stop oneaxe-voice.service`。

## 麦克风配置

`runtime/desktop.json` 为本机配置，未纳入 Git。默认 `source: null` 每次录音自动选择唯一 DJI 设备，不会修改系统默认输入源，也不会在 DJI 缺失时退回其他麦克风。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `mode` | `vad` | `vad` / `qwen-stream` / `r2t2`；录音中选择下轮生效 |
| `preview` | `true` | 显示不抢焦点的候选字幕 |
| `icon_theme` | `light` | `light` 适合深色顶栏，`dark` 适合浅色顶栏 |
| `source` | `null` | 自动选择 DJI；指定字符串时须与 devices 的 name 完全相同 |
| `pause_ms` | `700` | 连续无人声分段阈值，范围 300–2000 ms |
| `segment_seconds` | `15` | 单段最大秒数，范围 3–30 |
| `max_session_seconds` | `900` | 整轮录音最大秒数，范围 10–3600 |
| `vad_mode` | `2` | WebRTC VAD 模式，范围 0–3 |
| `vad_min_dbfs` | `-60` | 低电平过滤阈值 |
| `queue_size` | `8` | 等待 ASR 的片段上限，范围 1–16 |
| `clipboard_only` | `false` | 改成 true 后仅复制，不模拟粘贴按键 |
| `shortcut` | `F8` | 通知提示使用的名称；实际键绑定应通过 desktop-setup 调整 |

修改录音配置后，下次录音自动读取。VAD 仍可能把音乐、人声节目或噪音误判为说话；说话很轻时先检查麦克风增益，必要时调整判定门限。按键采用点按切换，长按可能产生系统按键重复。详细算法、队列行为和延迟说明见 [VAD 分段与持续输出](vad.md)。

## 隔离与数据

`oneaxe-voice-desktop.service` 只负责麦克风、状态通知、本机 HTTP 调用和 X11 输入，不加载模型。它通过权限为 0600 的 Unix socket 接受同一用户的命令，GPU 模型仍由 `oneaxe-voice.service` 管理。两者都不导入、改动或重启 VPlus。

音频分段在有界内存队列中，单段默认最多约 0.48 MB PCM；API 创建并清理临时 WAV。桌面日志不记录转写正文，结果文件只保留最近一轮有结果的文本。录音流中断、API 失败、取消或切换窗口都有对应的停止或复制处理。
