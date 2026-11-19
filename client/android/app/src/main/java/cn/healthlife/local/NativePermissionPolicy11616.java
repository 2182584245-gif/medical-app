package cn.healthlife.local;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Exact OS permission requests; a previous denial is never an automatic retry. */
final class NativePermissionPolicy11616 {
    private NativePermissionPolicy11616() { }
    static Set<String> kinds(List<?> values) {
        if (values == null || values.isEmpty() || values.size() > 3) throw new IllegalArgumentException();
        Set<String> result = new LinkedHashSet<>();
        for (Object value : values) {
            if (!(value instanceof String) || !("microphone".equals(value) || "camera".equals(value) || "notifications".equals(value))
                    || !result.add((String) value)) throw new IllegalArgumentException();
        }
        return result;
    }
    static boolean requestable(boolean granted, boolean asked, boolean rationale) {
        return !granted && (!asked || rationale);
    }
    static boolean firstEntryRequestable(boolean granted, boolean asked) {
        return !granted && !asked;
    }
    static String nextEntryKind(int sdk, boolean microphone, boolean microphoneAsked,
            boolean camera, boolean cameraAsked, boolean notifications, boolean notificationsAsked,
            boolean notificationsRationale, boolean notificationEntryAttempted) {
        if(firstEntryRequestable(microphone,microphoneAsked))return "microphone";
        if(firstEntryRequestable(camera,cameraAsked))return "camera";
        if(sdk>=33 && NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(
                notifications,notificationsAsked,notificationsRationale,notificationEntryAttempted))return "notifications";
        return null;
    }
    interface SettingsLauncher {boolean open(String target);}
    static boolean openNotificationSettings(int sdk,boolean notificationsEnabled,SettingsLauncher launcher) {
        String[] targets=sdk>=26?(notificationsEnabled?new String[]{"channel","app","details"}:new String[]{"app","details"}):new String[]{"details"};
        for(String target:targets)try{if(launcher.open(target))return true;}catch(RuntimeException unavailable){/* Try the next real system page. */}
        return false;
    }
}
