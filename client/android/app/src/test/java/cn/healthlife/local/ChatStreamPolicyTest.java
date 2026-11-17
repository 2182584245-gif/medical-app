package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;

public final class ChatStreamPolicyTest {
    private boolean send(ChatStreamPolicy policy, String type, long seq, long conversation, long assistant, int chars) {
        return policy.accept(type, seq, conversation, assistant, chars, 256, false);
    }

    @Test public void acceptsIncrementalAnswerBeforeCompletion() {
        ChatStreamPolicy policy = new ChatStreamPolicy();
        assertTrue(send(policy, "start", 1, 8, 19, 0));
        assertTrue(send(policy, "thinking", 2, 8, 19, 0));
        assertTrue(send(policy, "answer_delta", 3, 8, 19, 2));
        assertTrue(send(policy, "tool_start", 4, 8, 19, 0));
        assertTrue(send(policy, "tool_done", 5, 8, 19, 0));
        assertTrue(send(policy, "web_sources", 6, 8, 19, 0));
        assertTrue(send(policy, "proposal", 7, 8, 19, 0));
        assertTrue(send(policy, "answer_delta", 8, 8, 19, 20));
        assertTrue(send(policy, "done", 9, 8, 19, 0));
        assertFalse(send(policy, "answer_delta", 10, 8, 19, 1));
    }

    @Test public void rejectsCrossConversationAndMessageEventsWithoutPoisoningNextEvent() {
        ChatStreamPolicy policy = new ChatStreamPolicy();
        assertTrue(send(policy, "start", 1, 8, 19, 0));
        assertFalse(send(policy, "answer_delta", 2, 9, 19, 3));
        assertFalse(send(policy, "answer_delta", 2, 8, 20, 3));
        assertTrue(send(policy, "answer_delta", 2, 8, 19, 3));
        assertFalse(send(policy, "start", 3, 8, 19, 0));
        assertFalse(send(policy, "answer_delta", 2, 8, 19, 3));
    }

    @Test public void refusesUnstartedMalformedPrivateOrUnboundedEvents() {
        ChatStreamPolicy policy = new ChatStreamPolicy();
        assertFalse(send(policy, "answer_delta", 1, 8, 19, 1));
        assertFalse(send(policy, "start", 1, 0, 19, 0));
        assertFalse(send(policy, "start", 1, 8, Long.MAX_VALUE, 0));
        assertFalse(policy.accept("start",1,8,19,0,256,true));
        assertTrue(send(policy, "start", 1, 8, 19, 0));
        assertFalse(policy.accept("answer_delta",2,8,19,1,ChatStreamPolicy.MAX_EVENT_CHARS+1,false));
        assertFalse(send(policy, "answer_delta", 2, 8, 19, 8193));
        assertFalse(send(policy, "answer_delta", 2, 8, 19, 0));
        assertFalse(send(policy, "unknown", 2, 8, 19, 0));
        assertFalse(send(policy, "thinking", 2, 8, 19, 1));
        assertTrue(send(policy, "answer_delta", 2, 8, 19, 1));
    }

    @Test public void totalAnswerBudgetAndErrorAreTerminal() {
        ChatStreamPolicy policy = new ChatStreamPolicy();
        assertTrue(send(policy, "start", 1, 8, 19, 0));
        int seq=2;
        for (int i=0;i<ChatStreamPolicy.MAX_ANSWER_CHARS/8192;i++)
            assertTrue(send(policy, "answer_delta", seq++,8,19,8192));
        int remaining=ChatStreamPolicy.MAX_ANSWER_CHARS%8192;
        if (remaining>0) assertTrue(send(policy, "answer_delta",seq++,8,19,remaining));
        assertFalse(send(policy, "answer_delta",seq,8,19,1));
        assertTrue(send(policy, "error",seq++,8,19,0));
        assertFalse(send(policy, "done",seq,8,19,0));
    }
}
