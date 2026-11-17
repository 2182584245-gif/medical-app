"""Narrow owner goal/habit-point deletion; no generic ledger deletion."""
SCHEMA='medical_app_platform'
RUNTIME='medical_app_platform_runtime'
NAME='try1_delete_owned_goal_habit_points'
PREDICATE=(f"current_user='{RUNTIME}' AND current_setting('medical_app.role_code',true)='member' "
 "AND user_id=NULLIF(current_setting('medical_app.user_id',true),'')::bigint "
 "AND event_key ~ '^(reversal:)?habit:[1-9][0-9]*:[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
 f"AND EXISTS(SELECT 1 FROM {SCHEMA}.expansion_goals g "
 "WHERE g.user_id=NULLIF(current_setting('medical_app.user_id',true),'')::bigint "
 "AND g.id::text=split_part(regexp_replace(expansion_point_events.event_key,'^reversal:',''),':',2))")
def postgres_statements():
    target=SCHEMA+'.expansion_point_events'
    return(f'GRANT DELETE ON {target} TO {RUNTIME}',f'DROP POLICY IF EXISTS {NAME} ON {target}',
           f'CREATE POLICY {NAME} ON {target} FOR DELETE TO {RUNTIME} USING({PREDICATE})')
