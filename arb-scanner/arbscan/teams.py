"""NFL team identities and the codes/names each venue uses for them."""
from __future__ import annotations

# Canonical abbreviation -> (city, nickname)
TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona", "Cardinals"), "ATL": ("Atlanta", "Falcons"),
    "BAL": ("Baltimore", "Ravens"), "BUF": ("Buffalo", "Bills"),
    "CAR": ("Carolina", "Panthers"), "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"), "CLE": ("Cleveland", "Browns"),
    "DAL": ("Dallas", "Cowboys"), "DEN": ("Denver", "Broncos"),
    "DET": ("Detroit", "Lions"), "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"), "IND": ("Indianapolis", "Colts"),
    "JAX": ("Jacksonville", "Jaguars"), "KC": ("Kansas City", "Chiefs"),
    "LAC": ("Los Angeles", "Chargers"), "LAR": ("Los Angeles", "Rams"),
    "LV": ("Las Vegas", "Raiders"), "MIA": ("Miami", "Dolphins"),
    "MIN": ("Minnesota", "Vikings"), "NE": ("New England", "Patriots"),
    "NO": ("New Orleans", "Saints"), "NYG": ("New York", "Giants"),
    "NYJ": ("New York", "Jets"), "PHI": ("Philadelphia", "Eagles"),
    "PIT": ("Pittsburgh", "Steelers"), "SEA": ("Seattle", "Seahawks"),
    "SF": ("San Francisco", "49ers"), "TB": ("Tampa Bay", "Buccaneers"),
    "TEN": ("Tennessee", "Titans"), "WAS": ("Washington", "Commanders"),
}
ALIASES = {"JAC": "JAX", "LA": "LAR", "WSH": "WAS", "LVR": "LV", "OAK": "LV"}
_NICK = {nick.lower(): abbr for abbr, (_, nick) in TEAMS.items()}
# Kalshi titles use the city ("Kansas City wins by over 7.5 points?").
_CITY = {city.lower(): abbr for abbr, (city, _) in TEAMS.items()
         if city not in ("Los Angeles", "New York")}


def from_code(code: str) -> str | None:
    code = ALIASES.get(code.upper(), code.upper())
    return code if code in TEAMS else None


def from_name(name: str) -> str | None:
    """'Vikings', 'Minnesota Vikings', 'Minnesota' -> 'MIN'."""
    words = name.lower().replace(".", "").split()
    for w in reversed(words):
        if w in _NICK:
            return _NICK[w]
    low = name.lower().strip()
    for city, abbr in _CITY.items():
        if low.startswith(city):
            return abbr
    return None


def nickname(abbr: str) -> str:
    return TEAMS[abbr][1]
