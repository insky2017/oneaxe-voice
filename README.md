# OneAxe Voice

本机 GPU 听写工具。支持 DJI Mic 录音、F8 全局快捷键、本地 Qwen3-ASR / Confucius4-R2T2 识别及 X11 自动粘贴，也提供短 WAV API 和命令行客户端。

## 桌面听写

连接 DJI Mic，把光标放在输入位置，按 **F8** 开始持续听写。稳听在约 **700 ms** 停顿后识别整段；两种流式模式边说边识别，停顿约 **1 秒** 后自动补齐尚未输入的尾字，麦克风继续录音。再次按 **F8** 停止并收尾；不发送回车。

顶栏的“话筒 + 文字气泡”图标显示状态，并可切换三种模式：

| 模式 | 使用方式 |
| --- | --- |
| 稳听 · Qwen 分段 | 保留原来的 700 ms 停顿分段 |
| 随听 · Qwen 流式 | 连续送入官方流式接口，候选字幕实时更新、冻结前文自动输入 |
| 即听 · R2T2 流式 | 160 ms 音频步长，官方稳定前缀与滚动窗口 |

桌面启动会预热上次选择的模式，空闲时点击模式也会加载。默认常驻模型；顶栏“模型”子菜单提供立即加载、立即卸载，以及可选的“空闲 2 分钟后自动卸载”，策略保存后重启仍生效。等待模型显示“已就绪”后按 F8 可避免冷启动等待，仍需积累音频并完成推理才会输出文字。录音中切换模式在本轮收尾后预热，下一轮生效。

字幕区分“待确认”和“已发送”，默认显示在目标屏幕顶部，最多两行，可从顶栏菜单选择位置或拖动后记忆；也可关闭，不抢输入焦点。首次使用流式模式需安装独立环境，见 [三模式与顶栏说明](docs/modes.md)。

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
仅启动 API 时，模型在预热或识别请求到来后加载，同样遵循保存的生命周期策略。

## PC 与手机并发

R2T2 使用一份 GPU 权重和独立会话状态，支持 **1 路 PC + 1 路手机**。手机只能使用 PC 已加载的 R2T2；不能加载、切换或卸载模型。PC 保留模型控制权。手机结束、取消和断线只清理本路，模型继续驻留；两端都空闲后才计算用户已开启的自动卸载时间。

移动端通过 Tailnet 的 HTTPS/WSS 和独立设备凭据接入。入口需要单独配置证书，不因安装代码而自动开放。接入开发以 [移动接口 V1](docs/mobile-api-v1.md) 为准，部署与凭据操作见 [移动入口部署](docs/mobile-deployment.md)。Qwen 两种模式暂不提供移动并发。

## 文档

- [交互式架构与流程图（离线 HTML）](docs/architecture-map.html)：总体关系、听写流程、双路并发、模型生命周期、部署依赖及实现状态；点击节点查看职责与源码。
- [架构与隔离边界](docs/architecture.md)
- [三模式、顶栏与实时字幕](docs/modes.md)
- [桌面听写说明](docs/desktop.md)
- [VAD 分段与持续输出](docs/vad.md)
- [接口与运行说明](docs/api.md)
- [移动接口 V1](docs/mobile-api-v1.md) · [移动入口部署](docs/mobile-deployment.md)
- [R2T2 并发研究](docs/concurrency-research-2026-10-02.md)
- [超过两路的接入与容量研究](docs/multi-stream-capacity-research-2026-10-02.md)：4090 显存核算、速度瓶颈与逐级验证方案；生产仍为两路。
- [多路候选与隔离实验记录](docs/multi-stream-experiment-2026-10-02.md)：两路基线、三路退化、四路积压保护及实测曲线；未扩容生产。
- [CPU / GPU 预处理对照](docs/preprocessing-study-2026-10-02.md)：CPU 分工、等待开销、GPU 特征实测及优化顺序；尚未部署。
- [完整两路 CPU / GPU 优化对照](docs/preprocessing-e2e-2026-10-02.md)：被动等待、GPU 特征、CPU 核秒、逐路延迟和取消实测；正式服务未改动。
- [验证记录](docs/validation.md)
- [实施计划](docs/plan.md)

## 服务管理

    systemctl --user start oneaxe-voice.service
    systemctl --user stop oneaxe-voice.service
    journalctl --user -u oneaxe-voice.service -n 50

服务单元的源文件位于 systemd/oneaxe-voice.service。
桌面安装器启用顶栏图标随图形会话启动；API 与桌面控制随之启动，并预热上次选择的模式。F8 可以重新唤起退出的图标。

## 开发验证

    .venv/bin/python -m unittest discover -s tests -v

原分段模式使用 .venv，两个流式模式使用独立的 .venv-stream；均独立于 VPlus 的环境。模型权重从既有本地目录读取。
