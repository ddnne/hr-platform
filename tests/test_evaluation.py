import json
import math
import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical
from hr_platform.evaluation import compare, purchase_summary
from hr_platform.paper import decide, settle


def test_pending_is_not_zero_and_failed_race_stays_in_cohort(collected, config):
    decide(collected, f.RACE, f.schedule(), config)
    decide(collected, '20000101:SYNTHETIC:2', f.schedule(), config)
    report = compare(collected, config, f.at(10))
    assert report['race_count'] == 2
    for summary in report['models'].values():
        assert summary['race_count'] == 2
        assert summary['pending_count'] == 1
        assert summary['skip_rate'] == .5
        assert summary['reason_counts']['DATA_MISSING'] == 1
        assert summary['profit_yen'] is None and summary['roi'] is None
        assert summary['max_drawdown_yen_by_decision_time'] is None
        assert summary['bet_race_count'] == summary['purchased_ticket_count'] == 1
        assert summary['hit_race_count'] is None and summary['hit_ticket_count'] is None


def test_correction_and_late_settlement_do_not_rewrite_prior_report(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        record = settle(collected, decision['id'], f.payout())
        assert json.loads(collected.read_body(record['source_hash'], 'receipts')) == f.payout()
    original = compare(collected, config, f.at(22))
    assert all(m['profit_yen'] == 550 for m in original['models'].values())
    assert all(m['hit_race_count'] == m['hit_ticket_count'] == 1 for m in original['models'].values())
    collected.clock = lambda: f.at(25)
    for decision in decisions:
        settle(collected, decision['id'], f.payout('correction', tickets=[]))
    before = compare(collected, config, f.at(22))
    after = compare(collected, config, f.at(26))
    assert before['models'] == original['models']
    for summary in after['models'].values():
        assert summary['profit_yen'] == -100 and summary['roi'] == -1
        assert summary['max_drawdown_yen_by_decision_time'] == 100
        assert summary['settlement_source_counts'] == {'SYNTHETIC': 1}
        assert summary['hit_race_count'] == summary['hit_ticket_count'] == 0


def test_purchase_summary_distinguishes_races_tickets_refunds_and_pending():
    rows = [
        {'race_id': 'A', 'target': 'quinella', 'selection': '1-2', 'stake_yen': 100, 'payout_yen': 0, 'refund_yen': 100},
        {'race_id': 'A', 'target': 'quinella', 'selection': '1-3', 'stake_yen': 100, 'payout_yen': 150, 'refund_yen': 0},
        {'race_id': 'A', 'target': 'quinella', 'selection': '2-3', 'stake_yen': 200, 'payout_yen': 300, 'refund_yen': 0},
        {'race_id': 'B', 'target': 'quinella', 'selection': '1-2', 'stake_yen': 100, 'payout_yen': 0, 'refund_yen': 0},
        {'race_id': 'C', 'target': 'quinella', 'selection': None, 'stake_yen': 0, 'payout_yen': 0, 'refund_yen': 0},
    ]
    for row, quote in zip(rows, [10, 20, 30, 40, None]):
        row['purchase_odds'] = quote
    rows.append(dict(rows[2]))
    assert purchase_summary(rows) == {
        'bet_race_count': 2, 'purchased_ticket_count': 4, 'hit_race_count': 1, 'hit_ticket_count': 2,
        'average_purchase_odds': 26,
        'average_final_odds': None, 'final_odds_known_ticket_count': 0,
    }
    rows[3]['payout_yen'] = None
    assert purchase_summary(rows)['hit_race_count'] is None
    assert purchase_summary(rows)['hit_ticket_count'] is None
    assert purchase_summary(rows)['average_purchase_odds'] == 26
    rows[0]['purchase_odds'] = None
    assert purchase_summary(rows)['average_purchase_odds'] is None
    for row in rows:
        row['final_odds'] = 50 if row['stake_yen'] else None
    assert purchase_summary(rows)['average_final_odds'] == 50
    rows[3]['final_odds'] = None
    assert purchase_summary(rows)['average_final_odds'] is None
    assert purchase_summary(rows)['final_odds_known_ticket_count'] == 3


def test_average_odds_use_fixed_decision_input_even_before_settlement(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    original = compare(collected, config, f.at(10))
    assert all(row['average_final_odds'] is None for row in original['models'].values())
    for decision in decisions:
        quote = decision['input_view']['markets'][decision['target']]['content']['quotes'][decision['selection']]['odds']
        assert original['models'][decision['model']]['average_purchase_odds'] == quote
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    after = compare(collected, config, f.at(22))
    assert all(after['models'][model]['average_purchase_odds'] == row['average_purchase_odds']
               for model, row in original['models'].items())
    # A later available final quote is evaluation evidence, never a purchase input.
    collected.clock = lambda: f.at(25)
    collected.ingest(f.event('final-for-evaluation', 24, kind='FINAL_ONLY'), f.archive('uniform', distorted=False))
    later = compare(collected, config, f.at(26))
    assert all(row['average_final_odds'] == pytest.approx(4.8) for row in later['models'].values())
    assert all(row['average_purchase_odds'] == original['models'][model]['average_purchase_odds']
               for model, row in later['models'].items())
    assert compare(collected, config, f.at(22))['models'] == after['models']


def test_tied_revision_is_ambiguous_not_arbitrary(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
        settle(collected, decision['id'], f.payout('correction', tickets=[]))
    report = compare(collected, config, f.at(22))
    assert all(m['pending_count'] == 1 and m['roi'] is None for m in report['models'].values())


def test_no_bets_no_roi_and_future_decisions_excluded(collected, config):
    decide(collected, '20000101:SYNTHETIC:2', f.schedule(), config)
    before = compare(collected, config, f.at(4))
    assert before['status'] == 'EMPTY'
    report = compare(collected, config, f.at(5))
    assert all(m['roi'] is None and m['stake_yen'] == 0 for m in report['models'].values())


def test_missing_model_and_mismatched_input_do_not_select_better_cohort(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    changed = dict(decisions[0], asof_at=f.at(3))
    with collected.db:
        collected.db.execute('UPDATE decisions SET body=? WHERE id=?',
                             (canonical(changed).decode(), changed['id']))
    report = compare(collected, config, f.at(10))
    assert report['issues'][0]['reason'] == 'DIFFERENT_INPUTS'
    assert report['models'] == {}
    with collected.db:
        collected.db.execute('DELETE FROM decisions WHERE id=?', (changed['id'],))
    report = compare(collected, config, f.at(10))
    assert report['issues'][0]['reason'] == 'MISSING_MODEL_DECISION'
    assert report['race_count'] == 1


def test_configuration_cannot_change(collected, config):
    decide(collected, f.RACE, f.schedule(), config)
    with pytest.raises(ValueError, match='EXPERIMENT_CONFIG_CHANGED'):
        compare(collected, {**config, 'lambda': .1}, f.at(10))


def test_slow_receipt_save_does_not_backdate_profit(collected, config, monkeypatch):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    body = collected.body
    def slow_body(data, kind):
        result = body(data, kind)
        collected.clock = lambda: f.at(25)
        return result
    monkeypatch.setattr(collected, 'body', slow_body)
    for decision in decisions:
        record = settle(collected, decision['id'], f.payout())
        assert record['recorded_at'].startswith('2000-01-01T05:25:')
    before = compare(collected, config, f.at(22))
    assert all(m['profit_yen'] is None for m in before['models'].values())
    after = compare(collected, config, f.at(26))
    assert all(m['profit_yen'] == 550 for m in after['models'].values())


def test_missing_publication_marker_replay_uses_current_time(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        record = settle(collected, decision['id'], f.payout())
        record['recorded_at'] = None
        with collected.db:
            collected.db.execute('UPDATE settlements SET body=? WHERE decision_id=?',
                                 (canonical(record).decode(), decision['id']))
    assert all(m['profit_yen'] is None for m in compare(collected, config, f.at(22))['models'].values())
    collected.clock = lambda: f.at(25)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    assert all(m['profit_yen'] is None for m in compare(collected, config, f.at(22))['models'].values())
    assert all(m['profit_yen'] == 550 for m in compare(collected, config, f.at(26))['models'].values())


def scored_decisions(store, config, *, change=None):
    decisions = decide(store, f.RACE, f.schedule(), config)
    probabilities = {
        'p_direct': [.6, .1, .1, .1, .05, .05],
        'p_marg': [.5, .1, .1, .1, .1, .1],
        'p_ref': [.7, .06, .06, .06, .06, .06],
        'v_target': [1 / 6] * 6,
    }
    for decision in decisions:
        for i, row in enumerate(decision['diagnostics']['rows']):
            for field, values in probabilities.items():
                row[field] = values[i]
        if change:
            change(decision)
        with store.db:
            store.db.execute('UPDATE decisions SET body=? WHERE id=?',
                             (canonical(decision).decode(), decision['id']))
    return decisions


def test_distribution_scores_use_races_and_same_cohort(collected, config):
    decisions = scored_decisions(collected, config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    report = compare(collected, config, f.at(22))
    scores = report['prediction_scores']
    assert scores['cohort'] == [f.RACE] and scores['scored_race_count'] == 1
    assert scores['models']['direct']['mean_brier'] == pytest.approx(.195)
    assert scores['models']['marginal']['mean_brier'] == pytest.approx(.3)
    assert scores['models']['reference']['mean_brier'] == pytest.approx(.108)
    assert scores['models']['market']['mean_brier'] == pytest.approx(5 / 6)
    assert scores['models']['reference']['mean_log_loss'] == pytest.approx(-math.log(.7))
    assert all(m['race_count'] == 1 for m in scores['models'].values())
    assert not report['profitability_verified']
    before = compare(collected, config, f.at(20))['prediction_scores']
    assert before['scored_race_count'] == 0
    assert before['unscored_reason_counts'] == {'PAYOUT_UNAVAILABLE': 1}
    assert all(m['mean_brier'] is None and m['mean_log_loss'] is None for m in before['models'].values())


def test_no_bet_still_scores_complete_prediction(collected, config):
    config = {**config, 'daily_stake_limit_yen_per_model': 0}
    decisions = scored_decisions(collected, config)
    assert all(d['status'] == 'NO_BET' for d in decisions)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    report = compare(collected, config, f.at(22))
    assert report['prediction_scores']['scored_race_count'] == 1
    assert all(m['bet_count'] == 0 and m['roi'] is None for m in report['models'].values())


@pytest.mark.parametrize('payout,reason', [
    ({'void': True}, 'NON_ORDINARY_OUTCOME'),
    ({'special_payouts': [{'market': 'quinella', 'payout_per_100': 70}], 'tickets': []}, 'NON_ORDINARY_OUTCOME'),
    ({'tickets': [{'market': 'quinella', 'selection': '1-2', 'refund_per_100': 100}]}, 'NON_ORDINARY_OUTCOME'),
    ({'runners': {'4': {'status': 'CANCELLED_BEFORE_SALES'}}}, 'NON_ORDINARY_OUTCOME'),
    ({'tickets': []}, 'SINGLE_OUTCOME_UNAVAILABLE'),
    ({'tickets': [{'market': 'quinella', 'selection': s, 'payout_per_100': 200}
                 for s in ['1-2', '1-3']]}, 'SINGLE_OUTCOME_UNAVAILABLE'),
])
def test_exceptional_outcomes_remain_in_profit_cohort_without_forced_score(collected, config, payout, reason):
    decisions = scored_decisions(collected, config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout(**payout))
    report = compare(collected, config, f.at(22))
    assert report['race_count'] == 1
    assert all(m['race_count'] == 1 and m['complete'] for m in report['models'].values())
    assert report['prediction_scores']['cohort'] == []
    assert report['prediction_scores']['unscored_reason_counts'] == {reason: 1}
    if payout.get('special_payouts'):
        for summary in report['models'].values():
            assert summary['payout_yen'] == summary['entries'][0]['special_payout_yen'] == 70
            assert summary['profit_yen'] == -30
            assert summary['hit_race_count'] == summary['hit_ticket_count'] == 0


@pytest.mark.parametrize('change,reason', [
    (lambda d: d['diagnostics']['rows'].pop(), 'PREDICTION_SUPPORT_INVALID'),
    (lambda d: d['diagnostics'].update(rows=None), 'PREDICTION_SUPPORT_INVALID'),
    (lambda d: d['diagnostics']['rows'][0].pop('selection'), 'PREDICTION_SUPPORT_INVALID'),
    (lambda d: d['diagnostics']['rows'].__setitem__(0, None), 'PREDICTION_SUPPORT_INVALID'),
    (lambda d: d['diagnostics']['rows'][0].update(p_direct=None), 'PREDICTION_PROBABILITIES_INVALID'),
    (lambda d: d['diagnostics']['rows'][0].update(p_ref=.5), 'PREDICTION_PROBABILITIES_INVALID'),
])
def test_invalid_distribution_never_changes_probability_score_cohort_by_model(collected, config, change, reason):
    decisions = scored_decisions(collected, config, change=change)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    report = compare(collected, config, f.at(22))
    assert report['prediction_scores']['unscored_reason_counts'] == {reason: 1}
    assert all(m['race_count'] == 0 for m in report['prediction_scores']['models'].values())
    assert all(m['complete'] for m in report['models'].values())


def test_zero_outcome_probability_keeps_infinite_loss_explicit(collected, config):
    def change(d):
        rows = d['diagnostics']['rows']
        rows[0]['p_ref'], rows[1]['p_ref'] = 0., .76
    decisions = scored_decisions(collected, config, change=change)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    report = compare(collected, config, f.at(22))
    model = report['prediction_scores']['models']['reference']
    assert model['log_loss_status'] == 'INFINITE' and model['mean_log_loss'] is None
    assert model['zero_outcome_probability_count'] == 1 and model['race_count'] == 1
    assert report['prediction_scores']['models']['direct']['log_loss_status'] == 'FINITE'
    canonical(report)  # No nonstandard JSON Infinity/NaN and no probability clipping.


def test_result_correction_scores_only_after_publication(collected, config):
    decisions = scored_decisions(collected, config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    original = compare(collected, config, f.at(22))['prediction_scores']
    collected.clock = lambda: f.at(25)
    for decision in decisions:
        settle(collected, decision['id'], f.payout('correction', tickets=[
            {'market': 'quinella', 'selection': '1-3', 'payout_per_100': 500}]))
    assert compare(collected, config, f.at(22))['prediction_scores'] == original
    corrected = compare(collected, config, f.at(26))['prediction_scores']
    assert corrected['models']['reference']['mean_log_loss'] == pytest.approx(-math.log(.06))
    assert corrected['cohort'] == original['cohort']


def test_different_settlement_revisions_do_not_produce_mismatched_scores(collected, config):
    decisions = scored_decisions(collected, config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        settle(collected, decision['id'], f.payout())
    collected.clock = lambda: f.at(25)
    settle(collected, decisions[0]['id'], f.payout('correction'))
    report = compare(collected, config, f.at(26))
    assert report['prediction_scores']['unscored_reason_counts'] == {'PAYOUT_EVIDENCE_DIFFERS': 1}
