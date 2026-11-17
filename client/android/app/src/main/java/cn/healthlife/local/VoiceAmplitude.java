package cn.healthlife.local;

/** A display level derived only from the actual signed 16-bit microphone PCM. */
final class VoiceAmplitude {
    private VoiceAmplitude() { }

    static float level(byte[] pcm, int count) {
        if (pcm == null || count < 2 || count > pcm.length || (count & 1) != 0) return 0f;
        double energy = 0;
        for (int i = 0; i < count; i += 2) {
            int sample = (short) ((pcm[i] & 0xff) | (pcm[i + 1] << 8));
            double value = sample / 32768.0;
            energy += value * value;
        }
        double rms = Math.sqrt(energy / (count / 2));
        // Map -60 dBFS..0 dBFS to 0..1 without inventing motion in silence.
        double decibels = rms <= 0 ? -60 : 20 * Math.log10(rms);
        return (float) Math.max(0, Math.min(1, (decibels + 60) / 60));
    }
}
