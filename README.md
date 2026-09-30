# OneAxe Voice

本机 GPU 听写工具。支持 DJI Mic 录音、F8 全局快捷键、本地 Qwen3-ASR / Confucius4-R2T2 识别及 X11 自动粘贴，也提供短 WAV API 和命令行客户端。

## 桌面听写

连接 DJI Mic，把光标放在输入位置，按 **F8** 开始持续听写。停顿约 **700 ms** 后，前一段自动识别并追加到光标处，麦克风继续录音。再次按 **F8** 停止，并补齐最后一段；不发送回车。

顶栏的“话筒 + 文字气泡”图标显示状态，并可切换三种模式：

| 模式 | 使用方式 |
| --- | --- |
| 稳听 · Qwen 分段 | 保留原来的 700 ms 停顿分段 |
| 随听 · Qwen 流式 | 连续送入官方流式接口，候选字幕实时更新、冻结前文自动输入 |
| 即听 · R2T2 流式 | 160 ms 音频步长，官方稳定前缀与滚动窗口 |

三种模式都用 F8 开始和结束。录音中切换模式在下一轮生效；字幕可以关闭，不抢输入焦点。首次使用流式模式需安装独立环境，见 [三模式与顶栏说明](docs/modes.md)。

桌面通知显示开始和结束状态。默认每段最长 15 秒，每轮最长 15 分钟。切换窗口后本轮改为累积到剪贴板，不再自动输入。使用 GNOME X11；没有 DJI 设备时会提示，不会改用内置麦克风。保留原始口头语，不做文字润色。

首次安装或移动项目目录后运行：

    ./bin/oneaxe-voice desktop-setup --shortcut F8

查看状态或取消当前听写：

    ./bin/oneaxe-voice desktop-status
    ./bin/oneaxe-voice cancel

完整设置、卸载快捷键及输入行为见 [桌面听写说明](docs/desktop.md)。

## 使用

在项目目录运行：

    ./bin/oneaxe-voice health
    ./bin/oneaxe-voice status
    ./bin/oneaxe-voice transcribe /absolute/path/recording.wav
    ./bin/oneaxe-voice transcribe /absolute/path/recording.wav --json

支持 0.1–60 秒、8000–48000 Hz、单声道或双声道的 16-bit PCM WAV，最大 12 MiB。
服务地址为 http://127.0.0.1:8097。默认使用本地 Qwen3-ASR-1.7B 和 CUDA。
首次识别会加载模型；空闲 120 秒后卸载模型权重。

## 文档

- [架构与隔离边界](docs/architecture.md)
- [三模式、顶栏与实时字幕](docs/modes.md)
- [桌面听写说明](docs/desktop.md)
- [VAD 分段与持续输出](docs/vad.md)
- [接口与运行说明](docs/api.md)
- [验证记录](docs/validation.md)
- [实施计划](docs/plan.md)

## 服务管理

    systemctl --user start oneaxe-voice.service
    systemctl --user stop oneaxe-voice.service
    journalctl --user -u oneaxe-voice.service -n 50

服务单元的源文件位于 systemd/oneaxe-voice.service。
桌面安装器启用顶栏图标随图形会话启动；API 与桌面控制随之启动，GPU 模型按需加载。F8 可以重新唤起退出的图标。

## 开发验证

    .venv/bin/python -m unittest discover -s tests -v

原分段模式使用 .venv，两个流式模式使用独立的 .venv-stream；均独立于 VPlus 的环境。模型权重从既有本地目录读取。
