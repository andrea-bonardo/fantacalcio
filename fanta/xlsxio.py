"""Lettura delle rose da un file Excel (.xlsx), con la sola libreria standard.

Il file può avere uno di questi formati (viene riconosciuto da solo):

1. tabella con colonne tipo ``Squadra | Giocatore | (Serie A)``: una riga per
   giocatore, con il nome della squadra del fantacalcio in una colonna
   (intestazioni come "Fantasquadra", "Allenatore", "Presidente", oppure
   "Squadra" quando c'è anche la colonna della squadra di Serie A);
2. un foglio per ogni squadra del fantacalcio (il nome del foglio è il nome
   della squadra) con l'elenco dei giocatori;
3. una colonna per ogni squadra: il nome della squadra in prima riga e i
   giocatori sotto.

La colonna dei giocatori si riconosce dall'intestazione ("Giocatore", "Nome",
"Calciatore"...). Se c'è la squadra di Serie A (nome o sigla) viene usata per
distinguere i giocatori omonimi.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CODE_TO_NAME, TEAM_NAME_TO_CODE
from .dataio import DataError

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

XLSX_SUFFIXES = {".xlsx", ".xlsm"}

# Intestazioni (già normalizzate) riconosciute.
_PLAYER_STRONG = ("giocatore", "giocatori", "calciatore", "calciatori", "nome giocatore",
                  "nome calciatore", "player")
_PLAYER_WEAK = ("nome", "name", "cognome")
_FANTA_TEAM = {
    "fantasquadra", "fanta squadra", "squadra fantacalcio", "squadra fanta", "fantateam",
    "fanta team", "team fantacalcio", "squadra fantasy", "nome squadra", "proprietario",
    "allenatore", "presidente", "fantallenatore", "fanta allenatore", "utente", "owner",
    "manager",
}
_SERIE_A_TEAM = {
    "squadra reale", "club", "squadra serie a", "team serie a", "squadra di serie a",
    "squadra calcistica", "serie a", "sq",
}
_AMBIGUOUS_TEAM = {"squadra", "squadre", "team"}

_IGNORED_NAMES = {
    "portieri", "portiere", "difensori", "difensore", "centrocampisti", "centrocampista",
    "attaccanti", "attaccante", "totale", "totali", "crediti", "crediti residui",
    "giocatore", "giocatori", "calciatore", "calciatori", "nome", "ruolo",
}

_MIN_LIST_SIZE = 8  # sotto questa soglia un foglio "a elenco" non è considerato una rosa


_CAL_MATCHDAY = {"giornata", "turno", "round", "matchday", "g", "gg"}
_CAL_HOME = {"casa", "squadra casa", "in casa", "home", "squadra 1", "team 1"}
_CAL_AWAY = {"trasferta", "squadra trasferta", "fuori casa", "ospite", "ospiti", "away", "squadra 2", "team 2"}
_ROUND_TITLE = re.compile(r"^\s*(?:giornata\s*(\d+)|(\d+)\s*[ªa°]?\s*giornata)\s*$", re.IGNORECASE)
_LEAGUE_DAY = re.compile(r"^\s*(\d+)\s*[ªa°]?\s*giornata\s+lega\s*$", re.IGNORECASE)
_SERIE_A_DAY = re.compile(r"^\s*(\d+)\s*[ªa°]?\s*giornata\s+serie\s*a\s*$", re.IGNORECASE)
_SCORE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_PAIR_SPLIT = re.compile(r"\s+(?:-|–|—|vs\.?|v)\s+", re.IGNORECASE)


class XlsxError(DataError):
    """Errore nella lettura del file Excel."""


@dataclass
class LeagueMatch:
    """Una partita del calendario della lega (fantacalcio, non Serie A)."""

    league_day: int
    home: str
    away: str
    serie_a_day: int | None = None  # giornata di Serie A a cui corrisponde, se indicata
    home_points: float | None = None
    away_points: float | None = None
    score: tuple[int, int] | None = None  # gol, se la partita è stata giocata

    @property
    def played(self) -> bool:
        return self.score is not None

    @property
    def day(self) -> int:
        """Giornata da usare per cercare la partita (Serie A se nota, altrimenti della lega)."""
        return self.serie_a_day if self.serie_a_day is not None else self.league_day


# ---------------------------------------------------------------------------
# Lettura del file .xlsx
# ---------------------------------------------------------------------------


def _col_index(ref: str) -> int:
    match = re.match(r"[A-Za-z]+", ref or "")
    if not match:
        return 0
    number = 0
    for char in match.group(0).upper():
        number = number * 26 + (ord(char) - 64)
    return number - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    strings: list[str] = []
    for item in root.iter(f"{_MAIN}si"):
        parts: list[str] = []
        for child in item:
            if child.tag == f"{_MAIN}t":
                parts.append(child.text or "")
            elif child.tag == f"{_MAIN}r":
                parts.extend(t.text or "" for t in child.findall(f"{_MAIN}t"))
        strings.append("".join(parts))
    return strings


def _sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels.iter(f"{_PKG_REL}Relationship")}
    result: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_MAIN}sheet"):
        name = sheet.get("name", "")
        target = targets.get(sheet.get(f"{_REL}id"), "")
        if not target:
            continue
        target = target.lstrip("/")
        path = target if target.startswith("xl/") else f"xl/{target}"
        result.append((name, path))
    return result


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_MAIN}t"))
    value = cell.find(f"{_MAIN}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    if kind in {"str", "e", "b"}:
        return value.text
    try:
        number = float(value.text)
    except ValueError:
        return value.text
    return str(int(number)) if number.is_integer() else str(number)


def read_xlsx(path: Path | str) -> list[tuple[str, list[list[str]]]]:
    """Restituisce [(nome foglio, righe)] con tutte le celle come testo."""
    path = Path(path).expanduser()
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise XlsxError(f"Impossibile aprire {path.name}: non sembra un file .xlsx valido ({exc}).") from exc
    sheets: list[tuple[str, list[list[str]]]] = []
    with archive:
        try:
            shared = _shared_strings(archive)
            for name, sheet_path in _sheet_paths(archive):
                root = ET.fromstring(archive.read(sheet_path))
                rows: list[list[str]] = []
                for row in root.iter(f"{_MAIN}row"):
                    cells: dict[int, str] = {}
                    for position, cell in enumerate(row.findall(f"{_MAIN}c")):
                        ref = cell.get("r")
                        index = _col_index(ref) if ref else position
                        cells[index] = _cell_text(cell, shared).strip()
                    if cells:
                        rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
                sheets.append((name, rows))
        except (KeyError, ET.ParseError) as exc:
            raise XlsxError(f"Struttura del file {path.name} non riconosciuta: {exc}") from exc
    return sheets


# ---------------------------------------------------------------------------
# Interpretazione delle rose
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _serie_a_code(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.upper() in CODE_TO_NAME:
        return text.upper()
    return TEAM_NAME_TO_CODE.get(_norm(text), "")


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"[\d.,\s]+", value))


def _is_player_name(value: str) -> bool:
    text = value.strip()
    return bool(text) and not _is_number(text) and _norm(text) not in _IGNORED_NAMES


def _entry(name: str, code: str = "") -> Any:
    return {"nome": name, "squadra": code} if code else name


def _serie_a_ratio(rows: list[list[str]], col: int) -> float:
    values = [row[col] for row in rows if col < len(row) and row[col].strip()]
    if not values:
        return 0.0
    return sum(1 for value in values if _serie_a_code(value)) / len(values)


def _find_header(rows: list[list[str]]) -> int | None:
    for position, row in enumerate(rows[:15]):
        headers = {_norm(cell) for cell in row if cell}
        if headers & set(_PLAYER_STRONG) or headers & set(_PLAYER_WEAK):
            return position
    return None


def _parse_table(
    sheet_name: str,
    rows: list[list[str]],
    header_pos: int,
    teams: dict[str, list[Any]],
) -> None:
    header = [_norm(cell) for cell in rows[header_pos]]
    data = rows[header_pos + 1 :]

    def first(options: Any) -> int | None:
        for index, name in enumerate(header):
            if name in options:
                return index
        return None

    player_col = first(_PLAYER_STRONG)
    if player_col is None:
        player_col = first(_PLAYER_WEAK)
    assert player_col is not None

    fanta_col = first(_FANTA_TEAM)
    serie_a_col = first(_SERIE_A_TEAM)
    for index, name in enumerate(header):
        if name not in _AMBIGUOUS_TEAM:
            continue
        if serie_a_col is None and _serie_a_ratio(data, index) >= 0.8:
            serie_a_col = index
        elif fanta_col is None:
            fanta_col = index

    current_team = sheet_name
    for row in data:
        if fanta_col is not None and fanta_col < len(row) and row[fanta_col].strip():
            current_team = row[fanta_col].strip()
        if player_col >= len(row) or not _is_player_name(row[player_col]):
            continue
        code = ""
        if serie_a_col is not None and serie_a_col < len(row):
            code = _serie_a_code(row[serie_a_col])
        teams.setdefault(current_team, []).append(_entry(row[player_col].strip(), code))


def _parse_columns(rows: list[list[str]], teams: dict[str, list[Any]]) -> bool:
    heads = [(index, cell) for index, cell in enumerate(rows[0]) if cell.strip()]
    if len(heads) < 2 or len(rows) < 2:
        return False
    if any(_is_number(cell) for _, cell in heads):
        return False
    if sum(1 for _, cell in heads if _serie_a_code(cell)) >= len(heads) / 2:
        return False  # sembra un elenco "giocatore | squadra di Serie A"
    for index, team in heads:
        names = [
            row[index].strip()
            for row in rows[1:]
            if index < len(row) and _is_player_name(row[index])
        ]
        if names:
            teams.setdefault(team.strip(), []).extend(names)
    return True


def _parse_list(sheet_name: str, rows: list[list[str]], teams: dict[str, list[Any]]) -> None:
    width = max(len(row) for row in rows)
    counts = [
        sum(1 for row in rows if col < len(row) and _is_player_name(row[col]) and not _serie_a_code(row[col]))
        for col in range(width)
    ]
    player_col = max(range(width), key=lambda col: counts[col])
    if counts[player_col] < _MIN_LIST_SIZE:
        return
    serie_a_col = None
    if player_col + 1 < width and _serie_a_ratio(rows, player_col + 1) >= 0.8:
        serie_a_col = player_col + 1
    for row in rows:
        if player_col >= len(row) or not _is_player_name(row[player_col]):
            continue
        code = _serie_a_code(row[serie_a_col]) if serie_a_col is not None and serie_a_col < len(row) else ""
        teams.setdefault(sheet_name, []).append(_entry(row[player_col].strip(), code))


def xlsx_to_payload(path: Path | str) -> dict[str, Any]:
    """Converte un file Excel di rose nel formato JSON canonico della lega."""
    path = Path(path).expanduser()
    teams: dict[str, list[Any]] = {}
    for sheet_name, rows in read_xlsx(path):
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows or _is_calendar_sheet(sheet_name, rows):
            continue
        header_pos = _find_header(rows)
        if header_pos is not None:
            _parse_table(sheet_name, rows, header_pos, teams)
        elif not _parse_columns(rows, teams):
            _parse_list(sheet_name, rows, teams)

    teams = {name: entries for name, entries in teams.items() if entries}
    if not teams:
        raise XlsxError(
            f"Nessuna rosa trovata in {path.name}. Servono colonne come "
            "'Squadra' e 'Giocatore', oppure un foglio per squadra, "
            "oppure una colonna per squadra."
        )
    return {
        "nome": path.stem,
        "squadre": [{"nome": name, "rosa": entries} for name, entries in teams.items()],
    }


# ---------------------------------------------------------------------------
# Calendario della lega (chi gioca contro chi, giornata per giornata)
# ---------------------------------------------------------------------------


def _round_number(text: str) -> int | None:
    match = _ROUND_TITLE.match(text or "")
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _is_calendar_sheet(name: str, rows: list[list[str]]) -> bool:
    if "calendar" in _norm(name):
        return True
    if any(_LEAGUE_DAY.match(cell) for row in rows[:30] for cell in row):
        return True
    for row in rows[:15]:
        headers = {_norm(cell) for cell in row if cell}
        if headers & _CAL_MATCHDAY and headers & (_CAL_HOME | _CAL_AWAY):
            return True
    titles = sum(
        1 for row in rows
        if row and _round_number(row[0]) is not None and not any(cell.strip() for cell in row[1:])
    )
    return titles >= 2


def _calendar_table(rows: list[list[str]]) -> list[LeagueMatch]:
    for position, row in enumerate(rows[:15]):
        headers = [_norm(cell) for cell in row]
        day = next((i for i, h in enumerate(headers) if h in _CAL_MATCHDAY), None)
        home = next((i for i, h in enumerate(headers) if h in _CAL_HOME), None)
        away = next((i for i, h in enumerate(headers) if h in _CAL_AWAY), None)
        if day is None or home is None or away is None:
            continue
        result: list[LeagueMatch] = []
        for data in rows[position + 1 :]:
            if max(day, home, away) >= len(data):
                continue
            try:
                number = int(float(data[day]))
            except ValueError:
                continue
            if data[home].strip() and data[away].strip():
                result.append(LeagueMatch(number, data[home].strip(), data[away].strip()))
        return result
    return []


def _calendar_blocks(rows: list[list[str]]) -> list[LeagueMatch]:
    result: list[LeagueMatch] = []
    current: int | None = None
    for row in rows:
        cells = [cell.strip() for cell in row if cell.strip()]
        if not cells:
            continue
        title = _round_number(cells[0])
        if title is not None and len(cells) == 1:
            current = title
            continue
        if current is None:
            continue
        texts = [cell for cell in cells if not _is_number(cell)]
        if len(texts) >= 2:
            result.append(LeagueMatch(current, texts[0], texts[1]))
        elif len(texts) == 1:
            parts = _PAIR_SPLIT.split(texts[0])
            if len(parts) == 2:
                result.append(LeagueMatch(current, parts[0].strip(), parts[1].strip()))
    return result


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _calendar_side_by_side(rows: list[list[str]]) -> list[LeagueMatch]:
    """Formato di Leghe Fantacalcio: blocchi affiancati con intestazioni
    "N ª Giornata lega" / "M ª Giornata serie a" e righe
    ``squadra casa | punti | punti | squadra trasferta | risultato``."""
    result: list[LeagueMatch] = []
    for position, row in enumerate(rows):
        for column, cell in enumerate(row):
            league_day = _LEAGUE_DAY.match(cell)
            if not league_day:
                continue
            serie_a_day = None
            for offset in range(1, 4):
                if column + offset < len(row):
                    found = _SERIE_A_DAY.match(row[column + offset])
                    if found:
                        serie_a_day = int(found.group(1))
            for data in rows[position + 1 :]:
                if column + 3 >= len(data) or not data[column].strip() or _LEAGUE_DAY.match(data[column]):
                    break
                score = _SCORE.match(data[column + 4]) if column + 4 < len(data) else None
                result.append(
                    LeagueMatch(
                        league_day=int(league_day.group(1)),
                        home=data[column].strip(),
                        away=data[column + 3].strip(),
                        serie_a_day=serie_a_day,
                        home_points=_to_float(data[column + 1]),
                        away_points=_to_float(data[column + 2]),
                        score=(int(score.group(1)), int(score.group(2))) if score else None,
                    )
                )
    return result


def read_league_calendar(path: Path | str) -> list[LeagueMatch]:
    """Legge il calendario della lega (chi gioca contro chi, giornata per giornata).

    Riconosce il formato di Leghe Fantacalcio (blocchi affiancati con "N ª Giornata
    lega"), un foglio con colonne ``Giornata | Casa | Trasferta`` e blocchi
    introdotti da un titolo "Giornata N" seguito dalle partite.
    """
    matches: list[LeagueMatch] = []
    for name, rows in read_xlsx(path):
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows or not _is_calendar_sheet(name, rows):
            continue
        matches.extend(
            _calendar_side_by_side(rows) or _calendar_table(rows) or _calendar_blocks(rows)
        )
    return matches


def is_calendar_workbook(path: Path | str) -> bool:
    """True se il file contiene solo calendari (nessuna rosa): non è una lega da scegliere."""
    try:
        sheets = [(n, [r for r in rows if any(c.strip() for c in r)]) for n, rows in read_xlsx(path)]
    except XlsxError:
        return False
    sheets = [(n, rows) for n, rows in sheets if rows]
    return bool(sheets) and all(_is_calendar_sheet(n, rows) for n, rows in sheets)