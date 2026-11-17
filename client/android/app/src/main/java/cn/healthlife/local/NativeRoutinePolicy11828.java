package cn.healthlife.local;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.GregorianCalendar;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/** Device-local calendar decisions, also runnable on the host JVM and API24. */
final class NativeRoutinePolicy11828 {
    static final long GRACE_MS=15*60*1000L;
    private static final String PREFIX="routine11828.";
    private static final String[] KINDS={"breakfast","goals","lunch","dinner"};
    private static final int[] HOURS={8,9,12,18};
    private NativeRoutinePolicy11828() { }

    static final class Slot {
        final String id,kind,day;
        final long at,dayStart,dayEnd,previousDayStart,previousDayEnd;
        Slot(String kind,String day,long at,long start,long end,long previousStart,long previousEnd) {
            this.id=PREFIX+kind;this.kind=kind;this.day=day;this.at=at;
            dayStart=start;dayEnd=end;previousDayStart=previousStart;previousDayEnd=previousEnd;
        }
    }
    static boolean routine(String id) {
        if(id==null)return false;
        for(String kind:KINDS)if((PREFIX+kind).equals(id))return true;
        return false;
    }
    static boolean validScope(String scope) {return scope!=null&&scope.matches("[a-f0-9]{64}");}
    static List<Slot> next(long now,TimeZone zone) {
        ArrayList<Slot> slots=new ArrayList<>();
        for(int index=0;index<KINDS.length;index++) {
            Calendar date=new GregorianCalendar(zone,Locale.ROOT);date.setTimeInMillis(now);
            setHour(date,HOURS[index]);
            if(date.getTimeInMillis()<now)date.add(Calendar.DAY_OF_MONTH,1);
            slots.add(forDate(KINDS[index],date,zone));
        }
        return slots;
    }
    private static void setHour(Calendar date,int hour) {
        date.set(Calendar.HOUR_OF_DAY,hour);date.set(Calendar.MINUTE,0);
        date.set(Calendar.SECOND,0);date.set(Calendar.MILLISECOND,0);
    }
    private static Slot forDate(String kind,Calendar date,TimeZone zone) {
        int hour=-1;for(int i=0;i<KINDS.length;i++)if(KINDS[i].equals(kind))hour=HOURS[i];
        if(hour<0)throw new IllegalArgumentException("Unknown routine");
        Calendar value=new GregorianCalendar(zone,Locale.ROOT);value.setTimeInMillis(date.getTimeInMillis());
        String day=String.format(Locale.ROOT,"%04d-%02d-%02d",value.get(Calendar.YEAR),value.get(Calendar.MONTH)+1,value.get(Calendar.DAY_OF_MONTH));
        setHour(value,hour);long at=value.getTimeInMillis();setHour(value,0);long start=value.getTimeInMillis();
        // Some zones advance at midnight:00:00 becomes01:00 on that date.
        // Reset each adjacent date separately instead of carrying that01:00.
        value.add(Calendar.DAY_OF_MONTH,1);setHour(value,0);long end=value.getTimeInMillis();
        value.add(Calendar.DAY_OF_MONTH,-2);setHour(value,0);long previous=value.getTimeInMillis();
        return new Slot(kind,day,at,start,end,previous,start);
    }
    static boolean valid(Slot slot,long now,TimeZone zone,boolean delivery) {
        if(slot==null||!routine(slot.id)||!slot.day.matches("[0-9]{4}-[0-9]{2}-[0-9]{2}"))return false;
        if(delivery&&(now<slot.at||now-slot.at>GRACE_MS))return false;
        try {
            Calendar at=new GregorianCalendar(zone,Locale.ROOT);at.setTimeInMillis(slot.at);
            Slot expected=forDate(slot.kind,at,zone);
            return same(slot,expected);
        }catch(IllegalArgumentException failure){return false;}
    }
    static boolean same(Slot a,Slot b) {
        return a!=null&&b!=null&&a.id.equals(b.id)&&a.day.equals(b.day)&&a.at==b.at
            &&a.dayStart==b.dayStart&&a.dayEnd==b.dayEnd
            &&a.previousDayStart==b.previousDayStart&&a.previousDayEnd==b.previousDayEnd;
    }
    static boolean currentReceipt(long actor,String scope,long generation,
            long savedActor,String savedScope,long savedGeneration,long savedAt,long at) {
        return actor>0&&actor==savedActor&&validScope(scope)&&scope.equals(savedScope)
            &&generation>=0&&generation==savedGeneration&&at>0&&at==savedAt;
    }
    static String dailyStamp(String scope,Slot slot) {
        if(!validScope(scope)||slot==null||!routine(slot.id))throw new IllegalArgumentException("Invalid routine scope");
        return "shown.routine11828."+scope+":"+slot.kind+":"+slot.day;
    }
}
