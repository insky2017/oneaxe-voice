# CPU 等待策略与 GPU 特征：完整两路对照

日期：2026-10-02。承接[预处理微基准](preprocessing-study-2026-10-02.md)，本次完成真实 R2T2 模型、PC 与手机协议两路同时识别的隔离对照。**研究分支已实现候选，正式服务未部署，容量仍为两路。**

后续：[2026-10-03 两路长测](cpuwait-longrun-2026-10-03.md)已完成。被动等待候选通过 600 秒确认；该次原配置因积压提前停止。下文仍记录本轮 120 秒对照，不混用两次结果。

## 结论

被动等待是本轮最主要的收益来源：完整服务平均 CPU 从 2.60–2.64 个逻辑核降到 1.01 核，普通音频处理步骤 p95 从 180–186 ms 降到 123–124 ms。在此基础上，GPU 特征处理将 CPU 再降到 0.88 核，p95 小幅降到 120–122 ms。

两个候选相对前后两批原配置均通过既有速度门槛，两份受控中英文音频的最终转写 hash 完全相同。GPU 预处理可用，但不能将前一轮微基准的约 50% 预处理加速理解为完整模型推理加速 50%。

建议先把被动等待作为部署候选；GPU 组合作为继续确认的可选方案。每个配置只测了 120 秒，本轮没有新的 10 分钟长测，也没有测试或开放三路以上。正式上线前还应做目标模式的长时间确认，不依据本轮数据扩容。

## 方法与计量边界

- RTX 4090、i9-13900KF；torch 2.9.1、vLLM 0.14.0、transformers 4.57.6、qwen-asr 0.0.6。保持 FP16、两路、1 GiB KV、160 ms 后续步长、16 秒音频窗口和原识别规则。
- 顺序为原配置前测 → CPU 被动等待 → 被动等待 + GPU 特征 → 原配置后测。每轮重新启动独立用户 systemd unit，专用 loopback `18098`；模型加载与预热不计入性能阶段。
- 两路各 120 秒，按真实采集时钟同时输入，PC 为受控中文、手机身份为受控英文；每 24 秒同时 flush。所有原配置与候选使用相同音频、偏移、角色、模型、时长和 flush 规则。
- CPU 主数据为隔离 cgroup `cpu.stat` 起止差：包含 API、worker、EngineCore、辅助进程和调用间空转，排除外部客户端、GPU 采样器及正式服务。分母为实测窗口，约 121.3 秒，包含会话绑定和清理，不能直接用 120 秒相除。
- `/proc` 的角色 CPU 仅辅助归因，不再次加到 cgroup 总量；核心 PID 与启动时间必须前后相同。1 个逻辑核表示每秒消耗 1 CPU 秒，不等于整机占用 100%，也不等同 P / E 核的算力。
- 原配置清除 `OMP_WAIT_POLICY`，保留默认等待行为，**没有改成 ACTIVE**。被动等待组仅设置 `OMP_WAIT_POLICY=PASSIVE`；各组清除 GOMP / MKL / KMP 等干扰项，与本次核对的生产配置一致，worker 仍为 4 线程。
- 正式服务保持空闲、模型驻留，实验额外加载一份 R2T2。桌面与正式 API 同时作为保护信号；录音、手机会话或模型忙则中止并清理实验。没有操作输入框、tmux、VPlus 或共享权重。

## 结果

| 配置 | cgroup CPU 核秒 / 平均核数 | 普通步骤 p95：PC / 手机 | 音频积压 p95：PC / 手机 | 实验进程显存峰值之和 |
| --- | ---: | ---: | ---: | ---: |
| 原配置前测 | 315.83 / 2.602 | 185.28 / 185.76 ms | 0.401 / 0.401 秒 | 6414 MiB |
| CPU 被动等待 | 122.16 / 1.007 | 123.00 / 124.43 ms | 0.264 / 0.265 秒 | 6414 MiB |
| 被动等待 + GPU 特征 | 106.46 / 0.878 | 119.67 / 122.16 ms | 0.263 / 0.267 秒 | 6520 MiB |
| 原配置后测 | 320.02 / 2.638 | 179.92 / 180.91 ms | 0.485 / 0.476 秒 | 6414 MiB |

被动等待相对前后原配置减少约 61%–62% 的服务 CPU 核秒；GPU 组合减少约 66%–67%。GPU 组合相对仅被动等待再减少约 13%，但步骤 p95 只小幅改善约 2%–3%，手机积压 p95 反而略高 1.7 ms。这组短测不支持 GPU 组合在每个延迟指标上都更好。

CPU 核秒的角色分解（辅助观察）：

| 配置 | API | 音频 worker | EngineCore |
| --- | ---: | ---: | ---: |
| 原配置前测 | 8.53 | 195.81 | 111.49 |
| CPU 被动等待 | 7.05 | 37.22 | 77.88 |
| GPU 组合 | 7.10 | 20.45 | 78.93 |

这与“先减少线程等待消耗，再搬移特征计算”的解释一致。PASSIVE 同时由 worker 与其子进程继承，不能把服务总节省全部归因于特征提取函数。EngineCore 使用 `setproctitle`，其 `/proc/environ` 的原始内容可能被覆盖；不能以其中缺少 OMP 字段断言没有继承。

