"""Authoritative boundary definitions and folder-submission consent evaluation."""
import hashlib
import json


def hours_of(cfg):
    return list(range(int(cfg['hourStart']), int(cfg['hourEnd']) + 1))


def close_hour(day, cfg):
    if day == 'Sun':
        return 17
    value = (cfg.get('dayCloseHours') or {}).get(day)
    if value in (None, '') and day == 'Fri':
        value = cfg.get('fridayCloseHour')
    return None if value in (None, '') else int(value)


def required_staff(day, hour, cfg):
    close = close_hour(day, cfg)
    if close is not None and hour >= close:
        return 0
    return int(cfg['reqStaffOpen'] if hour < int(cfg['lateHourStart']) else cfg['reqStaffLate'])


def boundary_context(cfg):
    minimum = int(cfg['minShiftLength'])
    blocks = {}
    for day in cfg['days']:
        staffed = [h for h in hours_of(cfg) if required_staff(day, h, cfg) > 0]
        opening = list(range(staffed[0], staffed[0] + minimum)) if staffed else []
        closing = [h for h in staffed if h >= int(cfg['lateHourStart'])]
        closing_required = list(range(closing[-1] - max(minimum, len(closing)) + 1, closing[-1] + 1)) if closing else []
        blocks[day] = {
            'openingHour': staffed[0] if staffed else None,
            'closingHour': closing[0] if closing else None,
            'opening': opening if opening and all(h in staffed for h in opening) else [],
            'closing': closing,
            'closingRequired': closing_required if all(h in staffed for h in closing_required) else [],
        }
    agreement = {'caps': {'openings': int(cfg['maxMorningShifts']),
                          'closings': int(cfg['maxEveningShifts']),
                          'combined': int(cfg['maxMorningPlusEvening'])},
                 'minShiftLength': minimum, 'blocks': blocks}
    token = hashlib.sha256(json.dumps(agreement, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(agreement, token=token, enabled=cfg.get('allowPreferredBoundaryExtras') is True)


def consent_status(grid, recorded, cfg):
    context = boundary_context(cfg)
    candidates = {'openings': [], 'closings': []}
    for day, blocks in context['blocks'].items():
        for kind, block, required in [('openings', blocks['opening'], blocks['opening']),
                                      ('closings', blocks['closing'], blocks['closingRequired'])]:
            # Strict levels: malformed strings/numbers are never preferred.
            preferred = block and all(type(grid.get(f'{day}_{h:02d}')) is int and grid[f'{day}_{h:02d}'] == 2 for h in block)
            available = required and all(type(grid.get(f'{day}_{h:02d}')) in (bool, int) and grid[f'{day}_{h:02d}'] in (1, 2) for h in required)
            if preferred and available:
                candidates[kind].append(day)
    recorded = recorded or {}
    current = recorded.get('consentContext') == context['token']
    opening = recorded.get('allowExtraOpenings') is True
    closing = recorded.get('allowExtraClosings') is True
    return {'allowExtraOpenings': opening, 'allowExtraClosings': closing,
            'consentContext': recorded.get('consentContext'), 'contextCurrent': current,
            'reconfirmationNeeded': (opening or closing) and not current,
            'effectiveOpenings': context['enabled'] and current and opening,
            'effectiveClosings': context['enabled'] and current and closing,
            'candidates': candidates, 'caps': context['caps'], 'enabled': context['enabled']}


def employee_consent(employee, grid, cfg):
    # API freezes these records by stable employee ID in the selected folder.
    record = (cfg.get('boundaryConsents') or {}).get(str(employee.get('id')), {})
    return consent_status(grid, record, cfg)
