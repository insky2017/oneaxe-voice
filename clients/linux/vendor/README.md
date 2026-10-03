# WebRTC VAD

`webrtc-vad` 0.4.0 源自 crates.io，包含原有 Rust MIT 许可证和 libfvad/WebRTC 许可证。保留上游算法与 C 源码。

唯一功能性构建修正：删除 `build.rs` 中的 `git submodule update --init`。crates.io 包已经包含 libfvad 源码；这条命令不必要，而且会向上查找宿主仓库并操作无关子模块。现在构建直接使用随包源码，不访问 Git 或网络。
