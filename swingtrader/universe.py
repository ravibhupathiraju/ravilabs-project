"""Ticker universes of increasing size.

- watchlist: the 20 tickers in watchlist.txt (editable)
- top50:     50 US mega caps
- sp100:     ~100 S&P 100 constituents (approximate, update as needed)
"""

from .data import read_watchlist

SP100 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "AVGO", "JPM",
    "LLY", "UNH", "V", "XOM", "MA", "HD", "PG", "COST", "JNJ", "ORCL",
    "ABBV", "WMT", "NFLX", "CRM", "BAC", "KO", "MRK", "CVX", "AMD", "PEP",
    "TMO", "ADBE", "CSCO", "ACN", "LIN", "MCD", "ABT", "PM", "IBM", "TXN",
    "GE", "INTU", "QCOM", "VZ", "CAT", "DIS", "AMGN", "PFE", "NOW", "ISRG",
    "SPGI", "GS", "UBER", "CMCSA", "BKNG", "NEE", "AXP", "RTX", "HON", "T",
    "LOW", "UNP", "BLK", "MS", "SCHW", "COP", "ETN", "LMT", "BMY", "BA",
    "MDT", "PLD", "AMAT", "ADP", "DE", "SBUX", "BX", "GILD", "MMC", "CB",
    "SO", "INTC", "MO", "ELV", "TJX", "CI", "DHR", "NKE", "CL", "EMR",
    "FDX", "GD", "USB", "WFC", "C", "MET", "AIG", "COF", "PYPL", "TMUS",
]

TOP50 = SP100[:50]

_LABELS = {
    "watchlist": "My watchlist",
    "top50": "Top 50 large caps",
    "sp100": "S&P 100 (approx.)",
    "tradable": "TradeLab tradable",
    "all": "All (watchlist + S&P 100 + tradable)",
}


def get_universe(name: str) -> list[str]:
    if name == "watchlist":
        return read_watchlist()
    if name == "top50":
        return list(TOP50)
    if name == "sp100":
        return list(SP100)
    if name == "tradable":  # the 151-symbol list from the TradeLab project
        return read_watchlist("tradable.txt")
    if name == "all":  # union of everything, deduped, original order kept
        seen: set[str] = set()
        combined = read_watchlist() + SP100 + read_watchlist("tradable.txt")
        return [t for t in combined if not (t in seen or seen.add(t))]
    raise ValueError(f"unknown universe: {name!r}")


def universe_options() -> list[dict]:
    """Metadata for the web UI dropdown."""
    return [
        {"key": key, "label": label, "count": len(get_universe(key))}
        for key, label in _LABELS.items()
    ]
