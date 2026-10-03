package com.oneaxe.pocket.voicelab;

final class InputWriteAck {
    enum State { CONFIRMED, WAITING, DIVERGED }

    final String before;
    final String after;
    final String addition;
    final int beforeCursor;
    final int afterCursor;

    InputWriteAck(String before, int cursor, String addition) {
        this.before = before;
        this.beforeCursor = cursor;
        this.addition = addition;
        this.after = before.substring(0, cursor) + addition + before.substring(cursor);
        this.afterCursor = cursor + addition.length();
    }

    State observe(String actual, int selectionStart, int selectionEnd) {
        if (after.equals(actual)) {
            return selectionStart == afterCursor && selectionEnd == afterCursor
                    ? State.CONFIRMED : State.WAITING;
        }
        if (before.equals(actual) && (selectionStart == beforeCursor && selectionEnd == beforeCursor
                || selectionStart == afterCursor && selectionEnd == afterCursor
                || before.isEmpty() && beforeCursor == 0 && selectionStart < 0 && selectionEnd < 0)) {
            return State.WAITING;
        }
        return State.DIVERGED;
    }
}
