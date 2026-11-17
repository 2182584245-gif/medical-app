"""Transaction-only account erasure; no deployment, connections, or startup work."""
from __future__ import annotations

import re

AUDIT_ENTITIES = {
    'life_record': ('life_records', 'user_id'), 'conversation': ('conversations', 'user_id'),
    'ai_insight': ('ai_insights', 'user_id'), 'medical_report': ('medical_reports', 'user_id'),
    'membership': ('memberships', 'user_id'), 'order': ('orders', 'member_user_id'),
    'reminder': ('reminders', 'user_id'), 'user_file': ('user_files', 'user_id'),
    'visit_task': ('visit_tasks', 'member_user_id'),
    'product_recommendation': ('product_recommendations', 'member_user_id'),
    'profile_fact': ('profile_facts', 'user_id'),
    'advisor_summary': ('advisor_summaries', 'member_user_id'),
    'visit_record': ('visit_records', 'member_user_id'),
    'app_setting': ('app_settings', 'user_id'),
}


def audit_owner_predicate(actor: str, *, qualifier='a', qualified_tables=False) -> str:
    """Only server constants supply SQL identifiers/actor expressions."""
    table = lambda name: ('medical_app_platform.' if qualified_tables else '') + name
    terms = [f'{qualifier}.actor_user_id = {actor}',
             f"({qualifier}.entity_type IN ('user','member_profile','advisor_profile','developer') AND {qualifier}.entity_id = {actor})"]
    for entity, (name, owner) in AUDIT_ENTITIES.items():
        terms.append(f"({qualifier}.entity_type='{entity}' AND {qualifier}.entity_id IN "
                     f'(SELECT id FROM {table(name)} WHERE {owner}={actor}))')
    terms += [
        f"({qualifier}.entity_type='advisor_binding' AND {qualifier}.entity_id IN (SELECT id FROM {table('advisor_bindings')} WHERE member_user_id={actor} OR advisor_user_id={actor}))",
        f"({qualifier}.entity_type='message' AND {qualifier}.entity_id IN (SELECT m.id FROM {table('messages')} m JOIN {table('conversations')} c ON c.id=m.conversation_id WHERE c.user_id={actor}))",
        f"({qualifier}.entity_type='report_item' AND {qualifier}.entity_id IN (SELECT i.id FROM {table('report_items')} i JOIN {table('medical_reports')} r ON r.id=i.report_id WHERE r.user_id={actor}))",
        f"({qualifier}.entity_type='developer_console' AND {qualifier}.action IN ('developer.account.updated','developer.account.validity_updated','developer.record.staged') AND {qualifier}.entity_id={actor})",
    ]
    return '(' + ' OR '.join(terms) + ')'


def erase_account_rows(connection, user_id: int, *, postgres: bool = False) -> dict:
    """Call only inside a reviewed writable transaction and server identity.

    The administrator may use it in a restore rehearsal/full purge. Public
    service callers must first verify password + live session and set the
    transaction-local erasing identity. Shared catalogs are never deleted.
    """
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('Invalid account identity')
    if postgres:
        tables = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='medical_app_platform'").fetchall()}
    else:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    row = connection.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if row is None:
        raise ValueError('Account no longer exists')
    counts = {}
    # Audit entity IDs must be resolved before their parent business rows go.
    required = {name for name, _ in AUDIT_ENTITIES.values()} | {'messages','report_items','conversations','medical_reports'}
    if 'audit_logs' in tables:
        if not required <= tables:
            raise ValueError('Account erasure schema is incomplete')
        predicate = audit_owner_predicate(str(user_id))
        counts['audit_logs'] = connection.execute('DELETE FROM audit_logs AS a WHERE ' + predicate).rowcount
    actions = [
        ('orders', 'member_user_id=?', (user_id,)),
        ('developer_pending_changes', 'target_user_id=? OR created_by=?', (user_id, user_id)),
        ('developer_settings_snapshots', 'created_by=?', (user_id,)),
        # An advisor-source CHECK disallows silently SET NULL-ing its advisor.
        ('product_recommendations', 'member_user_id=? OR advisor_user_id=?', (user_id, user_id)),
    ]
    for name, where, parameters in actions:
        if name in tables:
            counts[name] = connection.execute('DELETE FROM ' + name + ' WHERE ' + where, parameters).rowcount
    if 'phone_auth_bindings' in tables and 'phone_auth_challenges' in tables:
        counts['phone_auth_challenges'] = connection.execute(
            'DELETE FROM phone_auth_challenges WHERE user_id=? OR phone IN '
            '(SELECT phone FROM phone_auth_bindings WHERE user_id=?)', (user_id, user_id)).rowcount
    counts['users'] = connection.execute('DELETE FROM users WHERE id=?', (user_id,)).rowcount
    if counts['users'] != 1:
        raise ValueError('Account erasure did not delete exactly one identity')
    # All other personal records, refreshes, sessions, binary attachments and
    # preferences have owner FKs with CASCADE. A missing/mismatched constraint
    # rejects the transaction instead of disabling FK/RLS or leaving a tombstone.
    return {'erased': True, 'deleted_rows': counts}


def cleanup_reference_plan() -> dict:
    """Non-executing guidance for the coordinator's current-schema rehearsal."""
    return {'requires_private_full_backup_and_verified_restore': True,
            'requires_stopped_writers_and_one_transaction': True,
            'preserve_shared': ['knowledge_library','language_library','reading_originals',
                                'products','forum_topics','shared_foods','global_settings'],
            'explicit_before_users': ['audit_logs','orders','developer_pending_changes',
                                     'developer_settings_snapshots','product_recommendations'],
            'backup_contents_are_not_claimed_erased': True}
