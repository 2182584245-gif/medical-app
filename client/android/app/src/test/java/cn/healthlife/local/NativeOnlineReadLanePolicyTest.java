package cn.healthlife.local;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeOnlineReadLanePolicyTest {
    @Test public void onlyTheExactTopLevelRecordPhotoGetUsesTheOnlineLane() {
        assertTrue(NativeOnlineReadLanePolicy.readOnly("record.photo.get",""));
        assertTrue(NativeOnlineReadLanePolicy.readOnly("record.photo.get",null));
        for(String action:List.of("record.photo.stage","record.photo.discard","record.photo.get.extra","record.photo.unknown"))
            assertFalse(action,NativeOnlineReadLanePolicy.readOnly(action,""));
        assertFalse(NativeOnlineReadLanePolicy.readOnly("member.request","record.photo.get"));
        assertFalse(NativeOnlineReadLanePolicy.readOnly("guest.request","record.photo.get"));
        assertFalse(NativeOnlineReadLanePolicy.readOnly("record.photo.get","unexpected"));
    }
    @Test public void articleForumAndMemoryReadsHaveAnExplicitOnlineLane() {
        for(String action:List.of("reading.catalog","reading.asset","reading.comments","forum.discovery.search","forum.detail.get","member.memory.overview","member.ai-profile.categories.get"))
            assertTrue(action,NativeOnlineReadLanePolicy.readOnly("member.request",action));
        assertTrue(NativeOnlineReadLanePolicy.readOnly("guest.request","ai.operations.status"));
        assertTrue(NativeOnlineReadLanePolicy.readOnly("guest.request","chat.tools.proposals"));
    }
    @Test public void writesAndGuessedReadSuffixesAreNeverParallelized() {
        for(String action:List.of("health.profile.save","preferences.update","forum.detail.comment","forum.detail.like","reading.share","reading.social.set","chat.conversations.create","chat.send","member.memory.forget","unknown.get","reading.catalog.extra"))
            assertFalse(action,NativeOnlineReadLanePolicy.readOnly("member.request",action));
        for(String outer:List.of("auth.login","auth.logout","account.erase","app.bootstrap","request","native.any"))
            assertFalse(outer,NativeOnlineReadLanePolicy.readOnly(outer,"reading.catalog"));
        assertFalse(NativeOnlineReadLanePolicy.readOnly(null,null));
        assertFalse(NativeOnlineReadLanePolicy.readOnly("guest.request","member.memory.overview"));
    }
    @Test public void twoSlowCloudReadsDoNotHoldTheSerializedWriteOrReminderLanes()throws Exception {
        ThreadPoolExecutor online=NativeOnlineReadLanePolicy.executor();
        ThreadPoolExecutor reminders=NativeReminderLanePolicy.executor();
        ExecutorService writes=Executors.newSingleThreadExecutor();
        CountDownLatch entered=new CountDownLatch(2),release=new CountDownLatch(1),written=new CountDownLatch(1),toggled=new CountDownLatch(1);
        try {
            Runnable slow=()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}};
            online.execute(slow);online.execute(slow);assertTrue(entered.await(2,TimeUnit.SECONDS));
            long start=System.nanoTime();writes.execute(written::countDown);reminders.execute(toggled::countDown);
            assertTrue(written.await(500,TimeUnit.MILLISECONDS));assertTrue(toggled.await(500,TimeUnit.MILLISECONDS));
            assertTrue((System.nanoTime()-start)/1_000_000<1000);assertEquals(1,release.getCount());
        }finally{release.countDown();online.shutdownNow();writes.shutdownNow();reminders.shutdownNow();}
    }
    @Test public void slowReadWasARealHeadOfLineBlockOnTheOldBusinessPlacement()throws Exception {
        ExecutorService old=Executors.newSingleThreadExecutor();CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),written=new CountDownLatch(1);
        try {
            old.execute(()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            assertTrue(entered.await(2,TimeUnit.SECONDS));old.execute(written::countDown);
            assertFalse(written.await(120,TimeUnit.MILLISECONDS));assertEquals(1,written.getCount());
        }finally{release.countDown();old.shutdownNow();}
    }
    @Test public void ordinaryWritesKeepTheirOriginalSingleLaneOrder()throws Exception {
        ExecutorService writes=Executors.newSingleThreadExecutor();List<Integer> seen=Collections.synchronizedList(new ArrayList<>());CountDownLatch done=new CountDownLatch(24);
        try {
            for(int i=0;i<24;i++){final int n=i;writes.execute(()->{seen.add(n);done.countDown();});}
            assertTrue(done.await(2,TimeUnit.SECONDS));for(int i=0;i<24;i++)assertEquals(i,(int)seen.get(i));
        }finally{writes.shutdownNow();}
    }
    @Test public void overflowRejectsWithoutRunningOnTheBridgeCaller()throws Exception {
        ThreadPoolExecutor online=NativeOnlineReadLanePolicy.executor();CountDownLatch entered=new CountDownLatch(2),release=new CountDownLatch(1);
        try {
            Runnable slow=()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}};
            online.execute(slow);online.execute(slow);assertTrue(entered.await(2,TimeUnit.SECONDS));
            for(int i=0;i<48;i++)online.execute(()->{});
            try{online.execute(()->fail("Caller ran overflow"));fail("Unbounded work accepted");}catch(RejectedExecutionException expected){}
            assertEquals(2,online.getMaximumPoolSize());assertEquals(48,online.getQueue().size());
        }finally{release.countDown();online.shutdownNow();assertTrue(online.awaitTermination(2,TimeUnit.SECONDS));}
    }
}
