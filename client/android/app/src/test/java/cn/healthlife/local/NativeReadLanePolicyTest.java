package cn.healthlife.local;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeReadLanePolicyTest {
    @Test public void onlyTheExplicitMemberAndGuestReadSetsBypassWrites() {
        for(String action:List.of("health.profile.get","preferences.get","expansion.records.list","health.reminders.list","local149.status","local149.draft.get"))assertTrue(action,NativeReadLanePolicy.readOnly("member.request",action));
        for(String action:List.of("preferences.get","chat.conversations.list","chat.messages.list","local149.draft.get"))assertTrue(action,NativeReadLanePolicy.readOnly("guest.request",action));
        assertTrue(NativeReadLanePolicy.readOnly("avatar.local.get",null));
    }
    @Test public void writesAuthenticationRefreshAndUnknownReadSuffixesStaySerialized() {
        for(String action:List.of("health.profile.save","local149.draft.save","preferences.update","guest.snapshot.refresh","chat.send","chat.conversations.create","unknown.get","member.sync.extra")){
            assertFalse(action,NativeReadLanePolicy.readOnly("member.request",action));assertFalse(action,NativeReadLanePolicy.readOnly("guest.request",action));
        }
        for(String outer:List.of("auth.login","auth.phone.entry","auth.logout","app.bootstrap","account.erase","avatar.local.get.extra","unknown"))assertFalse(outer,NativeReadLanePolicy.readOnly(outer,"preferences.get"));
        assertFalse(NativeReadLanePolicy.readOnly("guest.request","health.profile.get"));
    }
    @Test public void overflowIsBoundedAndNeverExecutesOnCallerThread()throws Exception {
        ThreadPoolExecutor executor=NativeReadLanePolicy.executor();CountDownLatch started=new CountDownLatch(2),release=new CountDownLatch(1);
        try{
            Runnable blocked=()->{started.countDown();try{release.await();}catch(InterruptedException error){Thread.currentThread().interrupt();}};
            executor.execute(blocked);executor.execute(blocked);assertTrue(started.await(2,TimeUnit.SECONDS));
            for(int i=0;i<48;i++)executor.execute(()->{});
            try{executor.execute(()->fail("Overflow executed on caller"));fail("Overflow accepted");}catch(RejectedExecutionException expected){}
            assertEquals(2,executor.getMaximumPoolSize());assertEquals(48,executor.getQueue().size());
        }finally{release.countDown();executor.shutdownNow();assertTrue(executor.awaitTermination(2,TimeUnit.SECONDS));}
    }
}
