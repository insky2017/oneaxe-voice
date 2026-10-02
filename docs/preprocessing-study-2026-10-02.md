# CPU / GPU 预处理对照

后续：[完整两路对照](preprocessing-e2e-2026-10-02.md)已完成真实 ASR、CPU 核秒、实际 GPU 设备与取消检查；本文保留微基准原始结果和数值阈值失败记录。

日期：2026-10-02；研究源码基线 `b0a4c37`。本轮只做源码核查和隔离的预处理器微基准，**未修改、部署正式程序，生产容量仍为两路**。未加载第二份 ASR 模型，未修改 VPlus 或共享权重。数值证据见 [JSON 汇总](evidence/preprocessing-2026-10-02.json)。

## 原方案已经如何分工

原 R2T2 两路方案是 CPU 预处理 + GPU 模型推理。CPU 负责 PCM / 音频数组、STFT、Mel 与 log 特征、tokenizer、会话调度；GPU 负责音频 encoder 和语言 decoder。这里使用的 `WhisperFeatureExtractor` 是 Qwen / R2T2 的音频预处理组件，不代表另跑 Whisper ASR 模型。

应用的 `prepare_ms` 只统计数组和状态准备；vLLM 内的特征计算也计入 `generate_ms`。此前容量实验没有分离这些阶段，不能用较小的 `prepare_ms` 判定 CPU 负担很轻，也不能用较低的 GPU 活动率认定 CPU 是全部瓶颈。

PyTorch 当前设为 4 个 intra-op 线程，不等于进程只有 4 个线程，更不等于持续占满 4 核。把预处理放进工作线程可以减少事件循环阻塞，但本身不会减少预处理计算量。

## 测量方法与边界

- 硬件：i9-13900KF，32 个逻辑 CPU；RTX 4090。环境：Python 3.11.14、torch 2.9.1、vLLM 0.14.0、transformers 4.57.6、qwen-asr 0.0.6。
- 只加载本地 Confucius4-R2T2 的官方 `Qwen3ASRProcessor`，使用受控测试音频和固定模拟文字前缀。16 kHz / 128 Mel / FFT 400 / hop 160 / dither 0；`padding=True`、`truncation=False`、attention mask，与当前音频入口对齐。没有补到固定 30 秒。
- CPU 时间用 `process_time()` 统计被测进程所有线程；CPU 秒 / 墙钟秒为平均逻辑核占用。**1 核表示每秒消耗 1 CPU 秒**，不是整机百分比，也不等同 P 核与 E 核的算力。
- GPU 计时包含上传、官方实现的 CPU 回传和显式同步。没有包含后续 vLLM 特征整理、再次上传、音频 encoder、语言模型推理、RPC 或真实会话排队。
- 读取桌面与 API 状态，录音、会话或模型忙则拒绝/中止。最后一组首次尝试被保护中止；后来确认服务空闲，四档重新完整执行。安全观察放在独立父进程，不计入被测子进程 CPU 时间。
- 四档按顺序各测一次，固定 12 秒窗口、batch=1、每 80 ms 一次，共 100 次 / 8 秒；模拟两路各 160 ms 步长合计 12.5 次/秒。预热不计入测量。**这是短时预处理对照，不是完整双路 ASR 的 CPU 占用或容量验收。**
- 正式服务在最后测量时为 `qwen-stream`、`ready`、无会话；本实验明确加载 R2T2 processor 配置。不能将当时生产状态当作 R2T2 双路实测。

## 按两路节奏调用的结果

| 方案 | 平均 CPU 逻辑核 | 单次预处理 p50 | p95 | 特征输出检查 |
| --- | ---: | ---: | ---: | --- |
| 当前 CPU 4 线程 / 默认等待 | 1.4966 | 10.77 ms | 16.05 ms | 对照基准 |
| CPU 4 线程 / `OMP_WAIT_POLICY=PASSIVE` | 0.2697 | 12.06 ms | 13.83 ms | 本样本特征 hash 与基准相同 |
| CPU 1 线程 | 0.1813 | 11.14 ms | 23.50 ms | 本样本特征 hash 与基准相同 |
| GPU 提取特征 / CPU 4 线程配置 | 0.0826 | 4.84 ms | 6.82 ms | 存在浮点差异，见下文 |

当前 CPU 配置在此样本上有值得处理的额外消耗，不能只按单次数学计算外推稳态占用。被动等待减少约 82% 的本阶段 CPU 时间，但 p50 增加约 12%；GPU 特征方案减少约 94% 的本阶段 CPU 时间，p50 降低约 55%。这些是一次短对照的差值，不是正式服务收益承诺。

本机 PyTorch 使用 GNU `libgomp`。官方文档说明未配置时会先主动等待；`OMP_WAIT_POLICY=PASSIVE` 且未设置 `GOMP_SPINCOUNT` 时默认自旋次数为 0。本组结果支持线程等待是额外 CPU 消耗的重要来源；尚未用 profiler 分解出精确自旋占比，不能将全部差值归因于某个函数。

