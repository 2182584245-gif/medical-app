package cn.healthlife.local;

import org.junit.Test;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import static org.junit.Assert.*;

/** Context may expose an ancestor alias while its real private root is stable. */
public final class ClientResetPathsTest {
    private static File frameworkAlias(File target) {
        return new File(target.getParentFile(),"context-alias-"+target.getName()) {
            @Override public File getAbsoluteFile() { return this; }
            @Override public File getCanonicalFile() throws IOException { return target.getCanonicalFile(); }
        };
    }
    @Test public void freshContextAliasCanCheckAndCommitItsRealMarker() throws Exception {
        File root=Files.createTempDirectory("reset-context-root").toFile();
        File files=new File(root,"files");assertTrue(files.mkdir());
        File alias=frameworkAlias(files);
        assertFalse(ClientResetFiles.complete(alias));
        ClientResetFiles.markComplete(alias);
        assertTrue(ClientResetFiles.complete(alias));
        assertTrue(ClientResetFiles.complete(files));
    }
    @Test public void cleanupAcceptsTrustedRootAliasesAndKeepsPublicResources() throws Exception {
        File root=Files.createTempDirectory("reset-context-cleanup").toFile();
        File files=new File(root,"files"),cache=new File(root,"cache");assertTrue(files.mkdir());assertTrue(cache.mkdir());
        File member=new File(files,"member");assertTrue(member.mkdir());Files.write(new File(member,"old").toPath(),"retired".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        File model=new File(files,"public-model");Files.write(model.toPath(),"shared".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        File update=new File(cache,"update");assertTrue(update.mkdir());Files.write(new File(update,"verified.apk").toPath(),"installer".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        ClientResetFiles.clearMemberFiles(frameworkAlias(files),frameworkAlias(cache),frameworkAlias(root));
        assertFalse(member.exists());assertTrue(model.isFile());assertTrue(new File(update,"verified.apk").isFile());
        assertFalse(ClientResetFiles.complete(files));
    }
}