首次固定文字没有数量级变化：PC 中文约 4.10–4.16 秒，手机英文约 1.07–1.17 秒，受音频内容和固定前缀规则影响。两个候选的 flush、finish 均通过相对门槛；GPU 组 PC / 手机 finish 为 0.211 / 0.363 秒。

整卡 GPU 活动率均值按顺序为 28.07%、26.67%、26.62%、29.94%。这些包含桌面和其他进程，不是计算单元利用率。GPU 特征工作已真实执行，但 GPU 百分比没有升高，不影响 CPU 和延迟收益成立；不以“用满 GPU”作为优化目标。GPU 组合实验进程显存峰值之和比 CPU 组多约 106 MiB，包含进程运行时，不能等同特征张量的显存大小。

## 正确性、实际设备与取消

四组均满足 `passed=true`、`measurement_valid=true`。每路 1,920,000 样本全部发送、接收并处理，750 个 audio、4 个 flush、1 个 finish 事件齐全；固定前缀、独有关键词、无串流、会话和模型代次保持检查通过。

两个候选分别与前置、后置原配置对比，PC 与手机最终转写 hash 都相同。该结论仅覆盖本次两份受控样本；前一轮特征绝对误差阈值 `1e-5` 未通过的记录仍保留，不能改写成所有音频的数值或识别等价。

GPU 候选采用官方 processor。关键接入点在 `AsyncR2T2Adapter.generate()`：每请求传入 `mm_processor_kwargs={"audio_kwargs":{"device":"cuda:0"}}`，normal、flush、finish 和 warmup 共用这条路径。仅传全局 engine kwargs 会被当前 Qwen 多模态处理中间层的浅合并覆盖，不作为可靠接入方式。

预热时通过局部 `TorchDispatchMode` 检查真实 FFT / Mel 算子的 tensor device，记录为：5 次 feature 调用，FFT 设备计数 `cuda:0=5`，Mel 设备计数 `cuda:0=10`。探针随后关闭并恢复原 extractor，性能阶段没有该探针开销。真实设备证据来自 warmup，后续请求共用同一生成入口，参数覆盖另有 CPU 测试。官方特征路径仍回传 CPU 再由 vLLM 上传，尚未实现零拷贝。

两个候选另各完成一次 24 秒取消检查：手机第 8 秒取消，收到明确 `final(reason="cancelled", complete=false)`，PC 继续到 24 秒并正常完成，模型与 worker 保持。审查发现旧测试容许取消时无 final 的 EOF；本轮已收紧判定，并核对此前 PASSIVE 实测确实收到 final，GPU 组使用收紧后的测试通过。

## 实现与验证

- [R2T2 适配器](../oneaxe_voice/r2t2_async.py)：实验 GPU 逐请求参数；默认 CPU 输入结构保持。
- [并发 worker](../oneaxe_voice/concurrent_worker.py)：`ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE=cpu|cuda:0`，默认 cpu，非法值拒绝；CUDA 候选默认启用预热设备探针。探针依赖当前 PyTorch 内部接口，尚非对任意版本的兼容承诺。
- [隔离 runner](../tests/run_capacity_profile.py)：新增 `--omp-wait-policy`、`--feature-device`、`--check-cancel`，cgroup CPU 计量、生产 API 会话保护和端口 TIME_WAIT 兼容；不改变正式服务配置。
- CPU 单元/组件测试 **264 项通过**。GPU/完整协议实测与这些模拟测试分别记账。测试命令及边界见[验证记录](validation.md)。

复现示例（先准备独立 runtime / 凭据 / `pair-ab` manifest，使用新 label）：

```bash
.venv/bin/python tests/run_capacity_profile.py \
  --live-runtime "$HOME/tools/oneaxe-voice/runtime" \
  --label new-gpu-passive --capacity 2 --kv-gib 1 \
  --omp-wait-policy passive --feature-device cuda:0 --check-cancel \
  pair-ab:120
```

精简结果、前后分开比较、实际设备、CPU 快照与原报告 hash 见 [JSON 证据](evidence/preprocessing-e2e-2026-10-02.json)。详细结果在忽略目录 `work/capacity/`，汇总脚本和 CPU 测试日志在 `work/preprocess-study/`；不提交音频、凭据或完整转写。

## 中止记录与收尾

`cpuwait-default-before` 在模型加载阶段因正式服务忙而被保护中止；`cpuwait-default-before2` 在启动前拒绝。用户随后明确暂停听写，`before3` 完整完成。首次 `cpuwait-passive` 在启动前遇到 `OSError`，未记录 errno，因此不能确定根因；之后补充端口 TIME_WAIT 兼容和 errno 记录，使用新标签 `passive2` 完成，没有覆盖失败记录。

四个完整实验的临时 unit 均已清理，`remaining_owned_pids=[]`。结束核查：正式 Voice 为 `ready / qwen-stream / cuda:0 / max_sessions=2`，worker PID 3268033；实验进程全部从 GPU 列表消失。VPlus PID 7301、启动时间 `2026-09-30 23:20:01 CST` 保持。本轮仅 loopback 服务端协议实测，不替代 Android 真机、跨网或桌面粘贴验收。
