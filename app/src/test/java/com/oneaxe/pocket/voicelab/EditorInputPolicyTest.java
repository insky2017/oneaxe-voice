package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import android.text.InputType;
import org.junit.Test;

public class EditorInputPolicyTest {
    @Test public void rejectsUnknownAndPasswordEditorsBeforeAccessibilityFallback() {
        assertTrue(EditorInputPolicy.blocked(InputType.TYPE_NULL));
        assertTrue(EditorInputPolicy.blocked(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD));
        assertTrue(EditorInputPolicy.blocked(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD));
        assertTrue(EditorInputPolicy.blocked(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD));
        assertTrue(EditorInputPolicy.blocked(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD));
    }

    @Test public void permitsOrdinaryEditors() {
        assertFalse(EditorInputPolicy.blocked(InputType.TYPE_CLASS_TEXT));
        assertFalse(EditorInputPolicy.blocked(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI));
        assertFalse(EditorInputPolicy.blocked(InputType.TYPE_CLASS_NUMBER));
    }
}
