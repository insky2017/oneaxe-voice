# Voice V1 真机执行记录

日期：2026-10-02。范围是独立 Voice Lab APK 经用户已建立的 Tailnet 使用 OneAxe Voice 正式移动接口；网络 App 与语音 App 长期独立安装、更新和调试。用户已授权完成客户端及真机验收。本页随验证进展更新；下述局部成功不等于 V01–V07 全部通过。

后续输入兼容修复与当前安装包见 [兼容性真机记录](voice-input-compatibility-2026-10-02.md)。下文 `2acc…e0fb3` 及 21/21 为本页对应阶段的历史版本，不表示当前手机仍安装该包；600/620 秒长流等证据仍归属各自 APK。

## 依据与环境

- [正式协议](/home/sky/tools/oneaxe-voice/docs/mobile-api-v1.md)与[Voice 交接](/home/sky/tools/oneaxe-voice/docs/pocket-handoff-2026-10-02.md)：Tailnet DNS `rtx4090.nase-stairs.ts.net`、HTTPS/WSS `8097` 已部署；服务端记录 R2T2 ready、TLS/认证/真实 WSS、PC + 移动双流至少 10 分钟。后者是**服务端验证**，不能充作 Android 麦克风、实际输入框或 PC + 手机双端 10 分钟验收。
- 手机客户端使用独立设备 Bearer，存储受 Android Keystore 保护且排除备份；HTTP 诊断与启动前查询均调用带凭据的 `GET /api/mobile/v1/capabilities`，不访问远端 `/health`。正式识别只走 DNS 身份校验的 HTTPS/WSS；当前无裸 IP 产品入口。手机不控制模型，也不自动恢复中断的录音。
- 本地确定性单元测试现为 **21/21 通过**，新增 `InputWriteAckTest` 3 项；18/18 是[客户端预实现历史](voice-v1-client-preparation-2026-10-02.md)。设备为 **Pixel 9 Pro XL / Android 17 / API 37**。当前已安装修复版 `com.oneaxe.pocket.voicelab` SHA-256 为 `2acc61a56c5e2a4773378e0b348c27adccd057381de65a09a7e7268efa1e0fb3`，见[当前安装记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/installed-apk.json)；前述 600 秒长流和故障实验使用修复前版 `b6b3b0a306ed715b18ec95ce2e114da501793bf1672911b28868fdb964049e0e`，见[旧版安装记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/installed-apk-before-input-ack.json)。**10 分钟测试未在新 APK 重跑。** 测试经已建立的 Tailnet HTTPS/WSS；跨不同网络的移动数据/热点条件仍不足。

## 已有局部结果

| 项目 | 结果及证据边界 |
| --- | --- |
| 手机能力查询与凭据 | 正式入口 `capabilities` 使用设备专用凭据成功；Keystore 加密保存、升级后持久化有手机检查。见[能力页面截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/capabilities.png)。此结果证明连接/认证/存储子场景，不证明模型全生命周期。 |
| App 内固定音频 | 普通 fixture 按实际 8.41 秒音频时长经正式 WSS 转写并追加 89 字符，见[结构化结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/normal-result.json)与[页面截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/fixture-normal-result.png)。文件注入不证明真实麦克风采集。 |
| 真实录音检查 | RecordCheck 用手机麦克风采集播放的测试音频 9 秒，16 kHz/单声道，RMS 527.09、峰值 4836；可回放，离开后缓存自动清理。见[采集统计](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/record-check-stats.json)与[回放截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/record-check-playback.png)。单独采集不等于上传转写。 |
| 麦克风到 Chrome | 真实 `AudioRecord` → 正式 WSS → Chrome 普通输入框自动追加 88 字符；服务报告已处理 143038 个真实音频样本，结束时麦克风自动释放。见[结构化结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/chrome-microphone.json)与[结果截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/chrome-microphone-result.png)。这是 Chrome 子场景，不代表所有 App、所有控件或两段连续听写通过。 |
| Termux 输入栏与密码框 | 固定音频文字进入 **Termux 内置 extra-key 文本输入栏**，未自动 Enter，见[结构化结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/termux-input.json)与[截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/termux-input.png)；**终端画布**未据此验收。密码框拒绝自动填入，见[截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/password-rejected.png)。 |
| PC + 手机短时 | PC 真实录音与手机同跑已有短时成功记录；这不等于两支真实麦克风连续 10 分钟。 |
| PC + 手机长时固定音频 | **持续子场景通过，完整 V05 未通过。** 手机真实 Android App 经 Tailnet DNS/WSS 按采集时钟发送固定 PCM 600 秒，PC 本机脚本经 `127.0.0.1` 发送另一段固定音频 620 秒；PC 比手机晚 20 秒正常结束。两端在同一 R2T2 模型代次下各自持续出字、完整收尾。手机与 PC 长流均非真实麦克风同时采集，PC 脚本不代表桌面输入。数值和边界见下一节。 |
| Voice 服务端 | 交接文档记载正式入口 TLS/认证/WSS 和 10 分钟 PC + 移动双流通过。此项属服务端证据，不能代替本页的手机验收。 |

