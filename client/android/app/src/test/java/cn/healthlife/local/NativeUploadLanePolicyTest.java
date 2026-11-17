package cn.healthlife.local;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeUploadLanePolicyTest {
    @Test public void onlyExactIndependentMemberUploadIsMoved() {
        assertTrue(NativeUploadLanePolicy.upload("member.request","files.upload"));
        for(String outer:List.of("guest.request","auth.logout","request","native.pickImage","member.request.extra"))
            assertFalse(NativeUploadLanePolicy.upload(outer,"files.upload"));
        for(String inner:List.of("files.delete","files.get","forum.detail.comment","preferences.update","files.upload.extra"))
            assertFalse(NativeUploadLanePolicy.upload("member.request",inner));
        assertFalse(NativeUploadLanePolicy.upload(null,null));
    }
    @Test public void heldUploadCannotBlockLocalWritesAuthWorkOrReminderToggle()throws Exception {
        ThreadPoolExecutor uploads=NativeUploadLanePolicy.executor(),reminders=NativeReminderLanePolicy.executor();
        ExecutorService business=Executors.newSingleThreadExecutor();CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),saved=new CountDownLatch(1),auth=new CountDownLatch(1),toggle=new CountDownLatch(1);
        try {
            uploads.execute(()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            assertTrue(entered.await(2,TimeUnit.SECONDS));long start=System.nanoTime();
            business.execute(saved::countDown);business.execute(auth::countDown);reminders.execute(toggle::countDown);
            assertTrue(saved.await(500,TimeUnit.MILLISECONDS));assertTrue(auth.await(500,TimeUnit.MILLISECONDS));assertTrue(toggle.await(500,TimeUnit.MILLISECONDS));
            assertTrue((System.nanoTime()-start)/1_000_000<1000);assertEquals(1,release.getCount());
        }finally{release.countDown();uploads.shutdownNow();business.shutdownNow();reminders.shutdownNow();}
    }
    @Test public void uploadQueueIsOneAtATimeWithOnlyTwoWaitingSlots()throws Exception {
        ThreadPoolExecutor uploads=NativeUploadLanePolicy.executor();CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),done=new CountDownLatch(2);List<Integer>seen=Collections.synchronizedList(new ArrayList<>());
        try {
            uploads.execute(()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            assertTrue(entered.await(2,TimeUnit.SECONDS));
            uploads.execute(()->{seen.add(1);done.countDown();});uploads.execute(()->{seen.add(2);done.countDown();});
            try{uploads.execute(()->fail("Caller ran overflow upload"));fail("Overflow accepted");}catch(RejectedExecutionException expected){}
            assertEquals(1,uploads.getMaximumPoolSize());assertEquals(2,uploads.getQueue().size());release.countDown();assertTrue(done.await(2,TimeUnit.SECONDS));assertEquals(List.of(1,2),seen);
        }finally{release.countDown();uploads.shutdownNow();assertTrue(uploads.awaitTermination(2,TimeUnit.SECONDS));}
    }
}
