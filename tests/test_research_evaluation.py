from copy import deepcopy
import pytest

from hr_platform import fixtures as f
from hr_platform.common import canonical
from hr_platform.model import key
from hr_platform.official_payout import HEADERS, parse_payout_page
from hr_platform.research_evaluation import evaluate_candidates, qualified_payout
from hr_platform.research_ticket_events import MARKETS, winning_selections


def ordinary_page(*, places=2, frames=None):
    frames = frames or {h: h for h in range(1, 5)}
    state = (1, 3, 4)
    header = '<tr>' + ''.join(f'<th>{h}</th>' for h in HEADERS) + '</tr>'
    rows = ''.join('<tr>' + ''.join(f'<td>{v}</td>' for v in
        [rank, frames[h], h] + ['SYNTHETIC'] * (len(HEADERS) - 3)) + '</tr>'
        for h, rank in ((1, 1), (3, 2), (4, 3), (2, 4)))
    labels = {'win': '単勝', 'place': '複勝', 'quinella': '馬連複', 'exacta': '馬連単',
              'wide': 'ワイド', 'bracket_quinella': '枠連複', 'bracket_exacta': '枠連単',
              'trio': '三連複', 'trifecta': '三連単'}
    payouts = []
    for m in sorted(MARKETS):
        winners = winning_selections(state, m, frames=frames, place_places=places)
        for i, selection in enumerate(winners):
            title = f'<td class="title" rowspan="{len(winners)}">{labels[m]}</td>' if i == 0 else ''
            payouts.append(f'<tr>{title}<td class="a">{key(selection)}</td>'
                           f'<td class="refundMoney">650円</td></tr>')
    return ('<a id="RaceList" href="../TodayRaceInfo/RaceList?k_babaCode=19&amp;k_raceDate=2000%2F01%2F01&amp;k_raceNo=1">出馬表</a>'
        '<h4>2000年1月1日（土） SYNTHETIC 第1競走 競走成績</h4>'
        f'<section class="gradeTable"><table>{header}{rows}</table></section>'
        '<section class="newRefundTable"><table>' + ''.join(payouts) + '</table></section>').encode()


def horses(frames=None):
    return {str(h): {'frame': str((frames or {}).get(h, h))} for h in range(1, 5)}


def test_all_markets_multiple_winners_and_same_frame_match_official_amounts():
    frames = {1: 1, 3: 1, 4: 2, 2: 3}
    raw = ordinary_page(frames=frames)
    evidence = parse_payout_page(raw, f.RACE)
    payout, basis = qualified_payout(raw, evidence, f.RACE, horses(frames), sorted(MARKETS), 2)
    assert set(payout['complete_markets']) == MARKETS
    assert basis == 'OFFICIAL_ORDINARY_ALL_FINISHED'
    assert sum(t['market'] == 'place' for t in payout['tickets']) == 2
    assert sum(t['market'] == 'wide' for t in payout['tickets']) == 3
    assert next(t for t in payout['tickets'] if t['market'] == 'bracket_exacta')['selection'] == '1-1'
    assert all(t['payout_per_100'] == 650 for t in payout['tickets'])


def test_incomplete_place_table_only_leaves_place_pending():
    raw = ordinary_page(places=2)
    evidence = parse_payout_page(raw, f.RACE)
    payout, _ = qualified_payout(raw, evidence, f.RACE, horses(), sorted(MARKETS), 3)
    assert set(payout['complete_markets']) == MARKETS - {'place'}


@pytest.mark.parametrize('fault', ['frame', 'roster', 'quarantined'])
def test_conflicting_or_quarantined_evidence_does_not_become_losing_bets(fault):
    raw, meta = ordinary_page(), horses()
    evidence = parse_payout_page(raw, f.RACE)
    if fault == 'frame':
        meta['1']['frame'] = '2'
    elif fault == 'roster':
        meta.pop('4')
    else:
        evidence['status'] = 'QUARANTINED'
    assert qualified_payout(raw, evidence, f.RACE, meta, sorted(MARKETS), 2)[0] == {}


def test_exceptions_keep_existing_refund_scope_without_expanding_markets():
    from test_official_payout import page
    raw = page('除外')
    evidence = parse_payout_page(raw, f.RACE)
    payout, basis = qualified_payout(raw, evidence, f.RACE, horses(), sorted(MARKETS), 2)
    assert payout['complete_markets'] == ['quinella', 'trio']
    assert basis == 'OFFICIAL_QUINELLA_TRIO_EXCEPTION_SCOPE'
    assert any(t.get('refund_per_100') == 100 for t in payout['tickets'])


