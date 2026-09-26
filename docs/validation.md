# 验证记录

日期：2026-09-26，时区 Asia/Shanghai。以下运行数据在 `~/tools/oneaxe-voice` checkout 上收集，按实施阶段记录；初始分支为 `codex/initial-api`，第三阶段在 `codex/vad-streaming` 开发，交付主分支为 `master`。这些是该机器当时的测量，不代表其他安装环境已经通过验证。本记录区分自动化模拟测试、真实 GPU 验证、用户试用和 VPlus 隔离检查。

## 自动化验证

最终代码运行以下检查，全部通过：

```bash
.venv/bin/python -m unittest discover -s tests -v
bash -n bin/serve bin/oneaxe-voice
.venv/bin/python -m compileall -q oneaxe_voice tests
systemd-analyze --user verify ~/.config/systemd/user/oneaxe-voice.service
```

17 项测试覆盖 WAV 校验、截断/过短/过长/声道/大小限制、真实 ffmpeg 格式转换、HTTP 鉴权、本机访问限制、流式上传限额、忙碌与 GPU 错误码、CUDA 不可用时不加载 CPU 模型、推理与卸载互斥、cuBLAS 清理时机、错误后 busy 恢复、本机客户端地址限制及令牌权限。

HTTP 测试使用模拟推理引擎，不作为真实模型成功的证据。最后一次测试输出为 `Ran 17 tests ... OK`。

systemd 文件现在是模板，需先从目标 checkout 运行 `./bin/install-service`，再使用上面的命令验证已渲染的用户单元。该命令属于未来安装后的操作；此记录中的原验证结果来自旧机器当时的静态单元。

## 真实中文音频

从用户既有课程音频截取前 10 秒，没有启动麦克风：

- 源文件：`~/netdisk/卢麒元/20260926/20260926amr/01.amr`，8 kHz 单声道。
- 样本：`work/smoke-zh-10s.wav`，16 kHz 单声道 PCM16，10.0 秒。
- 模型：本地 `Qwen3-ASR-1.7B`，FP16；实际设备 `cuda:0`，`NVIDIA GeForce RTX 4090`。
- 多次返回相同文字：`大家好，今天是二零二六年九月二十六日，丙午年。`

文字与已有转写开头一致；截取在原句中间结束。这是一个真实样本的功能验证，不是普遍准确率、长音频完整性或实时麦克风测试。

| 测量 | 模型加载 ms | 推理 ms | 服务端总计 ms | PyTorch 分配峰值 MiB |
| --- | ---: | ---: | ---: | ---: |
| 首轮冷启动 | 15196.56 | 3345.51 | 18659.46 | 3972.4 |
| 首轮热推理 | 0.19 | 818.78 | 931.70 | 3982.1 |
| 显存释放修复后冷启动 | 13793.26 | 3052.14 | 16950.88 | 3972.4 |
| 修复后热推理及并发验证 | 0.14 | 692.49 | 799.34 | 3982.1 |
| 自然空闲卸载后的同进程重新加载 | 2781.06 | 716.42 | 3602.78 | 3972.4 |

原始响应保存在本机忽略目录：`work/cold-result.json`、`work/warm-result.json`、`work/final-smoke.json`。最后一轮服务 PID 为 `252446`；PID 只是本次记录，重启后会改变。计时是服务端测量，包含归一化，不包含 HTTP 上传/响应传输。

## 并发与空闲卸载

首轮曾尝试用两个先后启动的客户端检查并发，但前一个请求已在不足 1 秒内完成，两次均返回 `200`，该结果不作为并发通过证据。

随后用同一个异步客户端同时提交两个 POST，得到 `[200, 429]`。修复后再次通过 `asyncio.gather` 并发提交，仍得到 `[200, 429]`，成功结果实际在 CUDA 识别；拒绝结果为“已有一段录音正在识别，请稍后重试”。

