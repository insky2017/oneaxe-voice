package com.oneaxe.pocket.voicelab;

final class TerminalDraftState {
    enum Phase { LISTENING, FINISHING, EDITING, SUBMITTING, SUBMITTED, DISCARDED }

    private Phase phase = Phase.LISTENING;
    private boolean targetChanged;

    Phase phase() { return phase; }
    boolean targetChanged() { return targetChanged; }
    void markTargetChanged() { targetChanged = true; }

    void stopping() {
        if (phase == Phase.LISTENING) phase = Phase.FINISHING;
    }

    void editable() {
        if (phase == Phase.LISTENING || phase == Phase.FINISHING) phase = Phase.EDITING;
    }

    boolean beginSubmit() {
        if (phase != Phase.EDITING || targetChanged) return false;
        phase = Phase.SUBMITTING;
        return true;
    }

    void retry() {
        if (phase == Phase.SUBMITTING) phase = Phase.EDITING;
    }

    void submitted() {
        if (phase == Phase.SUBMITTING) phase = Phase.SUBMITTED;
    }

    void discard() { phase = Phase.DISCARDED; }
}
