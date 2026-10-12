"""Day-trade universe: liquid, optionable US names grouped by theme.

The theme drives the sector-cluster filter (3+ names from one theme firing
on the same side). Edit freely; a ticker should appear under one theme only.
"""

THEMES: dict[str, list[str]] = {
    "mega_tech": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX", "ORCL", "CRM",
        "ADBE", "IBM", "INTU", "NOW", "UBER", "SHOP", "PLTR", "ABNB", "DASH", "SPOT",
    ],
    "semis": [
        "AMD", "AVGO", "QCOM", "INTC", "MU", "MRVL", "ARM", "TXN", "AMAT", "LRCX",
        "KLAC", "ADI", "NXPI", "ON", "MCHP", "SMCI", "TSM", "ASML", "ALAB", "CRDO",
        "WOLF", "SWKS", "MPWR", "TER", "ENTG", "FORM", "AMKR",
    ],
    "optical_networking": [
        "COHR", "LITE", "AAOI", "CIEN", "VIAV", "ANET", "CSCO", "JNPR", "FFIV", "CALX",
        "INFN", "HPE", "DELL", "NTAP", "PSTG", "STX", "WDC",
    ],
    "software_cloud": [
        "SNOW", "DDOG", "MDB", "NET", "CRWD", "ZS", "OKTA", "PANW", "FTNT", "S",
        "PATH", "TWLO", "ZM", "DOCU", "HUBS", "TEAM", "WDAY", "U", "FSLY", "ESTC",
        "GTLB", "CFLT", "ZETA", "APP", "TTD", "RBLX", "SNAP", "PINS", "RDDT", "ADSK",
        "SNPS", "CDNS", "FICO", "ACN",
    ],
    "crypto": [
        "MSTR", "COIN", "HOOD", "MARA", "RIOT", "CLSK", "CIFR", "IREN", "HUT", "WULF",
        "BTDR", "CORZ", "BITF", "GLXY", "CRCL", "APLD",
    ],
    "quantum_speculative": [
        "IONQ", "RGTI", "QBTS", "QUBT", "SOUN", "BBAI", "AI", "UPST", "AFRM", "SOFI",
        "GME", "AMC", "BB", "CVNA", "OPEN",
    ],
    "space_defense": [
        "ASTS", "RKLB", "LUNR", "RDW", "PL", "SPCE", "KTOS", "AVAV", "LMT", "RTX",
        "NOC", "GD", "BA", "JOBY", "ACHR", "SPCX",
    ],
    "energy_power": [
        "VST", "CEG", "NRG", "TLN", "OKLO", "SMR", "NNE", "BE", "FCEL", "PLUG",
        "ENPH", "FSLR", "RUN", "XOM", "CVX", "OXY", "COP", "MPC", "VLO", "PSX",
        "SLB", "HAL", "GEV", "ETN", "VRT",
    ],
    "biotech_health": [
        "MRNA", "NVAX", "PFE", "LLY", "NVO", "ABBV", "MRK", "BMY", "GILD", "AMGN",
        "VRTX", "REGN", "BIIB", "IOVA", "IBRX", "CRSP", "NTLA", "BEAM", "SMMT", "NKTR",
        "INSM", "UTHR", "TEM", "HIMS", "ALHC", "CVS", "UNH", "ELV", "CI", "HUM",
        "GRAL", "VKTX", "QURE", "CNC",
    ],
    "consumer_retail": [
        "WMT", "COST", "TGT", "HD", "LOW", "NKE", "LULU", "AEO", "GAP", "ANF",
        "M", "BURL", "TJX", "ROST", "WRBY", "CMG", "SBUX", "MCD", "DPZ", "PM",
        "MO", "EL", "ELF", "CELH", "DKNG", "PENN", "CCL", "RCL", "NCLH", "DAL",
        "UAL", "AAL", "LUV", "BKNG", "EXPE", "MAR",
    ],
    "financials": [
        "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "AXP", "V", "MA",
        "PYPL", "XYZ", "NU", "ALLY", "KKR", "BX", "APO",
    ],
    "auto_ev": [
        "RIVN", "LCID", "NIO", "XPEV", "LI", "F", "GM", "STLA", "QS",
    ],
    "telecom_media": [
        "T", "VZ", "TMUS", "CMCSA", "CHTR", "DIS", "WBD", "PARA", "ROKU",
    ],
    "materials_industrial": [
        "CLF", "X", "NUE", "AA", "FCX", "MP", "CAT", "DE", "GE", "HON",
        "UNP", "FDX", "UPS", "CTVA",
    ],
    "china_adr": [
        "BABA", "PDD", "JD", "BIDU", "TME", "FUTU",
    ],
}

TICKER_THEME: dict[str, str] = {
    ticker: theme for theme, tickers in THEMES.items() for ticker in tickers
}

UNIVERSE: list[str] = sorted(TICKER_THEME)


def theme_of(ticker: str) -> str:
    return TICKER_THEME.get(ticker.upper(), "other")