def inputs():
    market = {'quotes': {s: {'odds': 4.0, 'odds_max': 6.0, 'display_status': 'RANGE'}
                        for s in ['1-2', '1-3', '1-4', '2-3', '2-4', '3-4']}}
    record = {'content': market, 'observation_id': 'SYNTHETIC', 'received_at': f.at(2), 'available_at': f.at(3)}
    saved = {'view': {'markets': {'wide': record}}}
    result = {'race_id': f.RACE, 'output': {'candidates': {
        'multi': {'tickets': [{'market': 'wide', 'selection': s, 'stake_yen': stake}
                             for s, stake in [('1-3', 200), ('2-4', 100)]], 'stake_yen': 300},
        'excluded': {'status': 'INPUT_EXCLUDED', 'reason': 'DATA_MISSING', 'tickets': [], 'stake_yen': 0},
        'no_edge': {'status': 'NO_EDGE', 'tickets': [], 'stake_yen': 0}}}}
    final = {'markets': {'wide': {'parse_id': 'SYNTHETIC_FINAL', 'content': deepcopy(market)}}}
    for q in final['markets']['wide']['content']['quotes'].values():
        q.update(odds=2.0, odds_max=5.0)
    payout = {'final': True, 'complete_markets': ['wide'], 'runners': {h: {'status': 'FINISHED'} for h in horses()},
              'tickets': [{'market': 'wide', 'selection': '1-3', 'payout_per_100': 650}]}
    return result, saved, payout, final


def test_multi_purchase_summary_counts_stake_weights_and_ranges_without_repricing():
    result, saved, payout, final = inputs()
    before = canonical([result, saved, payout, final])
    evaluated = evaluate_candidates(result, saved, {'target': 'quinella'}, payout, 'SYNTHETIC', final)
    s = evaluated['multi']['summary']
    assert (s['bet_race_count'], s['purchased_ticket_count'], s['hit_race_count'], s['hit_ticket_count']) == (1, 2, 1, 1)
    assert (s['stake_yen'], s['payout_yen'], s['profit_yen']) == (300, 1300, 1000)
    assert (s['average_purchase_odds'], s['average_purchase_odds_upper']) == (4, 6)
    assert (s['average_final_odds'], s['average_final_odds_upper']) == (2, 5)
    assert evaluated['excluded']['status'] == 'INPUT_EXCLUDED' and evaluated['excluded']['summary']['eligible_race_count'] == 0
    assert evaluated['no_edge']['summary']['eligible_race_count'] == 1
    assert canonical([result, saved, payout, final]) == before


def test_missing_final_does_not_erase_official_cash_and_partial_settlement_stays_null():
    result, saved, payout, final = inputs()
    final['markets']['wide']['content']['quotes'].pop('2-3')
    s = evaluate_candidates(result, saved, {'target': 'quinella'}, payout, 'SYNTHETIC', final)['multi']['summary']
    assert s['profit_yen'] == 1000 and s['average_final_odds'] is None and s['final_odds_known_ticket_count'] == 0
    payout['runners']['2']['status'] = 'CANCELLED_BEFORE_SALES'
    s = evaluate_candidates(result, saved, {'target': 'quinella'}, payout, 'SYNTHETIC', final)['multi']['summary']
    assert s['payout_yen'] is None and s['profit_yen'] is None and s['hit_ticket_count'] is None
    assert s['pending_ticket_count'] == 1


@pytest.mark.parametrize('fault', ['duplicate', 'total', 'purchase_quote'])
def test_malformed_frozen_purchase_is_rejected_instead_of_corrected(fault):
    result, saved, payout, final = inputs()
    if fault == 'duplicate':
        result['output']['candidates']['multi']['tickets'][1]['selection'] = '1-3'
    elif fault == 'total':
        result['output']['candidates']['multi']['stake_yen'] = 100
    else:
        saved['view']['markets']['wide']['content']['quotes']['1-3']['odds'] = 0
    with pytest.raises(ValueError, match='RESEARCH_PURCHASE'):
        evaluate_candidates(result, saved, {'target': 'quinella'}, payout, 'SYNTHETIC', final)
