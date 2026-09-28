import json
import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical
from hr_platform.evaluation import compare
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


def test_correction_and_late_settlement_do_not_rewrite_prior_report(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        record = settle(collected, decision['id'], f.payout())
        assert json.loads(collected.read_body(record['source_hash'], 'receipts')) == f.payout()
    original = compare(collected, config, f.at(22))
    assert all(m['profit_yen'] == 550 for m in original['models'].values())
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
