package cn.healthlife.local;

import java.text.SimpleDateFormat;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;
import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeRoutinePolicy11828Test {
    private static final String SCOPE="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private long time(String text,String zone) throws Exception {
        SimpleDateFormat format=new SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS",Locale.ROOT);
        format.setTimeZone(TimeZone.getTimeZone(zone));return format.parse(text).getTime();
    }
    private NativeRoutinePolicy11828.Slot slot(long now,String zone,String kind) {
        for(NativeRoutinePolicy11828.Slot value:NativeRoutinePolicy11828.next(now,TimeZone.getTimeZone(zone)))if(value.kind.equals(kind))return value;
        throw new AssertionError(kind);
    }
    @Test public void fourIndependentSlotsUseEightNineTwelveAndEighteenLocalHours() throws Exception {
        long now=time("2026-11-01 07:00:00.000","Asia/Shanghai");
        List<NativeRoutinePolicy11828.Slot> rows=NativeRoutinePolicy11828.next(now,TimeZone.getTimeZone("Asia/Shanghai"));
        assertEquals(4,rows.size());
        String[] kinds={"breakfast","goals","lunch","dinner"};int[] hours={8,9,12,18};
        for(int i=0;i<4;i++){assertEquals(kinds[i],rows.get(i).kind);assertEquals(time(String.format(Locale.ROOT,"2026-11-01 %02d:00:00.000",hours[i]),"Asia/Shanghai"),rows.get(i).at);}
    }
    @Test public void passedBreakfastDoesNotMoveTodaysLunchOrDinnerToTomorrow() throws Exception {
        long now=time("2026-11-01 10:00:00.000","Asia/Shanghai");
        assertEquals("2026-11-02",slot(now,"Asia/Shanghai","breakfast").day);
        assertEquals("2026-11-02",slot(now,"Asia/Shanghai","goals").day);
        assertEquals("2026-11-01",slot(now,"Asia/Shanghai","lunch").day);
        assertEquals("2026-11-01",slot(now,"Asia/Shanghai","dinner").day);
    }
    @Test public void exactBoundaryCanStillBeDeliveredButConsumedOccurrenceReplansFuture() throws Exception {
        long at=time("2026-11-01 08:00:00.000","Asia/Shanghai");
        assertEquals(at,slot(at,"Asia/Shanghai","breakfast").at);
        assertEquals("2026-11-02",slot(at+1,"Asia/Shanghai","breakfast").day);
    }
    @Test public void springClockChangeMakesTwentyThreeHourDayWithoutMovingEightOClock() throws Exception {
        long now=time("2026-03-08 07:00:00.000","America/New_York");
        NativeRoutinePolicy11828.Slot value=slot(now,"America/New_York","breakfast");
        assertEquals(time("2026-03-08 08:00:00.000","America/New_York"),value.at);
        assertEquals(23*60*60*1000L,value.dayEnd-value.dayStart);
        assertEquals(value.dayStart,value.previousDayEnd);
    }
    @Test public void autumnClockChangeMakesTwentyFiveHourDay() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-11-01 07:00:00.000","America/New_York"),"America/New_York","breakfast");
        assertEquals(25*60*60*1000L,value.dayEnd-value.dayStart);
        assertTrue(NativeRoutinePolicy11828.valid(value,value.at,TimeZone.getTimeZone("America/New_York"),true));
    }
    @Test public void tomorrowIsCalendarAdditionAcrossSpringDstInsteadOfTwentyFourHours() throws Exception {
        long now=time("2026-03-07 12:00:00.000","America/New_York");
        NativeRoutinePolicy11828.Slot value=slot(now,"America/New_York","breakfast");
        assertEquals("2026-03-08",value.day);assertEquals(19*60*60*1000L,value.at-now);
    }
    @Test public void monthYearAndLeapDayRollOverNormally() throws Exception {
        assertEquals("2028-02-29",slot(time("2028-02-28 20:00:00.000","UTC"),"UTC","breakfast").day);
        assertEquals("2029-01-01",slot(time("2028-12-31 20:00:00.000","UTC"),"UTC","breakfast").day);
    }
    @Test public void deviceZoneIsUsedRatherThanHardCodedBeijingTime() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-11-01 07:00:00.000","America/Los_Angeles"),"America/Los_Angeles","breakfast");
        assertEquals(time("2026-11-01 08:00:00.000","America/Los_Angeles"),value.at);
        assertFalse(NativeRoutinePolicy11828.valid(value,value.at,TimeZone.getTimeZone("Asia/Shanghai"),true));
    }
    @Test public void timezoneChangedBroadcastCannotDeliverTheOldEightOClockOccurrence() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-11-01 07:00:00.000","Asia/Tokyo"),"Asia/Tokyo","breakfast");
        assertFalse(NativeRoutinePolicy11828.valid(value,value.at,TimeZone.getTimeZone("Asia/Shanghai"),true));
    }
    @Test public void tamperedDayBoundariesOrDayCannotBeAccepted() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-11-01 07:00:00.000","UTC"),"UTC","breakfast");
        NativeRoutinePolicy11828.Slot changed=new NativeRoutinePolicy11828.Slot(value.kind,value.day,value.at,value.dayStart+1,value.dayEnd,value.previousDayStart,value.previousDayEnd);
        assertFalse(NativeRoutinePolicy11828.valid(changed,value.at,TimeZone.getTimeZone("UTC"),true));
        changed=new NativeRoutinePolicy11828.Slot(value.kind,"2026-11-02",value.at,value.dayStart,value.dayEnd,value.previousDayStart,value.previousDayEnd);
        assertFalse(NativeRoutinePolicy11828.valid(changed,value.at,TimeZone.getTimeZone("UTC"),true));
    }
    @Test public void onlyFourReservedRoutineIdentifiersAreAccepted() {
        assertTrue(NativeRoutinePolicy11828.routine("routine11828.breakfast"));assertTrue(NativeRoutinePolicy11828.routine("routine11828.goals"));
        for(String value:new String[]{null,"routine11828.sleep","routine11828.breakfast.extra","42","../goals"})assertFalse(NativeRoutinePolicy11828.routine(value));
    }
    @Test public void deliveryAllowsFifteenMinutesButNeverEarlyOrStale() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-11-01 07:00:00.000","UTC"),"UTC","breakfast");
        assertTrue(NativeRoutinePolicy11828.valid(value,value.at+NativeRoutinePolicy11828.GRACE_MS,TimeZone.getTimeZone("UTC"),true));
        assertFalse(NativeRoutinePolicy11828.valid(value,value.at-1,TimeZone.getTimeZone("UTC"),true));
        assertFalse(NativeRoutinePolicy11828.valid(value,value.at+NativeRoutinePolicy11828.GRACE_MS+1,TimeZone.getTimeZone("UTC"),true));
    }
    @Test public void scopeIsAnOpaqueHashInsteadOfTokenOrAddress() {
        assertTrue(NativeRoutinePolicy11828.validScope(SCOPE));
        for(String value:new String[]{null,"","https://example.test","token","a".repeat(65),"A".repeat(64)})assertFalse(NativeRoutinePolicy11828.validScope(value));
    }
    @Test public void accountScopeGenerationAndOccurrenceAllHaveToMatch() {
        assertTrue(NativeRoutinePolicy11828.currentReceipt(59,SCOPE,3,59,SCOPE,3,1000,1000));
        assertFalse(NativeRoutinePolicy11828.currentReceipt(62,SCOPE,3,59,SCOPE,3,1000,1000));
        assertFalse(NativeRoutinePolicy11828.currentReceipt(59,"f".repeat(64),3,59,SCOPE,3,1000,1000));
        assertFalse(NativeRoutinePolicy11828.currentReceipt(59,SCOPE,4,59,SCOPE,3,1000,1000));
        assertFalse(NativeRoutinePolicy11828.currentReceipt(59,SCOPE,3,59,SCOPE,3,1000,2000));
    }
    @Test public void logoutThenSameAccountLoginCannotReuseAnOlderGeneration() {
        assertFalse(NativeRoutinePolicy11828.currentReceipt(59,SCOPE,8,59,SCOPE,6,1000,1000));
        assertFalse(NativeRoutinePolicy11828.currentReceipt(59,SCOPE,-1,59,SCOPE,-1,1000,1000));
    }
    @Test public void dailyStampDeduplicatesARepeatedLocalDateAfterTimezoneChange() throws Exception {
        NativeRoutinePolicy11828.Slot tokyo=slot(time("2026-11-01 07:00:00.000","Asia/Tokyo"),"Asia/Tokyo","breakfast");
        NativeRoutinePolicy11828.Slot shanghai=slot(time("2026-11-01 07:00:00.000","Asia/Shanghai"),"Asia/Shanghai","breakfast");
        assertNotEquals(tokyo.at,shanghai.at);
        assertEquals(NativeRoutinePolicy11828.dailyStamp(SCOPE,tokyo),NativeRoutinePolicy11828.dailyStamp(SCOPE,shanghai));
        assertNotEquals(NativeRoutinePolicy11828.dailyStamp(SCOPE,tokyo),NativeRoutinePolicy11828.dailyStamp("f".repeat(64),tokyo));
    }
    @Test public void yesterdaySleepBoundaryFollowsTheLocalCalendarAcrossDst() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-03-09 07:00:00.000","America/New_York"),"America/New_York","breakfast");
        assertEquals(23*60*60*1000L,value.previousDayEnd-value.previousDayStart);
        assertEquals(time("2026-03-08 00:00:00.000","America/New_York"),value.previousDayStart);
    }
    @Test public void allNextOccurrencesRemainFutureAfterEverySlotIsConsumed() throws Exception {
        long now=time("2026-11-01 23:59:59.999","Asia/Shanghai");
        for(NativeRoutinePolicy11828.Slot value:NativeRoutinePolicy11828.next(now,TimeZone.getTimeZone("Asia/Shanghai"))) {
            assertTrue(value.at>now);assertEquals("2026-11-02",value.day);
            assertTrue(NativeRoutinePolicy11828.valid(value,now,TimeZone.getTimeZone("Asia/Shanghai"),false));
        }
    }
    @Test public void midnightGapDayHasIndependentTwentyThreeHourBoundaries() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-09-06 07:00:00.000","America/Santiago"),"America/Santiago","breakfast");
        assertEquals(time("2026-09-06 01:00:00.000","America/Santiago"),value.dayStart);
        assertEquals(time("2026-09-07 00:00:00.000","America/Santiago"),value.dayEnd);
        assertEquals(time("2026-09-05 00:00:00.000","America/Santiago"),value.previousDayStart);
        assertEquals(23*3600000L,value.dayEnd-value.dayStart);
    }
    @Test public void dayBeforeMidnightGapStillStartsAtThePreviousCalendarMidnight() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-09-05 07:00:00.000","America/Santiago"),"America/Santiago","breakfast");
        assertEquals(time("2026-09-05 00:00:00.000","America/Santiago"),value.dayStart);
        assertEquals(time("2026-09-06 01:00:00.000","America/Santiago"),value.dayEnd);
        assertEquals(time("2026-09-04 00:00:00.000","America/Santiago"),value.previousDayStart);
    }
    @Test public void dayAfterMidnightGapKeepsTheCorrectYesterdayBoundary() throws Exception {
        NativeRoutinePolicy11828.Slot value=slot(time("2026-09-07 07:00:00.000","America/Santiago"),"America/Santiago","breakfast");
        assertEquals(time("2026-09-07 00:00:00.000","America/Santiago"),value.dayStart);
        assertEquals(time("2026-09-08 00:00:00.000","America/Santiago"),value.dayEnd);
        assertEquals(time("2026-09-06 01:00:00.000","America/Santiago"),value.previousDayStart);
        assertEquals(23*3600000L,value.previousDayEnd-value.previousDayStart);
    }
}
