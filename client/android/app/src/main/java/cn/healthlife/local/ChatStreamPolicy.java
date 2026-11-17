package cn.healthlife.local;

/** Request-local bounds and identity continuity for public AI stream events. */
final class ChatStreamPolicy {
    static final int MAX_EVENT_CHARS = 262144;
    static final int MAX_DELTA_CHARS = 8192;
    static final int MAX_ANSWER_CHARS = 2000000;
    private static final long MAX_JS_ID = 9007199254740991L;
    private long sequence, conversationId, assistantMessageId;
    private int answerChars;
    private boolean finished;

    synchronized boolean accept(String type, long nextSequence, long conversation, long assistant,
            int deltaChars, int eventChars, boolean containsPrivateReasoning) {
        if (finished || containsPrivateReasoning || eventChars <= 0 || eventChars > MAX_EVENT_CHARS
                || nextSequence <= sequence || conversation <= 0 || conversation > MAX_JS_ID
                || assistant <= 0 || assistant > MAX_JS_ID || deltaChars < 0
                || deltaChars > MAX_DELTA_CHARS) return false;
        boolean known = "start".equals(type) || "answer_delta".equals(type) || "thinking".equals(type)
                || "tool_start".equals(type) || "tool_done".equals(type) || "web_sources".equals(type)
                || "proposal".equals(type) || "login_required".equals(type) || "done".equals(type) || "error".equals(type);
        if (!known || (sequence == 0 && !"start".equals(type))) return false;
        if (sequence != 0 && (conversationId != conversation || assistantMessageId != assistant
                || "start".equals(type))) return false;
        if ("answer_delta".equals(type) && (deltaChars == 0 || deltaChars > MAX_ANSWER_CHARS - answerChars)) return false;
        if (!"answer_delta".equals(type) && deltaChars != 0) return false;
        conversationId = conversation; assistantMessageId = assistant; sequence = nextSequence;
        if ("answer_delta".equals(type)) answerChars += deltaChars;
        if ("done".equals(type) || "error".equals(type)) finished = true;
        return true;
    }
}
