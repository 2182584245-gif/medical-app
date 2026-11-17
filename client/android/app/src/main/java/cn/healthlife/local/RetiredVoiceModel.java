package cn.healthlife.local;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/** Removes only the retired model's expanded copy, never member or guest files. */
final class RetiredVoiceModel {
    private RetiredVoiceModel() { }

    static void cleanup(File filesDirectory) throws IOException {
        File root = filesDirectory.getCanonicalFile();
        File target = new File(root, "voice-model-cn-0.22");
        if (!target.exists()) return;
        List<File> entries = new ArrayList<>();
        inspect(target, target, entries, 0);
        for (File entry : entries) {
            requireOwned(entry, target);
            if (entry.exists() && !entry.delete()) throw new IOException("Retired voice model cleanup deferred");
        }
    }

    private static void requireOwned(File entry, File root) throws IOException {
        File canonical = entry.getCanonicalFile();
        if (!canonical.equals(entry.getAbsoluteFile()) ||
            !(canonical.equals(root) || canonical.getPath().startsWith(root.getPath() + File.separator)))
            throw new IOException("Retired voice model path changed");
    }

    private static void inspect(File entry, File root, List<File> entries, int depth) throws IOException {
        requireOwned(entry, root);
        if (depth > 8 || entries.size() >= 512) throw new IOException("Unexpected retired voice model layout");
        if (entry.isDirectory()) {
            File[] children = entry.listFiles();
            if (children == null) throw new IOException("Retired voice model unavailable");
            for (File child : children) inspect(child, root, entries, depth + 1);
        }
        entries.add(entry);
    }
}
