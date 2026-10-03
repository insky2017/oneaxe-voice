"""Shared mode identifiers; no GUI or GPU imports."""

MODES = {
    "vad": "稳听 · Qwen 分段",
    "qwen-stream": "随听 · Qwen 流式",
    "r2t2": "即听 · R2T2 流式",
}

# Modes adapted to the shared V1 transport and per-session worker contract.
STREAM_MODES = frozenset({"qwen-stream", "r2t2"})


def validate_mode(mode):
    if mode not in MODES:
        raise ValueError("未知听写模式")
    return mode
