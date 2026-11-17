"""Frozen additive API surface shared by cloud/client contract tests."""
REVISION = "platform_0006_fawnsee_expansion"
SCHEMA = "medical_app_platform"
NEW_TABLES = (
    "expansion_record_revisions", "expansion_foods", "expansion_food_favorites",
    "expansion_meals", "expansion_analyses", "expansion_memories",
    "expansion_observations", "expansion_goals", "expansion_habit_entries",
    "expansion_content_progress", "expansion_point_events",
    "expansion_invitation_codes", "expansion_referrals", "expansion_settings",
)
ANALYSIS_KINDS = frozenset({"meal_photo", "report_prefill", "memory_extract", "meal_review", "daily_review", "weekly_review", "monthly_review", "nutrition_check"})
REVIEW_KINDS = frozenset({"meal_review", "daily_review", "weekly_review", "monthly_review", "nutrition_check"})
NUTRIENTS = ("energy_kcal", "protein_g", "fat_g", "carbohydrate_g", "available_carbohydrate_g", "fiber_g", "sodium_mg", "gi")
ACTION_METHODS = {
    "expansion.bootstrap": "get_bootstrap",
    "expansion.settings.get": "get_settings", "expansion.settings.update": "update_settings",
    "expansion.records.list": "list_records", "expansion.records.get": "get_record", "expansion.records.update": "update_record", "expansion.records.delete": "delete_record", "expansion.records.revisions": "list_record_revisions",
    "expansion.foods.list": "list_foods", "expansion.foods.get": "get_food", "expansion.foods.create": "create_food", "expansion.foods.favorite": "set_food_favorite", "expansion.foods.favorites": "list_food_favorites",
    "expansion.meals.list": "list_meals", "expansion.meals.get": "get_meal", "expansion.meals.create": "create_meal", "expansion.meals.update": "update_meal", "expansion.meals.delete": "delete_meal",
    "expansion.analyses.create": "create_analysis", "expansion.analyses.get": "get_analysis", "expansion.analyses.list": "list_analyses", "expansion.analyses.context": "get_analysis_context", "expansion.analyses.finish": "finish_analysis", "expansion.analyses.confirm": "confirm_analysis", "expansion.analyses.dismiss": "dismiss_analysis",
    "expansion.memories.list": "list_memories", "expansion.memories.create": "create_memory", "expansion.memories.update": "update_memory", "expansion.memories.confirm": "confirm_memory", "expansion.memories.revoke": "revoke_memory", "expansion.memories.delete": "delete_memory",
    "expansion.observations.list": "list_observations", "expansion.observations.get": "get_observation", "expansion.observations.create": "create_observation", "expansion.observations.update": "update_observation", "expansion.observations.delete": "delete_observation",
    "expansion.goals.list": "list_goals", "expansion.goals.get": "get_goal", "expansion.goals.create": "create_goal", "expansion.goals.update": "update_goal", "expansion.goals.close": "close_goal", "expansion.goals.delete": "delete_goal", "expansion.goals.checkin": "checkin_goal", "expansion.goals.uncheckin": "uncheckin_goal",
    "expansion.growth.summary": "get_growth", "expansion.points.ledger": "list_points", "expansion.invites.get": "get_invite_code", "expansion.invites.create": "create_invite_code", "expansion.invites.accept": "accept_invite",
    "expansion.content.list": "list_content", "expansion.content.get": "get_content", "expansion.content.progress": "get_content_progress", "expansion.content.progress.update": "update_content_progress",
    "expansion.ai.scenes": "list_ai_scenes", "expansion.reviews.list": "list_reviews", "expansion.reviews.context": "get_review_context",
}
RPC_METHODS = frozenset(ACTION_METHODS.values())
# This is a client coordinator, never an additional cloud RPC or provider key endpoint.
CLIENT_AI_ACTION = "expansion.analyses.run"
