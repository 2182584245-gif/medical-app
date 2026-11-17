package cn.healthlife.local;

import org.junit.Test;
import org.junit.Assume;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import static org.junit.Assert.*;

public final class RetiredVoiceModelTest {
    @Test public void removesOnlyNamedRetiredModelAndPreservesAccountFiles() throws Exception {
        Path root = Files.createTempDirectory("retired-voice-model");
        Path model = root.resolve("voice-model-cn-0.22");
        Files.createDirectories(model.resolve("am"));
        Files.write(model.resolve("am/final.mdl"), new byte[]{1, 2, 3});
        Files.write(model.resolve(".complete"), new byte[0]);
        Path member = root.resolve("member/history.sqlite3");
        Files.createDirectories(member.getParent());
        Files.write(member, new byte[]{9, 8});
        RetiredVoiceModel.cleanup(root.toFile());
        assertFalse(Files.exists(model));
        assertArrayEquals(new byte[]{9, 8}, Files.readAllBytes(member));
        RetiredVoiceModel.cleanup(root.toFile());
    }

    @Test public void refusesModelSymlinkWithoutDeletingExternalFile() throws Exception {
        Path root = Files.createTempDirectory("retired-voice-link");
        Path outside = Files.createTempDirectory("retired-voice-outside");
        Path retained = outside.resolve("retain.txt");
        Files.write(retained, new byte[]{7});
        try { Files.createSymbolicLink(root.resolve("voice-model-cn-0.22"), outside); }
        catch (IOException | UnsupportedOperationException | SecurityException unsupported) {
            Assume.assumeNoException(unsupported); return;
        }
        try { RetiredVoiceModel.cleanup(root.toFile()); fail("Symlink was accepted"); }
        catch (IOException expected) { }
        assertArrayEquals(new byte[]{7}, Files.readAllBytes(retained));
    }

    @Test public void unexpectedDeepLayoutStopsBeforeDeletingAnyFiles() throws Exception {
        Path root = Files.createTempDirectory("retired-voice-depth");
        Path model = root.resolve("voice-model-cn-0.22");
        Files.createDirectories(model);
        Path keep = model.resolve("keep.bin");
        Files.write(keep, new byte[]{4});
        Path deep = model;
        for (int i = 0; i < 10; i++) deep = deep.resolve("nested");
        Files.createDirectories(deep);
        Files.write(deep.resolve("keep-too.bin"), new byte[]{5});
        try { RetiredVoiceModel.cleanup(root.toFile()); fail("Unexpected depth was accepted"); }
        catch (IOException expected) { }
        assertArrayEquals(new byte[]{4}, Files.readAllBytes(keep));
        assertArrayEquals(new byte[]{5}, Files.readAllBytes(deep.resolve("keep-too.bin")));
    }
}
