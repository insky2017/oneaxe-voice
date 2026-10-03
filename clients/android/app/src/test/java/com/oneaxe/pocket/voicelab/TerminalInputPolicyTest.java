package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.text.InputType;
import org.junit.Test;

public class TerminalInputPolicyTest {
    @Test public void onlyDocumentedTermuxEditorTypesAreEligible() {
        assertTrue(TerminalInputPolicy.inputType(InputType.TYPE_NULL));
        assertTrue(TerminalInputPolicy.inputType(InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS));
        assertFalse(TerminalInputPolicy.inputType(InputType.TYPE_CLASS_TEXT));
        assertFalse(TerminalInputPolicy.inputType(InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD));
        assertFalse(TerminalInputPolicy.inputType(InputType.TYPE_TEXT_VARIATION_PASSWORD));
    }

    @Test public void newlineNeverBecomesTerminalEnter() {
        assertEquals("first second third", TerminalInputPolicy.pasteText("first\r\nsecond\nthird"));
        assertEquals("中文 测试", TerminalInputPolicy.pasteText("中文\u2028测试"));
    }

    @Test public void rejectTerminalControlBytes() {
        assertNull(TerminalInputPolicy.pasteText("text\tmore"));
        assertNull(TerminalInputPolicy.pasteText("text\u001bmore"));
        assertNull(TerminalInputPolicy.pasteText("text\u0085more"));
        assertNull(TerminalInputPolicy.pasteText("text\u007fmore"));
    }
}
