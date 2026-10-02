"""Ordinary top-three ticket events for all nine single-race NAR markets."""
from itertools import combinations
import math

import numpy as np
from scipy import sparse

from .model import MODELED, ModelError, key, reference, ticket

MARKETS = MODELED | {'place', 'wide', 'bracket_quinella', 'bracket_exacta'}


def winning_selections(state, market, *, frames=None, place_places=3):
    """Return simultaneous winning selections; ordinary finishes only."""
    if market in MODELED:
        return (ticket(state, market),)
    if market == 'place':
        if place_places not in {2, 3}:
            raise ModelError('PLACE_RULE')
        return tuple((h,) for h in state[:place_places])
    if market == 'wide':
        return tuple(sorted(tuple(sorted(pair)) for pair in combinations(state, 2)))
    if market in {'bracket_quinella', 'bracket_exacta'}:
        if frames is None or any(h not in frames or type(frames[h]) is not int or not 1 <= frames[h] <= 8
                                 for h in state):
            raise ModelError('FRAME_MAPPING')
        pair = tuple(frames[h] for h in state[:2])
        return (tuple(sorted(pair)) if market == 'bracket_quinella' else pair,)
    raise ModelError('UNMODELED_MARKET')


def ticket_catalog(omega, market, quotes, *, frames=None, place_places=3):
    """Full support and conservative displayed prices; ranges stay ranges."""
    if market in MODELED:
        selections, event, _, _, lower = reference(omega, market, quotes)
        return selections, event, lower, lower.copy()
    events = [winning_selections(s, market, frames=frames, place_places=place_places) for s in omega]
    selections = sorted({selection for row in events for selection in row})
    if set(quotes) != {key(s) for s in selections}:
        raise ModelError('INCOMPLETE_MARKET')
    lookup = {s: i for i, s in enumerate(selections)}
    coords = [(lookup[s], i) for i, row in enumerate(events) for s in row]
    event = sparse.csr_matrix((np.ones(len(coords)), tuple(zip(*coords))), shape=(len(selections), len(omega)))
    lower, upper = [], []
    for selection in selections:
        quote = quotes[key(selection)]
        low, high = quote.get('odds'), quote.get('odds_max')
        def valid(v):
            return type(v) in {int, float} and math.isfinite(v) and v >= 1
        status = quote.get('display_status')
        if (not valid(low) or status not in {'FIXED', 'RANGE'}
                or status == 'RANGE' and (market not in {'place', 'wide'} or not valid(high) or high < low)
                or status == 'FIXED' and high is not None):
            raise ModelError('UNUSABLE_ODDS')
        lower.append(low)
        upper.append(high if status == 'RANGE' else low)
    return selections, event, np.asarray(lower), np.asarray(upper)
