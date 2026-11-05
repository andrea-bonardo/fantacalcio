"""Test di base per FantaLineup 26/27.

I test usano solo la libreria standard e non richiedono rete: costruiscono
piccoli dataset sintetici per verificare modificatore, matchup, motore e
ottimizzatore.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fanta.config import parse_formation
from fanta.dataio import (
    load_players,
    load_serie_a,
    load_settings,
    parse_player,
    write_json,
)
from fanta.engine import ProjectionEngine
from fanta.league import PlayerResolver, build_manual_league, load_league
from fanta.matchup import MatchupModel
from fanta.models import (
    Fixture,
    LeagueSettings,
    Matchup,
    Player,
    PlayerProjection,
    SerieAData,
    SerieATeam,
)
from fanta.optimizer import LineupOptimizer, defense_modifier, modifier_value


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_player(
    player_id: str,
    name: str,
    team: str,
    role: str,
    *,
    starter_prob: float = 0.9,
    price: float = 10.0,
    media_voto: float = 6.5,
    appearances: int = 10,
    goals: int = 0,
    assists: int = 0,
    goals_conceded: int = 0,
) -> Player:
    return Player(
        id=player_id,
        name=name,
        team=team,
        role=role,
        price=price,
        appearances=appearances,
        media_voto=media_voto,
        goals=goals,
        assists=assists,
        goals_conceded=goals_conceded,
        starter_prob=starter_prob,
    )


def make_matchup(
    *,
    opponent: str = "LAZ",
    home: bool = True,
    difficulty: float = 3.0,
    lambda_for: float = 1.5,
    lambda_against: float = 1.2,
) -> Matchup:
    return Matchup(
        opponent_code=opponent,
        opponent_name=opponent,
        home=home,
        difficulty=difficulty,
        label="Media",
        lambda_for=lambda_for,
        lambda_against=lambda_against,
        opponent_position=10,
    )


def make_projection(
    player: Player,
    *,
    points: float = 6.5,
    risk: float = 0.4,
    matchup: Matchup | None = None,
) -> PlayerProjection:
    return PlayerProjection(
        player=player,
        matchup=matchup or make_matchup(),
        base_vote=points,
        modifier_vote=0.0,
        expected_bonus=0.0,
        expected_malus=0.0,
        expected_points=points,
        goal_prob=0.0,
        assist_prob=0.0,
        expected_goals=0.0,
        expected_assists=0.0,
        risk=risk,
    )


def make_serie_a() -> SerieAData:
    teams = {
        "INT": SerieATeam("INT", "Inter", "inter", 1, 30, 10, 10, 0, 0, 25, 5, 20),
        "ROM": SerieATeam("ROM", "Roma", "roma", 2, 20, 10, 6, 2, 2, 18, 10, 8),
        "LAZ": SerieATeam("LAZ", "Lazio", "lazio", 3, 15, 10, 4, 3, 3, 14, 12, 2),
        "VEN": SerieATeam("VEN", "Venezia", "venezia", 4, 5, 10, 1, 2, 7, 5, 25, -20),
    }
    fixtures = [
        Fixture(11, "INT", "VEN", "2026-11-07", "15:00"),
        Fixture(11, "ROM", "LAZ", "2026-11-07", "18:00"),
        Fixture(11, "LAZ", "INT", "2026-11-14", "15:00"),
        Fixture(11, "VEN", "ROM", "2026-11-14", "18:00"),
    ]
    return SerieAData(
        season="2026-27",
        updated="2026-10-31",
        teams=teams,
        fixtures=fixtures,
        current_matchday=11,
    )


def make_roster() -> list[PlayerProjection]:
    projections: list[PlayerProjection] = []
    # 3 portieri
    for index in range(3):
        player = make_player(f"P{index}", f"Portiere {index}", "INT", "P", price=5)
        projections.append(make_projection(player, points=6.0))
    # 8 difensori
    for index in range(8):
        player = make_player(f"D{index}", f"Difensore {index}", "ROM", "D", price=8)
        projections.append(make_projection(player, points=6.4))
    # 8 centrocampisti
    for index in range(8):
        player = make_player(f"C{index}", f"Centrocampista {index}", "LAZ", "C", price=10)
        projections.append(make_projection(player, points=6.8))
    # 6 attaccanti
    for index in range(6):
        player = make_player(f"A{index}", f"Attaccante {index}", "VEN", "A", price=15)
        projections.append(make_projection(player, points=7.5))
    return projections


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_parse_formation(self) -> None:
        self.assertEqual(parse_formation("4-3-3"), (4, 3, 3))
        self.assertEqual(parse_formation(" 3 - 5 - 2 "), (3, 5, 2))
        with self.assertRaises(ValueError):
            parse_formation("4-4-4")
        with self.assertRaises(ValueError):
            parse_formation("4-3")


class TestDataIO(unittest.TestCase):
    def test_parse_player_italian_and_english(self) -> None:
        player = parse_player(
            {
                "id": "1",
                "nome": "Mario Rossi",
                "squadra": "Inter",
                "ruolo": "Attaccante",
                "quotazione": "12,5",
                "presenze": 8,
                "media_voto": "6,4",
                "titolarita": 90,
            }
        )
        self.assertEqual(player.team, "INT")
        self.assertEqual(player.role, "A")
        self.assertAlmostEqual(player.price, 12.5)
        self.assertAlmostEqual(player.starter_prob, 0.9)

        english = parse_player(
            {
                "id": "2",
                "name": "John Doe",
                "team": "VEN",
                "role": "midfielder",
                "price": 5,
                "appearances": 0,
                "average_vote": 0,
                "starter_prob": 0.25,
            }
        )
        self.assertEqual(english.team, "VEN")
        self.assertEqual(english.role, "C")
        self.assertIsNone(english.media_voto)


class TestModifier(unittest.TestCase):
    def test_modifier_value(self) -> None:
        table = [[6.00, 6.49, 1.0], [6.50, 6.99, 3.0], [7.00, 99.0, 6.0]]
        self.assertEqual(modifier_value(5.99, table), 0.0)
        self.assertEqual(modifier_value(6.00, table), 1.0)
        self.assertEqual(modifier_value(6.49, table), 1.0)
        self.assertEqual(modifier_value(6.50, table), 3.0)
        self.assertEqual(modifier_value(7.00, table), 6.0)

    def test_defense_modifier(self) -> None:
        settings = LeagueSettings()
        goalkeeper = make_projection(make_player("P", "GK", "INT", "P"), points=6.2)
        defenders = [
            make_projection(make_player(f"D{i}", f"D{i}", "INT", "D"), points=vote)
            for i, vote in enumerate([6.5, 6.4, 6.3, 6.0])
        ]
        modifier, average, best = defense_modifier(goalkeeper, defenders, settings)
        self.assertAlmostEqual(average, (6.5 + 6.4 + 6.3 + 6.2) / 4)
        self.assertEqual(modifier, 1.0)
        self.assertEqual(best, [6.5, 6.4, 6.3])

        modifier, average, best = defense_modifier(goalkeeper, defenders[:3], settings)
        self.assertEqual(modifier, 0.0)
        self.assertEqual(average, 0.0)
        self.assertEqual(best, [6.5, 6.4, 6.3])


class TestMatchup(unittest.TestCase):
    def test_strong_team_has_easier_matchup(self) -> None:
        serie_a = make_serie_a()
        model = MatchupModel(serie_a, LeagueSettings())
        strong = model.matchup("INT", 11)
        weak = model.matchup("VEN", 11)
        self.assertGreater(strong.lambda_for, weak.lambda_for)
        self.assertLess(strong.lambda_against, weak.lambda_against)
        self.assertLess(strong.difficulty, weak.difficulty)
        self.assertTrue(strong.home)
        self.assertFalse(weak.home)

    def test_missing_fixture(self) -> None:
        serie_a = make_serie_a()
        model = MatchupModel(serie_a, LeagueSettings())
        matchup = model.matchup("INT", 99)
        self.assertEqual(matchup.label, "Da definire")
        self.assertEqual(matchup.difficulty, 3.0)


class TestEngine(unittest.TestCase):
    def test_projection_and_matchup_effect(self) -> None:
        serie_a = make_serie_a()
        settings = LeagueSettings()
        engine = ProjectionEngine(settings, MatchupModel(serie_a, settings))

        strong_striker = make_player(
            "S1", "Striker Forte", "INT", "A", goals=6, assists=2, media_voto=6.6
        )
        weak_striker = make_player(
            "S2", "Striker Debole", "VEN", "A", goals=6, assists=2, media_voto=6.6
        )
        strong_projection = engine.project(strong_striker, 11)
        weak_projection = engine.project(weak_striker, 11)

        self.assertGreater(strong_projection.expected_goals, 0)
        self.assertGreater(strong_projection.expected_points, weak_projection.expected_points)
        self.assertGreater(strong_projection.goal_prob, 0)

        goalkeeper = make_player(
            "G1", "Portiere", "VEN", "P", media_voto=6.0, goals_conceded=20
        )
        gk_projection = engine.project(goalkeeper, 11)
        self.assertLess(gk_projection.expected_malus, -1.0)

    def test_low_starter_probability_is_penalized(self) -> None:
        serie_a = make_serie_a()
        settings = LeagueSettings()
        engine = ProjectionEngine(settings, MatchupModel(serie_a, settings))
        regular = make_player("R", "Titolare", "INT", "C", starter_prob=0.9)
        reserve = make_player("B", "Riserva", "INT", "C", starter_prob=0.05)
        self.assertGreater(
            engine.project(regular, 11).expected_points,
            engine.project(reserve, 11).expected_points,
        )


class TestOptimizer(unittest.TestCase):
    def test_basic_lineup(self) -> None:
        settings = LeagueSettings()
        settings.moduli = ["4-3-3", "4-4-2"]
        optimizer = LineupOptimizer(make_roster(), settings, max_alternatives=3)
        result = optimizer.optimize()
        lineup = result.best

        self.assertEqual(lineup.formation, "4-3-3")
        self.assertEqual(len(lineup.defenders), 4)
        self.assertEqual(len(lineup.midfielders), 3)
        self.assertEqual(len(lineup.forwards), 3)
        self.assertEqual(len(lineup.starters), 11)
        self.assertGreater(lineup.defense_modifier, 0)
        self.assertGreater(result.evaluated, 0)

        ids = [projection.player.id for projection in lineup.starters]
        self.assertEqual(len(ids), len(set(ids)))
        bench_ids = [projection.player.id for projection in lineup.bench]
        self.assertFalse(set(ids) & set(bench_ids))
        self.assertEqual(len(lineup.bench), 11)
        for role, quota in {"P": 2, "D": 3, "C": 3, "A": 3}.items():
            self.assertEqual(
                sum(1 for projection in lineup.bench if projection.player.role == role), quota
            )

    def test_team_limit(self) -> None:
        # Una rosa con i giocatori migliori concentrati nelle prime squadre:
        # senza limite l'ottimizzatore ne schiererebbe piu' di due per squadra.
        teams = ["ROM", "INT", "LAZ", "VEN", "CAG", "GEN", "TOR", "FIO"]
        points = {"P": 7.0, "D": 8.0, "C": 8.0, "A": 8.0}
        projections = []
        for role, step in (("P", 0.1), ("D", 0.2), ("C", 0.2), ("A", 0.2)):
            for index, team in enumerate(teams):
                value = points[role] - index * step
                player = make_player(
                    f"{role}{index}", f"{role}{index}", team, role, price=10
                )
                projections.append(make_projection(player, points=value))

        settings = LeagueSettings()
        settings.moduli = ["4-4-2"]
        optimizer = LineupOptimizer(projections, settings, max_alternatives=0)
        unrestricted = optimizer.optimize()
        counts: dict[str, int] = {}
        for projection in unrestricted.best.starters:
            counts[projection.player.team] = counts.get(projection.player.team, 0) + 1
        self.assertGreater(max(counts.values()), 2)

        settings.max_per_squadra_serie_a = 2
        optimizer = LineupOptimizer(projections, settings, max_alternatives=0)
        result = optimizer.optimize()
        counts = {}
        for projection in result.best.starters:
            counts[projection.player.team] = counts.get(projection.player.team, 0) + 1
        self.assertTrue(all(value <= 2 for value in counts.values()))
        self.assertEqual(len(result.best.starters), 11)

    def test_missing_role(self) -> None:
        projections = [p for p in make_roster() if p.player.role != "P"]
        optimizer = LineupOptimizer(projections, LeagueSettings())
        with self.assertRaises(Exception):
            optimizer.optimize()


class TestLeague(unittest.TestCase):
    def test_resolver_exact_and_fuzzy(self) -> None:
        players = {
            "1": make_player("1", "Mario Rossi", "INT", "A"),
            "2": make_player("2", "Luca Bianchi", "ROM", "D"),
            "3": make_player("3", "Rossi F.", "LAZ", "C"),
            "4": make_player("4", "Martinez L.", "INT", "A"),
        }
        resolver = PlayerResolver(players)
        player, warning = resolver.resolve("Mario Rossi")
        self.assertEqual(player.id, "1")
        self.assertIsNone(warning)
        player, warning = resolver.resolve("Bianchi (ROM)")
        self.assertEqual(player.id, "2")
        player, warning = resolver.resolve("Rossi F.")
        self.assertEqual(player.id, "3")
        player, warning = resolver.resolve("Lautaro Martinez")
        self.assertEqual(player.id, "4")

    def test_load_league_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            write_json(
                data_dir / "giocatori_2627.json",
                {
                    "giocatori": [
                        {
                            "id": "1",
                            "nome": "Mario Rossi",
                            "squadra": "INT",
                            "ruolo": "A",
                            "titolarita": 0.9,
                        },
                        {
                            "id": "2",
                            "nome": "Luca Bianchi",
                            "squadra": "ROM",
                            "ruolo": "D",
                            "titolarita": 0.8,
                        },
                    ]
                },
            )
            write_json(
                data_dir / "serie_a_2627.json",
                {
                    "stagione": "2026-27",
                    "giornata_corrente": 11,
                    "squadre": [
                        {
                            "codice": "INT",
                            "nome": "Inter",
                            "posizione": 1,
                            "punti": 30,
                            "giocate": 10,
                            "gf": 25,
                            "gs": 5,
                            "dr": 20,
                        },
                        {
                            "codice": "ROM",
                            "nome": "Roma",
                            "posizione": 2,
                            "punti": 20,
                            "giocate": 10,
                            "gf": 18,
                            "gs": 10,
                            "dr": 8,
                        },
                    ],
                    "partite": [
                        {
                            "giornata": 11,
                            "casa": "INT",
                            "trasferta": "ROM",
                            "data": "2026-11-07",
                        }
                    ],
                },
            )
            write_json(data_dir / "impostazioni.json", {})
            league_path = data_dir / "lega.json"
            write_json(
                league_path,
                {
                    "nome": "Lega Test",
                    "giornata": 11,
                    "impostazioni": {"bonus_gol": 3.5, "moduli": ["4-3-3"]},
                    "squadre": [
                        {
                            "nome": "Team A",
                            "rosa": ["1", "Luca Bianchi (ROM)"],
                        }
                    ],
                },
            )

            players, _ = load_players(data_dir=data_dir)
            serie_a = load_serie_a(data_dir=data_dir)
            settings = load_settings(data_dir=data_dir)
            league = load_league(
                league_path,
                data_dir=data_dir,
                players=players,
                serie_a=serie_a,
                settings=settings,
            )
            self.assertEqual(league.name, "Lega Test")
            self.assertEqual(league.matchday, 11)
            self.assertEqual(league.settings.bonus_gol, 3.5)
            self.assertEqual(league.settings.moduli, ["4-3-3"])
            self.assertEqual(len(league.teams), 1)
            self.assertEqual(len(league.teams[0].roster), 2)

    def test_manual_league(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            write_json(
                data_dir / "giocatori_2627.json",
                {
                    "giocatori": [
                        {
                            "id": "1",
                            "nome": "Mario Rossi",
                            "squadra": "INT",
                            "ruolo": "A",
                            "titolarita": 0.9,
                        }
                    ]
                },
            )
            write_json(
                data_dir / "serie_a_2627.json",
                {
                    "giornata_corrente": 11,
                    "squadre": [
                        {
                            "codice": "INT",
                            "nome": "Inter",
                            "posizione": 1,
                            "punti": 30,
                            "giocate": 10,
                            "gf": 25,
                            "gs": 5,
                            "dr": 20,
                        }
                    ],
                    "partite": [],
                },
            )
            write_json(data_dir / "impostazioni.json", {})
            league = build_manual_league(
                "Team Manuale",
                "Mario Rossi",
                data_dir=data_dir,
            )
            self.assertEqual(league.name, "Lega manuale")
            self.assertEqual(league.teams[0].name, "Team Manuale")
            self.assertEqual(league.teams[0].roster[0].name, "Mario Rossi")


if __name__ == "__main__":
    unittest.main()
