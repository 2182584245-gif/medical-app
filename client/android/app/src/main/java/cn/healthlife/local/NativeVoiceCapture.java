package cn.healthlife.local;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.SystemClock;
import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** One microphone owner. Capturing and WAV finalization never run on the activity thread. */
final class NativeVoiceCapture {
    private static final ExecutorService CAPTURE_WORKER = Executors.newSingleThreadExecutor();
    interface Listener {
        void started();
        default void level(float value) { }
        void completed(File wav, int durationMillis);
        void failed(String code);
    }
    private final File directory;
    private final int seconds;
    private final Listener listener;
    private volatile boolean stopRequested, cancelled;

    NativeVoiceCapture(File directory, int seconds, Listener listener) {
        this.directory = directory; this.seconds = seconds; this.listener = listener;
    }

    void start() { CAPTURE_WORKER.execute(this::record); }
    void stop() { stopRequested = true; }
    void cancel() { cancelled = true; stopRequested = true; }

    private void record() {
        AudioRecord microphone = null;
        File completed = null;
        String failure = null;
        int duration = 0;
        try (VoiceWav wav = new VoiceWav(directory, seconds)) {
            if (cancelled) return;
            int minimum = AudioRecord.getMinBufferSize(VoiceWav.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (minimum <= 0) throw new IllegalStateException();
            microphone = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,
                VoiceWav.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, Math.max(minimum, 6400));
            if (microphone.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException();
            if (cancelled) return;
            microphone.startRecording();
            if (microphone.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IllegalStateException();
            byte[] buffer = new byte[640]; // 20 ms; non-blocking reads also bound release/cancel latency.
            long deadline = SystemClock.elapsedRealtime() + 3000L;
            boolean receivedSamples = false;
            long lastLevelAt = 0;
            boolean levelsAvailable = true;
            while (!stopRequested && !wav.isFull() && SystemClock.elapsedRealtime() < deadline) {
                int count = microphone.read(buffer, 0, buffer.length, AudioRecord.READ_NON_BLOCKING);
                if (count < 0 || (count & 1) != 0) throw new IllegalStateException();
                if (count == 0) { SystemClock.sleep(8); continue; }
                wav.write(buffer, 0, count);
                long now = SystemClock.elapsedRealtime();
                if (!receivedSamples) {
                    receivedSamples = true;
                    deadline = now + seconds * 1000L;
                    if (!cancelled && !stopRequested) listener.started();
                }
                if (levelsAvailable && !cancelled && !stopRequested && now - lastLevelAt >= 80) {
                    lastLevelAt = now;
                    try { listener.level(VoiceAmplitude.level(buffer, count)); }
                    catch (RuntimeException | LinkageError error) {
                        levelsAvailable = false;
                        android.util.Log.w("FawnseeVoice", "voice_level_disabled:" + error.getClass().getSimpleName());
                    }
                }
            }
            duration = wav.durationMillis();
            if (!cancelled) {
                if (duration < 250) failure = "voice_too_short";
                else completed = wav.finish();
            }
        } catch (SecurityException ignored) { failure = "permission_denied"; }
        catch (Exception ignored) { failure = "microphone_error"; }
        finally {
            if (microphone != null) {
                try { microphone.stop(); } catch (RuntimeException ignored) { }
                try { microphone.release(); } catch (RuntimeException ignored) { }
            }
        }
        if (cancelled) { if (completed != null) completed.delete(); return; }
        if (completed != null) listener.completed(completed, duration);
        else listener.failed(failure == null ? "microphone_error" : failure);
    }
}
