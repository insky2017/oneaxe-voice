# 重复输入调查与待审核修复方案

日期：2026-10-03。状态：调查完成一轮，原 F8 客户端交付层和 Voice 服务源码保持不变；其修复方案待用户批准。

## 已确认的结果

用户反馈目标输入框出现重复，但“复制本轮全文”的结果正确。菜单名称及主控查得的运行状态指向原 F8 客户端，目标是 Codex Electron 输入框；当前证据未确认当时存在 F8/F9 双写，也未确认 Codex 一次粘贴被消费两次。

原交付链路为：`Desktop._deliver_stream_v1` 按 `committed_text` 截取固定全文后缀，`paste.deliver` 先用 `xclip` 设置剪贴板，再核对原目标，执行 `xdotool key --clearmodifiers ctrl+v`，命令成功后返回 `pasted`，客户端随即推进游标。这里没有目标读取或实际插入确认。

用户提供的正确全文作为临时黄金输入，共 49 字。真实旧 `Desktop` 接收与队列方法在逐词、逐字、重复快照和慢交付六组参数回放中，delta 串联均等于正确全文。这是算法与交付参数证据，不是端到端证据。

随后在独立 Xvfb、独立 D-Bus、临时浏览器配置中，用未修改的原 `paste.deliver` 和实际 GTK/Chromium 控件投递 23 个增量。保持原目标核对，禁用 GPU，没有操作真实桌面焦点。结果只保存数量、差异及哈希，临时控件正文随测试目录删除。

| 实际目标与条件 | 原命令返回 `pasted` | 目标粘贴事件 | 最终字数 | 与正确全文一致 |
| --- | ---: | ---: | ---: | --- |
| GTK Entry，正常 | 23 | 23 | 49 | 是 |
| Chromium textarea，正常 | 23 | 23 | 49 | 是 |
| Chromium textarea，阻塞 100 ms | 23 | 23 | 49 | 是 |
| Chromium textarea，阻塞 300 ms | 23 | 23 | 45 | 否 |
| Chromium textarea，阻塞 800 ms | 23 | 23 | 45 | 否 |
| GTK Entry，阻塞 4500 ms | 23 | 23 | 69 | 否 |
| Chromium textarea，阻塞 4500 ms | 23 | 23 | 69 | 否 |

Chromium 每组的 `Ctrl+V`、`paste`、`beforeinput`、`input` 数量均为 23。4500 ms 阻塞中，23 个 `paste` 事件全部读到了同一个末尾增量；300/800 ms 时收到的 payload 序列也已偏离预期。由此确认：目标仅需几百毫秒处理阻塞，就可能在处理旧粘贴键时读取已经被下一段改写的全局剪贴板。

正确全文 SHA-256：`cecadbb61af97a2b71c787a235807ebebef61be97c7049ae1319b079f78c3db5`。4500 ms 错误结果 SHA-256：`85aa4adba20b69a784c6edba7b8767ef57a1af4ab8a1edeeabe9625c3df9d2ea`。

边界：上述受控阻塞产生漏字及重复，不等同于用户本次逐词重复的全部形态，因此确认的是交付竞态，尚未还原当次唯一根因。普通 Chromium 没有出现单键双插入，不能据此排除 Codex 自身输入处理问题。普通 `contenteditable` 的原始 DOM 文本另有 8 个空白位置差异，未将其作为重复证据或成功验收。

此外，原 `Desktop.dispatch('copy')` 和新客户端修复前的复制按钮都绕过自动输入交付队列，另起线程设置剪贴板，存在“复制全文”插入到 `copy(delta)` 与粘贴键之间的并发路径。复制动作若发生在收尾结束后，不能据此解释此前的重复。新客户端已将复制纳入 FIFO，活动时明确显示“复制并暂停自动输入”；该修改解决内部命令交错，尚未解决目标延迟读取。

补充只读核查：已安装 Codex `26.901.31953` 的主进程使用标准 Electron `role: paste`；Web composer 人工插入分支及 ProseMirror 有相应消费/阻止默认行为逻辑。未找到明确的 native + Web 双插入证据。源码核查不能替代 Codex 实际输入复验。

## 待审核方案

建议修改原 F8 客户端桌面交付层，不涉及 ASR、GPU 推理、流协议、模型或服务端 API。保留原固定全文、候选分离、目标核对、取消门、完整原文及现有队列合并行为。

