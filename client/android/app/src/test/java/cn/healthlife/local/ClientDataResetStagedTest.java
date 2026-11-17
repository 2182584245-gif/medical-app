package cn.healthlife.local;

import org.junit.Test;
import java.io.File;
import java.nio.file.Files;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

/** Real gate + synthetic private files; no Android data, phone or worker mutation. */
public final class ClientDataResetStagedTest {
    @Test public void stagedForegroundAndWorkerReadinessDoNotTouchAndroidOrEpoch() throws Exception {
        org.junit.Assume.assumeFalse(BuildConfig.ACCOUNT_EPOCH_RESET_REQUIRED);
        // Null Context is deliberate: neither startup entry may call Android
        // services, clear reminders/caches, or require a marker in this build.
        ClientDataReset.requireReady(null);
        AtomicInteger callbacks=new AtomicInteger();
        ClientDataReset.prepare(null,error->{assertNull(error);callbacks.incrementAndGet();});
        assertEquals(1,callbacks.get());
        assertTrue(ClientDataReset.isPrepared(false,null));
    }
    @Test public void stagedGatePreservesPrivateFilesAndDoesNotMarkFutureResetComplete() throws Exception {
        File files=Files.createTempDirectory("staged-preserve").toFile();
        File member=new File(files,"member");assertTrue(member.mkdir());
        File token=new File(member,"existing-session.sealed");byte[] bytes={8,4,2,1};Files.write(token.toPath(),bytes);
        assertFalse(ClientResetFiles.complete(files));
        assertTrue(ClientDataReset.isPrepared(false,files));
        assertArrayEquals(bytes,Files.readAllBytes(token.toPath()));
        assertFalse(new File(files,"client-account-epoch").exists());
    }
    @Test public void fullActivationStillRequiresActualCommittedV3Marker() throws Exception {
        File files=Files.createTempDirectory("future-full-epoch").toFile();
        assertFalse(ClientDataReset.isPrepared(true,files));
        ClientResetFiles.markComplete(files);
        assertTrue(ClientDataReset.isPrepared(true,files));
    }
}
