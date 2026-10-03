package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class EditorWriteAckTest {
    @Test public void confirmsOnlyOneAppendAtTheOriginalCursor() {
        EditorWriteAck ack = new EditorWriteAck("abc", "def", 3, "语音");
        assertEquals(EditorWriteAck.State.WAITING, ack.observe("abc", "def", 3));
        assertEquals(EditorWriteAck.State.CONFIRMED, ack.observe("abc语音", "def", 5));
        assertEquals(EditorWriteAck.State.DIVERGED, ack.observe("abc语音语音", "def", 7));
        assertEquals(EditorWriteAck.State.DIVERGED, ack.observe("abc语音", "def", 4));
    }

    @Test public void confirmsAppendWhenContextWindowMoves() {
        String before = "x".repeat(EditorWriteAck.CONTEXT_CHARS);
        EditorWriteAck ack = new EditorWriteAck(before, "end", 1000, "hello");
        assertEquals(EditorWriteAck.State.CONFIRMED,
                ack.observe(EditorWriteAck.tail(before + "hello"), "end", 1005));
    }

    @Test public void unknownAbsoluteOffsetUsesRelativeCursorAndBoundedContext() {
        EditorWriteAck empty = new EditorWriteAck("", "", 0, "hello", false);
        assertEquals(EditorWriteAck.State.WAITING, empty.observe("", "", 0));
        assertEquals(EditorWriteAck.State.CONFIRMED, empty.observe("hello", "", 5));

        String before = "x".repeat(EditorWriteAck.CONTEXT_CHARS);
        EditorWriteAck ack = new EditorWriteAck(before, "end", 128, "hello", false);
        assertEquals(EditorWriteAck.State.WAITING, ack.observe(before, "end", 128));
        assertEquals(EditorWriteAck.State.CONFIRMED,
                ack.observe(EditorWriteAck.tail(before + "hello"), "end", 128));
        assertEquals(EditorWriteAck.State.DIVERGED,
                ack.observe(EditorWriteAck.tail(before + "hello"), "other", 128));
    }
}
