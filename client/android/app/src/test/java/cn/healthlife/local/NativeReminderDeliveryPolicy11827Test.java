package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeReminderDeliveryPolicy11827Test {
    @Test public void onlyUntouchedAppDefaultImportanceCanMigrate() {
        assertTrue(NativeReminderDeliveryPolicy11827.migrateDefaultChannel(29,3,false));
        assertTrue(NativeReminderDeliveryPolicy11827.migrateDefaultChannel(36,3,false));
        assertFalse(NativeReminderDeliveryPolicy11827.migrateDefaultChannel(36,3,true));
    }
    @Test public void blockedQuietOrAlreadyHighChannelsStayUnderUserControl() {
        for(int importance:new int[]{0,1,2,4,5})for(boolean changed:new boolean[]{false,true})
            assertFalse(NativeReminderDeliveryPolicy11827.migrateDefaultChannel(36,importance,changed));
    }
    @Test public void oldAndroidWithoutUserImportanceMetadataNeverResetsAChannel() {
        for(int sdk:new int[]{26,27,28})
            assertFalse(NativeReminderDeliveryPolicy11827.migrateDefaultChannel(sdk,3,false));
    }
    @Test public void sameDateMustBeRearmedWhenExactAccessChanges() {
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"inexact",59,"42",4000,true));
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"exact",59,"42",4000,false));
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"",59,"42",4000,true));
    }
    @Test public void unchangedOccurrenceKeepsItsReceiptWithoutRearmingEveryResume() {
        assertTrue(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"exact",59,"42",4000,true));
        assertTrue(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"inexact",59,"42",4000,false));
    }
    @Test public void editsAndOtherAccountsCanNeverReuseAnOldReceipt() {
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"exact",62,"42",4000,true));
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"exact",59,"43",4000,true));
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(59,"42",4000,"exact",59,"42",5000,true));
        assertFalse(NativeReminderDeliveryPolicy11827.sameReceipt(0,"42",4000,"exact",0,"42",4000,true));
    }
    @Test public void exactSettingsAreRequestedOnlyForExistingRemindersAndOnlyOnce() {
        assertTrue(NativeReminderDeliveryPolicy11827.requestExactAtEntry(31,false,true,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestExactAtEntry(30,false,true,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestExactAtEntry(36,true,true,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestExactAtEntry(36,false,false,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestExactAtEntry(36,false,true,true));
    }
    @Test public void newNotificationEntryAttemptRespectsGrantPermanentDenialAndOneAttempt() {
        assertTrue(NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(false,false,false,false));
        assertTrue(NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(false,true,true,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(false,true,false,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(true,false,false,false));
        assertFalse(NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(false,false,true,true));
        assertFalse(NativeReminderDeliveryPolicy11827.requestNotificationAtEntry(false,true,true,true));
    }
}
