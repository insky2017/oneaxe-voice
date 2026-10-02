# Voice Lab 输入兼容性修复与真机验证

日期：2026-10-02。用户报告 Keep、Gemini 可用，而微信、ChatGPT PWA、X 搜索、Firefox 地址栏异常，并授权用已连接的手机修复、安装与端到端测试。范围仅为独立 Voice Lab APK 的输入适配，不修改网络 App、Voice 服务端或用户默认键盘。

## 已复现原因

- X 原生 App 与 Firefox 的空输入框有焦点且可编辑，却返回 `selection=-1`；旧版将它当成未选中输入框，尚未启动识别即拒绝。
- 微信聊天框显示光标和键盘，但辅助功能树没有暴露可编辑节点，旧 `ACTION_SET_TEXT` 路径无法定位目标。
- ChatGPT PWA 属网页编辑器；整体替换文本的兼容性不足。此次改用输入连接后，实际累计增量写入已成功。尚未把该网页旧版的所有异常归结为单一机制。
- Android 输入连接返回的 `SurroundingText.offset=-1` 表示没有绝对位置，不等于没有文本或没有输入焦点；微信已真机观察到空框 `offset=-1, selection=0:0, textChars=0`。最初候选版过严拒绝，后续修正与最终结果见下文。

## 实现方向与验收要求

Android 13+ 使用辅助功能服务的 `FLAG_INPUT_METHOD_EDITOR` 和 `InputMethod.AccessibilityInputConnection.commitText()` 增量输入；不需要设为默认输入法。输入绑定当前编辑会话、包名与窗口；提交后读回确认，移动光标、换目标、取消后停止继续提交。密码输入、无法验证的输入类型仍拒绝。保留旧 Android 的节点路径，并修复空框负光标误判。

验收必须看到目标框内实际文本；服务端返回成功或客户端调用 `commitText()` 不能单独当作填入成功。固定音频走手机实际 Tailnet HTTPS/WSS，真实麦克风另测。只填入不发送，原有草稿保留，测试追加内容在验证后移除。

## 执行记录

调试候选 `ce145ff120853d2330913c88fd08aae50afbdb580c86bfb53131957c965ab0c2`：ChatGPT PWA 固定音频 89/89，原有 6 字草稿保留；X 非空与空搜索框、Firefox 空地址栏各 89/89，后两者通过已修正的节点路径。微信仍被未知 offset 拒绝，不能把这版记为全部通过。诊断包 `2d1413ecf46c2ce5e657e186926cfdf433fcee5a5b478beb9b355851da135fd6` 明确了上述微信 offset 返回值。

原始节点、裁剪截图、脱敏日志仅存本机私有目录 `/home/sky/.local/state/oneaxe-pocket/input-compat-20261002/`，不提交私人聊天或转写内容。

## 最终修复版

已安装并从设备 APK 读回核对 SHA-256：`371b3cb75a6b6f73d44dbbed8674b95e09a029f6b28a402e3db199e1a6b7d323`。Pixel 9 Pro XL / Android 17 / API 37；默认键盘仍为微信输入法 `com.tencent.wetype`。26/26 单元测试通过，构建成功。新增测试覆盖提交确认、128 字符上下文滑动、未知绝对 offset，以及密码/不可验证输入类型策略。

- 对 `offset=-1`，使用前后各至多 128 字符的上下文及相对光标确认增量写入；编辑会话代次、窗口与包名绑定不变。另跟踪系统选区回调，在下一次提交前校验外部光标变化，避免异步回调稍晚就误判刚完成的写入。
- 每次取得当前输入连接；不能比较连接包装对象的引用身份，系统可能返回新包装对象。
- 密码及 `TYPE_NULL` 在节点回退前拒绝；历史焦点节点必须重新刷新，并核实焦点、窗口与包名。API 33 以下仍使用原节点路径。

