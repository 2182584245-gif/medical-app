package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;

/** First entry uses the OS dialog once, including when a previous answer was no. */
public final class NativePermissionFirstEntryTest {
    @Test public void neverGrantedPermissionCanBeRequestedAtFirstEntry() {
        assertTrue(NativePermissionPolicy11616.firstEntryRequestable(false, false));
    }
    @Test public void aDeniedPermissionWithRationaleDoesNotAutomaticallyPromptAgain() {
        assertFalse(NativePermissionPolicy11616.firstEntryRequestable(false, true));
    }
    @Test public void grantedAndPermanentlyDeniedPermissionsNeverRepeat() {
        assertFalse(NativePermissionPolicy11616.firstEntryRequestable(true, false));
        assertFalse(NativePermissionPolicy11616.firstEntryRequestable(true, true));
        assertFalse(NativePermissionPolicy11616.firstEntryRequestable(false, true));
    }
    @Test public void explicitUserRetryStillUsesAndroidRationalePolicy() {
        assertTrue(NativePermissionPolicy11616.requestable(false, true, true));
        assertFalse(NativePermissionPolicy11616.requestable(false, true, false));
    }
}
