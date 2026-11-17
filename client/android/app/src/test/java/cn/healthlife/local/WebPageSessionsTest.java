package cn.healthlife.local;

import org.junit.Test;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public final class WebPageSessionsTest {
    private static final class Request {
        final String id = "mobile-1";
    }

    @Test public void recoveredPageReusingTransportIdCannotReceiveOldReceipt() {
        WebPageSessions pages = new WebPageSessions();
        long old = pages.open(); Request oldRequest = new Request(); pages.bind(oldRequest, old);
        long replacement = pages.open(); Request newRequest = new Request(); pages.bind(newRequest, replacement);
        assertEquals(oldRequest.id, newRequest.id);
        assertFalse(pages.accepts(oldRequest)); assertTrue(pages.accepts(newRequest));
        assertFalse(pages.accepts(old)); assertTrue(pages.accepts(replacement));
    }

    @Test public void replyQueuedBeforeRendererExitIsRejectedAtActualDelivery() {
        WebPageSessions pages = new WebPageSessions();
        long origin = pages.open(); List<String> delivered = new ArrayList<>();
        Runnable late = () -> { if (pages.accepts(origin)) delivered.add("old reply"); };
        pages.open(); late.run(); assertTrue(delivered.isEmpty());
    }

    @Test public void acceptedBusinessWriteRunsOnceWhileOnlyItsOldReceiptIsDiscarded() {
        WebPageSessions pages = new WebPageSessions();
        long origin = pages.open(); Request request = new Request(); pages.bind(request, origin);
        List<String> writes = new ArrayList<>(), replies = new ArrayList<>();
        Runnable acceptedBusinessWork = () -> {
            writes.add("saved once");
            if (pages.accepts(request)) replies.add("result");
        };
        pages.open(); acceptedBusinessWork.run();
        assertEquals(java.util.Collections.singletonList("saved once"), writes);
        assertTrue(replies.isEmpty());
    }

    @Test public void unregisteredRequestsAndBeforeFirstPageCannotReceiveReplies() {
        WebPageSessions pages = new WebPageSessions(); Request request = new Request();
        assertFalse(pages.accepts(0)); assertFalse(pages.accepts(request));
        pages.open(); assertFalse(pages.accepts(request));
    }
}