## 600 秒手机与 620 秒 PC 长流

| 观察项 | 结果 |
| --- | --- |
| 手机链路 | Pixel 9 Pro XL 的 Voice Lab 经正式 Tailnet DNS/WSS 发送固定 PCM，持续 600.523 秒；最终 `complete=true`，`sent=processed=9,600,000` 样本，固定文字 6417 字，成功追加 6417 字。Chrome 原有 `Long:` 前缀保留，总长 6422，无中文，说明未串入 PC 测试词。见[手机流指标](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/phone-long-metrics.json)和[输入核对](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/long-run-input.json)。 |
| 手机流速 | 首个固定文字 1.492 秒，最大文字更新间隔 2.502 秒；未处理在途音频的 p95 为 0.34 秒，前段 0.32、后段 0.29 秒，最大 0.59 秒。指标为该次固定音频与网络/设备条件下的观测，不是所有语音场景的性能承诺。 |
| PC 通道 | PC 本机脚本经 `127.0.0.1` 跑 620 秒，`sent=received=processed=9,920,000` 样本，`final complete=true`，固定 1363 字、899 次更新，预设中文关键词命中。真实时钟处理延迟 p95 0.315 秒，前段 0.304、后段 0.293 秒；未发送缓冲最大 0.16 秒，流控等待 0 次。见[PC 结构化结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/pc-companion.json)。 |
| 会话与资源 | 同时活跃记录含 PC/mobile 两个不同 session，共用同一 server instance、model generation、`r2t2` 和 worker PID `313684`；运行中身份保持。GPU 显存采样从约第 100 秒开始，观察窗口峰值 9300 MiB，**不是全程绝对峰值**。见[模型状态](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/long-run-model-status.json)与手机流指标。 |

本节是修复前 APK `b6b3…049e0e` 的证据，证明真实 Android 持续传输、双路固定音频处理与目标文字持续追加；**不能冒充修复版 `2acc…e0fb3` 已重跑 10 分钟**。手机音源为固定 PCM，PC 端是本机脚本，不证明两端真实麦克风连续 10 分钟、PC 桌面 F8 输入、两端换序及取消/断网隔离；这些仍是 V05 的待验部分。

## 停止、目标变化和故障试验

| 子场景 | 已观察结果与边界 |
| --- | --- |
| 悬浮手动停止 | 修复前 APK 的手机真实麦克风从悬浮控件正常结束，收到 47 字、输入框填入 47 字，麦克风释放；见[旧版结构化结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/manual-stop-result.json)。同名截图已被最终版回归覆盖，不能作为这次旧版 47 字证据。这覆盖一次手动结束，不覆盖长时间真人体验。 |
| 取消 | 手机取消时保留已写入的 27 字，取消后无迟到继续输入；见[取消结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/cancel-result.json)。 |
| 焦点转密码框 | 收到固定 89 字，目标变化前已填 21 字；切到密码框后未把剩余文字误填，密码框保持密码属性；见[焦点结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/focus-result.json)与[截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/focus-result.png)。 |
| 光标变化 | 修复前 APK 收到固定 89 字、先前填入 21 字，移动光标后拒绝继续提交并记录 `insert rejected:content or cursor changed`；见[旧版光标结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/cursor-result.json)。同名截图已被最终版回归覆盖；旧版剩余内容未自动补贴。 |
| PC 隔离与网络恢复 | 手机先开始、PC 后开始；PC 本机脚本 60 秒完整结束，`sent=processed=960000` 样本，固定 131 字、87 次更新、模型保持。手机取消后再开始，Wi-Fi 关闭约 15 秒；积压保护使手机停止并保留已插入的 27 字，Wi-Fi 恢复后不自动开始。见[手机隔离结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/isolation-result.json)与[PC 结果](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/pc-isolation.json)。这覆盖该次隔离/恢复子场景，未覆盖全部故障模式或完整 V05。 |
| 凭据错误 | 错误设备 token 被明确拒绝，恢复真实设备 token 后能力查询通过；见[凭据检查](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/credential-check.json)。原始 token 不写入文档。 |

**写入确认缺陷及修复证据：** 修复前 APK 的网络恢复[首次重试](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/network-retry.json)已收到固定全文 89 字，输入框只新增 82 字，尾部 7 字未填，`passed=false`。旧版[重复测试](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/retry-repeat.json)偶然成功不能抵消失败。最终版将 Accessibility `ACTION_SET_TEXT` 视为待确认：约每 40 ms 读回文本与光标后才计入 `inserted`，单次最多等 600 ms，成功 `final` 总 drain 最多 2 秒；取消/断线立即关闸。[Chrome 三轮](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-regression.json)各 `received=confirmed=89` 且前文保留，最终版原始断网/恢复组合及三项边界回归也通过，见下表。**该已知写入确认缺陷按这些回归视为已解决**；不承诺无限场景覆盖，完整 V01/V03 仍有其他未覆盖项。

