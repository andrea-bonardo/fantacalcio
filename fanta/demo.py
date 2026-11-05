"""Generazione della lega dimostrativa inclusa nel progetto.

La lega demo contiene 8 squadre con rose realistiche, costruite con un draft
a serpentina sui giocatori disponibili. Serve per provare subito il programma
senza dover importare una lega reale.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .dataio import (
    LEAGUE_FILE,
    ensure_data,
    load_players,
    load_serie_a,
    resolve_data_dir,
    write_json,
)
from .models import LeagueSettings, Player, SerieAData

DEMO_TEAMS: list[dict[str, str]] = [
    {"nome": "I Magnifici", "allenatore": "Luca"},
    {"nome": "Real Fantasia", "allenatore": "Marco"},
    {"nome": "Atletico Ma Non Troppo", "allenatore": "Giulia"},
    {"nome": "Gli Inossidabili", "allenatore": "Paolo"},
    {"nome": "FC Pizzoccheri", "allenatore": "Anna"},
    {"nome": "Dinamo Kebab", "allenatore": "Davide"},
    {"nome": "Sporting Cuggiono", "allenatore": "Sara"},
    {"nome": "I Cuggini", "allenatore": "Matteo"},
]

DEMO_SLOTS = {"P": 3, "D": 8, "C": 8, "A": 6}


def _draft_role(
    players: list[Player],
    teams: list[dict[str, str]],
    slots: int,
) -> dict[str, list[str]]:
    """Draft a serpentina: ogni squadra riceve al massimo ``slots`` giocatori."""
    rosters: dict[str, list[str]] = {team["nome"]: [] for team in teams}
    pool = players[: len(teams) * slots]
    index = 0
    for round_index in range(slots):
        order = teams if round_index % 2 == 0 else list(reversed(teams))
        for team in order:
            if index >= len(pool):
                return rosters
            rosters[team["nome"]].append(pool[index].id)
            index += 1
    return rosters


def build_demo_payload(
    players: dict[str, Player],
    serie_a: SerieAData,
    *,
    teams: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    teams = teams or DEMO_TEAMS
    eligible = [
        player
        for player in players.values()
        if player.available and player.starter_prob >= 0.20
    ]
    if len(eligible) < len(teams) * 10:
        eligible = [player for player in players.values() if player.available]

    rosters: dict[str, list[str]] = {team["nome"]: [] for team in teams}
    for role, slots in DEMO_SLOTS.items():
        pool = [player for player in eligible if player.role == role]
        pool.sort(key=lambda p: (-p.price, -p.fvm, -p.appearances, p.name))
        if len(pool) < len(teams) * slots:
            pool = [player for player in players.values() if player.available and player.role == role]
            pool.sort(key=lambda p: (-p.price, -p.fvm, -p.appearances, p.name))
        role_rosters = _draft_role(pool, teams, slots)
        for team in teams:
            rosters[team["nome"]].extend(role_rosters[team["nome"]])

    payload = {
        "nome": "Lega Demo 2026/27",
        "giornata": serie_a.current_matchday,
        "squadra": teams[0]["nome"],
        "impostazioni": dataclasses.asdict(LeagueSettings()),
        "squadre": [
            {
                "nome": team["nome"],
                "allenatore": team["allenatore"],
                "rosa": rosters[team["nome"]],
            }
            for team in teams
        ],
    }
    return payload


def ensure_demo_league(
    data_dir: Path | str | None = None,
    *,
    force: bool = False,
) -> Path:
    directory = resolve_data_dir(data_dir)
    path = directory / LEAGUE_FILE
    if path.exists() and not force:
        return path
    ensure_data(directory)
    players, _ = load_players(data_dir=directory)
    serie_a = load_serie_a(data_dir=directory)
    payload = build_demo_payload(players, serie_a)
    write_json(path, payload)
    return path
