package cn.healthlife.local;

import org.junit.Test;
import java.io.File;
import java.nio.file.Files;
import static org.junit.Assert.*;

/** Host-only synthetic app directories; never touches Android or real accounts. */
public final class ClientResetFilesTest {
    private static void write(File file,String value) throws Exception {
        Files.write(file.toPath(),value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }
    @Test public void resetsPrivateHistoryButRetainsInstallerAndPublicVoiceModel() throws Exception {
        File data=Files.createTempDirectory("client-reset").toFile();
        File files=new File(data,"files"),cache=new File(data,"cache");files.mkdirs();cache.mkdirs();
        File member=new File(files,"member/local149-session");member.mkdirs();write(new File(member,"old.sealed"),"synthetic");
        File voice=new File(files,"voice-model-cn-0.22");voice.mkdir();write(new File(voice,"model"),"public");
        File update=new File(cache,"update");update.mkdir();write(new File(update,"verified.apk"),"installer");
        File picked=new File(cache,"picked-images");picked.mkdir();write(new File(picked,"old.jpg"),"synthetic");
        File web=new File(data,"app_webview/Default");web.mkdirs();write(new File(web,"Local Storage"),"synthetic");
        assertFalse(ClientResetFiles.complete(files));ClientResetFiles.clearMemberFiles(files,cache,data);
        assertFalse(new File(files,"member").exists());assertFalse(new File(cache,"picked-images").exists());
        assertFalse(new File(data,"app_webview").exists());assertTrue(new File(update,"verified.apk").exists());assertTrue(new File(voice,"model").exists());
        assertFalse(ClientResetFiles.complete(files));ClientResetFiles.markComplete(files);assertTrue(ClientResetFiles.complete(files));
    }
    @Test public void refusesSymlinkEscapeAndDoesNotCommitMarker() throws Exception {
        File data=Files.createTempDirectory("client-reset-boundary").toFile(),outside=Files.createTempDirectory("outside-reset").toFile();
        File files=new File(data,"files"),cache=new File(data,"cache");files.mkdirs();cache.mkdirs();
        File sentinel=new File(outside,"keep");write(sentinel,"keep");
        try {Files.createSymbolicLink(new File(files,"member").toPath(),outside.toPath());}
        catch(UnsupportedOperationException | java.nio.file.FileSystemException unsupported) {org.junit.Assume.assumeNoException(unsupported);return;}
        try {ClientResetFiles.clearMemberFiles(files,cache,data);fail("Linked member directory accepted");}
        catch(java.io.IOException expected) { }
        assertTrue(sentinel.exists());assertFalse(ClientResetFiles.complete(files));
    }
    @Test public void rejectsTraversalAndUnknownEpoch() throws Exception {
        File files=Files.createTempDirectory("client-reset-marker").toFile();
        for(String name:new String[]{"..","../outside","nested/file","nested\\file"}) {
            try {ClientResetFiles.child(files,name);fail("Traversal accepted");}catch(java.io.IOException expected) { }
        }
        write(new File(files,"client-account-epoch"),"old-epoch");assertFalse(ClientResetFiles.complete(files));
    }
    @Test public void previousPhoneCandidateMarkerCannotSkipTheNewAccountReset() throws Exception {
        File files=Files.createTempDirectory("client-reset-previous-phone-candidate").toFile();
        write(new File(files,"client-account-epoch"),"client-account-epoch-v2");
        assertFalse(ClientResetFiles.complete(files));
        write(new File(files,"client-account-epoch"),"client-account-epoch-v3");
        assertTrue(ClientResetFiles.complete(files));
    }
}
