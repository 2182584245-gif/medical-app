package cn.healthlife.local;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeReminderLanePolicyTest {
    @Test public void onlyExactMemberToggleUsesDedicatedLane() {
        assertTrue(NativeReminderLanePolicy.localToggle("member.request","health.reminders.toggle"));
        for(String outer:List.of("guest.request","request","auth.login","auth.logout","native.any","member.request.extra"))
            assertFalse(NativeReminderLanePolicy.localToggle(outer,"health.reminders.toggle"));
        for(String inner:List.of("health.reminders.add","health.reminders.delete","health.reminders.update","chat.send","files.upload","health.reminders.toggle.extra","unknown"))
            assertFalse(NativeReminderLanePolicy.localToggle("member.request",inner));
        assertFalse(NativeReminderLanePolicy.localToggle(null,null));
    }
    @Test public void heldCloudBusinessWorkerCannotDelayReminderLane()throws Exception {
        ExecutorService business=Executors.newSingleThreadExecutor();
        ThreadPoolExecutor reminders=NativeReminderLanePolicy.executor();
        CountDownLatch cloudStarted=new CountDownLatch(1),releaseCloud=new CountDownLatch(1);
        CountDownLatch oldToggle=new CountDownLatch(1),newToggle=new CountDownLatch(1);
        try {
            business.execute(()->{cloudStarted.countDown();try{releaseCloud.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            assertTrue(cloudStarted.await(2,TimeUnit.SECONDS));
            // This is the old native placement: exact toggle behind cloud RPC.
            business.execute(oldToggle::countDown);
            assertFalse(oldToggle.await(150,TimeUnit.MILLISECONDS));
            long start=System.nanoTime();reminders.execute(newToggle::countDown);
            assertTrue(newToggle.await(250,TimeUnit.MILLISECONDS));
            assertTrue((System.nanoTime()-start)/1_000_000<250);
            assertEquals(1,oldToggle.getCount());
        }finally{releaseCloud.countDown();business.shutdownNow();reminders.shutdownNow();}
    }
    @Test public void boundedQueueRemainsSerialAndRejectsOverflowOnCaller()throws Exception {
        ThreadPoolExecutor reminders=NativeReminderLanePolicy.executor();
        CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);
        try {
            reminders.execute(()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            assertTrue(entered.await(2,TimeUnit.SECONDS));
            for(int i=0;i<16;i++)reminders.execute(()->{});
            try{reminders.execute(()->fail("Caller ran overflow"));fail("Overflow accepted");}catch(RejectedExecutionException expected){}
            assertEquals(1,reminders.getMaximumPoolSize());assertEquals(16,reminders.getQueue().size());
        }finally{release.countDown();reminders.shutdownNow();assertTrue(reminders.awaitTermination(2,TimeUnit.SECONDS));}
    }
}

