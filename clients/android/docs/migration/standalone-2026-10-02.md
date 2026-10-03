# 手机语音独立工程迁移（2026-10-02）

**后续目录变更（2026-10-04）：** Android 源码现位于 `~/work/touzi/OneAxe/oneaxe-voice/clients/android`，由统一 Voice 根 Git 管理，见[整合复验记录](unified-2026-10-04.md)。本页保留 2026-10-02 的授权范围、旧路径、Git 映射与 APK 证据，不作为当前目录或暂停约定。

用户授权将手机语音从 Pocket 提取为独立工程，与网络、通知分别维护。当前继续使用独立 Termux，暂停 Linux 方案比较和 App 内集成，也不执行此前建议的后续开发或真机验收批次。

## 目录与 Git

- 新源码根：`/home/sky/work/touzi/OneAxe/oneaxe-voice-android`，拥有真实独立的 `.git` 目录，不依赖父仓库，也不是子模块。
- 来源：Pocket 的 `3f60ab32ec5751d49faedc6a72932e547fec55d5`。在独立副本中提取历史，没有重写 Pocket 原仓库历史。
- 提取 `experiments/voice-lab/` 到根目录、`docs/mobile-workspace/voice*.md` 到 `docs/`，同时保留上游 `LICENSE`、`android/gradlew` 与 `android/gradle/wrapper/`；Gradle wrapper 现在属于本工程。
- 提取基线共 60 个文件，逐个 Git blob 核对与来源内容一致；保留 17 条相关历史提交，映射见 [git-commit-map.tsv](git-commit-map.tsv)。过滤会改变提交 ID；原最后语音提交 `7dd6e9b` 对应 `d02e567`，不能把来源仓库 HEAD 与过滤后 HEAD 视为同一提交。
- 基线标签为 `import/pocket-3f60ab3`，工作分支为 `codex/standalone-voice-android`；未配置远端、未推送。本次后续整理仅修改文档、链接与忽略规则，没有修改业务源码。
- Pocket 旧语音源码位置和语音文档保留迁移入口，后续语音开发以本工程为准。父仓库仅用本地 `.git/info/exclude` 排除独立工程目录。

## 新目录构建验证

在本工程根执行：

```bash
ANDROID_HOME=/home/sky/tools/android ./gradlew --offline --no-daemon :app:testDebugUnitTest :app:assembleDebug
```

结果：构建成功，32/32 单元测试通过。产物为 `app/build/outputs/apk/debug/app-debug.apk`，SHA-256：

```text
6a5390746921a454e0f5bf96ff1b609e5ff46dbd6c83b327068a9f5c806b38e3
```

包名仍为 `com.oneaxe.pocket.voicelab`，版本为 `1 / 0.1`。新旧 APK 的签名证书 SHA-256 相同：

```text
52e4a3b93137d62a012bb244f18d37db9741a422e6a6f6d87dc63cca7b9dc56f
```

对照迁移前已验 APK，新旧 APK 内 20 个条目的 CRC 与长度一致，`classes.dex` 和 Manifest 内容一致；整体 APK 哈希因封装差异不同。迁移前 APK 另存于被 Git 忽略的 `artifacts/baseline/voice-lab-before-move.apk`，SHA-256 为 `24c649c0fe73d8f0ad3637f8631f0d1dba0635cee2c191a4418ece59e6ae8405`。

**本次新 APK 未安装，未重新执行真机 E2E。** 既有真机结果仍属于各记录中的历史 APK，见 [Termux 悬浮编辑验收](../voice-termux-overlay-2026-10-02.md)。源码一致与构建通过不能替代新 APK 真机验收。

## 相关工程与运行服务

- 网络工程：[oneaxe-pocket](/home/sky/work/touzi/OneAxe/oneaxe-pocket)，保留原 Git 历史与 origin，新目录构建成功，67/67 单元测试通过。
- 通知工程：[oneaxe-notify](/home/sky/work/touzi/OneAxe/oneaxe-notify)，此前已经独立，本次未改动。
- PC Voice 仍在 `/home/sky/tools/oneaxe-voice`；接入说明仍以 [PC 能力入口](/home/sky/docs/oneaxe-voice/README.md) 为准。本轮没有操作手机、重启 PC Voice/Notify 服务或修改配对配置、凭据及运行数据库。
