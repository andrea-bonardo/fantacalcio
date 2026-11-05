"""Risoluzione della lega fantacalcio e importazione delle rose.

Una lega può essere fornita in tre modi:

1. file JSON locale (formato canonico descritto nel README) o file Excel (.xlsx);
2. URL JSON pubblico;
3. rosa manuale (file di testo o elenco separato da virgole).

Gli indirizzi privati ``https://leghe.fantacalcio.it/...`` richiedono il login:
non sono leggibili da uno script senza credenziali. In quel caso il programma
spiega come esportare la rosa e continua a funzionare con JSON o rosa manuale.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from .dataio import (
    DataError,
    LEAGUE_FILE,
    load_players,
    load_serie_a,
    load_settings,
    merge_settings,
    normalize_team,
    read_json,
    resolve_data_dir,
    write_json,
)
from .models import FantasyTeam, League, LeagueSettings, Player, SerieAData
from .xlsxio import XLSX_SUFFIXES, XlsxError, xlsx_to_payload


class LeagueError(DataError):
    """Errore generico nella lettura della lega."""


class FantacalcioURLError(LeagueError):
    """URL privato di Fantacalcio.it non accessibile senza login."""


class PlayerNotFoundError(LeagueError):
    """Un giocatore della rosa non è presente nel dataset."""


FANTACALCIO_LOGIN_MESSAGE = """\
Gli indirizzi https://leghe.fantacalcio.it/... richiedono il login al sito:
non possono essere scaricati da uno script senza credenziali.

Come procedere (in ordine di preferenza):
  1. esporta la rosa della tua lega in un file JSON e passa quel file con --lega;
  2. crea un file di testo con un giocatore per riga e usa --rosa;
  3. incolla i nomi sulla riga di comando:
       python fantacalcio.py consiglia --squadra "La mia squadra" --rosa "Carnesecchi, Bastoni, Barella, ..."

