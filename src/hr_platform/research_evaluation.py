"""Evaluate frozen research purchases, using published evidence only. No I/O."""
import math

from .evaluation import purchase_summary
from .official_payout import FinishPage, parse_payout_page
from .paper_rules import settlement_values
from .payout_check import parse_result_page
from .race_state import compact

VERSION = 'research-cash-final-prices-v1'


def qualified_payout(raw, evidence, race_id, horses, markets, place_places):
    """Extend the existing refund gate only for consistent ordinary finishes.

    Lower ties cannot affect top-three events. Unsupported exceptions retain
    only the existing quinella/trio qualification; missing winners stay pending.
    """
    from .model import key
    from .research_ticket_events import winning_selections
    if not evidence or evidence.get('status') != 'PAYOUT_QUALIFIED' or not evidence.get('final'):
        return {}, 'PAYOUT_UNAVAILABLE_OR_UNQUALIFIED'
    if evidence.get('source_kind') != 'OFFICIAL' or evidence['race_id'] != race_id:
        raise ValueError('PAYOUT_SOURCE_OR_RACE')
    gate = parse_payout_page(raw, race_id)
    if set(gate['runners']) != set(horses):
        return {}, 'PAYOUT_ROSTER_CONFLICT'
    if any(r['status'] != 'FINISHED' for r in gate['runners'].values()):
        return gate, 'OFFICIAL_QUINELLA_TRIO_EXCEPTION_SCOPE'
    frames = {int(h): int(v['frame']) for h, v in horses.items()}
    page = FinishPage()
    page.feed(raw.decode('utf-8-sig', errors='strict'))
    for row in page.grade_rows[1:]:
        frame, horse = (compact(row[i]['text']) for i in (1, 2))
        if not frame.isdecimal() or int(frame) != frames[int(horse)]:
            return {}, 'PAYOUT_FRAME_CONFLICT'
    ranks = {int(v['finish_label']): int(h) for h, v in gate['runners'].items()}
    if not {1, 2, 3} <= set(ranks):
        return {}, 'PAYOUT_TOP_THREE_UNAVAILABLE'
    state = tuple(ranks[i] for i in (1, 2, 3))
    published = parse_result_page(raw, race_id)
    complete = []
    for market in markets:
        expected = {key(t) for t in winning_selections(state, market, frames=frames, place_places=place_places)}
        if {s for m, s in published if m == market} == expected:
            complete.append(market)
    return {**gate, 'complete_markets': complete,
            'tickets': [{'market': m, 'selection': s, 'payout_per_100': value}
                        for (m, s), value in published.items() if m in complete],
            'scope': 'OFFICIAL_ORDINARY_ALL_FINISHED_MATCHING_MARKETS'}, 'OFFICIAL_ORDINARY_ALL_FINISHED'


def quote_interval(quote):
    """A range stays two bounds; unavailable or zero prices stay null."""
    if not isinstance(quote, dict):
        return None, None
    low, high = quote.get('odds'), quote.get('odds_max')
    def valid(x):
        return type(x) in {int, float} and math.isfinite(x) and x >= 1
    if quote.get('display_status') == 'FIXED' and valid(low) and high is None:
        return low, low
    if quote.get('display_status') == 'RANGE' and valid(low) and valid(high) and low <= high:
        return low, high
    return None, None


def evaluate_candidates(result, saved, base, payout, payout_basis, final_prices):
    """Keep every registered strategy; pending money never becomes a loss/zero."""
    evaluations = {}
    for name, choice in result['output']['candidates'].items():
        entries, seen = [], set()
        for ticket in choice['tickets']:
            market, selection, stake = ticket.get('market', base['target']), ticket['selection'], ticket['stake_yen']
            if ((market, selection) in seen or type(stake) is not int or stake <= 0 or stake % 100):
                raise ValueError('RESEARCH_PURCHASE_INVALID')
            seen.add((market, selection))
            record = saved['view']['markets'].get(market)
            quotes = record['content']['quotes'] if record else {}
            low, high = quote_interval(quotes.get(selection))
            if low is None:
                raise ValueError('RESEARCH_PURCHASE_QUOTE')
            final = final_prices['markets'].get(market)
            final_quotes = final['content']['quotes'] if final else {}
            final_low, final_high = (quote_interval(final_quotes.get(selection))
                                     if set(final_quotes) == set(quotes) else (None, None))
            entry = {'race_id': result['race_id'], 'target': market, 'selection': selection, 'stake_yen': stake,
                'purchase_odds': low, 'purchase_odds_upper': high, 'final_odds': final_low,
                'final_odds_upper': final_high, 'purchase_observation_id': record['observation_id'],
                'purchase_received_at': record['received_at'], 'purchase_available_at': record['available_at'],
                'final_parse_id': final['parse_id'] if final else None,
                'final_odds_reason': None if final_low is not None else 'FINAL_SUPPORT_OR_PRICE_UNAVAILABLE',
                'payout_basis': payout_basis}
            # Cancellation before sales must not become an ordinary losing bet.
            sold = market.startswith('bracket') or all(
                h in payout.get('runners', {}) and payout['runners'][h]['status'] != 'CANCELLED_BEFORE_SALES'
                for h in selection.split('-'))
            entry.update(settlement_values(entry, payout if sold else {}))
            entries.append(entry)
        stake = sum(e['stake_yen'] for e in entries)
        if stake != choice['stake_yen']:
            raise ValueError('RESEARCH_PURCHASE_TOTAL')
        pending = any(e['status'] == 'PENDING' for e in entries)
        summary = purchase_summary(entries)
        upper = purchase_summary([{**e, 'purchase_odds': e['purchase_odds_upper'],
                                   'final_odds': e['final_odds_upper']} for e in entries])
        cash = {field: None if pending else sum(e[field] for e in entries)
                for field in ('payout_yen', 'refund_yen', 'profit_yen')}
        excluded = choice.get('status') == 'INPUT_EXCLUDED'
        evaluations[name] = {'candidate_status': choice.get('status'), 'reason': choice.get('reason'),
            'status': 'INPUT_EXCLUDED' if excluded else 'PENDING' if pending else 'SETTLED' if stake else 'NO_BET',
            'entries': entries, 'summary': {**summary, **cash, 'stake_yen': stake,
                'eligible_race_count': int(not excluded),
                'roi': cash['profit_yen'] / stake if stake and not pending else None,
                'average_purchase_odds_upper': upper['average_purchase_odds'],
                'average_final_odds_upper': upper['average_final_odds'],
                'pending_ticket_count': sum(e['status'] == 'PENDING' for e in entries)}}
    return evaluations
