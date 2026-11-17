package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;

public final class VoiceResultPolicyTest {
    @Test public void onlyExactNegotiatedQwenProviderIsAcceptedWithoutOfflineFallback() {
        String model = "qwen-audio-3.1-asr-flash-message";
        assertTrue(VoiceResultPolicy.isCloudResult("qwen-asr", "qwen", model, false));
        assertFalse(VoiceResultPolicy.isCloudResult("fun-asr-nano", "qwen", model, false));
        assertFalse(VoiceResultPolicy.isCloudResult("vosk-offline", "vosk", "local", true));
        assertFalse(VoiceResultPolicy.isCloudResult("qwen-asr", "other", model, false));
        assertFalse(VoiceResultPolicy.isCloudResult("qwen-asr", "qwen", "other-model", false));
        assertFalse(VoiceResultPolicy.isCloudResult("qwen-asr", "qwen", model, true));
        assertFalse(VoiceResultPolicy.isCloudResult(null, null, null, false));
    }
    @Test public void cleansChineseSpacingWithoutChangingEnglishOrNumbers() {
        assertEquals("今天喝了 200 毫升水，吃了 two eggs。", VoiceResultPolicy.cleanText(" 今 天 喝 了 200 毫 升 水，吃 了 two eggs。 "));
        assertEquals("", VoiceResultPolicy.cleanText(null));
    }
    @Test public void boundsTranscriptAndDropsControlCharacters() {
        assertEquals("今天\n喝水", VoiceResultPolicy.cleanText("今\0天\n喝水\u0007"));
        assertEquals(4000, VoiceResultPolicy.cleanText("水".repeat(5000)).length());
    }
}