CPU1 虽省 CPU，但本次 p95 较差，不能只看平均占用就直接设为 1。PASSIVE 影响等待策略，后续还需验证共享 worker 与引擎的唤醒延迟。

## 连续调用、数值一致性与 GPU 路径

另一组连续调用实验覆盖 0.32 / 1.6 / 4 / 8 / 16 秒窗口，每档每模式 3 轮 × 20 次、轮换顺序。以下为完整 HF processor，尚未包含完整 vLLM 处理链：

| 窗口 | 方案 | p50 / p95 | CPU 毫秒 / 次 |
| --- | --- | ---: | ---: |
| 8 秒 | CPU4 | 5.64 / 6.04 ms | 22.58 |
| 8 秒 | GPU4 | 3.35 / 3.85 ms | 3.43 |
| 16 秒 | CPU4 | 8.07 / 8.26 ms | 32.15 |
| 16 秒 | GPU4 | 4.14 / 4.90 ms | 4.33 |

连续调用时 8 / 16 秒窗的 GPU 预处理 p50 分别降低约 41% / 49%。本组显式设置 `MKL_NUM_THREADS=4`；上述间隔调用组与正式环境一致，不设置该变量。两组调用节奏、环境不同，不混合计算节省比例。原脚本另有同进程观察器的间隔调用试验，其 CPU 扣除归因不够干净，已由独立进程四档数据替代，不作为最终占用结论。

CPU1 相对 CPU4 的特征最大差为 0～`1.19e-7`；GPU 相对 CPU4 为 `1.35e-5`～`4.53e-5`，超过预设绝对误差阈值 `1e-5`。原微基准因此 `passed=false`、退出码 1，**数值验收未通过**；其性能数据仍有效。其他输出（mask、token IDs 等）一致。不能临时放宽阈值后宣称通过，也不能据此断言真实识别已出错；实际转写回归尚未做。

GPU 参数须传入 `audio_kwargs={"device": "cuda:0", ...}`；仅传顶层 `device` 在当前 processor 中可能被忽略。间隔实验用轻量 wrapper 核对实际 feature device。官方 GPU 特征实现会 `.detach().cpu().numpy()`，所以仍有 CPU 回传，尚未实现跨进程零拷贝。连续组 tensor peak allocated 17.44 MiB / reserved 26 MiB，仅是张量计数，不包含 CUDA context 和整个进程的显存。

## 优化顺序

1. **先减少 CPU 空转。** 将被动等待作为隔离候选，保留模型、精度、160 ms 步长和窗口规则；测完整两路的 CPU 核秒、GPU 活动、每路积压和尾延迟。短实验不能替代该验收。
2. **把特征计算交给 GPU。** 优先复用官方 feature extractor；先验证真实转写、固定前缀、收尾和取消，再检查它与主推理争用 GPU 后是否仍有净收益。GPU 方案与 PASSIVE 的组合尚未实测。
3. **有界调度作为配套。** 在线程安全和取消边界明确后，将仍需 CPU 的工作移出共享事件循环，保持每路最多一步在途；这主要解决等待，不以增加 CPU 线程数作为省资源手段。减少重复特征处理、批量特征和减少回传可后续研究，目前未实现。

优化目标是减少 CPU 核秒、稳定每路出字延迟；不把 GPU 利用率越高当作越好。不能由本次预处理加速推断三路已能保持速度，也未进行新的扩容或完整 ASR 长测。

## 依据与复核

- 应用：[worker 线程配置](../oneaxe_voice/stream_worker.py)、[进程环境](../oneaxe_voice/engines.py)、[R2T2 异步计时](../oneaxe_voice/r2t2_async.py)。
- [Transformers 4.57.6 WhisperFeatureExtractor](https://github.com/huggingface/transformers/blob/v4.57.6/src/transformers/models/whisper/feature_extraction_whisper.py)：STFT、Mel、默认 CPU 与 GPU 回传。
- [vLLM 0.14.0 InputProcessor](https://github.com/vllm-project/vllm/blob/v0.14.0/vllm/v1/engine/input_processor.py)：引擎提交前的处理入口。
- GNU 官方：[OMP_WAIT_POLICY](https://gcc.gnu.org/onlinedocs/libgomp/OMP_005fWAIT_005fPOLICY.html)、[GOMP_SPINCOUNT](https://gcc.gnu.org/onlinedocs/libgomp/GOMP_005fSPINCOUNT.html)。

本机详细脚本和结果保存在忽略目录 `work/preprocess-study/`：`benchmark_preprocess.py`、`processor-cpu-gpu.json`、`run_paced.py`、`paced_child.py`、`paced-*.json`。安全汇总只保留参数、时间、计数、hash 和数值差异，无音频、完整转写或凭据。
