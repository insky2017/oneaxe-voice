package com.oneaxe.pocket.voicelab;

import android.text.InputType;

final class TerminalInputPolicy {
    static final String PACKAGE = "com.termux";
    static final String VIEW_ID = "com.termux:id/terminal_view";
    static final String DRAWER_ID = "com.termux:id/left_drawer";

    private TerminalInputPolicy() { }

    static boolean inputType(int type) {
        return type == InputType.TYPE_NULL
                || type == (InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
    }

    static String pasteText(String text) {
        StringBuilder result = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char value = text.charAt(i);
            if (value == '\r' || value == '\n' || value == '\u2028' || value == '\u2029') {
                if (result.length() == 0 || result.charAt(result.length() - 1) != ' ') result.append(' ');
            } else if (Character.isISOControl(value)) {
                return null;
            } else {
                result.append(value);
            }
        }
        return result.toString();
    }
}
