# Voice Lab：Termux 终端输入兼容

日期：2026-10-02。用户确认其他六个 App 当前可用，但 Termux 提示“该输入框未提供可验证的输入类型”，已授权继续修复。范围为独立 Voice Lab 输入适配，不更改 Termux 源码、网络 App 或 Voice 服务端。

## 原因与实现选择

手机 Termux 为 `0.119.0-beta.3` / versionCode 1022。聚焦对象是 `com.termux:id/terminal_view`，属于终端画布，不是普通 EditText。参考上游 [v0.119.0-beta.3](https://github.com/termux/termux-app/tree/v0.119.0-beta.3)（commit `e634d8f981f48b6b89202cf0e04533f0889e03b3`）的 `TerminalView.onCreateInputConnection()`：

- 默认输入类型为 `TYPE_NULL`，触发了 Voice Lab 原有的未验证类型保护。
- `commitText()` 把内容立即送入 PTY 后清空输入连接缓冲，普通输入框的 `getSurroundingText()` 提交确认不适用。
- 直接提交还会读取 Termux 的粘滞 Ctrl/Alt/Shift/Fn 状态，普通字母可能变成控制字符，不能只解除 TYPE_NULL 限制。
- Termux 原生 Ctrl+Alt+V 调用 `doPaste()` → `TerminalEmulator.paste()`，绕过上述逐字修饰键处理。该能力要求没有禁用 Termux 的硬件键盘快捷键；Android InputConnection 默认的 `performContextMenuAction(paste)` 不是该路径。

专用路径仅针对已核验包名、终端资源 ID、焦点和输入连接的 Termux 终端。听写期间保留草稿，正常结束后一次调用原生粘贴；不附带 Enter，不在取消或断线后补发，不重试不确定的提交。提示用户“结束后一次粘贴，文字同时保留在剪贴板”。剪贴板保留本轮安全文本作为备用，避免无接收回执时定时恢复导致异步读取错误。

终端提交与普通框读回确认分开记账：`terminalSubmittedChars` 表示已请求终端粘贴，不能单凭 `sendKeyEvent()` 返回就声称 PTY 收到。实际到达由下述端到端捕获与屏幕回显验证。

## 验证办法

在手机创建临时本地 Termux 会话，运行 raw PTY 输入捕获脚本，仅记录收到的测试字节并回显，不执行输入文字；保留用户原有远程会话。对比发起前后字节、UTF-8 内容和控制码。ASR 使用原有手机 Tailnet HTTPS/WSS 与独立设备凭据，不用 USB 网络转发。

预定检查：固定文本探针、固定音频到 PTY、真实麦克风样本、正常停止与取消、打开会话抽屉/切换目标、Ctrl/Alt 粘滞状态，以及普通输入框与密码保护的针对性回归。执行结果如下。

## 真机结果

最终已安装 APK SHA-256：`1c26380dd93f83e8345f5d1becac3762b839657fac13f2858070d99ff265b39b`，已从设备读回核对。设备 Pixel 9 Pro XL / Android 17 / API 37；29/29 单测通过，APK 构建与 `git diff --check` 通过。控制字符过滤和 Termux 特例输入类型有独立单测；普通密码/其他 TYPE_NULL 保护继续保留。

私有证据目录：`/home/sky/.local/state/oneaxe-pocket/termux-input-20261002/`。公开记录仅列计数及行为，不提交原始输入、剪贴板或用户终端历史。

| 场景 | 结果 | 证据文件 |
| --- | --- | --- |
| 固定文字探针 | UTF-8 测试文字 12 字符 / 16 字节实际到达 PTY，内容精确相等，无控制码 | `termux-probe.json` |
| 固定音频端到端 | 手机经真实 Tailnet WSS 收到 89 字；第 4 秒捕获新增字节为 0，正常结束后一次收到 89 字节 / 89 字符，无回车、ESC 或其他控制码 | `termux-fixture.json` |
| 粘滞 Ctrl + Alt | 附加键均激活为红色时，探针仍收到准确 16 字节；没有变成控制指令，测试后按钮恢复未激活 | `termux-modifiers-probe.json`、`modifiers-active.png`、`modifiers-final-row.png` |
| 真实麦克风 | 手机播放样本后经真实 AudioRecord 回录、Tailnet 转写；收到 81 字符，PTY 实际收到 81 字符 / 95 UTF-8 字节；麦克风释放。包含声学与环境影响，不作为识别准确率验收 | `termux-microphone.json` |
| 悬浮按钮正常结束 | 第 4 秒结束真实麦克风听写；28 个最终字符实际到达 PTY，无控制码，采集已释放 | `termux-manual-stop.json` |
| 悬浮取消 | 已识别 29 字时取消，PTY 新增 0 字节，`terminalSubmittedChars=0`，没有补贴 | `termux-cancel.json` |
| 抽屉切换会话 | 听写中打开抽屉切到原有会话，目标解绑；最终虽收到 89 字，提交为 0，捕获会话也未收到字节 | `termux-switch-session.json` |
| 原有远程 shell | 短测试文字实际显示在原空提示符后，无执行；删除测试字符后提示符恢复。辅助功能 content-description 的前后差分不可靠，此项依赖截图人工核对，不能充作通用终端读回协议 | `remote-terminal-probe.json`、`remote-probe-current.png`、`remote-probe-cleared.png` |
| 普通框与密码回归 | 普通输入框追加 12 字并得到 `CONFIRMED`，原文恢复一致；密码框明确拒绝，不创建识别会话 | `standard-regression.json`、`password-regression.png` |

`insertedChars=0` 与 `terminalSubmittedChars=89` 是有意区分：生产客户端没有 PTY 接收回执；本次实际收到 89 字的结论来自独立捕获脚本，不是发送 API 自报成功。剪贴板留存是终端模式的明确行为；未使用“只复制”代替自动输入。

## 边界与收尾

- Termux 原生硬件快捷键必须保持启用；禁用时 Ctrl+Alt+V 不走粘贴路径。本轮核验默认配置，不修改用户配置，也不支持所有 Termux 分支的任意配置。
- 自动换行转空格，其余控制字符拒绝；只请求文字粘贴，不附带 Enter，不触发模型管理。取消、断线不补发的代码路径已独立复核；本轮取消有真机结果，断网没有重复网络长测。
- 抽屉切换、焦点/窗口/IME 代次有保护；未把未验证的硬件快捷键直接切会话、所有 TUI、隐藏密码提示或未知 Termux 分支写成全面支持。终端没有普通密码输入框的类型信息，不在密码提示符使用听写。
- Android 13+ 才有本专用输入连接路径。其他 Android 版本仍需单独验证；前版六个 App 的固定音频结果仍归属[前次兼容性记录](voice-input-compatibility-2026-10-02.md)，本版做了上述针对性回归。
- 已关闭临时第 3 个 Termux 会话，保留原 2 个会话并回到原远程提示符；捕获脚本/结果与手机 fixture 清理，测试音频菜单隐藏。常亮恢复 0、默认键盘保持原值，ADB reverse 为空。主剪贴板按新路径保留最近测试文本，没有做不可靠的定时恢复。见 `final-state.json`。
