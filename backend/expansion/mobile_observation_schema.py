"""Additive observation values; existing ownership/CAS/audit stay in Member."""
from __future__ import annotations
from .common import body, choice, number, text, fail

KINDS = {'weight', 'glucose', 'blood_pressure', 'lipids', 'height', 'custom'}


def observation_data(payload, current=None):
    merged = {**(current or {}), **payload}
    kind = choice(merged['kind'], KINDS)
    units = {'weight': {'kg'}, 'glucose': {'mmol/L', 'mg/dL'},
             'blood_pressure': {'mmHg'}, 'lipids': {'mmol/L', 'mg/dL'}, 'height': {'cm'}}
    unit = text(merged['unit'], 30, kind == 'custom') or ''
    if kind in units:
        choice(unit, units[kind])
    low, high = {'weight': (.1, 1000), 'glucose': (.1, 1000 if unit == 'mg/dL' else 100),
                 'blood_pressure': (20, 300), 'lipids': (0, 2000 if unit == 'mg/dL' else 100),
                 'height': (20, 300), 'custom': (-1000000, 1000000)}[kind]
    value = number(merged['value'], low, high)
    result = {'value': value, 'unit': unit,
              'context': choice(merged.get('context', 'unspecified'), {'fasting', 'before_meal', 'after_meal', 'random', 'unspecified'}),
              'notes': text(merged.get('notes', ''), 4000, True) or '', 'report_item_id': merged.get('report_item_id')}
    if kind == 'blood_pressure':
        diastolic = number(merged.get('diastolic'), 10, 200)
        if diastolic >= value:
            fail('invalid_blood_pressure')
        result['diastolic'] = diastolic
    elif merged.get('diastolic') is not None:
        fail('unexpected_diastolic')
    name = text(merged.get('metric_name', ''), 100, True) or ''
    if kind == 'custom' and not name:
        fail('metric_name_required')
    if kind in {'custom', 'lipids'} or name:
        result['metric_name'] = name
    return result