初版空闲卸载后，模型对象已经回收，但进程仍占约 3800 MiB。进一步复现显示 PyTorch 活跃分配仅约 18.2 MiB，而分配器保留量达 3322 MiB；cuBLAS 工作区使部分大块显存不能归还。修复为在无人推理时同步 CUDA、清理本进程的 cuBLAS 工作区，再释放分配器缓存。独立诊断中，清理后 PyTorch 活跃及保留量均为 0 MiB。该修复使用 PyTorch 2.10 的内部接口，依赖升级后必须复测。

服务默认空闲 120 秒、每 5 秒检查一次。22:15:11 核对自然空闲结果：`state=unloaded`、`model_loaded=false`，PyTorch `allocated_mib=0.0`、`reserved_mib=0.0`，nvidia-smi 显示该进程从 5024 MiB 降到 478 MiB；VPlus 仍为 3762 MiB。结果记录在 `work/final-idle.json`。CUDA 上下文的剩余显存不属于模型权重，停止 OneAxe Voice 服务才能完全释放。

卸载后向同一 PID `252446` 再次提交样本，模型成功重新加载，返回相同中文文字、`cuda:0`，总计 3602.78 ms；响应保存于 `work/reload-result.json`。同进程重载保留了依赖导入和 CUDA 初始化等状态，因此该数据与进程首次冷启动不同。

## VPlus 隔离核对

本轮只启动/停止/重启新 `oneaxe-voice.service`。VPlus 的用户服务始终保持：

```text
MainPID=1240024
ActiveEnterTimestamp=Sat 2026-09-19 23:13:55 CST
```

VPlus 仓库原本已有未提交的分块上传等改动。保留原状态，前后核对以下 SHA-256 一致：

| 内容 | SHA-256 |
| --- | --- |
| `git diff` 输出 | `6e831263476ed5a2817831fb03b46c86e84a946cb8fd1c2d4b12fd944bcb5281` |
| `app/main.py` | `959e14750de4eeb15f3c385b359c7534a7426447bcb0f7753518aa57c849b1fd` |
| `app/services/asr_service.py` | `6a7b63d487a217dc8dcea7b1daa71085773bbc244cd7f0ea7a5522e682cf7755` |

新环境由 conda clone 建到本项目 `.venv`，torch 与 qwen_asr 的导入位置均在该目录。未向 VPlus 环境安装依赖，也未修改共享模型权重。

新服务推理前后，VPlus 的 `http://127.0.0.1:8092/health` 成功响应；GPU 进程 PID `1240024` 显存观测均为 3762 MiB。任务状态仍为 52 done、4 error，无 queued/running；本轮未提交 VPlus 任务。

这些证据支持“未改 VPlus 代码和环境、未重启 VPlus、既有服务仍存活”。**没有执行 VPlus 与听写同时满负载的回归，不能据此保证两者互不影响性能。** 两个进程仍共享 RTX 4090。

## 第一阶段当时的交付边界

- 已实现并运行：独立本机 HTTP API、CLI、访问令牌、短 WAV 转写、真实 GPU 检查、单次推理及空闲释放。
- 服务处于用户级运行状态，安装状态为 `linked`，没有 enable 登录自启。
- 未实现：DJI Mic 采集、全局快捷键、录音状态提示、剪贴板及向 VSCode/Terminal/Chrome 自动输入。
- 后续先接入麦克风，验证真实说话录音到文本，再实现快捷键及受控粘贴。

## 第二阶段：路径、DJI Mic、F8 与粘贴

日期同为 2026-09-26。基础 API 与可移植路径已提交为 `90e1a2a`，桌面代码作为后续独立提交。

### 路径与安装

