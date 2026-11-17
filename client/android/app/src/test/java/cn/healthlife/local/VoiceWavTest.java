package cn.healthlife.local;

import org.junit.Test;
import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import static org.junit.Assert.*;

/** Real PCM files, without microphone, member data, or network. */
public final class VoiceWavTest {
    @Test public void writesStandard16KhzMono16BitWavWithExactPayload() throws Exception {
        File directory = Files.createTempDirectory("voice-wav").toFile();
        byte[] pcm = new byte[32000];
        for (int i = 0; i < pcm.length; i++) pcm[i] = (byte) (i * 19);
        File file;
        try (VoiceWav recording = new VoiceWav(directory, 60)) {
            assertEquals(pcm.length, recording.write(pcm, 0, pcm.length));
            file = recording.finish();
        }
        byte[] bytes = Files.readAllBytes(file.toPath());
        assertEquals(32044, bytes.length);
        assertEquals("RIFF", new String(bytes, 0, 4, StandardCharsets.US_ASCII));
        assertEquals("WAVE", new String(bytes, 8, 4, StandardCharsets.US_ASCII));
        assertEquals("fmt ", new String(bytes, 12, 4, StandardCharsets.US_ASCII));
        assertEquals("data", new String(bytes, 36, 4, StandardCharsets.US_ASCII));
        ByteBuffer header = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        assertEquals(32036, header.getInt(4)); assertEquals(16, header.getInt(16));
        assertEquals(1, header.getShort(20)); assertEquals(1, header.getShort(22));
        assertEquals(16000, header.getInt(24)); assertEquals(32000, header.getInt(28));
        assertEquals(2, header.getShort(32)); assertEquals(16, header.getShort(34));
        assertEquals(32000, header.getInt(40));
        assertArrayEquals(pcm, java.util.Arrays.copyOfRange(bytes, 44, bytes.length));
        assertTrue(file.delete()); assertTrue(directory.delete());
    }

    @Test public void boundsCaptureAtRequestedDurationWithoutExtraSamples() throws Exception {
        File directory = Files.createTempDirectory("voice-limit").toFile();
        File file;
        try (VoiceWav recording = new VoiceWav(directory, 1)) {
            assertEquals(32000, recording.write(new byte[64000], 0, 64000));
            assertEquals(0, recording.write(new byte[640], 0, 640));
            assertEquals(1000, recording.durationMillis());
            file = recording.finish();
        }
        assertEquals(32044, file.length());
        assertTrue(file.delete()); assertTrue(directory.delete());
    }

    @Test public void cancellationAndUnfinishedCloseRemovePrivateAudio() throws Exception {
        File directory = Files.createTempDirectory("voice-cancel").toFile();
        try (VoiceWav recording = new VoiceWav(directory, 60)) {
            recording.write(new byte[16000], 0, 16000);
            assertEquals(1, directory.list().length);
        }
        assertEquals(0, directory.list().length); assertTrue(directory.delete());
    }

    @Test public void tapAndEmptyRecordingsAreRejectedAndRemoved() throws Exception {
        for (int bytes : new int[]{0, 640, 7998}) {
            File directory = Files.createTempDirectory("voice-short").toFile();
            try (VoiceWav recording = new VoiceWav(directory, 60)) {
                recording.write(new byte[bytes], 0, bytes);
                try { recording.finish(); fail("Empty or accidental tap accepted"); }
                catch (IOException expected) { }
            }
            assertEquals(0, directory.list().length); assertTrue(directory.delete());
        }
    }

    @Test public void rejectsImpossibleDurationAndOddPcmBeforeWriting() throws Exception {
        File directory = Files.createTempDirectory("voice-invalid").toFile();
        for (int seconds : new int[]{0, -1, 61, Integer.MAX_VALUE}) {
            try { new VoiceWav(directory, seconds); fail("Unbounded recording accepted"); }
            catch (IllegalArgumentException expected) { }
        }
        assertEquals(0, directory.list().length);
        try (VoiceWav recording = new VoiceWav(directory, 1)) {
            try { recording.write(new byte[3], 0, 3); fail("Half sample accepted"); }
            catch (IllegalArgumentException expected) { }
            assertEquals(0, recording.durationMillis());
        }
        assertTrue(directory.delete());
    }

    @Test public void finishedWavCannotBeExtendedOrFinishedTwice() throws Exception {
        File directory = Files.createTempDirectory("voice-final").toFile();
        File file;
        try (VoiceWav recording = new VoiceWav(directory, 1)) {
            recording.write(new byte[8000], 0, 8000); file = recording.finish();
            try { recording.write(new byte[640], 0, 640); fail("Completed audio changed"); }
            catch (IOException expected) { }
            try { recording.finish(); fail("Completed audio reused"); }
            catch (IOException expected) { }
        }
        assertEquals(8044, file.length()); assertTrue(file.delete()); assertTrue(directory.delete());
    }

    @Test public void startupCleanupDeletesOnlyOwnedRegularWavFiles() throws Exception {
        File directory = Files.createTempDirectory("voice-cleanup").toFile();
        File owned = new File(directory, "voice-123456.wav"); Files.write(owned.toPath(), new byte[]{1});
        File other = new File(directory, "keep.wav"); Files.write(other.toPath(), new byte[]{2});
        File text = new File(directory, "voice-123456.txt"); Files.write(text.toPath(), new byte[]{3});
        File nested = new File(directory, "voice-98765.wav"); assertTrue(nested.mkdir());
        File child = new File(nested, "voice-42.wav"); Files.write(child.toPath(), new byte[]{4});
        VoiceWav.cleanAbandoned(directory);
        assertFalse(owned.exists()); assertTrue(other.isFile()); assertTrue(text.isFile()); assertTrue(child.isFile());
        assertTrue(child.delete()); assertTrue(nested.delete()); assertTrue(other.delete()); assertTrue(text.delete()); assertTrue(directory.delete());
    }

    @Test public void rejectsNonDirectoryWithoutModifyingItsContents() throws Exception {
        File directory = Files.createTempDirectory("voice-path").toFile();
        File occupied = new File(directory, "voice-capture"); Files.write(occupied.toPath(), new byte[]{7, 8});
        try { new VoiceWav(occupied, 60); fail("Non-directory capture destination accepted"); }
        catch (IOException expected) { }
        try { VoiceWav.cleanAbandoned(occupied); fail("Non-directory cleanup accepted"); }
        catch (IOException expected) { }
        assertArrayEquals(new byte[]{7, 8}, Files.readAllBytes(occupied.toPath()));
        assertTrue(occupied.delete()); assertTrue(directory.delete());
    }
}
