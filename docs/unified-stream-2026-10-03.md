# Qwen / R2T2 统一远端流式接口

日期：2026-10-03。用户已批准扩展服务端接口实现；协议版本、路径和客户端模型管理权限不变。部署与最终验收记录见本页末尾。

## 对使用者的变化

PC 顶栏选择并预热 Qwen 流式或 R2T2 后，Android 与 Linux 客户端都先查询 capabilities，再通过同一个移动 V1 WebSocket 听写。客户端不选择或加载模型；模型名称只用于显示。现有 Android 协议代码无需按模型分支，Linux 移除了原来的 R2T2 名称白名单。

容量仍为 1 路 PC + 1 路远端；Android 和 Linux 共用一个远端名额。稳听 `vad` 尚未接入远端流式协议。公网入口和更多并发不在本次范围。

## 实现与官方依据

```text
Android / Linux ── capabilities + V1 音频流 ──┐
本机 F8 ───────── 原本机流式入口 ────────────┤
                                           ▼
                             EngineRouter：权限 / 模型代次 / 1+1 名额
                                           ▼
                             WorkerClient：rpc_id + session 分发
                                           ▼
                      concurrent_worker：每会话独立状态、队列、取消
                                ┌──────────┴─────────┐
                        AsyncQwenDecoder       AsyncR2T2Decoder
                                └──────────┬─────────┘
                                           ▼
                                当前选定模型的一个 AsyncLLM
```

- Qwen 基于安装的官方 `qwen-asr==0.0.6`，对照 `qwen_asr/inference/qwen3_asr.py` 的 `init_streaming_state`、`streaming_transcribe`、`finish_streaming_transcribe`。官方工程：[Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)。适配将准备输入、异步生成、更新状态分开；生成交给同一个 vLLM AsyncLLM。
- 保留 Qwen 2 秒块、2 块 / 5 token 回退、项目既有额外 8 token 固定安全边界、256 输出 token 上限和 30 秒窗口收尾。官方算法黄金对照验证前缀、音频、参数及结果，不把模拟结果称为 GPU 识别证据。
- Qwen 保留 512 MiB KV 默认预算，允许两个推理请求，CUDA decode graph 大小为 1 / 2。R2T2 保留 1 GiB KV 与原算法。模型、编码器和 CUDA 运行时另占显存。
- 两模式均保留子进程的 `OMP_NUM_THREADS=4`、`OMP_WAIT_POLICY=PASSIVE`。不修改 VPlus、共享模型权重或用户全局环境。
- Qwen 的原 F8 WebSocket 保持原消息格式，内部使用新的独立会话 worker，可与远端 V1 同时运行。取消只中止本路请求，迟到结果不得再提交；不通过全局重置来取消。

## 同一协议如何处理不同出字节奏

`text` 始终是累计固定全文；`pending` 是可修订候选；`seq`、会话身份、流控、结束和错误语义一致。模型的块大小和首次固定文字时间仍可不同。

Qwen 每次需要 32000 个真实样本。原 V1 32000 额度会让 2560 样本整帧发送停在 30720：模型缺下一帧，客户端又缺新额度。现在服务端为 Qwen 声明 `flow.window_samples=34560`，允许完整一帧跨过推理边界。R2T2 仍为 32000；客户端使用能力查询及 `ready/flow` 给出的额度，不写死数字。

`audio_processed_samples` 只计已完成处理的真实样本及明确跳过的数字静音，不把缓存当完成，不计预热或补零。停止时必须 `processed == sent`。客户端未发送缓存仍限制为 2 秒。

## 生命周期与权限

远端 start 不接受 `mode/model`。服务端原子核对已加载模型的实例、代次和名额；查询后发生切换则返回 `MODEL_CHANGED`。已开始的会话不会悄悄跨模型续写。

PC 录音时保留管理保护。只有远端活动时，PC 主动切换 / 卸载会结束远端并分别报告 `MODEL_CHANGED` / `MODEL_NOT_READY`，保留已固定文字。远端取消、断线或错误只释放本路，不能卸载模型或结束 PC。认证方式与设备凭据保持不变。

## 验证记录

最终结果和限制将在部署回归后填写；过程报告位于私有 runtime，发布证据只保留计数、时延、哈希和检查结果，不含凭据或转写正文。

真实验证入口：`tests/e2e_unified_stream.py`。默认不管理模型；切换与卸载场景必须显式启用 `--allow-model-management`，且仅允许独立 loopback 测试端口。跨机器 10 分钟验证使用 Linux 工程的 `tests/dual_stream.py` 与 e15l 上实际安装的客户端。

Qwen 的已处理音频进度以 2 秒块前进，因此本轮进度时延 P95 门槛显式为 2.5 秒，末段相对前段增长仍不超过 0.5 秒；这不是把 R2T2 原有 2 秒指标改写为通过。远端 `sent-processed` 不超过服务授予的 34560 样本额度；未发送缓存不超过 32000 样本。首次固定文字单独记录，不与 GPU 单步推理时延混淆。