- gpt-6-luna 完成默认模型目录、环境变量路径展开、systemd 模板及安装器；仓库不再硬编码特定用户名的 home 路径。
- 复核修复了旧服务软链接的安装问题：生成的用户单元通过临时文件及 `os.replace` 原子替换，避免跟随软链接覆盖仓库模板。
- 5 项路径测试通过，包含带空格/百分号/美元符号的渲染和旧软链接安装回归。渲染后的两个用户服务通过 `systemd-analyze --user verify`。
- API 和桌面服务安装为用户级单元，均未 enable；GNOME F8 绑定已写入并读回验证，其他自定义绑定保留。

### 硬件与完整流程

| 验证层 | 结果 | 限制 |
| --- | --- | --- |
| DJI 硬件采集 | 识别 Wireless Mic Rx，3 秒采集得到 2.9 秒有效 PCM16 单声道、16 kHz 数据 | 本次电平为底噪，RMS -69.6 dBFS，最响帧 -61.7 dBFS；未以该片段评价识别准确率 |
| 全流程 | 真正发送 F8，经独立测试音频源采集已知中文样本，第二次 F8 结束；服务返回 `cuda:0`，文字进入真实 X11 文本框 | 测试源模拟麦克风流，用于可重复验证；与 DJI 硬件检查分开记录 |
| 窗口保护 | 切换窗口后返回 `focus_changed`，原文本框内容未改变 | 同一应用内的标签页或输入框变化未必改变 X11 焦点 |
| GNOME Terminal + tmux | 在专用 tmux 服务及原始输入探针中收到中文粘贴 | 实收内容无 CR/LF，未触发终端命令执行 |
| VS Code / Chrome | 实现 Shift+Insert / Ctrl+V 对应策略，并通过按键选择测试 | 本轮未逐个运行这两个应用的粘贴验收 |

用户随后明确反馈 tmux 已亲自验证、目前使用良好，并要求停止重复验证；本轮据此结束 tmux 检查。

完整流程的录音时长为 11.75 秒，模型请求 ID 为 `9016fec5-0502-488f-9a73-959acf644a45`，桌面结果 `last_action=pasted`。探针确认包含预期日期文字。测试音频源模块和 FIFO 随后卸载/删除，`runtime/desktop.json` 恢复默认 DJI 自动选择，测试剪贴板也已恢复。

本机证据保存在忽略的 `work/dji-capture-check.json`、`work/desktop-e2e.json` 和 `work/terminal-input.json`。临时测试窗口在验证后关闭。

桌面新增 11 项测试覆盖设备选择、缺失/静音拒绝、PCM 信号电平、控制字符和换行清理、窗口切换不发按键、终端粘贴按键、完整状态转换、录音取消、识别中取消、低电平跳过以及忙碌时不重复录音；与基础及路径测试一起共 33 项。

### VPlus

本轮最终核对仍为 PID `1240024`，启动时间 `2026-09-19 23:13:55 CST`。`git diff`、`app/main.py`、`app/services/asr_service.py` 的 SHA-256 与第一阶段记录一致。只安装和启停 OneAxe Voice 自己的服务，未修改 VPlus、其 Conda 环境或共享权重。共享 GPU 负载的限制仍与第一阶段一致。

## 第三阶段：VAD 持续分段

日期：2026-09-26。用户授权按 F8 开始、停顿分段、边录边识别并按顺序输出；用户随后明确口头语整理推迟，当前原文保留。上一版基线为 `9cc57b7`，开发在 `codex/vad-streaming`；沿用已有 GitHub 默认分支 `master` 交付。

### 自动化及依赖

- 最终 46 项测试通过，另有 compileall、Git 空白检查、文档链接及特定用户名绝对路径扫描通过。
- 新覆盖：700 ms 停顿、240 ms 开口前缓冲、任意 PCM 包边界、F8 尾段、时长切分后的短尾音、短敲击/数字静音过滤；预热时继续采集、首段在停止前输出、顺序追加、口头语原样保留、英文词边界、焦点变化后整轮只复制、队列满时末段保留、取消时等待已发出的粘贴、API 失败后回收采集进程。
- 新增受同一令牌保护的预热 API，覆盖鉴权、CUDA 结果、busy 429 和 GPU 错误 503。
- `webrtcvad-wheels==2.0.14` 仅安装到本项目 `.venv`。配置的镜像下载返回 403 后，改用官方 PyPI 成功安装 83 KB wheel；未更改全局 pip 配置或 VPlus 环境。

