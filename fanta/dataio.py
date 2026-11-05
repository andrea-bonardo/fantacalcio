"""Caricamento e normalizzazione dei dataset JSON.

Il programma lavora con tre file principali, tutti generati dal comando
``update``:

- ``data/giocatori_2627.json``: quotazioni, statistiche e titolarità;
- ``data/serie_a_2627.json``: classifica e calendario della Serie A;
- ``data/impostazioni.json``: bonus, malus, modificatore e moduli.

I file possono essere modificati a mano: il caricatore accetta sia le chiavi
italiane usate dal programma sia quelle inglesi più comuni.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any, Iterable
import sys
import time 

from .config import (
    CODE_TO_NAME,
    DEFAULT_SETTINGS,
    ROLE_ORDER,
    SETTINGS_ALIASES,
    TEAM_NAME_TO_CODE,
)
from .models import (
    Fixture,
    LeagueSettings,
    Player,
    SerieAData,
    SerieATeam,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
PLAYERS_FILE = "giocatori_2627.json"
SERIE_A_FILE = "serie_a_2627.json"
SETTINGS_FILE = "impostazioni.json"
LEAGUE_FILE = "lega_demo.json"


class DataError(RuntimeError):
    """Errore nella lettura o nella struttura dei dati."""


# ---------------------------------------------------------------------------
# Utility di base
# ---------------------------------------------------------------------------


def _first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(" ", "")
    if not text or text in {"-", "—", "N/D", "nd"}:
        return default
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


def _to_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    result = _to_float(value, default=float("nan"))
    if result != result:  # NaN
        return None
    return result


def _to_int(value: Any, default: int = 0) -> int:
    number = _to_optional_float(value)
    return int(round(number)) if number is not None else default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "si", "sì", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def normalize_team(value: Any) -> str:
    """Converte nome, slug o codice di una squadra nel codice a 3 lettere."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in CODE_TO_NAME:
        return upper
    return TEAM_NAME_TO_CODE.get(text.lower(), TEAM_NAME_TO_CODE.get(upper.lower(), upper))


