"""Aggiornamento dei dati da Fantacalcio.it e Wikipedia.

Il modulo usa solo la libreria standard. I parser sono volutamente semplici e
tolleranti: se una pagina cambia struttura il comando `update` lo segnala e
lascia intatti i dati precedenti.

Fonti:
- https://www.fantacalcio.it/quotazioni-fantacalcio
- https://www.fantacalcio.it/statistiche-serie-a
- https://www.fantacalcio.it/probabili-formazioni-serie-a
- https://it.wikipedia.org/wiki/Serie_A_2026-2027 (classifica e calendario)
"""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from .config import (
    CODE_TO_NAME,
    DEFAULT_SETTINGS,
    ROLE_ORDER,
    TEAM_NAME_TO_CODE,
    TEAM_SLUG_TO_CODE,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
USER_AGENT = "FantaLineup/1.0 (progetto personale; dati pubblici)"

QUOTAZIONI_URL = "https://www.fantacalcio.it/quotazioni-fantacalcio"
STATISTICHE_URL = "https://www.fantacalcio.it/statistiche-serie-a"
PROBABILI_URL = "https://www.fantacalcio.it/probabili-formazioni-serie-a"
WIKI_PAGE = "Serie_A_2026-2027"
WIKI_API = (
    "https://it.wikipedia.org/w/api.php?action=parse&page={page}"
    "&prop=wikitext&format=json&formatversion=2"
)

MONTHS = {
    "gen": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "mag": 5,
    "giu": 6,
    "lug": 7,
    "ago": 8,
    "set": 9,
    "ott": 10,
    "nov": 11,
    "dic": 12,
}


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def http_get(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except OSError as exc:
        raise OSError(f"{url}: {exc}") from exc
    return raw.decode("utf-8", "replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    return " ".join(value.replace("\xa0", " ").split())


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = strip_tags(value).replace("%", "").strip()
    if text in {"", "-", "—", "N/D", "nd"}:
        return None
    text = text.replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: str | None) -> int:
    number = parse_float(value)
    return int(round(number)) if number is not None else 0


def _player_link(block: str) -> tuple[str, str, str] | None:
    """Restituisce (slug squadra, slug giocatore, id) da un link Fantacalcio."""
    match = re.search(
        r'href="(?:https://www\.fantacalcio\.it)?/serie-a/squadre/([^/"?]+)/([^/"?]+)/(\d+)"',
        block,
    )
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _name_from_block(block: str) -> str:
    # Il nome è nello <span> dentro il link del giocatore.
    match = re.search(
        r'<a[^>]*class="[^"]*player-name[^"]*"[^>]*>.*?<span>(.*?)</span>',
        block,
        re.S,
    )
    if not match:
        match = re.search(r"<span>(.*?)</span>", block, re.S)
    return strip_tags(match.group(1)) if match else ""


def _cell(block: str, key: str) -> str | None:
    match = re.search(
        rf'data-col-key="{re.escape(key)}"[^>]*>(.*?)</td>',
        block,
        re.S,
    )
    return strip_tags(match.group(1)) if match else None


def _team_from_block(block: str) -> str | None:
    match = re.search(r'class="player-team"[^>]*>\s*([A-Z]{3})\s*<', block)
    if match:
        return match.group(1)
    return None


def _role_from_block(block: str) -> str | None:
    match = re.search(r'data-filter-role-classic="([pdca])"', block, re.I)
    if match:
        return match.group(1).upper()
    match = re.search(r'class="role"[^>]*data-value="([pdca])"', block, re.I)
    if match:
        return match.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# Parser pagine Fantacalcio.it
# ---------------------------------------------------------------------------


def parse_quotazioni(page: str) -> dict[str, dict[str, Any]]:
    players: dict[str, dict[str, Any]] = {}
    for block in re.findall(r'<tr class="player-row".*?</tr>', page, re.S):
        link = _player_link(block)
        if not link:
            continue
        team_slug, player_slug, player_id = link
        name = _name_from_block(block)
        if not name:
            continue
        players[player_id] = {
            "id": player_id,
            "nome": name,
            "slug": player_slug,
            "squadra_slug": team_slug,
            "squadra": _team_from_block(block),
            "ruolo": _role_from_block(block),
            "ruoli_mantra": [
                x.strip()
                for x in (
                    re.search(r'data-filter-role-mantra="([^"]*)"', block).group(1)
                    if re.search(r'data-filter-role-mantra="([^"]*)"', block)
                    else ""
                ).split("|")
                if x.strip()
            ],
            "quotazione": parse_float(
                re.search(
                    r'class="player-classic-current-price"[^>]*>(.*?)</td>', block, re.S
                ).group(1)
                if re.search(
                    r'class="player-classic-current-price"[^>]*>(.*?)</td>', block, re.S
                )
                else None
            )
            or 0.0,
            "quotazione_iniziale": parse_float(
                re.search(
                    r'class="player-classic-initial-price"[^>]*>(.*?)</td>', block, re.S
                ).group(1)
                if re.search(
                    r'class="player-classic-initial-price"[^>]*>(.*?)</td>', block, re.S
                )
                else None
            )
            or 0.0,
            "fvm": parse_float(
                re.search(r'class="player-classic-fvm"[^>]*>(.*?)</td>', block, re.S).group(1)
                if re.search(r'class="player-classic-fvm"[^>]*>(.*?)</td>', block, re.S)
                else None
            )
            or 0.0,
            "presenze_totali": parse_int(
                re.search(r'data-filter-playeds="(\d+)"', block).group(1)
                if re.search(r'data-filter-playeds="(\d+)"', block)
                else None
            ),
        }
    return players


def parse_statistiche(page: str) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for block in re.findall(r'<tr class="player-row".*?</tr>', page, re.S):
        link = _player_link(block)
        if not link:
            continue
        _, _, player_id = link
        rig = _cell(block, "rig") or ""
        rig_match = re.search(r"(\d+)\s*/\s*(\d+)", rig)
        stats[player_id] = {
            "id": player_id,
            "nome": _name_from_block(block),
            "squadra": _team_from_block(block),
            "ruolo": _role_from_block(block),
            "presenze": parse_int(_cell(block, "pg")),
            "media_voto": parse_float(_cell(block, "mv")),
            "fantamedia": parse_float(_cell(block, "mfv")),
            "gol": parse_int(_cell(block, "gol")),
            "gol_subiti": parse_int(_cell(block, "gs")),
            "rigori_segnati": int(rig_match.group(1)) if rig_match else 0,
            "rigori_tirati": int(rig_match.group(2)) if rig_match else 0,
            "rigori_parati": parse_int(_cell(block, "rp")),
            "assist": parse_int(_cell(block, "ass")),
            "ammonizioni": parse_int(_cell(block, "amm")),
            "espulsioni": parse_int(_cell(block, "esp")),
        }
    return stats


def _progress_from_block(block: str) -> float | None:
    match = re.search(r'class="progress-value"[^>]*>\s*(\d+)\s*%', block, re.S)
    if not match:
        match = re.search(r"--value:\s*(\d+)", block)
    if not match:
        return None
    return max(0.0, min(1.0, int(match.group(1)) / 100.0))


def _links_in_section(page: str, section_class: str) -> list[tuple[str, str, str]]:
    links: list[tuple[str, str, str]] = []
    for section in re.findall(
        rf'<section class="{re.escape(section_class)}">.*?</section>', page, re.S
    ):
        for block in re.findall(r"<li[^>]*>.*?</li>", section, re.S):
            link = _player_link(block)
            if link:
                links.append(link)
    return links


def parse_probabili(page: str) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Estrae titolarità, ruolo e stato dei giocatori dalla pagina delle probabili."""
    result: dict[str, dict[str, Any]] = {}
    matchday_counter = Counter(int(x) for x in re.findall(r"/calendario/(\d+)/", page))
    matchday = matchday_counter.most_common(1)[0][0] if matchday_counter else None

    def add_block(block: str, default_prob: float) -> None:
        link = _player_link(block)
        if not link:
            return
        team_slug, _, player_id = link
        prob = _progress_from_block(block)
        if prob is None:
            prob = default_prob
        role = _role_from_block(block)
        current = result.get(player_id, {})
        result[player_id] = {
            **current,
            "id": player_id,
            "nome": _name_from_block(block) or current.get("nome", ""),
            "squadra_slug": team_slug,
            "squadra": TEAM_SLUG_TO_CODE.get(team_slug),
            "ruolo": role or current.get("ruolo"),
            "titolarita": max(float(current.get("titolarita", 0.0)), prob),
            "stato": current.get("stato", "ok"),
            "nota": current.get("nota", ""),
        }

    # La pagina contiene sia il campo grafico (senza percentuali) sia le liste
    # "Probabili formazioni" con la percentuale di titolarità. Usiamo le liste.
    starter_blocks = re.findall(r'<ul class="player-list">.*?</ul>', page, re.S)
    for block in starter_blocks:
        for item in re.findall(r"<li[^>]*>.*?</li>", block, re.S):
            add_block(item, 0.9)
    for block in re.findall(r'<ul class="player-list reserves">.*?</ul>', page, re.S):
        for item in re.findall(r"<li[^>]*>.*?</li>", block, re.S):
            add_block(item, 0.03)
    # fallback: se la pagina non avesse le liste, usa il campo grafico
    if not starter_blocks:
        for block in re.findall(r'<ul class="team-lineup".*?</ul>', page, re.S):
            for item in re.findall(r'<li class="player[^"]*".*?</li>', block, re.S):
                add_block(item, 0.9)

    for _, _, player_id in _links_in_section(page, "injureds"):
        result.setdefault(player_id, {"id": player_id, "titolarita": 0.0, "stato": "infortunato"})
        result[player_id]["titolarita"] = 0.0
        result[player_id]["stato"] = "infortunato"
    for _, _, player_id in _links_in_section(page, "suspendeds"):
        result.setdefault(player_id, {"id": player_id, "titolarita": 0.0, "stato": "squalificato"})
        result[player_id]["titolarita"] = 0.0
        result[player_id]["stato"] = "squalificato"
    for _, _, player_id in _links_in_section(page, "cautioneds"):
        result.setdefault(player_id, {"id": player_id, "titolarita": 0.0, "stato": "diffidato"})
        result[player_id]["stato"] = "diffidato"
    for _, _, player_id in _links_in_section(page, "dubts"):
        entry = result.setdefault(
            player_id, {"id": player_id, "titolarita": 0.25, "stato": "in dubbio"}
        )
        entry["stato"] = "in dubbio"
        entry["nota"] = "In dubbio: possibile ballottaggio"
        if entry.get("titolarita", 0.0) <= 0.0:
            entry["titolarita"] = 0.25

    return result, matchday


# ---------------------------------------------------------------------------
# Parser Wikipedia
# ---------------------------------------------------------------------------


def fetch_wikipedia_wikitext() -> str:
    url = WIKI_API.format(page=WIKI_PAGE)
    payload = json.loads(http_get(url))
    return payload["parse"]["wikitext"]


def _extract_between(text: str, start_pattern: str, end_pattern: str) -> str:
    start = re.search(start_pattern, text)
    if not start:
        return ""
    rest = text[start.end() :]
    end = re.search(end_pattern, rest)
    return rest[: end.start()] if end else rest


def parse_standings(wikitext: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in re.split(r"\n\|-", wikitext):
        team_match = re.search(r"\{\{Calcio\s*\|?\s*([^}|]+)", block)
        if not team_match:
            continue
        team_name = team_match.group(1).strip()
        code = TEAM_NAME_TO_CODE.get(team_name.lower())
        if not code:
            # template con link o nome esteso
            for key, value in TEAM_NAME_TO_CODE.items():
                if key in team_name.lower():
                    code = value
                    break
        if not code:
            continue
        position_match = re.search(r"\|\|\s*(\d+)\.\s*\|\|", block)
        position = int(position_match.group(1)) if position_match else len(rows) + 1
        rest = block[team_match.end() :]
        numbers = [int(x) for x in re.findall(r"[+-]?\d+", rest)]
        if len(numbers) < 8:
            continue
        points, played, wins, draws, losses, gf, ga, dr = numbers[:8]
        rows.append(
            {
                "codice": code,
                "nome": CODE_TO_NAME[code],
                "posizione": position,
                "punti": points,
                "giocate": played,
                "vittorie": wins,
                "pareggi": draws,
                "sconfitte": losses,
                "gf": gf,
                "gs": ga,
                "dr": dr,
            }
        )
    # rimuove eventuali duplicati mantenendo la prima occorrenza
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["codice"], row)
    ordered = sorted(
        unique.values(),
        key=lambda r: (r["posizione"], -r["punti"], -r["dr"], -r["gf"]),
    )
    return ordered


def _season_date(day: int, month_key: str) -> str | None:
    month = MONTHS.get(month_key.lower())
    if not month:
        return None
    year = 2026 if month >= 8 else 2027
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_fixtures(wikitext: str) -> list[dict[str, Any]]:
    team_names = sorted(CODE_TO_NAME.values(), key=len, reverse=True)
    team_pattern = "|".join(re.escape(name) for name in team_names)
    pair_re = re.compile(rf"({team_pattern})\s*-\s*({team_pattern})")
    fixtures: list[dict[str, Any]] = []
    headers = list(re.finditer(r"'''(\d+)\D*?giornata'''", wikitext))
    for index, header in enumerate(headers):
        matchday = int(header.group(1))
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(wikitext)
        block = wikitext[header.end() : block_end]
        last_date: str | None = None
        for row in re.split(r"\n\|-", block):
            pair = pair_re.search(row)
            if not pair:
                continue
            home_name, away_name = pair.group(1), pair.group(2)
            home = TEAM_NAME_TO_CODE.get(home_name.lower())
            away = TEAM_NAME_TO_CODE.get(away_name.lower())
            if not home or not away:
                continue
            date_match = re.search(
                r"(\d{1,2})\s+(ago|set|ott|nov|dic|gen|feb|mar|apr|mag|giu)\.", row, re.I
            )
            if date_match:
                last_date = _season_date(int(date_match.group(1)), date_match.group(2))
            value_match = re.search(r"\|\|\s*([^|\n]*)", row[pair.end() :])
            value = value_match.group(1).strip() if value_match else ""
            home_goals = away_goals = None
            time_value = None
            score_match = re.match(r"(\d+)\s*-\s*(\d+)$", value)
            if score_match:
                home_goals = int(score_match.group(1))
                away_goals = int(score_match.group(2))
            elif re.match(r"\d{1,2}:\d{2}", value):
                time_value = value
            fixtures.append(
                {
                    "giornata": matchday,
                    "casa": home,
                    "trasferta": away,
                    "data": last_date,
                    "ora": time_value,
                    "gol_casa": home_goals,
                    "gol_trasferta": away_goals,
                }
            )
    # dedup
    unique: dict[tuple[int, str, str], dict[str, Any]] = {}
    for fixture in fixtures:
        unique.setdefault(
            (fixture["giornata"], fixture["casa"], fixture["trasferta"]), fixture
        )
    return sorted(unique.values(), key=lambda f: (f["giornata"], f["casa"]))


# ---------------------------------------------------------------------------
# Costruzione dataset
# ---------------------------------------------------------------------------


def build_players(
    quotazioni: dict[str, dict[str, Any]],
    statistiche: dict[str, dict[str, Any]],
    probabili: dict[str, dict[str, Any]],
    current_matchday: int,
) -> list[dict[str, Any]]:
    ids = set(quotazioni) | set(statistiche) | set(probabili)
    players: list[dict[str, Any]] = []
    has_probables = bool(probabili)

    for player_id in ids:
        q = quotazioni.get(player_id, {})
        s = statistiche.get(player_id, {})
        p = probabili.get(player_id, {})
        team = s.get("squadra") or q.get("squadra") or p.get("squadra")
        role = s.get("ruolo") or q.get("ruolo") or p.get("ruolo")
        name = q.get("nome") or s.get("nome") or p.get("nome")
        if not team or not role or not name:
            continue
        if team not in CODE_TO_NAME:
            # codice non riconosciuto: prova dallo slug
            team = TEAM_SLUG_TO_CODE.get(q.get("squadra_slug", "")) or team
        if team not in CODE_TO_NAME:
            continue

        appearances = s.get("presenze", 0) or 0
        if player_id in probabili:
            starter_prob = float(p.get("titolarita", 0.0))
        elif has_probables:
            # non compare tra i convocati/probabili: margine molto basso
            starter_prob = 0.01
        else:
            starter_prob = 0.0

        status = p.get("stato", "ok")
        if status in {"infortunato", "squalificato"}:
            starter_prob = 0.0

        players.append(
            {
                "id": player_id,
                "nome": name,
                "squadra": team,
                "ruolo": role,
                "ruoli_mantra": q.get("ruoli_mantra", []),
                "quotazione": q.get("quotazione", 0.0),
                "quotazione_iniziale": q.get("quotazione_iniziale", 0.0),
                "fvm": q.get("fvm", 0.0),
                "presenze": appearances,
                "presenze_totali": q.get("presenze_totali", appearances),
                "media_voto": s.get("media_voto"),
                "fantamedia": s.get("fantamedia"),
                "gol": s.get("gol", 0),
                "assist": s.get("assist", 0),
                "gol_subiti": s.get("gol_subiti", 0),
                "rigori_segnati": s.get("rigori_segnati", 0),
                "rigori_tirati": s.get("rigori_tirati", 0),
                "rigori_parati": s.get("rigori_parati", 0),
                "ammonizioni": s.get("ammonizioni", 0),
                "espulsioni": s.get("espulsioni", 0),
                "titolarita": round(starter_prob, 3),
                "stato": status,
                "nota": p.get("nota", ""),
            }
        )

    # fallback titolarità per dataset senza probabili formazioni
    if not has_probables:
        max_apps: dict[str, int] = {}
        for player in players:
            max_apps[player["squadra"]] = max(
                max_apps.get(player["squadra"], 0), player["presenze"]
            )
        for player in players:
            if player["presenze"] > 0:
                denominator = max(1, max_apps.get(player["squadra"], 1))
                player["titolarita"] = round(min(0.9, player["presenze"] / denominator * 0.95), 3)
            else:
                player["titolarita"] = 0.02

    players.sort(key=lambda x: (x["squadra"], ROLE_ORDER.get(x["ruolo"], 9), x["nome"]))
    return players


def build_serie_a(
    standings: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    current_matchday: int | None,
) -> dict[str, Any]:
    if current_matchday is None:
        played_matchdays = [f["giornata"] for f in fixtures if f["gol_casa"] is not None]
        current_matchday = (max(played_matchdays) + 1) if played_matchdays else 1
    teams = []
    for row in standings:
        teams.append(
            {
                "codice": row["codice"],
                "nome": row["nome"],
                "slug": next(
                    (slug for slug, code in TEAM_SLUG_TO_CODE.items() if code == row["codice"]),
                    row["codice"].lower(),
                ),
                "posizione": row["posizione"],
                "punti": row["punti"],
                "giocate": row["giocate"],
                "vittorie": row["vittorie"],
                "pareggi": row["pareggi"],
                "sconfitte": row["sconfitte"],
                "gf": row["gf"],
                "gs": row["gs"],
                "dr": row["dr"],
            }
        )
    return {
        "stagione": "2026-27",
        "aggiornato": date.today().isoformat(),
        "giornata_corrente": current_matchday,
        "fonte_classifica": f"https://it.wikipedia.org/wiki/{WIKI_PAGE}",
        "fonte_calendario": f"https://it.wikipedia.org/wiki/{WIKI_PAGE}",
        "squadre": teams,
        "partite": fixtures,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def update_data(
    data_dir: Path | str = DATA_DIR,
    *,
    verbose: bool = True,
) -> dict[str, Any]:
    """Scarica e ricostruisce i dataset. Restituisce un riepilogo."""
    data_dir = Path(data_dir)
    if verbose:
        print("Scarico quotazioni...")
    quotazioni = parse_quotazioni(http_get(QUOTAZIONI_URL))
    if verbose:
        print(f"  {len(quotazioni)} giocatori")
        print("Scarico statistiche...")
    statistiche = parse_statistiche(http_get(STATISTICHE_URL))
    if verbose:
        print(f"  {len(statistiche)} giocatori con statistiche")
        print("Scarico probabili formazioni...")
    probabili, probable_matchday = parse_probabili(http_get(PROBABILI_URL))
    if verbose:
        print(f"  {len(probabili)} giocatori con titolarità (giornata {probable_matchday})")
        print("Scarico classifica e calendario da Wikipedia...")
    wikitext = fetch_wikipedia_wikitext()
    standings_section = _extract_between(wikitext, r"==\s*Classifica\s*==", r"==\s*Risultati\s*==")
    andata_section = _extract_between(
        wikitext, r"===\s*Girone di andata\s*===", r"===\s*Girone di ritorno\s*==="
    )
    ritorno_section = _extract_between(
        wikitext, r"===\s*Girone di ritorno\s*===", r"==\s*Statistiche\s*=="
    )
    standings = parse_standings(standings_section)
    fixtures = parse_fixtures(andata_section + "\n" + ritorno_section)
    if not standings:
        raise RuntimeError("Classifica non trovata nella pagina Wikipedia")
    if not fixtures:
        raise RuntimeError("Calendario non trovato nella pagina Wikipedia")

    current_matchday = probable_matchday or (max(f["giornata"] for f in fixtures if f["gol_casa"] is not None) + 1)
    players = build_players(quotazioni, statistiche, probabili, current_matchday)
    serie_a = build_serie_a(standings, fixtures, current_matchday)
    players_payload = {
        "stagione": "2026-27",
        "aggiornato": date.today().isoformat(),
        "giornata_titolarita": probable_matchday,
        "fonte": {
            "quotazioni": QUOTAZIONI_URL,
            "statistiche": STATISTICHE_URL,
            "titolarita": PROBABILI_URL,
        },
        "giocatori": players,
    }
    _write_json(data_dir / "giocatori_2627.json", players_payload)
    _write_json(data_dir / "serie_a_2627.json", serie_a)
    settings_path = data_dir / "impostazioni.json"
    if not settings_path.exists():  # non cancellare le impostazioni modificate a mano
        _write_json(settings_path, DEFAULT_SETTINGS)
    if verbose:
        print(f"Scritti {len(players)} giocatori e {len(fixtures)} partite in {data_dir}")
    return {
        "giocatori": len(players),
        "partite": len(fixtures),
        "squadre": len(standings),
        "giornata": current_matchday,
        "giornata_titolarita": probable_matchday,
    }