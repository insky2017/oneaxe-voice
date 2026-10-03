package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.*;
import org.junit.Test;

public final class DictationDraftTest {
    @Test public void cancellationKeepsReceivedTextBeforeUiCanInsertIt() {
        DictationDraft draft = new DictationDraft();
        draft.receive("已收到", "候选");
        draft.close();
        assertEquals("已收到", draft.fixed());
        assertEquals(0, draft.insertedLength());
        assertEquals("", draft.addition());
        assertFalse(draft.receive("已收到迟到文字", ""));
        assertEquals("已收到", draft.fixed());
    }

    @Test public void slowUiCoalescesCumulativeSnapshotsWithoutDuplicateInput() {
        DictationDraft draft = new DictationDraft();
        draft.receive("先", "说");
        draft.receive("先说一句。", "下一");
        String addition = draft.addition();
        assertEquals("先说一句。", addition);
        draft.receive("先说一句。再说一句。", "");
        draft.inserted(addition);
        assertEquals("再说一句。", draft.addition());
        draft.inserted(draft.addition());
        draft.receive("先说一句。再说一句。", "");
        assertEquals("", draft.addition());
    }

    @Test public void targetChangeKeepsDraftButNeverReplaysMissingInput() {
        DictationDraft draft = new DictationDraft();
        draft.receive("前段", "");
        draft.inserted(draft.addition());
        draft.detachTarget();
        draft.receive("前段后段", "候选");
        assertEquals("前段后段", draft.fixed());
        assertEquals(2, draft.insertedLength());
        assertEquals("", draft.addition());
    }

    @Test public void prefixConflictPreservesOriginalAndStopsInput() {
        DictationDraft draft = new DictationDraft();
        draft.receive("原始文字", "");
        try {
            draft.receive("修订文字", "");
            fail("accepted changed prefix");
        } catch (IllegalArgumentException expected) { }
        assertEquals("原始文字", draft.fixed());
        assertEquals("", draft.addition());
    }

    @Test public void preservesWhitespaceAndUnicodeExactly() {
        DictationDraft draft = new DictationDraft();
        draft.receive("a  b\n中文🙂", "");
        assertEquals("a  b\n中文🙂", draft.addition());
        draft.inserted(draft.addition());
        draft.receive("a  b\n中文🙂 tail", "");
        assertEquals(" tail", draft.addition());
    }
}
