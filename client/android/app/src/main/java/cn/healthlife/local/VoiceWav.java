package cn.healthlife.local;

import java.io.Closeable;
import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

/** A bounded, app-private 16 kHz mono PCM recording. Only a finished WAV survives close. */
final class VoiceWav implements Closeable {
    static final int SAMPLE_RATE = 16000;
    static final int BYTES_PER_SECOND = SAMPLE_RATE * 2;
    static final int HEADER_BYTES = 44;
    private static final int MIN_PCM_BYTES = BYTES_PER_SECOND / 4;
    private final File file;
    private final int limit;
    private RandomAccessFile output;
    private int size;
    private boolean finished;

    VoiceWav(File directory, int seconds) throws IOException {
        if (seconds < 1 || seconds > 60) throw new IllegalArgumentException("Voice duration out of bounds");
        requirePrivateDirectory(directory);
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("Voice storage unavailable");
        requirePrivateDirectory(directory);
        limit = seconds * BYTES_PER_SECOND;
        file = File.createTempFile("voice-", ".wav", directory);
        try {
            output = new RandomAccessFile(file, "rw");
            output.write(new byte[HEADER_BYTES]);
        } catch (IOException error) {
            close(); throw error;
        }
    }

    int write(byte[] buffer, int offset, int length) throws IOException {
        if (output == null || finished) throw new IOException("Voice recording closed");
        if (buffer == null || offset < 0 || length < 0 || offset > buffer.length - length || (length & 1) != 0)
            throw new IllegalArgumentException("Invalid PCM samples");
        int accepted = Math.min(length, limit - size);
        output.write(buffer, offset, accepted); size += accepted;
        return accepted;
    }

    int durationMillis() { return (int) (1000L * size / BYTES_PER_SECOND); }
    boolean isFull() { return size == limit; }

    private static void requirePrivateDirectory(File directory) throws IOException {
        File parent = directory.getParentFile();
        if (parent == null || (directory.exists() && !directory.isDirectory())
                || !directory.getCanonicalFile().equals(new File(parent.getCanonicalFile(), directory.getName())))
            throw new IOException("Voice storage is not a private directory");
    }

    static void cleanAbandoned(File directory) throws IOException {
        requirePrivateDirectory(directory);
        if (!directory.exists()) return;
        File[] abandoned = directory.listFiles();
        if (abandoned == null) throw new IOException("Voice storage unavailable");
        File canonicalDirectory = directory.getCanonicalFile();
        for (File file : abandoned) {
            if (file.getName().matches("voice-[A-Za-z0-9_-]+\\.wav") && file.isFile()
                    && file.getCanonicalFile().equals(new File(canonicalDirectory, file.getName()))) file.delete();
        }
    }

    File finish() throws IOException {
        if (output == null || finished) throw new IOException("Voice recording closed");
        if (size < MIN_PCM_BYTES) throw new IOException("Voice recording too short");
        ByteBuffer header = ByteBuffer.allocate(HEADER_BYTES).order(ByteOrder.LITTLE_ENDIAN);
        header.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + size);
        header.put("WAVEfmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16);
        header.putShort((short) 1).putShort((short) 1).putInt(SAMPLE_RATE).putInt(BYTES_PER_SECOND);
        header.putShort((short) 2).putShort((short) 16);
        header.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(size);
        output.seek(0); output.write(header.array()); output.getFD().sync(); output.close();
        output = null; finished = true;
        return file;
    }

    @Override public void close() {
        if (output != null) {
            try { output.close(); } catch (IOException ignored) { }
            output = null;
        }
        if (!finished && file.exists()) file.delete();
    }
}
