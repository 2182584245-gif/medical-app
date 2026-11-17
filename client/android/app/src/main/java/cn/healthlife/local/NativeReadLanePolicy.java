package cn.healthlife.local;

import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Only explicit facade reads may bypass serialized authentication and writes. */
final class NativeReadLanePolicy {
    private NativeReadLanePolicy() { }
    private static final Set<String> MEMBER = Set.of(
        "health.profile.get", "preferences.get", "health.records.list",
        "expansion.records.list", "expansion.records.get", "expansion.records.revisions",
        "expansion.observations.list", "expansion.observations.get", "health.reminders.list",
        "expansion.goals.list", "expansion.goals.get", "health.dashboard", "health.statistics",
        "member.sync", "local149.status", "local149.draft.get", "local149.draft-attachment.get");
    private static final Set<String> GUEST = Set.of(
        "preferences.get", "chat.conversations.list", "chat.messages.list",
        "chat.message.attachments", "chat.image.get", "chat.files.list",
        "local149.draft.get", "local149.draft-attachment.get");
    static boolean readOnly(String outer, String inner) {
        if ("avatar.local.get".equals(outer)) return true;
        if (inner == null) return false;
        return "member.request".equals(outer) && MEMBER.contains(inner)
            || "guest.request".equals(outer) && GUEST.contains(inner);
    }
    static ThreadPoolExecutor executor() {
        // Never run overflow work on the WebView bridge/UI caller.
        return new ThreadPoolExecutor(2, 2, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(48), new ThreadPoolExecutor.AbortPolicy());
    }
}