### 真实模型与桌面行为

使用独立临时 PulseAudio pipe source，以实时速率送入“10 秒已有中文样本 + 3 秒停顿 + 同一样本”。真实触发 F8，真实 WebRTC VAD 分段，调用运行中的 GPU API，并向专用 X11 文本框粘贴。

首轮尝试在开始采集前遇到桌面命令 `CalledProcessError`，没有算作通过；随后补充只记录命令名和退出码的诊断信息，重新运行完整流程成功，未再复现。该失败的具体外部命令未被最初日志记录，不能把原因视为已确认。

成功证据在本机忽略文件 `work/vad-e2e.json`：

| 检查 | 实际结果 |
| --- | --- |
| 第一段出现时 | F8 开始后 17.817 秒，文本框已有内容，`capture_active=true`，第二段仍在录音 |
| 第一次切分 | `reason=pause`，送入 ASR 的语音为 7.64 秒 |
| 第二次切分 | `reason=stop`，F8 停止时补齐 7.44 秒尾段 |
| 结束耗时 | 第二次 F8 到测试完成 1.655 秒，含状态和文本框采样检查 |
| GPU | `device=cuda:0`；使用现有 Qwen3-ASR-1.7B |
| 输出顺序 | `segments_done=2`、`segments_pasted=2`；文本框完整内容与按次序累积的结果严格相同 |
| 结束状态 | idle，capture_active=false，队列长度 0，无错误 |

本次是模型进程冷启动后的验证，首段等到了模型加载完成，不能把 17.817 秒当成热模型的停顿响应速度。两次请求 ID 分别为 `34283d63-d7cd-406e-98ad-87bf24bafe61`、`5b16723d-f231-4961-95d3-24d7095447f8`。

测试后临时音频模块和 FIFO 已清理，用户剪贴板与之前的结果文件已恢复；真实设备配置恢复为 DJI 自动选择。新 VAD 参数已写入本机配置，过时的 max_seconds/silence_dbfs 字段移除。本轮遵循用户指示，没有重复 tmux 验证。

### 用户 DJI 实际验收

用户随后使用 DJI Mic 现场口述，确认停顿识别及分段输出效果符合预期，并要求完成文档后提交 GitHub。读取 `desktop-status` 得到以下结果，没有读取或收录该轮转写正文：

| 检查 | 实际结果 |
| --- | --- |
| 输入设备 | `Wireless Mic Rx Digital Stereo (IEC958)` |
| 录音时长 | 34.5 秒 |
| 识别与输入 | `segments_done=8`，`segments_pasted=8` |
| GPU | `device=cuda:0` |
| 结束状态 | `state=idle`，`last_action=pasted`，`last_error=null` |
| 资源收尾 | `capture_active=false`，`recognizing=false`，`warming=false`，队列长度 0 |

该结果补充了真实 DJI 输入的用户验收，不是各类噪声环境下的准确率测评。停顿会提交上一段，整轮采集仍由再次按 F8 或时长上限结束。

### 隔离与限制

VPlus 最终仍为 PID `1240024`、原启动时间，三项代码/差异哈希均与此前记录一致，两项服务健康检查均为 200。仅重启了 OneAxe Voice 自己的 API 和桌面服务。

可复现音频流验证和一次真实 DJI 试用，不能代替各种麦克风距离、背景噪声或长时间口述的准确率测试。背景人声和音乐仍可能被 VAD 接受；15 秒强制切分可能落在词句中间；首次加载和长时间停顿后的再次加载仍会增加延迟。口头语整理未实现。
