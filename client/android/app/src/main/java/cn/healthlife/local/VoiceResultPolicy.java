package cn.healthlife.local;

/** Only the negotiated Qwen result is a successful voice transcription. */
final class VoiceResultPolicy {
    private VoiceResultPolicy() { }
    static boolean isCloudResult(String engine, String provider, String model, boolean offline) {
        return !offline && "qwen-asr".equals(engine) && "qwen".equals(provider)
            && "qwen-audio-3.1-asr-flash-message".equals(model);
    }
    static String cleanText(String text) {
        if (text == null) return "";
        String value = text.replaceAll("[\\p{Cc}&&[^\\n\\t]]", "")
            .replaceAll("(?<=[\\p{IsHan}]) +(?=[\\p{IsHan}])", "").trim();
        return value.substring(0, Math.min(4000, value.length()));
    }
}
