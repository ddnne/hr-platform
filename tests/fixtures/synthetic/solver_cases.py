"""Deterministic synthetic markets; no observed prices or race identifiers."""

import math
from hr_platform.model import states, ticket, key


def capacity_markets(runners):
    omega = states(runners)
    mass = [math.exp(-0.10*i - 0.07*j - 0.03*k + 0.4*(abs(i-j) == 1)) for i, j, k in omega]
    total = sum(mass)
    markets = {}
    for market in ("win", "exacta", "quinella"):
        probabilities = {}
        for state, value in zip(omega, mass):
            selection = key(ticket(state, market))
            probabilities[selection] = probabilities.get(selection, 0.0) + value / total
        markets[market] = {"quotes": {
            selection: {"odds": 0.75 / p, "display_status": "FIXED"}
            for selection, p in probabilities.items()
        }}
    return markets
