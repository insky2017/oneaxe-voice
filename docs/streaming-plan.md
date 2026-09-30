# 三模式与顶栏集成：实施记录

用户于 2026-10-01 确认方案并授权实现及充分端到端测试。

## 范围

- 稳听（Qwen VAD）：保留现有停顿分段。
- 随听（Qwen streaming）：采用官方接口；候选文字单独预览，只追加已冻结前缀。
- 即听（Confucius4-R2T2）：使用官方稳定前缀及滚动窗口代码，160 ms 音频块。
- AppIndicator 顶栏菜单、状态图标、可选无焦点字幕、下轮切换模式。
- 单个活跃引擎，独立流式环境，原权重只读；保持 VPlus 隔离。
- 保留 F8 整轮开始/结束、取消、窗口保护和尾部补齐。口头语整理不在范围内。

## 官方依据

- Qwen3-ASR：本机 qwen-asr 0.0.6 的 `init_streaming_state`、`streaming_transcribe`、`finish_streaming_transcribe`，默认 2 s / 2 个初始可变块 / 5 个可回退 token。
- R2T2：固定 [26d55a54](https://github.com/netease-youdao/Confucius4-R2T2/tree/26d55a54ce5670cff9947a167d8ed95d569fd4d9)，优先复用官方 `r2t2_asr.py`、稳定前缀和无重置滚动窗口实现；保留许可证及来源。
- Ubuntu：GTK3 + Ayatana AppIndicator，使用已安装的 GNOME AppIndicator 扩展。

## 交付步骤

- [x] 在独立环境验证两个流式模型的 CUDA 推理及实际输出约定。
- [x] 实现引擎生命周期、本机鉴权流式通道、背压与取消。
- [x] 接入持续采集、稳定文字追加及字幕预览。
- [x] 实现模式菜单、状态图标、设置、托盘焦点保护与安装。
- [x] 自动化测试及真实 GPU、F8、X11 文本框、菜单交互端到端测试。
- [x] 长音频/窗口衔接、静音、错误与资源释放验证；文档及提交。

以上复用点在验证中若发现官方边界或兼容性问题，将在验证记录中记载实际处理与限制。
