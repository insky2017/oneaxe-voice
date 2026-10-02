package com.oneaxe.pocket.voicelab;

/** A bounded view around the cursor, never the editor's whole document. */
final class EditorWriteAck {
    enum State { CONFIRMED, WAITING, DIVERGED }

    static final int CONTEXT_CHARS = 128;
    final String before;
    final String after;
    final int cursor;
    final String addition;
    final boolean absoluteCursor;

    EditorWriteAck(String before, String after, int cursor, String addition) {
        this(before, after, cursor, addition, true);
    }

    EditorWriteAck(String before, String after, int cursor, String addition, boolean absoluteCursor) {
        this.before = before;
        this.after = after;
        this.cursor = cursor;
        this.addition = addition;
        this.absoluteCursor = absoluteCursor;
    }

    State observe(String actualBefore, String actualAfter, int actualCursor) {
        int writtenCursor = absoluteCursor ? cursor + addition.length() : tail(before + addition).length();
        if (actualCursor == writtenCursor
                && actualBefore.equals(tail(before + addition)) && actualAfter.equals(after)) {
            return State.CONFIRMED;
        }
        if (actualCursor == cursor && actualBefore.equals(before) && actualAfter.equals(after)) {
            return State.WAITING;
        }
        return State.DIVERGED;
    }

    static String tail(String text) {
        return text.length() <= CONTEXT_CHARS ? text : text.substring(text.length() - CONTEXT_CHARS);
    }
}
