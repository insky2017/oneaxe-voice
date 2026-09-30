# 三种听写模式、状态图标与实时字幕

## 使用

Ubuntu 顶栏的“话筒 + 文字气泡”图标显示状态。点击菜单可选择：

| 模式 | 引擎 | 输出方式 |
| --- | --- | --- |
| 稳听 · Qwen 分段 | Qwen3-ASR / Transformers | 约 700 ms 停顿时提交片段；现有稳定版本 |
| 随听 · Qwen 流式 | Qwen3-ASR / vLLM | 连续输入音频；候选字幕可修订，已冻结前文逐步输入 |
| 即听 · R2T2 流式 | Confucius4-R2T2 / vLLM | 使用官方稳定前缀，连续追加已确定文字 |

三个模式均使用 F8 开始、F8 结束并补齐尾部。流式出字无需等一句话结束；标点由模型识别，句号不会关闭麦克风。口头语不另行清理。

空闲时切换模式会开始加载；加载时图标显示圆环，第一次需等待依赖导入、权重及 CUDA 初始化。听写中切换模式在下一轮生效，不混用两套模型处理同一轮音频。选择会保存。

麦克风实心加红点表示录音；文字省略号表示收尾；剪贴板标记表示仅复制；感叹号表示异常。错误详情在菜单中显示。

实时字幕可以关闭；字幕不获取输入焦点，不接收鼠标点击。它显示模型最近的候选文字，随听尾部可能变化。自动输入只追加冻结的文字，F8 结束会提交剩余尾部。终端不发送回车。

打开顶栏菜单会暂缓粘贴；关闭后重新检查原窗口。切换到其他窗口或打开设置时，原有保护仍会让本轮转为只复制。菜单中的“复制本轮全文”可找回结果，完整文本也保存在 `runtime/last-transcript.txt`。

## 安装

原本机 API 和桌面依赖仍需安装。顶栏另使用系统 Python 的 GTK/GI：

```bash
sudo apt install python3-gi python3-cairo gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1 gir1.2-dbusmenu-glib-0.4 \
  gnome-shell-extension-appindicator
```

新流式环境独立于现有 `.venv` 与 VPlus。使用官方 Qwen 依赖组合：

```bash
./bin/install-stream-env
./bin/oneaxe-voice desktop-setup --shortcut F8
```

如本机 pip 镜像不可用，可为安装命令指定 `PIP_INDEX_URL=https://pypi.org/simple`，或按本机网络配置设置代理。推理使用本地权重，运行不依赖外网。

R2T2 默认只读加载 `~/tools/models/Confucius4-R2T2`；可用 `ONEAXE_VOICE_R2T2_MODEL_DIR` 指定其他位置。Qwen 沿用 `ONEAXE_VOICE_MODEL_DIR`。`ONEAXE_VOICE_STREAM_PYTHON` 可指向其他独立流式环境解释器。

安装后 `oneaxe-voice-tray.service` 随图形会话启动，并启动轻量桌面控制和 API；GPU 模型按需加载。图标退出时取消录音，下次 F8 可以重新唤起。移除使用原 `desktop-setup --remove`。

```bash
./bin/oneaxe-voice set-mode vad
./bin/oneaxe-voice set-mode qwen-stream
./bin/oneaxe-voice set-mode r2t2
./bin/oneaxe-voice desktop-status
```

## 官方实现与适配

