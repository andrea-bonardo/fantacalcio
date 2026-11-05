"""Costanti, impostazioni predefinite e mappe della Serie A 2026/27."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Squadre Serie A 2026/27
# ---------------------------------------------------------------------------

TEAMS: dict[str, dict[str, str]] = {
    "ATA": {"nome": "Atalanta", "slug": "atalanta"},
    "BOL": {"nome": "Bologna", "slug": "bologna"},
    "CAG": {"nome": "Cagliari", "slug": "cagliari"},
    "COM": {"nome": "Como", "slug": "como"},
    "FIO": {"nome": "Fiorentina", "slug": "fiorentina"},
    "FRO": {"nome": "Frosinone", "slug": "frosinone"},
    "GEN": {"nome": "Genoa", "slug": "genoa"},
    "INT": {"nome": "Inter", "slug": "inter"},
    "JUV": {"nome": "Juventus", "slug": "juventus"},
    "LAZ": {"nome": "Lazio", "slug": "lazio"},
    "LEC": {"nome": "Lecce", "slug": "lecce"},
    "MIL": {"nome": "Milan", "slug": "milan"},
    "MON": {"nome": "Monza", "slug": "monza"},
    "NAP": {"nome": "Napoli", "slug": "napoli"},
    "PAR": {"nome": "Parma", "slug": "parma"},
    "ROM": {"nome": "Roma", "slug": "roma"},
    "SAS": {"nome": "Sassuolo", "slug": "sassuolo"},
    "TOR": {"nome": "Torino", "slug": "torino"},
    "UDI": {"nome": "Udinese", "slug": "udinese"},
    "VEN": {"nome": "Venezia", "slug": "venezia"},
}

CODE_TO_NAME: dict[str, str] = {code: data["nome"] for code, data in TEAMS.items()}
TEAM_SLUG_TO_CODE: dict[str, str] = {data["slug"]: code for code, data in TEAMS.items()}
TEAM_NAME_TO_CODE: dict[str, str] = {}
for _code, _data in TEAMS.items():
    TEAM_NAME_TO_CODE[_data["nome"].lower()] = _code
    TEAM_NAME_TO_CODE[_data["slug"].lower()] = _code
TEAM_NAME_TO_CODE.update(
    {
        "internazionale": "INT",
        "inter milano": "INT",
        "ac milan": "MIL",
        "as roma": "ROM",
        "ss lazio": "LAZ",
        "juve": "JUV",
        "verona": "VEN",  # non usato nel 26/27, utile per dati storici
    }
)

ROLE_ORDER = {"P": 0, "D": 1, "C": 2, "A": 3}
ROLE_LABEL = {"P": "Portiere", "D": "Difensore", "C": "Centrocampista", "A": "Attaccante"}
ROLE_LABEL_SHORT = {"P": "P", "D": "D", "C": "C", "A": "A"}
# Panchina valida: quanti giocatori per ruolo. Gli altri della rosa restano in tribuna.
BENCH_QUOTAS = {"P": 2, "D": 3, "C": 3, "A": 3}

# ---------------------------------------------------------------------------
# Moduli classici ammessi (difensori, centrocampisti, attaccanti)
# ---------------------------------------------------------------------------

DEFAULT_FORMATIONS: list[str] = [
    "3-4-3",
    "3-5-2",
    "3-6-1",
    "4-3-3",
    "4-4-2",
    "4-5-1",
    "5-3-2",
    "5-4-1",
]


def parse_formation(formation: str) -> tuple[int, int, int]:
    """Converte '4-3-3' in (4, 3, 3)."""
    parts = formation.strip().replace(" ", "").split("-")
    if len(parts) != 3:
        raise ValueError(f"Modulo non valido: {formation!r}")
    d, c, a = (int(x) for x in parts)
    if d + c + a != 10:
        raise ValueError(f"Il modulo {formation!r} non schiera 10 giocatori di movimento")
    return d, c, a


# ---------------------------------------------------------------------------
# Impostazioni predefinite della lega
# ---------------------------------------------------------------------------

# Fasce del modificatore difesa della lega: portiere + 3 migliori difensori,
# almeno 4 difensori a voto. Sotto 6,00 non assegna nulla.
DEFAULT_MODIFIER_TABLE: list[list[float]] = [
    [6.00, 6.24, 1.5],
    [6.25, 6.49, 2.5],
    [6.50, 6.74, 4.0],
    [6.75, 6.99, 5.5],
    [7.00, 99.00, 7.0],
]

DEFAULT_SETTINGS: dict[str, Any] = {
    # Modificatore difesa
    "min_difensori_modificatore": 4,
    "modificatore_portiere": True,
    "tabella_modificatore": DEFAULT_MODIFIER_TABLE,
    "voto_sostituto_modificatore": 5.5,
    # Bonus / malus classici
    "bonus_gol": 3.0,
    "bonus_assist": 1.0,
    "bonus_rigore_parato": 3.0,
    "bonus_porta_inviolata": 0.0,
    "malus_ammonizione": -1.0,
    "malus_espulsione": -1.0,
    "malus_autogol": -3.0,
    "malus_rigore_sbagliato": -3.0,
    "malus_gol_subito": -1.0,
    "gol_subito_solo_portiere": True,
    # Quanto scende il voto (non il fantavoto) di portiere e difensori per ogni
    # gol subito atteso in più rispetto al solito della loro squadra.
    "peso_gol_subiti_voto": 0.50,
    # Regole rosa / formazione
    "moduli": DEFAULT_FORMATIONS,
    "max_per_squadra_serie_a": 0,  # 0 = nessun limite
    "panchina": 11,
    "rischio": 0.15,  # avversione al rischio (0 = neutrale)
    # Modello previsionale
    "partite_shrink_voto": 4,
    "partite_shrink_rate": 6,
    "fattore_casa_attacco": 1.08,
    "fattore_trasferta_attacco": 0.92,
    "giornata": None,  # None = giornata corrente dei dati
}

# Alias utili per leggere file di lega in italiano o in inglese.
SETTINGS_ALIASES: dict[str, str] = {
    "min_defenders_modifier": "min_difensori_modificatore",
    "modifier_includes_goalkeeper": "modificatore_portiere",
    "modifier_table": "tabella_modificatore",
    "modifier_replacement_vote": "voto_sostituto_modificatore",
    "goal_bonus": "bonus_gol",
    "assist_bonus": "bonus_assist",
    "penalty_saved_bonus": "bonus_rigore_parato",
    "clean_sheet_bonus": "bonus_porta_inviolata",
    "yellow_malus": "malus_ammonizione",
    "red_malus": "malus_espulsione",
    "own_goal_malus": "malus_autogol",
    "penalty_missed_malus": "malus_rigore_sbagliato",
    "goal_conceded_malus": "malus_gol_subito",
    "goal_conceded_only_gk": "gol_subito_solo_portiere",
    "conceded_vote_weight": "peso_gol_subiti_voto",
    "formations": "moduli",
    "max_per_serie_a_team": "max_per_squadra_serie_a",
    "bench_size": "panchina",
    "risk_aversion": "rischio",
    "vote_shrink_games": "partite_shrink_voto",
    "rate_shrink_games": "partite_shrink_rate",
    "home_attack_factor": "fattore_casa_attacco",
    "away_attack_factor": "fattore_trasferta_attacco",
    "matchday": "giornata",
}