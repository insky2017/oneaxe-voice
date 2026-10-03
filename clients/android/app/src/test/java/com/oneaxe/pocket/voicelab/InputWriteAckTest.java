package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class InputWriteAckTest {
    @Test public void delayedTextAndCursorAreOnlyConfirmedAfterBothArrive() {
        InputWriteAck write = new InputWriteAck("hello", 5, " world");
        assertEquals(InputWriteAck.State.WAITING, write.observe("hello", 5, 5));
        assertEquals(InputWriteAck.State.WAITING, write.observe("hello", 11, 11));
        assertEquals(InputWriteAck.State.WAITING, write.observe("hello world", 5, 5));
        assertEquals(InputWriteAck.State.CONFIRMED, write.observe("hello world", 11, 11));
    }

    @Test public void externalEditOrCursorMoveDiverges() {
        InputWriteAck write = new InputWriteAck("hello", 5, " world");
        assertEquals(InputWriteAck.State.DIVERGED, write.observe("hello!", 6, 6));
        assertEquals(InputWriteAck.State.DIVERGED, write.observe("hello", 2, 2));
    }

    @Test public void hintWithoutSelectionCanWaitForFirstWrite() {
        InputWriteAck write = new InputWriteAck("", 0, "测试");
        assertEquals(InputWriteAck.State.WAITING, write.observe("", -1, -1));
        assertEquals(InputWriteAck.State.CONFIRMED, write.observe("测试", 2, 2));
    }
}
