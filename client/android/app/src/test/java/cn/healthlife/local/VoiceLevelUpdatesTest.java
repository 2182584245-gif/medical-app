package cn.healthlife.local;

import org.junit.Test;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicBoolean;
import static org.junit.Assert.*;

public final class VoiceLevelUpdatesTest {
    @Test public void blockedUiKeepsOnlyLatestLevelAndOnePendingTask() {
        List<Runnable> queue = new ArrayList<>(); List<Float> shown = new ArrayList<>();
        VoiceLevelUpdates updates = new VoiceLevelUpdates(queue::add, () -> true, shown::add, type -> fail(type));
        for (int i = 0; i <= 1000; i++) updates.offer(i / 1000f);
        assertEquals(1, queue.size());
        queue.remove(0).run();
        assertEquals(1, shown.size()); assertEquals(1f, shown.get(0), 0f);
        updates.offer(.25f); assertEquals(1, queue.size());
        queue.remove(0).run(); assertEquals(.25f, shown.get(1), 0f);
    }

    @Test public void cancelledOrReplacedSessionCannotDeliverItsQueuedLevel() {
        List<Runnable> queue = new ArrayList<>(); AtomicBoolean current = new AtomicBoolean(true);
        List<Float> shown = new ArrayList<>();
        VoiceLevelUpdates updates = new VoiceLevelUpdates(queue::add, current::get, shown::add, type -> fail(type));
        updates.offer(.5f); current.set(false); queue.remove(0).run();
        assertTrue(shown.isEmpty());
    }

    @Test public void linkageFailureDisablesOptionalDisplayInsteadOfEscapingToMainThread() {
        List<String> errors = new ArrayList<>(); List<Float> shown = new ArrayList<>();
        VoiceLevelUpdates updates = new VoiceLevelUpdates(Runnable::run, () -> true,
            value -> { shown.add(value); throw new NoSuchMethodError("private callback data must not be logged"); }, errors::add);
        updates.offer(.5f); updates.offer(.75f);
        assertEquals(1, shown.size()); assertEquals(java.util.Collections.singletonList("NoSuchMethodError"), errors);
    }

    @Test public void rejectedUiQueueDoesNotEscapeIntoRecordingWorker() {
        List<String> errors = new ArrayList<>();
        VoiceLevelUpdates updates = new VoiceLevelUpdates(task -> { throw new RejectedExecutionException(); },
            () -> true, value -> fail("No queued display is expected"), errors::add);
        updates.offer(.5f); updates.offer(.75f);
        assertEquals(java.util.Collections.singletonList("RejectedExecutionException"), errors);
    }

    @Test public void invalidLevelsAreDiscardedAndValidLevelsAreClamped() {
        List<Float> shown = new ArrayList<>();
        VoiceLevelUpdates updates = new VoiceLevelUpdates(Runnable::run, () -> true, shown::add, type -> fail(type));
        updates.offer(Float.NaN); updates.offer(Float.POSITIVE_INFINITY); updates.offer(Float.NEGATIVE_INFINITY);
        assertTrue(shown.isEmpty()); updates.offer(-1); updates.offer(2);
        assertEquals(java.util.Arrays.asList(0f, 1f), shown);
    }

    @Test public void fatalVmErrorsAreNotSwallowedByOptionalDisplayProtection() {
        VoiceLevelUpdates updates = new VoiceLevelUpdates(Runnable::run, () -> true,
            value -> { throw new OutOfMemoryError("fault injection"); }, type -> fail(type));
        try { updates.offer(.5f); fail("VM failures must not be disguised as an optional callback error"); }
        catch (OutOfMemoryError expected) { }
    }
}
