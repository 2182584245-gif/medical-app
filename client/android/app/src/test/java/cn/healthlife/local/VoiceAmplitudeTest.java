package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceAmplitudeTest {
    @Test public void silenceRemainsStill() { assertEquals(0f, VoiceAmplitude.level(new byte[640], 640), 0f); }
    @Test public void actualLoudnessIncreasesLevelAndIsBounded() {
        byte[] soft = new byte[640], loud = new byte[640];
        for (int i = 0; i < 640; i += 2) {
            int a = i % 4 == 0 ? 100 : -100, b = i % 4 == 0 ? 16000 : -16000;
            soft[i] = (byte) a; soft[i+1] = (byte) (a >> 8);
            loud[i] = (byte) b; loud[i+1] = (byte) (b >> 8);
        }
        float a = VoiceAmplitude.level(soft, 640), b = VoiceAmplitude.level(loud, 640);
        assertTrue(a > 0 && b > a && b <= 1);
    }
    @Test public void malformedSamplesDoNotReachDisplay() {
        assertEquals(0f, VoiceAmplitude.level(null, 10), 0f);
        assertEquals(0f, VoiceAmplitude.level(new byte[4], 3), 0f);
        assertEquals(0f, VoiceAmplitude.level(new byte[4], 8), 0f);
    }
}
