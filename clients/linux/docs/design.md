# 实现边界

客户端独立工程，通过移动 V1 能力契约连接 OneAxe Voice。统一模型适配由服务端提供；客户端没有模型管理权限。

```text
F9 / 托盘 / 窗口按钮
        ↓
单实例 GTK 应用 → 设置 / 系统密钥环
        ↓
查询 capabilities → WSS start → ready
        ↓
parec → WebRTC VAD → 有界音频队列 → WSS / GPU 服务
        ↑                                  ↓
        └──────── 累计发送额度 flow ──────────┘
                                           ↓
                             会话 / 序号 / 固定前缀检查
                                           ↓
                           候选字幕 + 已固定文字增量
                                           ↓
                              目标窗口核对 → 粘贴 / 复制
```

GTK3 用于兼容 e15l 的 Ubuntu 20.04。网络与音频独立于界面线程。没有本地 HTTP 服务、模型、CUDA 或 Python 运行环境。

| 接口 | 作用 |
| --- | --- |
| `GET /api/mobile/v1/capabilities` | 查询模式、模型代次、可开始状态、音频及流控参数；不加载模型 |
| `WSS /api/mobile/v1/dictation/stream` | 发送 PCM 和控制，接收流控、固定文字、候选与终止状态 |

当前源码支持服务端发布为兼容移动 V1 的 R2T2 和 Qwen 流式能力。客户端按 `protocol_version`、音频及流控参数、`ready`、`stream_supported`、`can_start`、模型代次与空闲名额判断能否开始；`model_id` 和 `mode` 只用于显示，不是模型白名单。远端 `start` 仅包含协议版本、预期服务实例/模型代次和音频格式，不携带模型名称或模式。

音频为 PCM16LE / 16 kHz / 单声道，每包不超过 5120 bytes。先收到 ready，再开启麦克风。WebRTC VAD 以 20 ms 检测语音，保留 240 ms 前置音频，默认停顿 1000 ms 发 flush。flush 不重置累计样本数。

未发送音频最多 2 秒，包括正在等待额度的包。超过则停止并提示，不静默丢音频。finish 与此前音频用同一有序队列。cancel 先关闭文字交付门，再结束网络会话。

固定全文必须保持原前缀。候选只预览，粘贴只用固定增量。外部应用没有通用输入回执，不能保证任意焦点变化或应用故障下严格 exactly-once；遇到不确定状态时关闭本轮自动粘贴，保留全文供复制。

手动复制与自动交付使用同一 FIFO worker，桌面操作期间不持有队列锁。录音/收尾中复制会让本轮转为仅复制，后续快照写累计全文。该串行化解决应用内部的命令交错；目标应用延迟读取剪贴板仍是独立风险，详见 [重复输入调查](duplicate-input-investigation.md)。

当前容量为 1 PC + 1 远端，Linux 与 Android 共用远端名额。公网、多远端扩容、文字润色、长期历史和 Wayland 自动输入均未纳入首版。Qwen 的实际服务与两机安装验收状态在 [验证记录](validation.md) 单列。
