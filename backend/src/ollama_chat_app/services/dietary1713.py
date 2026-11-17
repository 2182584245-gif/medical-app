"""Shared validation for the member's dietary questionnaire and meal details.

Values are self-reported ranges or AI estimates, not clinical measurements.
The JSON-backed preference and life-record resources already provide durable
account-bound storage and sync; no separate copy of user history is created.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import math
import re


QUESTIONNAIRE_CHOICES = {
    'gender': frozenset({'male', 'female', 'unspecified'}),
    'age_band': frozenset({'under18', '18_29', '30_44', '45_59', '60_plus'}),
    'height_band': frozenset({'lt150', '150_164', '165_179', 'ge180'}),
    'weight_band': frozenset({'lt50', '50_59', '60_69', '70_84', 'ge85'}),
    'activity': frozenset({'sedentary', 'occasional_walk', 'light', 'regular', 'vigorous'}),
    'goal': frozenset({'lose_weight', 'gain_muscle', 'balanced'}),
}
PROFILE_CHOICES = {**QUESTIONNAIRE_CHOICES, 'age_band': QUESTIONNAIRE_CHOICES['age_band'] |
                   frozenset({'14_17', '60_74', '75_plus'})}
# Recording a known age below fourteen must not turn it into an unknown/default
# adult age. The recommendation engine separately restricts targets to 14+.
PROFILE_NUMERIC_BOUNDS = {'age_years': (0, 120), 'height_cm': (100, 230), 'weight_kg': (20, 250)}
NUTRIENT_GRAMS = ('carbohydrate_g', 'protein_g', 'fat_g')


def validate_questionnaire(value):
    """None explicitly resets the questionnaire; incomplete objects never pass."""
    if value is None:
        return None
    if (not isinstance(value, Mapping) or set(value) != {'schema_version', *QUESTIONNAIRE_CHOICES}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('请完整填写饮食问卷。')
    for key, options in QUESTIONNAIRE_CHOICES.items():
        if type(value[key]) is not str or value[key] not in options:
            raise ValueError('饮食问卷包含无效选项，请重新选择。')
    return dict(value)


def validate_diet_profile(value):
    """Optional, confirmed personal facts collected one at a time; no guesses."""
    if value is None:
        return None
    if (not isinstance(value, Mapping) or set(value) != {'schema_version', 'confirmed'}
            or type(value['schema_version']) is not int or value['schema_version'] != 2
            or not isinstance(value['confirmed'], Mapping)
            or set(value['confirmed']) - (set(PROFILE_CHOICES) | set(PROFILE_NUMERIC_BOUNDS))):
        raise ValueError('饮食个人资料格式无效。')
    facts = dict(value['confirmed'])
    for key, option in facts.items():
        if key in PROFILE_NUMERIC_BOUNDS:
            low, high = PROFILE_NUMERIC_BOUNDS[key]
            if (type(option) not in {int, float} or not low <= option <= high
                    or not math.isfinite(option) or key == 'age_years' and not float(option).is_integer()):
                raise ValueError('饮食个人资料包含无效数值。')
            facts[key] = int(option) if key == 'age_years' else round(option, 1)
        elif type(option) is not str or option not in PROFILE_CHOICES[key]:
            raise ValueError('饮食个人资料包含无效选项。')
    return {'schema_version': 2, 'confirmed': facts}


def confirmed_diet_profile(profile=None, legacy=None):
    """Reuse existing self-reports without erasing them or requiring all six."""
    facts = {}
    if legacy is not None:
        facts.update({key: value for key, value in validate_questionnaire(legacy).items()
                      if key in QUESTIONNAIRE_CHOICES})
    if profile is not None:
        facts.update(validate_diet_profile(profile)['confirmed'])
    return {'schema_version': 2, 'confirmed': facts}


def validate_diet_library(value):
    """Small owner-scoped bookmark IDs, never arbitrary URLs or content copies."""
    if value is None:
        return None
    if (not isinstance(value, Mapping) or set(value) != {'schema_version', 'foods', 'recipes'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('饮食收藏格式无效。')
    clean = {'schema_version': 1}
    for category in ('foods', 'recipes'):
        identifiers = value[category]
        if (type(identifiers) is not list or len(identifiers) > 100
                or any(type(item) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', item)
                       for item in identifiers) or len(set(identifiers)) != len(identifiers)):
            raise ValueError('饮食收藏条目无效或超出上限。')
        clean[category] = list(identifiers)
    return clean


def validate_diet_details(value):
    """Validate new fields while preserving existing manual-record extensions."""
    result = dict(value)
    for key in NUTRIENT_GRAMS:
        if key not in result or result[key] is None:
            continue
        number = result[key]
        if type(number) not in {int, float} or not 0 <= number <= 20000 or not math.isfinite(number):
            raise ValueError('营养克数须为有效的非负数，未知可留空。')
        result[key] = round(number, 1)
    if 'estimated' in result and type(result['estimated']) is not bool:
        raise ValueError('营养估算标记格式无效。')
    if 'input_source' in result and (type(result['input_source']) is not str or result['input_source'] not in {'text', 'voice', 'photo'}):
        raise ValueError('饮食输入来源格式无效。')
    # Historical manual records can use labels such as "加餐" and "其他".
    # The new AI extraction separately requires the six requested meal labels.
    for key, limit in (('food', 1000), ('meal', 40), ('meal_type', 40)):
        if key in result and result[key] is not None and (type(result[key]) is not str or len(result[key]) > limit):
            raise ValueError('饮食内容或餐次格式无效。')
    if 'analysis_started_at' in result:
        stamp = result['analysis_started_at']
        try:
            if type(stamp) is not str or len(stamp) > 80:
                raise ValueError
            parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ValueError('饮食分析时间必须包含时区。') from None
    return result
