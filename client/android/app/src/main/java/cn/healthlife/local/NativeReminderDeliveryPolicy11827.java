package cn.healthlife.local;

/** Decisions shared by scheduling and its platform-independent regressions. */
final class NativeReminderDeliveryPolicy11827 {
    private NativeReminderDeliveryPolicy11827() { }
    static boolean migrateDefaultChannel(int sdk, int importance, boolean userImportance) {
        // Before29 Android cannot tell us whether DEFAULT was chosen by the user.
        return sdk >= 29 && importance == 3 && !userImportance;
    }
    static String mode(boolean exact) { return exact ? "exact" : "inexact"; }
    static boolean sameReceipt(long savedActor, String savedId, long savedAt, String savedMode,
            long actor, String id, long at, boolean exact) {
        return actor > 0 && actor == savedActor && id != null && id.equals(savedId)
            && at > 0 && at == savedAt && mode(exact).equals(savedMode);
    }
    static boolean requestExactAtEntry(int sdk, boolean exactGranted, boolean scheduled, boolean asked) {
        return sdk >= 31 && !exactGranted && scheduled && !asked;
    }
    static boolean requestNotificationAtEntry(boolean granted, boolean previouslyAsked, boolean rationale,
            boolean thisVersionAttempted) {
        return !granted && !thisVersionAttempted && (!previouslyAsked || rationale);
    }
}
