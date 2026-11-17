package cn.healthlife.local;

import java.util.Collections;
import java.util.Map;
import java.util.WeakHashMap;
import java.util.concurrent.atomic.AtomicLong;

/** Requests belong to a page instance, even when that page reuses a transport ID. */
final class WebPageSessions {
    private final AtomicLong epoch = new AtomicLong();
    // JSONObject request objects use identity equality; IDs may restart after page recovery.
    private final Map<Object, Long> origins = Collections.synchronizedMap(new WeakHashMap<>());
    long open() { return epoch.incrementAndGet(); }
    long current() { return epoch.get(); }
    boolean accepts(long origin) { return origin > 0 && origin == epoch.get(); }
    void bind(Object request, long origin) { origins.put(request, origin); }
    long origin(Object request) {
        Long origin = origins.get(request); return origin == null ? -1L : origin;
    }
    boolean accepts(Object request) { return accepts(origin(request)); }
}
