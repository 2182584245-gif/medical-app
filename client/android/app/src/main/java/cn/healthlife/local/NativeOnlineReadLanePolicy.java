package cn.healthlife.local;

import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Explicit online reads cannot occupy the serialized authentication/write lane. */
final class NativeOnlineReadLanePolicy {
    private NativeOnlineReadLanePolicy() { }
    private static final Set<String> MEMBER = Set.of(
        "chat.conversations.list", "chat.messages.list", "chat.message.attachments",
        "chat.image.get", "chat.files.list", "chat.tools.proposals",
        "reading.catalog", "reading.get", "reading.article", "reading.asset",
        "reading.social.get", "reading.bookmarks", "reading.comments", "reading.recommendations",
        "library.catalog", "library.search", "library.card", "library.source",
        "forum.topics", "forum.posts", "forum.posts.comments", "forum.image.get", "forum.member.profile",
        "forum.discovery.search", "forum.detail.get", "forum.detail.comments",
        "service.appointments", "service.history", "service.tasks", "health.service-summary",
        "shopping.products", "shopping.cart", "shopping.favorites", "shopping.orders", "shopping.recommendations",
        "member.memory.settings.get", "member.memory.overview", "member.memory.days",
        "member.memory.preferences.list", "member.ai-profile.get", "member.ai-profile.categories.get",
        "expansion.growth.summary", "expansion.content.list", "expansion.content.get",
        "expansion.content.progress", "expansion.settings.get", "expansion.bootstrap", "mobile.weather.current");
    private static final Set<String> GUEST = Set.of("chat.tools.proposals", "ai.operations.status");

    static boolean readOnly(String outer, String inner) {
        if ("record.photo.get".equals(outer)) return inner == null || inner.isEmpty();
        if (inner == null) return false;
        return "member.request".equals(outer) && MEMBER.contains(inner)
            || "guest.request".equals(outer) && GUEST.contains(inner);
    }

    static ThreadPoolExecutor executor() {
        // Bounded concurrency and no caller-runs fallback: never block WebView.
        return new ThreadPoolExecutor(2, 2, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(48), new ThreadPoolExecutor.AbortPolicy());
    }
}
