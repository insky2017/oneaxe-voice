# 多路候选实现与隔离实验记录

日期：2026-10-02。用户已授权按[容量研究](multi-stream-capacity-research-2026-10-02.md)执行探索。候选代码在 `codex/multi-stream-capacity`，功能与测量基线为 `7a7670d`；正式工程仍为 `8aee122`，保持 1 PC + 1 手机，未部署本分支。

## 已完成的候选

- 总容量由 `ONEAXE_VOICE_STREAM_MAX_NUM_SEQS` 配置，默认 2，范围 2–16。API 与 worker 核对实际容量，失配加载失败。
- 保留 PC 1 路，远端池 N−1；每个服务端认证设备最多 1 路。正在建立的会话计入名额，凭据轮换不能绕过配额。
- CUDA graph 默认捕获 1..N；按 N 路预热首步与 16 秒窗口。四路候选计划使用 2 GiB KV，保持 FP16、160 ms 步长和既有窗口。
- 新增普通音频、flush、finish 分类，以及 API 排队、RPC、worker 排队、准备、AsyncLLM 等待和结果应用耗时。指标不含录音或转写正文。
- 实验客户端按同一个真实时钟同步起跑，采集不等待识别，每 24 秒同时 flush，最终核对全部样本处理、稳定前缀、不同语料关键词及独立会话。

## CPU 与审查

完整 CPU 单元/组件测试 **232 项通过**，证据在本机忽略文件 `work/capacity/cpu-suite-final.log`。可提交实验 runner 的 **6 项 CPU 检查**另行通过；也验证了独立 systemd cgroup 的真实假进程回收。未把模拟推理结果当作 GPU 容量证据。

审查发现并修复三个影响实验可信度的问题：

1. 仅停止 API PID 不能保证清理另起进程组的 worker。改成每轮唯一的临时用户 systemd unit，`KillMode=control-group`、`TimeoutStopSec=5`；退出核对 unit 停止及原 cgroup 无进程。CPU 假进程覆盖清理期间才新建的 detached child，以及连续两次 SIGTERM。
2. 首字和收尾原先只有记录，没有参与“保持速度”的判断。现在与普通步骤和积压一起作为门槛。
3. 文字快照合并可能丢弃性能样本。现在核对 audio 事件数等于发送帧数、flush 等于发送次数、finish 等于 1；缺样时测量无效，不计算通过的速度结论。

## 实验材料与顺序

四种受控音频为既有 10 秒中文课程测试片段，以及 CPU Flite 合成的 garden、planet、music 英语语料。音频和凭据留在忽略目录，报告只存 hash、计数、耗时和关键词检查布尔值。

PC 与远端身份都连接专用 loopback `18098`；这轮研究比较服务端容量，不替代 Android 真机或跨网验收。生产 Voice 模型保持驻留且空闲，VPlus 不改动；测试实例的额外显存独立记账。

计划先用两路配置测单路 A、双路 AB、双路 CD，每组 90 秒；再加载四路配置测 ABCD，并重复双路基线排除背景波动。按匹配音频而非所有通道混合平均值比较，取最差一路：

| 指标 | 研究门槛 |
| --- | --- |
| 普通步骤 p95 / p99 | 相对双路基线增幅 ≤10% / ≤20% |
| 音频积压 p95 | 相对双路增加 ≤0.1 秒 |
| 末三分之一相对首三分之一积压 p95 | 增加 ≤0.1 秒 |
| 首次固定文字 | 基线 + max(0.3 秒, 基线的 20%) |
| flush p95 / finish | 基线 + max(0.1 秒, 基线的 20%) |

这些是实验判定目标，不是产品 SLA。正确性、绝对实时与相对速度分别报告；只有全部满足才继续 6→8 路。遇到速度拐点停止加路，对通过档位做 10 分钟确认。

## 当前 GPU 进度

GPU 速度基准尚未完成，没有新增可承诺路数。

- `baseline2`：启动前检测到生产正在录音，未启动测试 API 或加载测试模型。
- `baseline2-run1`：生产曾空闲，测试实例进入加载；随后生产恢复录音，保护检查停止实验。没有进入任何音频基准阶段。临时 unit 已回收，`remaining_owned_pids=[]`；`nvidia-smi` 只剩原生产推理实例。

上述两次都是录音保护触发，不能算作模型性能失败，也不能算作基准通过。证据为 `work/capacity/baseline2-profile.json`、`work/capacity/baseline2-run1-profile.json`。继续实验需要连续约 8–10 分钟不录音的窗口；通过档位长测另需约 10 分钟。

正式 Voice API PID `313597`、worker `313684`、EngineCore `317181`，VPlus PID `7301` 保持；这些是本轮核对快照，不是未来固定进程号。未重测 tmux、改变生产容量或修改共享模型。

## 继续实验的入口

可提交的 [run_capacity_profile.py](../tests/run_capacity_profile.py) 只读监测显式指定的生产 runtime，在专用端口 `18098` 启动临时实验 unit；实验 runtime 和工作目录必须与生产分开。恢复听写、读取状态失败或退出信号都会触发实验清理。需要事先准备独立实验凭据和 manifest，不复用生产 token。

当前隔离工程的凭据、四份音频与 manifest 已准备好，继续基线可运行：

```bash
.venv/bin/python tests/run_capacity_profile.py \
  --live-runtime "$HOME/tools/oneaxe-voice/runtime" \
  --label baseline2-run2 --capacity 2 --kv-gib 1 \
  single-a:90 pair-ab:90 pair-cd:90
```

基线通过后再用新标签、`--capacity 4 --kv-gib 2 four:90` 测四路；随后重复双路。用 [e2e_capacity.py](../tests/e2e_capacity.py) 的两个 `--compare-baseline` 参数分别传入 AB/CD 报告，再传 `--compare-candidate`。需要新实验标签保留失败记录，不能覆盖前述保护触发证据。
