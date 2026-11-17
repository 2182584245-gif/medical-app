package cn.healthlife.local;

import java.util.concurrent.Executor;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

/** Optional metering must neither queue unbounded UI work nor interrupt recording. */
final class VoiceLevelUpdates {
    interface Sink { void accept(float value); }
    private final Executor ui;
    private final BooleanSupplier current;
    private final Sink sink;
    private final Consumer<String> report;
    private boolean queued, disabled;
    private float latest;

    VoiceLevelUpdates(Executor ui, BooleanSupplier current, Sink sink, Consumer<String> report) {
        this.ui = ui; this.current = current; this.sink = sink; this.report = report;
    }

    void offer(float value) {
        if (Float.isNaN(value) || Float.isInfinite(value)) return;
        synchronized (this) {
            if (disabled) return;
            latest = Math.max(0f, Math.min(1f, value));
            if (queued) return;
            queued = true;
        }
        try { ui.execute(this::deliver); }
        catch (RuntimeException | LinkageError error) { disable(error); }
    }

    private void deliver() {
        final float value;
        synchronized (this) {
            queued = false;
            if (disabled) return;
            value = latest;
        }
        try { if (current.getAsBoolean()) sink.accept(value); }
        catch (RuntimeException | LinkageError error) { disable(error); }
    }

    private void disable(Throwable error) {
        synchronized (this) { disabled = true; queued = false; }
        // Error type only: never log audio, transcript, member identity or callback arguments.
        try { report.accept(error.getClass().getSimpleName()); }
        catch (RuntimeException | LinkageError ignored) { }
    }
}
