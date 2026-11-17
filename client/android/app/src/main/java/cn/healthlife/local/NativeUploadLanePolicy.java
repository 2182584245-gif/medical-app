package cn.healthlife.local;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Only independent immutable member-file creation has a separate serial lane. */
final class NativeUploadLanePolicy {
    private NativeUploadLanePolicy() { }
    static boolean upload(String outer, String inner) {
        // Guest mutation snapshots retain their existing serialized ordering.
        return "member.request".equals(outer) && "files.upload".equals(inner);
    }
    static ThreadPoolExecutor executor() {
        return new ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(2), new ThreadPoolExecutor.AbortPolicy());
    }
}