Il formato JSON canonico è descritto nel README (sezione "Importare la lega").
"""


# ---------------------------------------------------------------------------
# Normalizzazione nomi
# ---------------------------------------------------------------------------


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _initial_variants(name: str) -> list[tuple[str, str]]:
    """Coppie (cognome, iniziale) per forme come 'Lautaro Martinez'.

    Fantacalcio usa spesso 'Cognome N.'; l'utente può però scrivere il nome
    completo. Questa funzione prova entrambe le interpretazioni.
    """
    normalized = _normalize_text(name)
    parts = normalized.split()
    if len(parts) < 2:
        return []
    variants: list[tuple[str, str]] = []
    if len(parts[-1]) == 1:
        variants.append((parts[0], parts[-1]))
    if len(parts[0]) == 1:
        variants.append((parts[-1], parts[0]))
    if len(parts[-1]) > 1:
        variants.append((parts[-1], parts[0][0]))
    if len(parts[0]) > 1:
        variants.append((parts[0], parts[-1][0]))
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for surname, initial in variants:
        key = (surname, initial)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _strip_team_hint(entry: str) -> tuple[str, str]:
    """Estrae un eventuale codice squadra da 'Nome (ATA)' o 'Nome - ATA'."""
    text = entry.strip()
    patterns = [
        r"^(.*?)\s*[\(\[]\s*([A-Za-z]{3})\s*[\)\]]\s*$",
        r"^(.*?)\s+-\s+([A-Za-z]{3})\s*$",
        r"^(.*?)\s*,\s*([A-Za-z]{3})\s*$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group(1).strip(), normalize_team(match.group(2))
    return text, ""


class PlayerResolver:
    def __init__(self, players: dict[str, Player]):
        self.players = players
        self.by_name: dict[str, list[Player]] = {}
        self.by_surname: dict[str, list[Player]] = {}
        for player in players.values():
            name_key = _normalize_text(player.name)
            self.by_name.setdefault(name_key, []).append(player)
            # I nomi di Fantacalcio sono spesso "Cognome N." ma possono anche
            # essere "Nome Cognome": indicizziamo tutti i token utili.
            for token in name_key.split():
                if len(token) > 1:
                    self.by_surname.setdefault(token, []).append(player)
        self.all_names = list(self.by_name)

    def _filter_team(self, candidates: Iterable[Player], team_hint: str) -> list[Player]:
        if not team_hint:
            return list(candidates)
        return [p for p in candidates if p.team == team_hint]

    def _best(self, candidates: list[Player], name: str) -> Player:
        if len(candidates) == 1:
            return candidates[0]
        return max(candidates, key=lambda p: (p.price, p.appearances, p.starter_prob))

    def resolve(self, entry: Any) -> tuple[Player, str | None]:
        """Restituisce (giocatore, avviso). Solleva PlayerNotFoundError se assente."""
        warning: str | None = None
        if isinstance(entry, dict):
            raw_id = entry.get("id") or entry.get("player_id") or entry.get("codice")
            if raw_id is not None and str(raw_id) in self.players:
                return self.players[str(raw_id)], None
            name = str(entry.get("nome") or entry.get("name") or entry.get("giocatore") or "").strip()
            team_hint = normalize_team(entry.get("squadra") or entry.get("team") or "")
            if not name:
                raise PlayerNotFoundError(f"Voce rosa senza nome: {entry!r}")
            return self._resolve_name(name, team_hint)

        if isinstance(entry, int):
            key = str(entry)
            if key in self.players:
                return self.players[key], None
            raise PlayerNotFoundError(f"ID giocatore {key} non presente nel dataset")

        text = str(entry).strip()
        if not text:
            raise PlayerNotFoundError("Voce rosa vuota")
        if text.isdigit() and text in self.players:
            return self.players[text], None

        name, team_hint = _strip_team_hint(text)
        return self._resolve_name(name, team_hint)

    def _resolve_name(self, name: str, team_hint: str = "") -> tuple[Player, str | None]:
        normalized = _normalize_text(name)
        if not normalized:
            raise PlayerNotFoundError(f"Nome giocatore non valido: {name!r}")

        # 1) corrispondenza esatta
        candidates = self._filter_team(self.by_name.get(normalized, []), team_hint)
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            chosen = self._best(candidates, name)
            warning = (
                f"'{name}' è ambiguo: scelto {chosen.name} ({chosen.team}). "
                "Aggiungi il codice squadra, es. 'Rossi (GEN)'."
            )
            return chosen, warning

        # 2) forma cognome + iniziale (es. 'Rossi F.' oppure 'Lautaro Martinez')
        for surname, initial in _initial_variants(name):
            pool = self._filter_team(self.by_surname.get(surname, []), team_hint)

            def matches_initial(player: Player) -> bool:
                tokens = _normalize_text(player.name).split()
                if any(token == initial for token in tokens):
                    return True
                return any(len(token) > 1 and token.startswith(initial) for token in tokens)

            pool = [p for p in pool if matches_initial(p)]
            if len(pool) == 1:
                return pool[0], None
            if len(pool) > 1:
                chosen = self._best(pool, name)
                warning = f"'{name}' è ambiguo: scelto {chosen.name} ({chosen.team})."
                return chosen, warning

        # 3) solo cognome
        pool = self._filter_team(self.by_surname.get(normalized, []), team_hint)
        if len(pool) == 1:
            return pool[0], None
        if len(pool) > 1:
            chosen = self._best(pool, name)
            warning = (
                f"'{name}' corrisponde a più giocatori: scelto {chosen.name} ({chosen.team}). "
                "Aggiungi il codice squadra per essere più preciso."
            )
            return chosen, warning

        # 4) fuzzy matching
        matches = difflib.get_close_matches(normalized, self.all_names, n=8, cutoff=0.78)
        fuzzy: list[Player] = []
        for match in matches:
            fuzzy.extend(self.by_name.get(match, []))
        fuzzy = self._filter_team(fuzzy, team_hint)
        if fuzzy:
            chosen = self._best(fuzzy, name)
            warning = f"'{name}' non trovato esattamente: interpretato come {chosen.name} ({chosen.team})."
            return chosen, warning

        suggestions = difflib.get_close_matches(normalized, self.all_names, n=5, cutoff=0.5)
        suggestion_text = ""
        if suggestions:
            names = []
            for suggestion in suggestions:
                player = self._best(self.by_name[suggestion], suggestion)
                names.append(f"{player.name} ({player.team})")
            suggestion_text = " Suggerimenti: " + ", ".join(names) + "."
        raise PlayerNotFoundError(f"Giocatore '{name}' non trovato nel dataset.{suggestion_text}")


# ---------------------------------------------------------------------------
# Lettura della lega
# ---------------------------------------------------------------------------


def default_league_path(data_dir: Path | str | None = None) -> Path:
    return resolve_data_dir(data_dir) / LEAGUE_FILE


def _read_payload(address: Any, data_dir: Path | str | None) -> tuple[Any, str]:
    text = "" if address is None else str(address).strip()
    if not text or text.lower() in {"demo", "lega demo", "esempio"}:
        path = default_league_path(data_dir)
        if not path.exists():
            from .demo import ensure_demo_league

            ensure_demo_league(data_dir)
        return read_json(path), str(path)

    if text.startswith("{"):
        try:
            return json.loads(text), "inline"
        except json.JSONDecodeError as exc:
            raise LeagueError(f"JSON inline non valido: {exc}") from exc

    if text.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(text)
        host = parsed.netloc.lower()
        if "leghe.fantacalcio.it" in host:
            raise FantacalcioURLError(FANTACALCIO_LOGIN_MESSAGE)
        try:
            return read_json(text), text
        except Exception as exc:  # pragma: no cover - dipende dalla rete
            raise LeagueError(
                f"Impossibile scaricare la lega da {text}: {exc}\n"
                "Verifica che l'URL restituisca JSON pubblico e non una pagina HTML."
            ) from exc

    path = Path(text).expanduser()
    if path.is_dir():
        for candidate in ("lega.json", "league.json", LEAGUE_FILE):
            if (path / candidate).exists():
                path = path / candidate
                break
    if not path.exists():
        raise LeagueError(
            f"File lega non trovato: {path}\n"
            "Usa un file JSON, un file Excel (.xlsx), un URL JSON pubblico oppure --rosa "
            "per la rosa manuale."
        )
    if path.suffix.lower() in XLSX_SUFFIXES:
        try:
            return xlsx_to_payload(path), str(path)
        except XlsxError as exc:
            raise LeagueError(str(exc)) from exc
    return read_json(path), str(path)


def _team_name(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in ("nome", "name", "squadra", "team", "fantasy_team"):
            value = item.get(key)
            if value:
                return str(value).strip()
    if isinstance(item, str) and item.strip():
        return item.strip()
    return f"Squadra {index}"


def _team_roster(item: Any, rose_map: dict[str, Any]) -> list[Any]:
    if isinstance(item, dict):
        for key in ("rosa", "roster", "giocatori", "players", "calciatori"):
            value = item.get(key)
            if isinstance(value, (list, tuple)):
                return list(value)
            if isinstance(value, str) and value.strip():
                return read_roster_entries(value)
        name = _team_name(item, 0)
        if name in rose_map:
            return list(rose_map[name])
        return []
    if isinstance(item, str):
        if item in rose_map:
            return list(rose_map[item])
        if Path(item).exists():
            return read_roster_entries(item)
    return []


def _extract_teams(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rose_map: dict[str, Any] = {}
    for key in ("rose", "rosters"):
        value = raw.get(key)
        if isinstance(value, dict):
            rose_map.update({str(k): v for k, v in value.items()})

    teams_raw = None
    for key in ("squadre", "teams", "squads", "fantasy_teams"):
        if key in raw:
            teams_raw = raw[key]
            break

    if isinstance(teams_raw, dict):
        result = []
        for name, value in teams_raw.items():
            if isinstance(value, dict):
                item = {"nome": name, **value}
            else:
                item = {"nome": name, "rosa": value}
            result.append(item)
        return result

    if isinstance(teams_raw, list):
        result = []
        for index, item in enumerate(teams_raw, start=1):
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str):
                result.append({"nome": item, "rosa": _team_roster(item, rose_map)})
            else:
                result.append({"nome": _team_name(item, index), "rosa": _team_roster(item, rose_map)})
        # se le rose sono in una mappa separata, agganciale
        for item in result:
            if not item.get("rosa") and not item.get("roster") and item.get("nome") in rose_map:
                item["rosa"] = rose_map[item["nome"]]
        return result

    if rose_map:
        return [{"nome": name, "rosa": roster} for name, roster in rose_map.items()]

    # lega con una sola squadra
    roster = None
    for key in ("rosa", "roster", "giocatori", "players", "calciatori"):
        if isinstance(raw.get(key), list):
            roster = raw[key]
            break
    if roster is not None:
        name = (
            raw.get("squadra")
            or raw.get("nome_squadra")
            or raw.get("team")
            or raw.get("nome")
            or "La mia squadra"
        )
        return [{"nome": str(name), "rosa": roster}]
    return []


def read_roster_entries(source: Any) -> list[str]:
    """Legge una rosa da file, lista o stringa separata da virgole/punti e virgola."""
    if isinstance(source, (list, tuple)):
        return [str(x).strip() for x in source if str(x).strip()]
    if source is None:
        return []
    text = str(source).strip()
    if not text:
        return []
    path = Path(text).expanduser()
    if path.exists() and path.is_file():
        lines = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if line:
                    lines.append(line)
        return lines
    if "\n" in text:
        chunks = text.splitlines()
    elif ";" in text:
        chunks = text.split(";")
    else:
        chunks = text.split(",")
    result = []
    for chunk in chunks:
        chunk = chunk.split("#", 1)[0].strip()
        if chunk:
            result.append(chunk)
    return result


def _resolve_roster(
    entries: list[Any],
    resolver: PlayerResolver,
) -> tuple[list[Player], list[str]]:
    roster: list[Player] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        player, warning = resolver.resolve(entry)
        if warning:
            warnings.append(warning)
        if player.id in seen:
            warnings.append(f"Giocatore duplicato nella rosa: {player.name} ({player.team}).")
            continue
        seen.add(player.id)
        roster.append(player)
    return roster, warnings


def load_league(
    address: Any = None,
    *,
    data_dir: Path | str | None = None,
    players: dict[str, Player] | None = None,
    serie_a: SerieAData | None = None,
    settings: LeagueSettings | None = None,
) -> League:
    """Carica una lega da indirizzo, file JSON, URL pubblico o rosa manuale."""
    data_dir = resolve_data_dir(data_dir)
    players = players or load_players(data_dir=data_dir)[0]
    serie_a = serie_a or load_serie_a(data_dir=data_dir)
    settings = settings or load_settings(data_dir=data_dir)

    raw, source = _read_payload(address, data_dir)
    if isinstance(raw, list):
        raw = {"squadra": "La mia squadra", "rosa": raw}
    if not isinstance(raw, dict):
        raise LeagueError("Il file della lega deve contenere un oggetto JSON.")

    league_settings = merge_settings(
        settings,
        raw.get("impostazioni") or raw.get("settings") or raw.get("regole") or {},
    )
    matchday = (
        raw.get("giornata")
        or raw.get("matchday")
        or league_settings.giornata
        or serie_a.current_matchday
    )
    try:
        matchday = int(matchday)
    except (TypeError, ValueError):
        matchday = serie_a.current_matchday

    league_name = str(raw.get("nome") or raw.get("name") or raw.get("lega") or "Lega fantacalcio")
    team_items = _extract_teams(raw)
    if not team_items:
        raise LeagueError(
            "Nessuna squadra trovata nel file della lega. "
            "Usa la chiave 'squadre' (lista) oppure 'rose' (mappa nome -> rosa)."
        )

    resolver = PlayerResolver(players)
    teams: list[FantasyTeam] = []
    warnings: list[str] = []
    for index, item in enumerate(team_items, start=1):
        name = _team_name(item, index)
        entries = _team_roster(item, {})
        roster, roster_warnings = _resolve_roster(entries, resolver)
        warnings.extend(roster_warnings)
        manager = ""
        if isinstance(item, dict):
            manager = str(item.get("allenatore") or item.get("manager") or item.get("presidente") or "")
        teams.append(FantasyTeam(name=name, manager=manager, roster=roster))
        if not roster:
            warnings.append(f"La squadra '{name}' non ha una rosa: non potrà essere ottimizzata.")

    return League(
        name=league_name,
        source=source,
        teams=teams,
        settings=league_settings,
        serie_a=serie_a,
        matchday=matchday,
        players=players,
        warnings=warnings,
    )


def build_manual_league(
    team_name: str,
    roster_source: Any,
    *,
    league_name: str = "Lega manuale",
    data_dir: Path | str | None = None,
    players: dict[str, Player] | None = None,
    serie_a: SerieAData | None = None,
    settings: LeagueSettings | None = None,
    matchday: int | None = None,
) -> League:
    """Crea una lega con una sola squadra a partire da una rosa manuale."""
    data_dir = resolve_data_dir(data_dir)
    players = players or load_players(data_dir=data_dir)[0]
    serie_a = serie_a or load_serie_a(data_dir=data_dir)
    settings = settings or load_settings(data_dir=data_dir)
    entries = read_roster_entries(roster_source)
    if not entries:
        raise LeagueError("La rosa manuale è vuota.")
    resolver = PlayerResolver(players)
    roster, warnings = _resolve_roster(entries, resolver)
    if not roster:
        raise LeagueError("Nessun giocatore della rosa è stato riconosciuto.")
    team = FantasyTeam(name=team_name, manager="", roster=roster)
    return League(
        name=league_name,
        source="manuale",
        teams=[team],
        settings=settings,
        serie_a=serie_a,
        matchday=matchday or settings.giornata or serie_a.current_matchday,
        players=players,
        warnings=warnings,
    )


def create_league_file(
    team_name: str,
    roster_source: Any,
    output: Path | str,
    *,
    league_name: str = "Lega importata",
    data_dir: Path | str | None = None,
    players: dict[str, Player] | None = None,
    serie_a: SerieAData | None = None,
    settings: LeagueSettings | None = None,
    matchday: int | None = None,
) -> Path:
    """Risolve una rosa manuale e scrive un file di lega canonico."""
    data_dir = resolve_data_dir(data_dir)
    players = players or load_players(data_dir=data_dir)[0]
    serie_a = serie_a or load_serie_a(data_dir=data_dir)
    settings = settings or load_settings(data_dir=data_dir)
    league = build_manual_league(
        team_name,
        roster_source,
        league_name=league_name,
        data_dir=data_dir,
        players=players,
        serie_a=serie_a,
        settings=settings,
        matchday=matchday,
    )
    team = league.teams[0]
    payload = {
        "nome": league_name,
        "giornata": league.matchday,
        "impostazioni": dataclasses.asdict(league.settings),
        "squadre": [
            {
                "nome": team.name,
                "allenatore": team.manager,
                "rosa": [player.id for player in team.roster],
            }
        ],
    }
    output_path = Path(output).expanduser()
    write_json(output_path, payload)
    return output_path


def describe_league(league: League) -> str:
    lines = [
        f"Lega: {league.name}",
        f"Fonte: {league.source}",
        f"Giornata: {league.matchday}",
        f"Squadre: {len(league.teams)}",
    ]
    for team in league.teams:
        lines.append(f"  - {team.name}: {len(team.roster)} giocatori")
    return "\n".join(lines)