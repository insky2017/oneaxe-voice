# 官方推理代码来源

`r2t2/` 中的 Python 文件原样来自网易有道 [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2)，固定提交 `26d55a54ce5670cff9947a167d8ed95d569fd4d9`。采用 Apache-2.0 代码许可证，随附 LICENSE；模型权重另适用 MODEL_LICENSE，权重不在本仓库内。

仅包含独立推理适配器，未引入其 WebSocket 服务、VAD 模型、日志及文本重复清理。OneAxe Voice 使用自己的本机鉴权、资源管理和桌面输入边界。`streaming_transcribe_no_reset` 的 16 秒窗口 / 8 秒移动及初始 320 ms、后续 160 ms 步长均遵循本版本官方实现。停止时用短静音尾垫触发官方 final flush，防止恰好整块结束时遗漏保留 token。
