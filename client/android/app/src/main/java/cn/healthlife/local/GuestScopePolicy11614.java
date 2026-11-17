package cn.healthlife.local;

/** Pure scope boundaries shared by Native media and their local unit tests. */
final class GuestScopePolicy11614 {
    private GuestScopePolicy11614() { }
    static boolean usable(String kind,String id,String epoch,int actor,boolean cleanupPending) {
        if(cleanupPending || id==null || epoch==null || epoch.isEmpty())return false;
        if("guest".equals(kind))return id.matches("guest:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");
        return "member".equals(kind) && actor>0 && id.equals("member:"+actor);
    }
    static boolean same(String expectedId,String expectedEpoch,String actualId,String actualEpoch,boolean pending) {
        return !pending && expectedId!=null && !expectedId.isEmpty() && expectedEpoch!=null && !expectedEpoch.isEmpty()
            && expectedId.equals(actualId) && expectedEpoch.equals(actualEpoch);
    }
    static boolean allowedNative(String action) {
        return java.util.Arrays.asList("native.permissions.status","native.permissions.request","native.permissions.openSettings","native.getInfo","native.pickImage","native.pickFile","native.startVoice","native.stopVoice","native.cancelVoice","native.cancelChat","native.openLink","native.copyText","native.shareText",
            "native.update.check","native.update.start","native.update.status","native.update.cancel","native.update.install").contains(action);
    }
}