| 应用与场景 | 最终版结果 | 私有证据文件名 |
| --- | --- | --- |
| 微信 8.0.77 空聊天输入框 | 固定音频两轮各 89/89 确认写入；第二轮累计 178 字跨过 128 字符窗口，截图可见实际文字；无消息发送，测试文字清理 | `wechat-final-fixture.json`、`wechat-final-fixture-second.json` 及同名 PNG |
| 微信真实麦克风 | 手机播放样本，真实 `AudioRecord` 采集并经 Tailnet WSS 识别，88/88 写入，结束麦克风释放；声学回录把 phone 识别为 film，不是逐字识别准确率验收 | `wechat-final-microphone.json` 及 PNG |
| ChatGPT PWA / Chrome 154.0.8037.57 | 固定音频 89/89，通过新输入连接写入；原有 6 字草稿保留，清理追加后恢复 6 字，无提交 | `chatgpt-final-fixture.json` |
| X 12.30.0-prod.01 空搜索框 | 89/89，通过新输入连接写入；恢复原有搜索文字，没有按搜索确认键 | `twitter-final-fixture.json` |
| Firefox 157.0 空地址栏 | 89/89，通过新输入连接写入；清理后为空，未打开测试文字对应的查询或地址 | `firefox-final-fixture.json` |
| Gemini 输入框 | 89/89，当前输入连接上下文返回 null，安全回退节点路径成功；初始 9 字是 hint，不是用户草稿，证据保留 `rawBeforeLength` 和 `beforeWasHint` 区分 | `gemini-final-fixture.json` |
| Keep 5.26.391.03.90 正文中间光标 | 89/89，在原文第 82 字符处插入；前后原文保持，清理后逐字等于原 99 字正文。此处 `prefixPreserved=false` 是中间插入，另有 `originalPreserved=true` 和 `restoredExactly=true` 校验 | `keep-final-insertion.json` |

所有固定音频均为同一 8.41 秒、16 kHz 单声道 PCM16 样本，经已配置的 `https/wss://rtx4090.nase-stairs.ts.net:8097`；没有使用 USB reverse、localhost 或临时 ASR 代理。微信因节点不暴露文字，以输入连接读回确认和截图共同验收，不能伪称 UIAutomator 读到了全文。

## 输入保护回归

最终 APK 在 Tailnet 静态测试表单上验证（该表单仅提供两个 HTML 输入框，不承载 ASR 或代理）：

| 场景 | 实际结果 |
| --- | --- |
| 识别中左移光标 | 已填 21 字后停止提交，最终收到 89、确认 21；其余保留草稿，日志 `editor context or cursor changed`。`final-cursor.json`。 |
| 悬浮取消 | 取消时收到/写入 21 字，取消后无继续写入；下一轮以保留的 66 字正文为前文继续测试，前文保持。`final-cancel.json`。 |
| 切换至密码输入框 | 已填 21 后编辑会话代次变化，停止旧框写入；最终收到 89、确认 21，密码框仍是原先 11 字占位内容。`final-focus-password.json`。 |

另在微信的未知绝对 offset 通道重复左移光标，仍为收到 89、确认 21 后停止提交，见 `wechat-final-cursor.json`。直接从密码框点测试音频时，提示“密码输入框不支持听写”，没有创建会话、没有修改原内容，见 `final-password-rejected.json` 和 PNG。

测试脚本首轮用全选再注入种子文字时，实际保留了非折叠选区，未满足听写前置条件；客户端拒绝写入。这些记录另存 `*-setup-selection-error.json`，不算上述边界通过。调整脚本使光标折叠后才执行真正的边界测试，没有为测试修改产品代码。

## 支持边界

这些结果覆盖上述设备、应用版本及控件，不代表所有 Android 应用。Android 8–12 保留节点路径，本轮没有这些系统的真机；不把 Android 13+ 的输入连接能力写成旧系统已支持。自定义终端画布、不给出节点也不给出可读输入连接的编辑器仍需单独适配。无法验证时保留草稿并提示，不盲目覆盖或自动发送。网络恢复、双端长流和权限完整用例沿用各自执行记录，本轮没有重跑无关的 10 分钟并发测试。

## 收尾

已移除手机 `files/fixture.wav`，测试音频菜单隐藏；临时静态表单进程已退出，端口 18897 无监听。USB reverse 为空，辅助功能服务保持启用，默认微信输入法保持原值；调试充电常亮从 3 恢复本轮原值 0。微信测试输入清空，ChatGPT 原有 6 字、Keep 原有 99 字和 X 原搜索文字恢复。PC 结束检查为 idle、R2T2 仍加载、worker PID 313684、无活跃识别会话，本轮没有重启或修改 Voice 服务。环境元数据见私有目录的 `installed-apk.json`、`final-state.json` 和 `pc-final-status.json`。
