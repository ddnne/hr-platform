import copy

import numpy as np
import pytest

from hr_platform.model import key, states
from hr_platform.research_ticket_events import ticket_catalog, winning_selections


def quotes(omega, market, **kwargs):
    support = {key(t) for s in omega for t in winning_selections(s, market, **kwargs)}
    return {s: {'odds': 2.0, 'odds_max': 4.0, 'display_status': 'RANGE'} for s in support}


def test_place_and_wide_have_multiple_simultaneous_hits():
    assert winning_selections((3, 1, 2), 'place', place_places=2) == ((3,), (1,))
    assert winning_selections((3, 1, 2), 'place') == ((3,), (1,), (2,))
    assert winning_selections((3, 1, 2), 'wide') == ((1, 2), (1, 3), (2, 3))
    omega = states([1, 2, 3, 4])
    for market, count in [('place', 2), ('wide', 3)]:
        q = quotes(omega, market, place_places=2)
        before = copy.deepcopy(q)
        _, events, lower, upper = ticket_catalog(omega, market, q, place_places=2)
        assert np.all(events.sum(axis=0) == count)
        assert np.all(lower == 2) and np.all(upper == 4)
        assert q == before


def test_frames_include_valid_same_frame_and_exclude_singleton_diagonal():
    frames = {1: 1, 2: 1, 3: 2, 4: 3}
    assert winning_selections((1, 2, 4), 'bracket_exacta', frames=frames) == ((1, 1),)
    assert winning_selections((4, 1, 2), 'bracket_quinella', frames=frames) == ((1, 3),)
    omega = states(list(frames))
    q = quotes(omega, 'bracket_quinella', frames=frames)
    for v in q.values():
        v.update(odds_max=None, display_status='FIXED')
    selections, events, _, _ = ticket_catalog(omega, 'bracket_quinella', q, frames=frames)
    assert (1, 1) in selections and (2, 2) not in selections
    assert np.all(events.sum(axis=0) == 1)
    with pytest.raises(ValueError, match='FRAME_MAPPING'):
        winning_selections((1, 2, 3), 'bracket_exacta', frames={1: 1})


def test_range_missing_quote_zero_and_invalid_maximum_are_not_imputed():
    omega = states([1, 2, 3])
    q = quotes(omega, 'wide')
    for change, expected in [(lambda d: d.pop('1-2'), 'INCOMPLETE_MARKET'),
                             (lambda d: d['1-2'].update(odds=0), 'UNUSABLE_ODDS'),
                             (lambda d: d['1-2'].update(odds_max=1), 'UNUSABLE_ODDS')]:
        bad = copy.deepcopy(q)
        change(bad)
        with pytest.raises(ValueError, match=expected):
            ticket_catalog(omega, 'wide', bad)