| 最终版 `2acc…e0fb3` 回归 | 结果 |
| --- | --- |
| 连续三轮固定音频 | Chrome 原内容和前轮文字保留；每轮 89 字已收到、89 字确认填入，分别为 `4→93→182→271` 字符，见[回归 JSON](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-regression.json)。 |
| 取消 | [最终版取消记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-cancel.json) `passed=true`，27 字已收到/确认，取消后保留已填内容且无迟到继续输入。 |
| 光标左移 | [最终版光标记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-cursor.json) `passed=true`；收到 89 字、确认 21 字后，用户左移使预期光标 28 与实际选区 `27:27` 不同，自动提交停止，剩余文字保留草稿；见[当前截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/cursor-result.png)。 |
| 真实麦克风手动结束 | [最终版麦克风记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-microphone.json) `passed=true`；悬浮点击结束后收到 44 字、确认填入 44 字，麦克风已释放；见[当前截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/manual-stop-result.png)。 |
| 原始 Wi-Fi 断开/恢复组合 | [最终版断网记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-disconnect.json)显示 Wi-Fi 关闭约 15 秒后手机按积压保护停止，已收到/确认 27 字保留；恢复 Wi-Fi 后 `no_automatic_restart=true`。随后用户主动开始新会话，[最终版重试记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-network.json) `passed=true`，`received=confirmed=89`，目标文本只增加 89 字，旧 `Disconnect:` 内容及已填 27 字保留；见[截图](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/ack-network-retry.png)。这是同一设备上的断网/恢复，不是手机与 PC 位于不同局域网的 V06 全项。 |

## V01–V07 细分状态

| 用例 | 当前状态 | 仍需确认 |
| --- | --- | --- |
| V01 选框/悬浮/填入/结束 | **局部通过，完整用例未通过**：旧版 fixture/Termux 输入栏有结果；最终版 Chrome 三轮 89/89 且前文保留，真实麦克风手动结束 44/44 并释放，断网恢复后用户主动重试 89/89 | 两段连续口述、Termux 终端画布等控件分项确认 |
| V02 焦点与权限 | **局部通过，完整用例未通过**：旧版密码框/焦点保护有结果；最终版左移光标后收到 89、只确认 21，拒绝误填并保留草稿 | 最终版权限撤销、焦点转密码框及更多目标切换 |
| V03 断线/取消/流控 | **原始断网/恢复子场景通过，完整用例未通过**：旧版 89/82 失败保留；最终版断网后保留 27 字、不自动开始，用户主动重试 89/89 且旧内容保留；取消无迟到 | busy、finish、重复/迟到/前缀冲突、更多流控异常仍需分别验证 |
| V04 模型权限 | **局部执行中，完整用例未通过**：能力查询使用设备凭据，错 token 被拒且真 token 可恢复；PC 隔离子场景模型保持 | 管理/旧接口拒绝、PC 切换与代次竞态、模型未就绪/不支持/容量满 |
| V05 PC + 手机持续并发 | **长时固定音频及 60 秒隔离子场景通过，完整用例未通过**：Android WSS 手机 600 秒与 PC 本机脚本 620 秒各自持续出字、完整收尾；手机取消/断 Wi-Fi 未带走 PC 60 秒脚本会话 | 两端真实麦克风至少 10 分钟、PC 桌面输入、换序和更多停止/故障隔离仍待验 |
| V06 跨网 Tailnet 直连 | **局部执行中，完整用例未通过**：正式入口手机链路可用；最终版同一设备断 Wi-Fi 后不自动开始、主动重试成功 | 手机移动数据/热点与 PC 不同网络及更多错误分型；当前缺 SIM/热点条件 |
| V07 设置和诊断 | **局部执行中，完整用例未通过**：正式能力查询成功，错设备 token 明确拒绝、恢复真 token 后通过 | 当前 APK 配置持久化、无效/断开端点、TLS 错误与凭据更换；确认没有远端 `/health` |

后续按 [E2E 用例](e2e-test-plan.md)补真实双麦克风/桌面输入、权限撤销与剩余异常。跨不同网络需 SIM/热点条件，缺环境时标阻塞。真人试录应在固定音频和自动化采集稳定后安排；不要用用户反复试说来代替排查。所有令牌、私人录音和完整私人转写不得写入本页。

结束清理见[最终环境记录](/home/sky/.local/state/oneaxe-pocket/voice-v1-device-20261002/final-state.json)：能力查询 ready，Wi-Fi 已恢复，`stay_awake` 还原为 0，测试 fixture 已移除，ADB reverse 为空；PC desktop idle、模型仍已加载且 worker PID `313684`，无活跃会话，`thsauto` 保持原有 inactive 状态。临时日志监听与测试表单服务已停止。这是本轮环境收尾，不代表未覆盖用例通过。