def normalize_role(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().upper()
    aliases = {
        "PORTIERE": "P",
        "GOALKEEPER": "P",
        "GK": "P",
        "DIFENSORE": "D",
        "DEFENDER": "D",
        "CENTROCAMPISTA": "C",
        "MIDFIELDER": "C",
        "ATTACCANTE": "A",
        "FORWARD": "A",
        "STRIKER": "A",
    }
    if text in aliases:
        return aliases[text]
    if text[:1] in ROLE_ORDER:
        return text[:1]
    return ""


def normalize_status(value: Any) -> str:
    if value is None:
        return "ok"
    text = str(value).strip().lower()
    aliases = {
        "ok": "ok",
        "disponibile": "ok",
        "available": "ok",
        "infortunato": "infortunato",
        "injured": "infortunato",
        "squalificato": "squalificato",
        "suspended": "squalificato",
        "diffidato": "diffidato",
        "in dubbio": "in dubbio",
        "dubbio": "in dubbio",
        "doubtful": "in dubbio",
    }
    return aliases.get(text, text or "ok")


def _normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is None:
        return DEFAULT_DATA_DIR
    return Path(data_dir).expanduser().resolve()


def read_json(path_or_url: str | Path) -> Any:
    """Legge un JSON locale oppure remoto (solo URL http/https)."""
    text = str(path_or_url)
    if text.startswith(("http://", "https://")):
        from .updater import http_get

        return json.loads(http_get(text))
    path = Path(text).expanduser()
    if not path.exists():
        raise DataError(f"File JSON non trovato: {path}")
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path | str, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# ---------------------------------------------------------------------------
# Giocatori
# ---------------------------------------------------------------------------


def parse_player(raw: dict[str, Any]) -> Player:
    player_id = str(_first(raw, "id", "player_id", "codice", default="")).strip()
    name = str(_first(raw, "nome", "name", "giocatore", default="")).strip()
    team = normalize_team(_first(raw, "squadra", "team", "club", "squadra_serie_a"))
    role = normalize_role(_first(raw, "ruolo", "role", "posizione"))
    if not player_id:
        player_id = f"{team}-{_normalize_key(name)}"
    if not name:
        raise DataError(f"Giocatore senza nome: {raw!r}")
    if not team:
        raise DataError(f"Giocatore {name!r} senza squadra")
    if not role:
        raise DataError(f"Giocatore {name!r} senza ruolo")

    appearances = _to_int(_first(raw, "presenze", "appearances", "pg", default=0))
    media_voto = _to_optional_float(_first(raw, "media_voto", "media voto", "mv", "average_vote"))
    if media_voto is not None and media_voto <= 0:
        media_voto = None
    fantamedia = _to_optional_float(_first(raw, "fantamedia", "mfv", "fantasy_average"))
    if fantamedia is not None and fantamedia <= 0:
        fantamedia = None

    starter_prob = _to_float(
        _first(raw, "titolarita", "titolarità", "starter_prob", "probabilita_titolarita", default=0.0)
    )
    if starter_prob > 1.0:
        starter_prob /= 100.0
    starter_prob = max(0.0, min(1.0, starter_prob))

    mantra = _first(raw, "ruoli_mantra", "mantra", "mantra_roles", default=[])
    if isinstance(mantra, str):
        mantra = [x.strip() for x in mantra.replace(";", "|").replace(",", "|").split("|") if x.strip()]
    elif not isinstance(mantra, list):
        mantra = []

    return Player(
        id=player_id,
        name=name,
        team=team,
        role=role,
        mantra=[str(x).strip() for x in mantra if str(x).strip()],
        price=_to_float(_first(raw, "quotazione", "price", "costo", default=0.0)),
        initial_price=_to_float(
            _first(raw, "quotazione_iniziale", "initial_price", "prezzo_iniziale", default=0.0)
        ),
        fvm=_to_float(_first(raw, "fvm", "FVM", default=0.0)),
        appearances=appearances,
        media_voto=media_voto,
        fantamedia=fantamedia,
        goals=_to_int(_first(raw, "gol", "goals", "gf", default=0)),
        assists=_to_int(_first(raw, "assist", "assists", default=0)),
        goals_conceded=_to_int(_first(raw, "gol_subiti", "goals_conceded", "gs", default=0)),
        penalties_scored=_to_int(_first(raw, "rigori_segnati", "penalties_scored", default=0)),
        penalties_attempted=_to_int(_first(raw, "rigori_tirati", "penalties_attempted", default=0)),
        penalties_saved=_to_int(_first(raw, "rigori_parati", "penalties_saved", default=0)),
        yellow_cards=_to_int(_first(raw, "ammonizioni", "yellow_cards", "amm", default=0)),
        red_cards=_to_int(_first(raw, "espulsioni", "red_cards", "esp", default=0)),
        starter_prob=starter_prob,
        status=normalize_status(_first(raw, "stato", "status", default="ok")),
        note=str(_first(raw, "nota", "note", "notes", default="") or ""),
    )


def load_players(
    path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> tuple[dict[str, Player], dict[str, Any]]:
    if path is None:
        path = resolve_data_dir(data_dir) / PLAYERS_FILE
    raw = read_json(path)
    if isinstance(raw, list):
        items = raw
        metadata: dict[str, Any] = {}
    elif isinstance(raw, dict):
        items = _first(raw, "giocatori", "players", "calciatori", default=[])
        metadata = {k: v for k, v in raw.items() if k not in {"giocatori", "players", "calciatori"}}
    else:
        raise DataError(f"Struttura giocatori non riconosciuta in {path}")
    if not isinstance(items, list) or not items:
        raise DataError(f"Nessun giocatore trovato in {path}")
    players: dict[str, Player] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        player = parse_player(item)
        players[player.id] = player
    if not players:
        raise DataError(f"Nessun giocatore valido in {path}")
    return players, metadata


# ---------------------------------------------------------------------------
# Serie A
# ---------------------------------------------------------------------------


def parse_serie_a_team(raw: dict[str, Any]) -> SerieATeam:
    code = normalize_team(_first(raw, "codice", "code", "squadra", "team", "nome"))
    if code not in CODE_TO_NAME:
        raise DataError(f"Squadra di Serie A non riconosciuta: {raw!r}")
    name = str(_first(raw, "nome", "name", default=CODE_TO_NAME[code]))
    slug = str(_first(raw, "slug", default=code.lower()))
    played = _to_int(_first(raw, "giocate", "played", "pg", default=0))
    points = _to_int(_first(raw, "punti", "points", "pt", default=0))
    gf = _to_int(_first(raw, "gf", "goals_for", "fatti", default=0))
    ga = _to_int(_first(raw, "gs", "goals_against", "subiti", default=0))
    goal_diff = _to_int(_first(raw, "dr", "goal_diff", "differenza_reti", default=gf - ga))
    return SerieATeam(
        code=code,
        name=name,
        slug=slug,
        position=_to_int(_first(raw, "posizione", "position", "rank", default=0)),
        points=points,
        played=played,
        wins=_to_int(_first(raw, "vittorie", "wins", "v", default=0)),
        draws=_to_int(_first(raw, "pareggi", "draws", "n", default=0)),
        losses=_to_int(_first(raw, "sconfitte", "losses", "p", default=0)),
        gf=gf,
        ga=ga,
        goal_diff=goal_diff,
    )


def parse_fixture(raw: dict[str, Any]) -> Fixture:
    home = normalize_team(_first(raw, "casa", "home", "squadra_casa"))
    away = normalize_team(_first(raw, "trasferta", "away", "squadra_trasferta"))
    if home not in CODE_TO_NAME or away not in CODE_TO_NAME:
        raise DataError(f"Partita con squadre non riconosciute: {raw!r}")
    home_goals = _to_optional_float(_first(raw, "gol_casa", "home_goals", "score_home"))
    away_goals = _to_optional_float(_first(raw, "gol_trasferta", "away_goals", "score_away"))
    return Fixture(
        matchday=_to_int(_first(raw, "giornata", "matchday", "round", default=0)),
        home=home,
        away=away,
        date=_first(raw, "data", "date"),
        time=_first(raw, "ora", "time", "kickoff"),
        home_goals=int(round(home_goals)) if home_goals is not None else None,
        away_goals=int(round(away_goals)) if away_goals is not None else None,
    )


def load_serie_a(
    path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> SerieAData:
    if path is None:
        path = resolve_data_dir(data_dir) / SERIE_A_FILE
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise DataError(f"Struttura Serie A non riconosciuta in {path}")
    teams_raw = _first(raw, "squadre", "teams", "classifica", default=[])
    fixtures_raw = _first(raw, "partite", "fixtures", "calendario", default=[])
    if isinstance(teams_raw, dict):
        teams_raw = [
            {"codice": code, **value} if isinstance(value, dict) else {"codice": code}
            for code, value in teams_raw.items()
        ]
    teams: dict[str, SerieATeam] = {}
    for item in teams_raw:
        if isinstance(item, dict):
            team = parse_serie_a_team(item)
            teams[team.code] = team
    if not teams:
        raise DataError(f"Nessuna squadra di Serie A trovata in {path}")

    fixtures: list[Fixture] = []
    for item in fixtures_raw:
        if isinstance(item, dict):
            fixture = parse_fixture(item)
            if fixture.matchday > 0:
                fixtures.append(fixture)
    current_matchday = _to_int(
        _first(raw, "giornata_corrente", "current_matchday", "matchday", default=0)
    )
    if current_matchday <= 0:
        played = [f.matchday for f in fixtures if f.played]
        current_matchday = (max(played) + 1) if played else 1
    return SerieAData(
        season=str(_first(raw, "stagione", "season", default="2026-27")),
        updated=str(_first(raw, "aggiornato", "updated", default="")),
        teams=teams,
        fixtures=fixtures,
        current_matchday=current_matchday,
        source_standings=str(_first(raw, "fonte_classifica", "source_standings", default="")),
        source_fixtures=str(_first(raw, "fonte_calendario", "source_fixtures", default="")),
    )


# ---------------------------------------------------------------------------
# Impostazioni
# ---------------------------------------------------------------------------


def parse_modifier_table(value: Any) -> list[list[float]]:
    if not value:
        return [list(row) for row in DEFAULT_SETTINGS["tabella_modificatore"]]
    table: list[list[float]] = []
    if isinstance(value, dict):
        # formato {"6.0": 1, "6.5": 3} oppure {"min": 6.0, "max": 6.49, "value": 1}
        if {"min", "max", "value"} <= set(value):
            table.append([_to_float(value["min"]), _to_float(value["max"]), _to_float(value["value"])])
        else:
            for key, bonus in value.items():
                threshold = _to_optional_float(key)
                if threshold is None:
                    continue
                table.append([threshold, threshold + 0.49, _to_float(bonus)])
    elif isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                if {"min", "max", "value"} <= set(row):
                    table.append(
                        [_to_float(row["min"]), _to_float(row["max"]), _to_float(row["value"])]
                    )
                elif {"soglia", "bonus"} <= set(row):
                    threshold = _to_float(row["soglia"])
                    table.append([threshold, threshold + 0.49, _to_float(row["bonus"])])
            elif isinstance(row, (list, tuple)):
                if len(row) >= 3:
                    table.append([_to_float(row[0]), _to_float(row[1]), _to_float(row[2])])
                elif len(row) == 2:
                    threshold = _to_float(row[0])
                    table.append([threshold, threshold + 0.49, _to_float(row[1])])
    if not table:
        return [list(row) for row in DEFAULT_SETTINGS["tabella_modificatore"]]
    table.sort(key=lambda row: row[0])
    return table


def _settings_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    # alcuni export incapsulano le impostazioni in una chiave
    for key in ("impostazioni", "settings", "regole", "rules"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            return nested
    return raw


def merge_settings(base: LeagueSettings, overrides: dict[str, Any] | None) -> LeagueSettings:
    settings = base.clone()
    if not overrides:
        return settings
    normalized: dict[str, Any] = {}
    for key, value in overrides.items():
        canonical = SETTINGS_ALIASES.get(key, key)
        normalized[canonical] = value
    for key, value in normalized.items():
        if not hasattr(settings, key):
            continue
        current = getattr(settings, key)
        if key == "tabella_modificatore":
            setattr(settings, key, parse_modifier_table(value))
        elif key == "moduli":
            if isinstance(value, str):
                value = [x.strip() for x in value.split(",") if x.strip()]
            if isinstance(value, list):
                setattr(settings, key, [str(x).strip() for x in value if str(x).strip()])
        elif key == "giornata":
            matchday = _to_int(value, 0)
            setattr(settings, key, matchday if matchday > 0 else None)
        elif isinstance(current, bool):
            setattr(settings, key, _to_bool(value, current))
        elif isinstance(current, int) and not isinstance(current, bool):
            setattr(settings, key, _to_int(value, current))
        elif isinstance(current, float):
            setattr(settings, key, _to_float(value, current))
        else:
            setattr(settings, key, value)
    return settings


def load_settings(
    path: Path | str | None = None,
    data_dir: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> LeagueSettings:
    if path is None:
        path = resolve_data_dir(data_dir) / SETTINGS_FILE
    raw: Any = {}
    if Path(path).exists():
        raw = read_json(path)
    base = merge_settings(LeagueSettings(), _settings_payload(raw))
    if overrides:
        base = merge_settings(base, overrides)
    return base


# ---------------------------------------------------------------------------
# Controllo presenza dati
# ---------------------------------------------------------------------------


def missing_data_files(data_dir: Path | str | None = None) -> list[Path]:
    directory = resolve_data_dir(data_dir)
    return [
        directory / name
        for name in (PLAYERS_FILE, SERIE_A_FILE)
        if not (directory / name).exists()
    ]


MAX_DATA_AGE_HOURS = 3


def data_age_hours(data_dir: Path | str | None = None) -> float | None:
    """Ore trascorse dall'ultimo aggiornamento dei dati (None se il file non esiste)."""
    path = resolve_data_dir(data_dir) / PLAYERS_FILE
    try:
        return (time.time() - path.stat().st_mtime) / 3600
    except OSError:
        return None


def ensure_data(
    data_dir: Path | str | None = None,
    *,
    auto_update: bool = True,
    max_age_hours: float = MAX_DATA_AGE_HOURS,
) -> Path:
    directory = resolve_data_dir(data_dir)
    missing = missing_data_files(directory)
    if not missing:
        age = data_age_hours(directory)
        if auto_update and max_age_hours and age is not None and age >= max_age_hours:
            print(f"Dati vecchi di {age:.0f} ore: li aggiorno...")
            from .updater import update_data

            try:
                update_data(directory, verbose=False)
            except Exception as exc:  # noqa: BLE001 - si continua con i dati vecchi
                print(f"Aggiornamento non riuscito ({exc}): uso i dati precedenti.", file=sys.stderr)
        return directory
    if not auto_update:
        names = ", ".join(path.name for path in missing)
        raise DataError(
            f"Dati mancanti in {directory}: {names}. Esegui `python fantacalcio.py update`."
        )
    print("Dati non trovati: scarico i dataset aggiornati...")
    from .updater import update_data

    update_data(directory, verbose=True)
    return directory

# ---------------------------------------------------------------------------
# Serializzazione per l'output JSON
# ---------------------------------------------------------------------------


def player_to_dict(player: Player) -> dict[str, Any]:
    return {
        "id": player.id,
        "nome": player.name,
        "squadra": player.team,
        "ruolo": player.role,
        "quotazione": player.price,
        "fvm": player.fvm,
        "presenze": player.appearances,
        "media_voto": player.media_voto,
        "fantamedia": player.fantamedia,
        "gol": player.goals,
        "assist": player.assists,
        "gol_subiti": player.goals_conceded,
        "titolarita": player.starter_prob,
        "stato": player.status,
    }


def players_to_dict(players: Iterable[Player]) -> list[dict[str, Any]]:
    return [player_to_dict(player) for player in players]