[Qwen 官方仓库](https://github.com/QwenLM/Qwen3-ASR)对应的 `qwen-asr==0.0.6` 使用 的官方 `init_streaming_state`、`streaming_transcribe`、`finish_streaming_transcribe`，并配合其指定的 `vllm==0.14.0`。参数为 2 秒音频块、前 2 块允许整体修订、之后回退 5 个 token。OneAxe 另外保留 8 个尾部 token，避免 token 边界重分词影响已经输入的文字；这会增加稳定文字的输出延迟，候选字幕仍及时更新。

Qwen 官方接口会累积整段音频，且不提供音频与文字的时间对齐。本项目每 30 秒调用官方结束接口，再创建新的流式状态；已经输出的文字保留，采集不停。这是固定资源窗口，不是句子结束判定。窗口边界可能影响个别字词及标点，长篇连续听写优先选择有原生滚动窗口的即听。不使用语音停顿判断，也不通过文本相似度删除重复表达。任何已提交前缀不一致会停止本轮，保留已完成结果。

R2T2 代码固定在 [26d55a54](https://github.com/netease-youdao/Confucius4-R2T2/tree/26d55a54ce5670cff9947a167d8ed95d569fd4d9)，来源及许可证见 [vendor](../vendor/README.md)。使用官方 `streaming_transcribe_no_reset`：首次 320 ms（含前瞻），以后 160 ms，16 秒滚动窗口、每次移走 8 秒；读取整轮稳定前缀，按差量输入。没有引入其完整 WebSocket 服务器、额外 VAD 模型或重复口头语整理。

两个官方结束接口在恰好整块结束、缓冲为空时会跳过最后推理。本项目在结束时附加 80 ms 静音上下文，调用官方 final flush，补齐保留的 token。初始全零 PCM 直接忽略，这只是数字静音优化，不是 VAD；底噪、背景人声或音乐仍由模型处理。

## 生命周期与数据

OneAxe Voice 同时只持有一个活跃识别引擎。切换时先释放原模型/工作进程，再加载新模型；流式听写持有整轮互斥权，其他 API 请求不会抢占。流式工作进程及 CUDA 子进程在取消、异常或空闲卸载时回收；正常结束可继续复用热模型。

模型仍默认空闲 120 秒卸载；切换回刚卸载的模式需要再次加载。vLLM 使用固定 512 MiB KV 缓存、单请求、4096 token 上下文和单请求 CUDA graph。权重、编码器与 CUDA 运行时另占显存；512 MiB 不是总显存上限。启动前要求至少 7 GiB 空闲显存。固定缓存采用 vLLM 官方参数，可避免其他进程释放显存导致自动探测失败。它与 VPlus 仍共享物理 GPU，不代表两者并行重负载时没有性能影响。

采集与识别独立，流式音频队列最多约 64 秒 / 2.05 MB。积压达到上限会停止采集并补齐已接收内容，避免无限堆积。打开菜单太久也可能积压。

状态接口不包含转写正文。字幕仅通过当前用户可访问的私有 Unix socket 返回；录音和转写不进入常规日志。官方 final 方法中的正文打印被适配器屏蔽。故障诊断位于私有 `runtime/stream-worker.log`。

## 验证与边界

自动化、真实模型和桌面端到端证据见 [验证记录](validation.md)。真实桌面测试脚本：

```bash
PYTHONPATH=. .venv/bin/python tests/e2e_desktop.py \
  --audio /path/to/known-16khz-mono-pcm16.wav --mode all
```

测试需要保持专用输入框焦点，并暂时独占 F8。脚本结束恢复设备设置、剪贴板、结果文件和原窗口。测试使用独立 PulseAudio 音频源，不向终端发送文字，不访问 VPlus。

也可在独立测试桌面执行，不占用日常桌面的 F8 和剪贴板。需要 Xvfb、xfwm4、Tk 和前述桌面依赖：

```bash
.venv/bin/python tests/run_isolated_e2e.py \
  --audio /path/to/known-16khz-mono-pcm16.wav --mode all --safety
```

`--xvfb /path/to/Xvfb` 可指定已解包的 Xvfb，`--display :100` 可避开现有测试显示器。脚本创建私有 DBus、runtime、端口及配置，并在结束时关闭测试服务。样本需为已知的“大家好”开头中文录音；测试按重复次数核对片段顺序与最终文字，不等同于准确率评测。
