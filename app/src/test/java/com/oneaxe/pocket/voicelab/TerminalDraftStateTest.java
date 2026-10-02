package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.*;
import org.junit.Test;

public class TerminalDraftStateTest {
    @Test public void finalMustArriveBeforeConfirmationAndOnlyOneSubmitIsAllowed() {
        TerminalDraftState state = new TerminalDraftState();
        assertFalse(state.beginSubmit());
        state.stopping();
        assertFalse(state.beginSubmit());
        state.editable();
        assertTrue(state.beginSubmit());
        assertFalse(state.beginSubmit());
        state.submitted();
        assertFalse(state.beginSubmit());
    }

    @Test public void changedTerminalCannotBeReenabledByLateFinalOrRetry() {
        TerminalDraftState state = new TerminalDraftState();
        state.markTargetChanged();
        state.editable();
        assertFalse(state.beginSubmit());
        state.retry();
        assertFalse(state.beginSubmit());
    }

    @Test public void failedConnectionCanRetryButDiscardIsFinal() {
        TerminalDraftState state = new TerminalDraftState();
        state.editable();
        assertTrue(state.beginSubmit());
        state.retry();
        assertTrue(state.beginSubmit());
        state.discard();
        state.retry();
        assertFalse(state.beginSubmit());
    }
}
