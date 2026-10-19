"""The seventeen signal paper books. Each is its own £200 database.

Five of them tilt the volatility trend. Twelve are long-or-cash timing rules.
The names are the variant ids. Nothing here fetches the network.
"""

TILT = {
    "signal_engine": "baseline",
    "signal_purge": "purge",
    "signal_equal": "equal",
    "signal_multitest": "multitest",
    "signal_news": "news",
}
REGIME = {
    "signal_funding_contrarian": "funding_contrarian",
    "signal_funding_carry": "funding_carry",
    "signal_basis": "basis_contrarian",
    "signal_dvol": "dvol_fear",
    "signal_vrp": "variance_premium",
    "signal_stablecoin": "stablecoin",
    "signal_macro": "macro",
    "signal_tsmom": "tsmom",
    "signal_gold": "gold",
    "signal_onchain": "onchain",
    "signal_gdelt": "gdelt",
    "signal_dominance": "dominance",
}
ORDER = tuple(TILT) + tuple(REGIME)


def is_signal(name):
    return name in TILT or name in REGIME


def kind(name):
    """'tilt' or 'regime', and the mode that book uses. (None, None) otherwise."""
    if name in TILT:
        return "tilt", TILT[name]
    if name in REGIME:
        return "regime", REGIME[name]
    return None, None
