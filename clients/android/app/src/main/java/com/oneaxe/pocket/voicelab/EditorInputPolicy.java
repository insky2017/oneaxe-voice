package com.oneaxe.pocket.voicelab;

import android.text.InputType;

final class EditorInputPolicy {
    private EditorInputPolicy() { }

    static boolean blocked(int type) {
        int kind = type & InputType.TYPE_MASK_CLASS;
        if (kind == InputType.TYPE_NULL) return true;
        int variation = type & InputType.TYPE_MASK_VARIATION;
        return kind == InputType.TYPE_CLASS_TEXT && (variation == InputType.TYPE_TEXT_VARIATION_PASSWORD
                || variation == InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
                || variation == InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD)
                || kind == InputType.TYPE_CLASS_NUMBER && variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD;
    }
}