1. 将自动输入和“复制本轮全文”纳入同一交付执行序列。一次只允许一份未确认的不可变 delta；尚未交付的累计快照仍可合并，后缀以已确认传输的游标计算。
2. 由受控 X11 clipboard owner 持有当前 delta，确认取得 selection 所有权后，再核对原目标并发出一次粘贴键。保持这份数据，直到目标发起支持的文本格式请求且本次传输完成。`TARGETS` 等格式查询、剪贴板管理器预读取及所有权丢失均不能冒充目标读取确认。
3. 读取确认后推进游标并允许下一份 delta。超时、身份无法确认、所有权被外部程序抢走或传输失败时，关闭本轮自动输入，不自动重发不确定的粘贴键，完整固定原文仍保留。已有键事件可能迟到，因此超时后不能立刻把全文覆盖到剪贴板；未决 owner 应保持当前不可变数据，停止后续自动投递。

实现需要识别 selection request 的 `requestor`，处理浏览器隐藏请求窗口与目标进程的关系，并区分文本传输和元数据查询。小文本可确认数据已放到 requestor 的 property 并观察读取完成；大文本须正确处理 INCR 传输。无法建立请求者身份或读取完成证据时，保守停止输入并保留原文。

优先评估一个独立、可测试的 X11 owner/helper，避免旧 Python 客户端和新 Rust 客户端各自实现一套协议。它属于中等工程复杂度：包含 selection 所有权、请求者识别、传输完成、超时/取消及进程回收，不能按“加一个 sleep”估算。具体目标归属和 property/INCR 完成判定仍需原型验证后确定。

| 路径 | 能确认什么 | 本轮判断 |
| --- | --- | --- |
| 固定 sleep / 节流 | 只给目标更多处理时间 | 可缓解；阻塞超过等待时间仍会覆盖，不能作为完成条件 |
| `xclip -quiet -loops 1` | 至多确认某个内容请求被服务 | 缺请求者过滤；所有权丢失也可退出成功，不能直接作为目标 ACK |
| GTK `set_with_data` | 在取数据回调中提供当前不可变内容 | 可复用 selection 实现，但回调不直接提供 requestor；仍需 X11 事件观测和身份/完成判定，旧 Python 的 PyGObject 接口也不能直接调用此方法 |
| 受控 X11 owner | 可明确记录请求者、格式、当前 delta 与传输状态 | 推荐原型方向；只在验证后的目标范围启用读取确认 |

读取确认不等于最终 DOM 插入确认。目标可以忽略文本、自行转换空白、因自身处理而重复插入或在读取后改变输入状态。本方案修复剪贴板覆盖竞态，不能承诺任意应用严格 exactly-once。保留合法重复口述，不通过文本去重掩盖问题。

## 审核后的验收

- 原 F8 完整链路在真实目标上确认：固定全文、全部交付 payload 串联、目标最终文本一致；增加目标延迟和复制按钮并发的历史故障样例。
- 在 GTK、Chromium textarea 和 Codex 实际输入框分别验证，记录键事件、读取确认和输入结果。没有 Codex 的证据时，不把普通浏览器通过作为 Codex 已修复。
- 验证超时停止、取消、目标变化、外部剪贴板写入及合法重复文本；不自动重试不确定键事件，不修改服务端或原录音状态。

## 复验入口

隔离诊断脚本和发行版 Xvfb 解包均位于 Git 忽略的 `runtime/duplicate-audit-x11/`。脚本总是创建新的独立 DISPLAY；`agent-browser` 仅连接脚本创建的临时 Chromium，临时进程与正文在退出时清理。脚本仍使用用户提供的临时黄金文字，未移入正式测试目录。

```bash
dbus-run-session -- "$HOME/work/touzi/OneAxe/oneaxe-voice/server"/.venv/bin/python "$HOME/work/touzi/OneAxe/oneaxe-voice/clients/linux"/runtime/duplicate-audit-x11/replay_x11.py
dbus-run-session -- "$HOME/work/touzi/OneAxe/oneaxe-voice/server"/.venv/bin/python "$HOME/work/touzi/OneAxe/oneaxe-voice/clients/linux"/runtime/duplicate-audit-x11/replay_x11.py --short-blocks
```

报告：`runtime/duplicate-audit-x11/result.json`、`runtime/duplicate-audit-x11/short-result.json`。这些脚本是诊断资产，尚不是经过发布审核的通用测试入口。

参考：上游 [xclip 0.13 主循环](https://github.com/astrand/xclip/blob/0.13/xclip.c) 与 [selection 传输实现](https://github.com/astrand/xclip/blob/0.13/xclib.c)。其循环忽略 `TARGETS`，但没有目标 requestor 过滤；`SelectionClear` 可正常退出。GTK 3 本机接口说明显示 `set_with_data` 为 `introspectable=0`，`ClipboardGetFunc` 提供格式和数据回调，不直接提供请求者。
