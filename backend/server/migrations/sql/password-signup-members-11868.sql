CREATE TABLE medical_app_platform.password_signup_members (
    user_id bigint PRIMARY KEY REFERENCES medical_app_platform.users(id) ON DELETE CASCADE
);
REVOKE ALL ON medical_app_platform.password_signup_members FROM PUBLIC, medical_app_platform_runtime;
GRANT SELECT, INSERT ON medical_app_platform.password_signup_members TO medical_app_platform_runtime;
ALTER TABLE medical_app_platform.password_signup_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_app_platform.password_signup_members FORCE ROW LEVEL SECURITY;
CREATE POLICY password_signup_read ON medical_app_platform.password_signup_members
    FOR SELECT TO medical_app_platform_runtime USING (
        user_id = NULLIF(current_setting('medical_app.user_id', true),'')::bigint
        AND current_setting('medical_app.role_code', true) = 'member'
    );
CREATE POLICY password_signup_register ON medical_app_platform.password_signup_members
    FOR INSERT TO medical_app_platform_runtime WITH CHECK (
        user_id = NULLIF(current_setting('medical_app.user_id', true),'')::bigint
        AND current_setting('medical_app.role_code', true) = 'member'
        AND current_setting('medical_app.registering', true) = 'true'
        AND EXISTS (SELECT 1 FROM medical_app_platform.users u
          WHERE u.id = user_id AND u.role_code = 'member' AND u.account_status = 'active'
            AND u.username_normalized = current_setting('medical_app.auth_username', true))
    );
