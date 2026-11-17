package cn.healthlife.local;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** One bounded lane for durable local reminder toggles, separate from cloud RPC. */
final class NativeReminderLanePolicy {
    private NativeReminderLanePolicy() { }
    static boolean localToggle(String outer, String inner) {
        return "member.request".equals(outer) && "health.reminders.toggle".equals(inner);
    }
    static ThreadPoolExecutor executor() {
        // Serialize toggles; never execute overflow work on the WebView caller.
        return new ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(16), new ThreadPoolExecutor.AbortPolicy());
    }
}

