package com.oneaxe.pocket.voicelab;

/** Received text survives UI delays; only successful target writes advance the input offset. */
final class DictationDraft {
    private String fixed = "";
    private String pending = "";
    private int inserted;
    private boolean accepting = true;
    private boolean targetAttached = true;

    synchronized boolean receive(String text, String candidate) {
        if (!accepting) return false;
        if (!text.startsWith(fixed)) {
            accepting = false;
            targetAttached = false;
            pending = "";
            throw new IllegalArgumentException("固定文字不连续，本轮已停止自动输入");
        }
        fixed = text;
        pending = candidate;
        return true;
    }

    synchronized String addition() {
        return accepting && targetAttached ? fixed.substring(inserted) : "";
    }

    synchronized void inserted(String addition) {
        if (!fixed.substring(inserted).startsWith(addition)) {
            throw new IllegalArgumentException("文字提交位置不一致");
        }
        inserted += addition.length();
    }

    synchronized void detachTarget() { targetAttached = false; }
    synchronized void close() { accepting = false; targetAttached = false; pending = ""; }
    synchronized String fixed() { return fixed; }
    synchronized String pending() { return pending; }
    synchronized int insertedLength() { return inserted; }
}
