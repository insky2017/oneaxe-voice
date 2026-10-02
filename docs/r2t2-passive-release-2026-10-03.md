# R2T2 被动等待：正式启用与回归

日期：2026-10-03。代码提交：`cdf00bd`。已部署到正式 `master`，重启一次 `oneaxe-voice.service`。容量仍为 **1 路 PC + 1 路手机**。

## 要点

- R2T2 启动时启用 `OMP_WAIT_POLICY=PASSIVE`，让 CPU 线程在等待时休息。仍使用 4 个 CPU 线程；CPU 做音频特征处理，GPU 做模型推理。
- 204 项 CPU 单元 / 组件测试通过。正式接口完成两路各 120 秒识别及独立取消检查。
- 两路识别期间，服务 CPU 平均占用约 1.11 个逻辑核。全部音频已处理，后段没有积压增长。
- 已恢复原先选择的 Qwen 流式模式。Qwen 的等待环境、桌面设置和 VPlus 保持原状。下次选择“即听 · R2T2”即可使用优化。

## 修改范围

[Worker 启动代码](../oneaxe_voice/engines.py)在 `Popen` 前仅对 R2T2 子进程设置等待策略。没有设置 systemd 全局环境，没有引入研究分支的扩容或 GPU 特征实验代码。

[启动环境测试](../tests/test_worker_environment.py)通过真实 `Worker` 构造和 socket 就绪握手，检查 R2T2 默认启用及覆盖父环境的 `ACTIVE`，Qwen 保留原值，父进程环境不变。模型进程启动由测试替身代替，这部分不算真实 GPU 验证。

[并发回归脚本](../tests/e2e_concurrent.py)收紧取消判断：必须收到明确的 `final(reason="cancelled", complete=false)`。无最终消息的断线不再算取消成功。

## 验证结果

CPU 回归命令如下，204 项全部通过：

```bash
OMP_NUM_THREADS=1 CUDA_VISIBLE_DEVICES='' \
  .venv/bin/python -m unittest discover -s tests -q
```

正式测试使用受控中英文 WAV，PC 走本机接口，手机身份走 Tailnet DNS 的 HTTPS/WSS，保持证书验证。每路音频按实际采集速度送入。测试期间不向用户输入框发送文字。

| 检查 | 实测结果 |
| --- | --- |
| 实际设备与启动环境 | R2T2 就绪于 `cuda:0`；worker 环境为 `OMP_WAIT_POLICY=PASSIVE`、`OMP_NUM_THREADS=4` |
| 双路持续识别 | 各 120 秒，各 1,920,000 个样本全部发送、接收和处理，正常最终结束 |
| 音频积压 p95：PC / 手机 | 0.2659 / 0.2319 秒 |
| 后段相对前段积压 p95 变化 | -0.0108 / -0.0087 秒 |
| 最后收尾：PC / 手机 | 0.2192 / 0.1776 秒 |
| 服务 CPU 平均占用 | 1.111 个逻辑核，包含 API、worker、EngineCore 等服务子进程 |
| 手机取消 | 第 8 秒明确取消；PC 继续完成 24 秒、384,000 个样本；worker 和模型代次保持 |
| 两路上限 | 两路占用时第三路被拒绝：`CAPACITY_EXCEEDED`，WebSocket 1013 |
| 访问限制 | 未认证能力查询 401；合法移动凭据 200；移动凭据管理模型及 Tailnet 管理路径均为 403 |
| 收尾 | 测试凭据已吊销并删除临时明文文件，无活动会话；Qwen 已恢复 `ready / cuda:0` |

测试同时检查固定文字前缀、独有关键词与串流隔离。CPU 数据来自正式 Voice 服务 cgroup 的起止差，排除模型加载、客户端和监控工具；不等于音频特征处理自身的消耗。一个逻辑核表示每秒消耗一个 CPU 秒，不等于整机 CPU 占用 100%。

EngineCore 的进程名改写会覆盖 `/proc/environ` 的原始环境区，本次无法从该文件直接读取其等待策略。证据明确记录 `policy_directly_observed=false`；实际直读确认的是 worker。子引擎继承关系来自现有 spawn 启动方式，不将它写成直接测量结果。

这是部署后的 2 分钟回归，与此前隔离环境的两路 10 分钟研究分开记录。本轮不计算新的前后加速比例，也不推算更多并发。HTTPS/WSS 在本机通过真实 Tailnet 入口访问，不代替 Android 麦克风、跨不同网络或桌面粘贴验收。没有重测 tmux，也没有测试 Qwen 转写或 VPlus 同时满负载。

## 失败记录与服务恢复

第一次回归在创建临时凭据文件时遇到 `Path.open()` 不支持 `opener` 的 `TypeError`，尚未开始双路识别。脚本已吊销测试凭据并恢复 Qwen。修正为 `os.open(..., O_EXCL, 0600)` 与 `os.fdopen` 后重新执行，最终回归全部通过。首次失败记录保留，没有覆盖。

最终桌面与 API 都空闲。`desktop.json`、模型空闲策略和移动监听配置的 hash 保持。桌面与托盘服务未重启；VPlus PID 7301、启动时间 `2026-09-30 23:20:01 CST` 保持，健康接口为 200。R2T2 测试进程在恢复 Qwen 后已退出。

完整精简证据见 [JSON](evidence/r2t2-passive-release-2026-10-03.json)。原始数值报告与测试脚本保存在忽略目录 `work/passive-release/`；不提交音频、凭据或完整转写。

## 回滚

回滚点为 `cdf00bd` 的父提交 `8aee122`。若此优化出现回归，先确认本机和手机都空闲，再执行 `git revert cdf00bd`，重启 `oneaxe-voice.service`，然后通过顶栏重新加载所选模式。该操作撤销本次启动策略及对应测试变更，不修改模型权重或 VPlus。本轮未执行回滚。
