"""Prospective Paper enrollment from observed schedules. No provider transport here."""
from datetime import timedelta
import json
import re
from urllib.parse import urlencode
from .common import instant, paper_asof, seconds, stamp
from .prospective_rules import configuration, metadata_reason


def validate_schedule_policy(policy, base, collection):
    fields = {'version', 'enrollment_horizon_seconds', 'minimum_lead_seconds',
              'state_before_asof_seconds', 'race_before_asof_seconds', 'payout_after_start_seconds',
              'minimum_decision_spacing_seconds', 'venue_codes'}
    if (set(policy) != fields or policy['version'] != 'cloud-paper-schedule-v1'
            or any(type(policy[k]) is not int or policy[k] <= 0 for k in fields - {'version', 'venue_codes'})
            or not isinstance(policy['venue_codes'], dict) or not policy['venue_codes']
            or any(not isinstance(venue, str) or not venue or not isinstance(code, str)
                   or not re.fullmatch(r'\d{2}', code) for venue, code in policy['venue_codes'].items())):
        raise ValueError('PAPER_SCHEDULE_POLICY')
    derived = configuration(base)
    inputs = derived['input_policy']
    if (policy['enrollment_horizon_seconds'] <= policy['minimum_lead_seconds']
            or policy['minimum_lead_seconds'] < policy['state_before_asof_seconds'] + collection['page_min_lead_seconds']
            or not 0 < policy['race_before_asof_seconds'] < policy['state_before_asof_seconds']
            or policy['state_before_asof_seconds'] - policy['race_before_asof_seconds'] < collection['interval_seconds']
            or policy['race_before_asof_seconds'] <= collection['interval_seconds']
            or policy['state_before_asof_seconds'] > inputs['max_state_age_seconds']
            or policy['race_before_asof_seconds'] > inputs['max_metadata_age_seconds']):
        raise ValueError('PAPER_SCHEDULE_TIMING')
    return derived


def packet(race_id, start, at, policy, collection):
    date, venue, number = race_id.split(':')
    query = urlencode({'k_raceDate': f'{date[:4]}/{date[4:6]}/{date[6:]}',
                       'k_raceNo': number, 'k_babaCode': policy['venue_codes'][venue]})
    return [{
        'kind': kind, 'race_id': race_id,
        'url': collection['urls']['race'] if kind == 'race' else
               'https://www.keiba.go.jp' + collection['page_paths'][kind] + '?' + query,
        'at': round((instant(start if kind == 'payout' else at) + timedelta(seconds=offset)).timestamp() * 1000),
    } for kind, offset in [('state', -policy['state_before_asof_seconds']),
                           ('race', -policy['race_before_asof_seconds']),
                           ('payout', policy['payout_after_start_seconds'])]]


async def schedule_day(paper, collector, base, policy, collection):
    """Choose one feasible future race per invocation, using time/capacity only."""
    config = validate_schedule_policy(policy, base, collection)
    now = await paper.now()
    control = await paper.first("SELECT blocked FROM source_control WHERE source='nar-daily-odds'")
    if not control or control['blocked']:
        return {'status': 'SOURCE_STOPPED'}
    date = (instant(now) + timedelta(minutes=collection['timezone_offset_minutes'])).strftime('%Y%m%d')
    observed = await paper.races.schedules(date, now)
    if observed['status'] != 'OK':
        return {'status': observed['status']}
    snapshot = observed['snapshot']
    revision = '|'.join([stamp(snapshot['received_at']), stamp(snapshot['available_at']), snapshot['parse_id']]) if snapshot else None
    rows = await paper.all('SELECT race_id,asof_at,plan_body,decisions FROM cloud_paper_plans WHERE day=?', date)
    existing = {r['race_id']: r for r in rows}
    captures = await paper.all('''SELECT p.*,c.status AS capture_status FROM page_capture_plans p
        LEFT JOIN captures c USING(event_id)''')
    by_event = {r['event_id']: r for r in captures}
    candidates = []
    for race_id, metadata in observed['races'].items():
        venue = race_id.split(':')[1]
        old = existing.get(race_id)
        if (old and (old['decisions'] is not None or json.loads(old['plan_body'])['config'] != config)
                or venue not in policy['venue_codes']
                or metadata_reason(race_id, {'status': 'OBSERVED_UNQUALIFIED', 'metadata': metadata})):
            continue
        at = paper_asof(metadata, config, race_id)
        requests = packet(race_id, metadata['scheduled_start_at'], at, policy, collection)
        reserved = all((saved := by_event.get(f"nar-daily-{r['kind']}:{r['at']}"))
                       and saved['capture_status'] != 'SUPERSEDED_PLAN'
                       and all(saved[k] == r[k] for k in ('at', 'kind', 'url', 'race_id')) for r in requests)
        if reserved and old and old['asof_at'] == at and all(
                by_event[f"nar-daily-{r['kind']}:{r['at']}"].get('packet_revision') == revision for r in requests):
            continue
        lead = seconds(at, now)
        minimum_lead = 0 if reserved else policy['minimum_lead_seconds']
        if not minimum_lead < lead <= policy['enrollment_horizon_seconds']:
            continue
        if any(row['race_id'] != race_id and abs(seconds(at, row['asof_at'])) < policy['minimum_decision_spacing_seconds'] for row in rows):
            continue
        # Keep other evidence out of this race's state/race/regular-odds interval.
        start = requests[0]['at'] - policy['race_before_asof_seconds'] * 1000
        end = round(instant(at).timestamp() * 1000)
        requested = {f"nar-daily-{r['kind']}:{r['at']}" for r in requests}
        if any(r['capture_status'] is None and start <= r['at'] <= end
               and r['event_id'] not in requested
               and not (r['race_id'] == race_id and r['packet_owner'] == policy['version']
                        and r['at'] > round(instant(now).timestamp() * 1000)) for r in captures):
            continue
        candidates.append((at, race_id, requests))
    if not candidates:
        return {'status': 'NO_FUTURE_SLOT'}
    at, race_id, requests = min(candidates)
    # Reserve all three observations together before enrolling the fixed decision.
    # Same ordering as the observed schedule selection: receipt, publication, ID.
    result = json.loads(await collector.scheduleEvidenceBatch(json.dumps({'entries': requests, 'revision': revision})))
    if result.get('status') != 'REGISTERED':
        return {'status': 'COLLECTION_PLAN_PENDING'}
    plan = await paper.register(race_id, config)
    return {'status': 'ENROLLED' if plan['asof_at'] == at else 'SCHEDULE_CHANGED',
            'plan_id': plan['id'], 'metadata_parse_id': observed['snapshot']['parse_id']}
