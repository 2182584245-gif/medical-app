package cn.healthlife.local;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public final class NativePermissionPolicy11616Test {
    @Test public void guideCanRequestOnlyActualAudioCameraAndNotificationPermissions() {
        assertEquals(List.of("microphone", "notifications"), List.copyOf(NativePermissionPolicy11616.kinds(List.of("microphone", "notifications"))));
        assertEquals(Collections.singleton("microphone"), NativePermissionPolicy11616.kinds(List.of("microphone")));
        assertEquals(List.of("microphone","camera","notifications"),List.copyOf(NativePermissionPolicy11616.kinds(List.of("microphone","camera","notifications"))));
    }
    @Test public void arbitraryPermissionsAndMalformedInputsCannotReachTheSystemDialog() {
        for (List<?> input : Arrays.asList(Collections.emptyList(), List.of("location"), List.of("all"),List.of("overlay"),List.of("contacts"),
                List.of("microphone", "microphone"), List.of("notifications", "microphone", "camera","storage"),
                Arrays.asList((Object) null), List.of(1), List.of("microphone "), List.of("MICROPHONE"))) {
            try {NativePermissionPolicy11616.kinds(input);fail("Unexpected permission request " + input);}
            catch (IllegalArgumentException expected) { }
        }
        try {NativePermissionPolicy11616.kinds(null);fail();} catch (IllegalArgumentException expected) { }
    }
    @Test public void grantedPermissionsAreNeverRequestedAgain() {
        assertFalse(NativePermissionPolicy11616.requestable(true, false, false));
        assertFalse(NativePermissionPolicy11616.requestable(true, true, true));
    }
    @Test public void explicitRequestMayRetryWithAndroidRationale() {
        assertTrue(NativePermissionPolicy11616.requestable(false, false, false));
        assertTrue(NativePermissionPolicy11616.requestable(false, true, true));
    }
    @Test public void deniedDoNotAskAgainGoesToSettingsInsteadOfRepeatingDialog() {
        assertFalse(NativePermissionPolicy11616.requestable(false, true, false));
    }
    @Test public void firstEntryAsksExactlyOnePermissionInAudioCameraNotificationOrder() {
        assertEquals("microphone",NativePermissionPolicy11616.nextEntryKind(36,false,false,false,false,false,false,false,false));
        // Denial counts as an answered system prompt. It must not trap the queue.
        assertEquals("camera",NativePermissionPolicy11616.nextEntryKind(36,false,true,false,false,false,false,false,false));
        assertEquals("notifications",NativePermissionPolicy11616.nextEntryKind(36,false,true,false,true,false,false,false,false));
        assertNull(NativePermissionPolicy11616.nextEntryKind(36,false,true,false,true,false,true,true,true));
    }
    @Test public void grantsAreSkippedAndDeniedPermissionsNeverRestartOnWindowFocus() {
        assertEquals("camera",NativePermissionPolicy11616.nextEntryKind(33,true,false,false,false,true,false,false,false));
        assertEquals("notifications",NativePermissionPolicy11616.nextEntryKind(33,true,false,true,false,false,false,false,false));
        assertNull(NativePermissionPolicy11616.nextEntryKind(33,false,true,false,true,false,true,false,false));
        assertNull(NativePermissionPolicy11616.nextEntryKind(36,true,true,true,true,true,true,true,true));
    }
    @Test public void olderAndroidNeverRequestsTheAbsentNotificationRuntimePermission() {
        for(int sdk:new int[]{24,25,26,29,31,32})
            assertNull(NativePermissionPolicy11616.nextEntryKind(sdk,false,true,false,true,false,false,false,false));
    }
    @Test public void permanentlyDeniedNotificationsStillOfferOneSystemSettingsEntry() {
        assertNull(NativePermissionPolicy11616.nextEntryKind(36,true,true,true,true,false,true,false,true));
        assertTrue(NativePermissionPolicy11616.notificationSettingsAtEntry(false,false,false));
        assertFalse(NativePermissionPolicy11616.notificationSettingsAtEntry(true,false,false));
        assertFalse(NativePermissionPolicy11616.notificationSettingsAtEntry(false,true,false));
        assertFalse(NativePermissionPolicy11616.notificationSettingsAtEntry(false,false,true));
    }
    @Test public void channelSettingsFailureFallsBackToAppThenDetails() {
        java.util.ArrayList<String> attempted=new java.util.ArrayList<>();
        assertTrue(NativePermissionPolicy11616.openNotificationSettings(36,true,target->{attempted.add(target);return "details".equals(target);}));
        assertEquals(List.of("channel","app","details"),attempted);
    }
    @Test public void notificationsOffAndOldAndroidUseTheRightSettingsPages() {
        java.util.ArrayList<String> attempted=new java.util.ArrayList<>();
        assertTrue(NativePermissionPolicy11616.openNotificationSettings(36,false,target->{attempted.add(target);return true;}));
        assertEquals(List.of("app"),attempted);attempted.clear();
        assertTrue(NativePermissionPolicy11616.openNotificationSettings(24,false,target->{attempted.add(target);return true;}));
        assertEquals(List.of("details"),attempted);
    }
    @Test public void noResolvablePageOrRejectedLaunchDoesNotClaimSettingsOpened() {
        java.util.ArrayList<String> attempted=new java.util.ArrayList<>();
        assertFalse(NativePermissionPolicy11616.openNotificationSettings(36,true,target->{attempted.add(target);throw new IllegalStateException("OEM unavailable");}));
        assertEquals(List.of("channel","app","details"),attempted);
    }
    @Test public void guestGetsOnlyThreeExactPermissionActions() {
        for (String action : List.of("native.permissions.status", "native.permissions.request", "native.permissions.openSettings"))
            assertTrue(action, GuestScopePolicy11614.allowedNative(action));
        for (String action : List.of("native.permissions", "native.permissions.request.all", "native.permissions.openSettings.extra", "native.permissions.camera", "native.permissions.request ", "native.settings"))
            assertFalse(action, GuestScopePolicy11614.allowedNative(action));
    }
}
