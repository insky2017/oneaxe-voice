# OneAxe Voice

本机 GPU 听写工具。第一阶段提供短 WAV 录音转文字接口及命令行客户端。

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
- [接口与运行说明](docs/api.md)
- [验证记录](docs/validation.md)
- [实施计划](docs/plan.md)

## 服务管理

    systemctl --user start oneaxe-voice.service
    systemctl --user stop oneaxe-voice.service
    journalctl --user -u oneaxe-voice.service -n 50

服务单元的源文件位于 systemd/oneaxe-voice.service。
本阶段仅启动服务，没有启用登录自启。

## 开发验证

    .venv/bin/python -m unittest discover -s tests -v

本项目运行环境位于 .venv，独立于 VPlus 的环境。模型权重从既有本地目录读取。
