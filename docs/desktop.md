# 桌面听写

## 使用

1. 连接 DJI 接收器并开启麦克风发射器，将光标放入要输入文字的位置。
2. 按 **F8** 开始录音，看到录音通知后说话。
3. 再按 **F8** 停止，等待本地 GPU 识别。保持目标窗口及输入位置不变，文字会自动粘贴。

录音最长 60 秒，到时自动识别。识别期间再按 F8 只提示正在处理，不会再开一段录音。首次推理需加载模型，时间更长。

窗口或 X11 焦点改变时不自动输入，文字保留在剪贴板并通知你手动粘贴。剪贴板会替换成识别结果，不会在粘贴后恢复旧内容。同一窗口内的标签页或输入框变化未必会改变 X11 焦点，等待识别时应保持输入位置；这是 X11 窗口检查的限制。

所有转写合并为一行并移除控制字符，不发送 Enter。GNOME Terminal、Konsole 等使用 Ctrl+Shift+V，VS Code 使用 Shift+Insert（兼容编辑区和集成终端），常规输入框使用 Ctrl+V。tmux 在终端里接收粘贴内容，不自动执行命令。

## 命令

```bash
./bin/oneaxe-voice devices
./bin/oneaxe-voice toggle
./bin/oneaxe-voice desktop-status
./bin/oneaxe-voice cancel
```

`cancel` 会取消录音或等待中的识别，并阻止本次文字输入；已经发出的粘贴无法撤回。服务端若已开始 GPU 推理，会继续完成本次推理，但取消后的桌面端不会接收并粘贴结果。

`desktop-status` 显示 idle、starting、recording、stopping、transcribing、delivering 或 error，以及最近一次操作结果、音频长度和电平，不包含转写正文。最近一次成功识别的单行文字保存在私有的 `runtime/last-transcript.txt`，便于粘贴失败后找回。

## 安装与移除

系统需有 `pactl`、`parec`（通常由 `pulseaudio-utils` 提供）、`xdotool`、`xprop`、`xclip`、`gsettings`、`notify-send`，以及第一阶段的模型和 Python 环境。当前实现面向 GNOME X11。

```bash
./bin/oneaxe-voice desktop-setup --shortcut F8
```

安装器按当前项目实际路径渲染两个用户级服务，并注册一个 GNOME 自定义快捷键。服务模板保存在仓库，生成的本机路径只出现在用户配置中。已有 API 服务软链接会被原子替换为生成的单元文件，不会写回模板。安装保留其他自定义快捷键，遇到已存在的 GNOME F8 绑定会报错；其他应用自行注册的全局快捷键仍可能冲突。

当前服务已启动，没有 enable 登录自启。F8 的命令行入口会按需启动桌面服务，桌面服务通过 `Wants` 启动 OneAxe Voice ASR。移动项目后重新运行安装命令以更新服务与快捷键路径。重新登录若显示环境变化，也可重新运行该命令。

```bash
systemctl --user status oneaxe-voice-desktop.service
journalctl --user -u oneaxe-voice-desktop.service -n 50 --no-pager
./bin/oneaxe-voice desktop-setup --remove
```

移除操作只删除本项目的快捷键并停止桌面控制器。需要停止 GPU 服务时另行运行 `systemctl --user stop oneaxe-voice.service`。

## 麦克风配置

`runtime/desktop.json` 为本机配置，未纳入 Git。默认 `source: null` 每次录音自动选择唯一 DJI 设备，不会修改系统默认输入源，也不会在 DJI 缺失时退回其他麦克风。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `source` | `null` | 自动选择 DJI；指定字符串时须与 devices 的 name 完全相同 |
| `max_seconds` | `60` | 每次最大录音秒数，范围 0.1–60 |
| `silence_dbfs` | `-50` | 最响的 20 ms 帧也低于此值时，跳过模型识别 |
| `clipboard_only` | `false` | 改成 true 后仅复制，不模拟粘贴按键 |
| `shortcut` | `F8` | 通知提示使用的名称；实际键绑定应通过 desktop-setup 调整 |

修改这些录音配置后，下次录音自动读取。低电平门限不是语音活动检测，仍可能把噪音送入 ASR；说话很轻时先检查麦克风增益，必要时降低门限。按键采用点按切换，长按可能产生系统按键重复。

## 隔离与数据

`oneaxe-voice-desktop.service` 只负责麦克风、状态通知、本机 HTTP 调用和 X11 输入，不加载模型。它通过权限为 0600 的 Unix socket 接受同一用户的命令，GPU 模型仍由 `oneaxe-voice.service` 管理。两者都不导入、改动或重启 VPlus。

录音在内存中组装，最大约 1.92 MB PCM；API 内部按第一阶段规则创建并清理临时 WAV。桌面日志不记录转写正文，`runtime/last-transcript.txt` 只保留最近一次成功结果。麦克风拔出、录音流中断、API 失败、取消或切换窗口均不会盲目向当前窗口输入。
