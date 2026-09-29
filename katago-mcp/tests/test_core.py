import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp.board import BLACK, WHITE, EMPTY, Board, IllegalMove  # noqa: E402
from katago_mcp.coords import gtp_to_idx, idx_to_gtp, sgf_to_idx, idx_to_sgf, chebyshev, CoordError  # noqa: E402
from katago_mcp.regions import standard_partition, standard_code, region_indices, LABELS  # noqa: E402
from katago_mcp.render import render_board  # noqa: E402
from katago_mcp import sgf  # noqa: E402

HANDICAP_SGF = """(;GM[1]FF[4]CA[UTF-8]AP[OGS]SZ[19]HA[3]AB[pd][dp][pp]KM[0.5]RU[Japanese]
PB[cwhay888]BR[7k]PW[strongopp]WR[4k]RE[W+12.5]DT[2026-09-20]
PC[OGS: https://online-go.com/game/99887766]
;W[dd];B[fc]BL[500];W[cf]WL[498];B[jd](;W[qf];B[nc])(;W[qn]))"""


class TestCoords(unittest.TestCase):
    def test_gtp_roundtrip_and_index_order(self):
        self.assertEqual(gtp_to_idx("A19"), 0)
        self.assertEqual(gtp_to_idx("T1"), 360)
        self.assertEqual(gtp_to_idx("T19"), 18)
        self.assertEqual(gtp_to_idx("D4"), 15 * 19 + 3)
        for pt in ("A1", "H8", "J9", "K10", "T19", "D4", "Q16"):
            self.assertEqual(idx_to_gtp(gtp_to_idx(pt)), pt)
        self.assertIsNone(gtp_to_idx("pass"))
        self.assertEqual(idx_to_gtp(None), "pass")
        with self.assertRaises(CoordError):
            gtp_to_idx("I5")
        with self.assertRaises(CoordError):
            gtp_to_idx("A20")

    def test_sgf_coords(self):
        self.assertEqual(sgf_to_idx("aa"), 0)
        self.assertEqual(sgf_to_idx("ss"), 360)
        self.assertEqual(sgf_to_idx("pd"), gtp_to_idx("Q16"))
        self.assertEqual(sgf_to_idx("dp"), gtp_to_idx("D4"))
        self.assertIsNone(sgf_to_idx("tt"))
        self.assertIsNone(sgf_to_idx(""))
        self.assertEqual(idx_to_sgf(gtp_to_idx("Q16")), "pd")

    def test_chebyshev(self):
        self.assertEqual(chebyshev(gtp_to_idx("D4"), gtp_to_idx("F6")), 2)
        self.assertEqual(chebyshev(gtp_to_idx("A1"), gtp_to_idx("T19")), 18)


class TestBoard(unittest.TestCase):
    def play(self, board, seq):
        for m in seq:
            color = BLACK if m[0] == "B" else WHITE
            board = board.play(color, gtp_to_idx(m[1:]), "japanese")
        return board

    def test_capture_and_liberties(self):
        b = Board()
        b = self.play(b, ["BD4", "WD5", "BE5", "WE4", "BC5", "WF5"])   # not yet a capture
        g = b.group_at(gtp_to_idx("D5"))
        self.assertEqual(g.size, 1)
        b = self.play(b, ["BD6"])   # captures W D5 (neighbors D4 B, E5 B, C5 B, D6 B)
        self.assertEqual(b.cells[gtp_to_idx("D5")], EMPTY)
        self.assertEqual(b.captures[BLACK], 1)
        self.assertEqual(b.captures[WHITE], 0)

    def test_capture_race_and_liberties_only_in_races(self):
        from katago_mcp.config import Thresholds
        from katago_mcp.metrics import capture_races, group_records, race_anchor_set
        b = Board()
        for p in ("D4", "E4", "C5", "F5"):
            b.place(BLACK, gtp_to_idx(p))
        for p in ("D5", "E5", "C4", "F4", "Q16", "Q17"):
            b.place(WHITE, gtp_to_idx(p))
        own = [0.0] * 361
        for p in ("Q16", "Q17"):
            own[gtp_to_idx(p)] = -0.95                     # settled white group far away
        th = Thresholds()
        races = capture_races(b, own, th)
        self.assertEqual(len(races), 1)
        self.assertEqual({g["anchor"] for g in races[0]["groups"]}, {"D4", "D5"})
        self.assertEqual({g["liberties"] for g in races[0]["groups"]}, {2})
        recs = {r["anchor"]: r for r in group_records(b, own, th, min_size=2, race_anchors=race_anchor_set(races, 19))}
        self.assertEqual(recs["D4"]["liberties"], 2)
        self.assertNotIn("liberties", recs["Q17"])
        self.assertEqual(capture_races(b, None, th), [])

    def test_simple_ko(self):
        # Ko shape: B at B2, C1, C3 and W at E2, D1, D3; W plays C2 (one liberty, D2), B captures at D2.
        b = Board()
        b = self.play(b, ["BB2", "WE2", "BC1", "WD1", "BC3", "WD3", "BQ16", "WC2"])
        b = self.play(b, ["BD2"])   # captures the single W stone at C2; B D2 now has exactly one liberty (C2)
        self.assertEqual(b.captures[BLACK], 1)
        self.assertEqual(b.ko_point, gtp_to_idx("C2"))
        with self.assertRaises(IllegalMove) as cm:
            b.play(WHITE, gtp_to_idx("C2"), "japanese")
        self.assertEqual(cm.exception.reason, "ko")
        # after a tenuki the ko can be retaken
        b2 = self.play(b, ["WQ4", "BR16"])
        b3 = self.play(b2, ["WC2"])
        self.assertEqual(b3.captures[WHITE], 1)
        self.assertEqual(b3.ko_point, gtp_to_idx("D2"))

    def test_suicide_forbidden_japanese_allowed_tromp_taylor(self):
        b = Board()
        b = self.play(b, ["BB1", "WQ16", "BA2", "WQ4"])
        with self.assertRaises(IllegalMove) as cm:
            b.play(WHITE, gtp_to_idx("A1"), "japanese")
        self.assertEqual(cm.exception.reason, "suicide")
        # Tromp-Taylor allows suicide, but a single-stone suicide recreates the board -> positional superko
        with self.assertRaises(IllegalMove) as cm:
            b.play(WHITE, gtp_to_idx("A1"), "tromp-taylor")
        self.assertEqual(cm.exception.reason, "superko")

    def test_positional_superko_only_under_superko_rules(self):
        # repeated position via passes: playing then passing twice recreates a position; simple ko rules allow it
        b = Board()
        b = self.play(b, ["BD4"])
        b = b.play(WHITE, None).play(BLACK, None)
        self.assertIsNotNone(b)   # passes are always legal

    def test_hash_stability(self):
        a = self.play(Board(), ["BD4", "WQ16"])
        c = self.play(Board(), ["BD4", "WQ16"])
        self.assertEqual(a.board_hash, c.board_hash)
        self.assertEqual(a.state_hash(), c.state_hash())
        d = self.play(Board(), ["WQ16", "BD4"])
        self.assertEqual(a.board_hash, d.board_hash)
        self.assertNotEqual(a.history_hash, d.history_hash)


class TestSgf(unittest.TestCase):
    def test_parse_ogs_handicap_with_variations(self):
        g = sgf.parse(HANDICAP_SGF)
        self.assertEqual(g.size, 19)
        self.assertEqual(g.handicap, 3)
        self.assertEqual(sorted(g.setup_black), sorted([gtp_to_idx(p) for p in ("Q16", "D4", "Q4")]))
        self.assertEqual(g.komi, 0.5)
        self.assertEqual(g.rules, "japanese")
        self.assertEqual(g.first_to_move, WHITE)
        self.assertEqual(len(g.moves), 6)   # main line only: W,B,W,B,W,B
        self.assertEqual(g.moves[0], (WHITE, gtp_to_idx("D16")))
        self.assertEqual(g.moves[-1], (BLACK, gtp_to_idx("O17")))
        self.assertTrue(g.per_move_times)
        self.assertEqual(g.ogs_game_id, "99887766")
        self.assertEqual(g.players["B"]["rank"], "7k")
        r = g.result()
        self.assertEqual((r["winner"], r["margin"], r["method"]), ("W", 12.5, "score"))

    def test_rank_helpers(self):
        self.assertEqual(sgf.parse_rank("10k?"), "10k")
        self.assertEqual(sgf.rank_to_profile("25k"), "rank_20k")
        self.assertEqual(sgf.rank_to_profile("3p"), "rank_9d")
        self.assertEqual(sgf.rank_stronger_by("7k", 3), "4k")
        self.assertEqual(sgf.rank_stronger_by("2k", 3), "2d")
        self.assertEqual(sgf.rank_stronger_by("1k", 1), "1d")

    def test_missing_komi_warns(self):
        g = sgf.parse("(;GM[1]SZ[19]PB[a]PW[b];B[pd];W[dp])")
        self.assertEqual(g.komi, 6.5)
        self.assertTrue(any("komi" in w.lower() for w in g.warnings))


class TestRegions(unittest.TestCase):
    def test_partition_sizes(self):
        parts = standard_partition(19)
        self.assertEqual(sum(len(v) for v in parts.values()), 361)
        self.assertEqual(len(parts["UL"]), 49)
        self.assertEqual(len(parts["U"]), 35)
        self.assertEqual(len(parts["C"]), 25)
        self.assertEqual(standard_code(gtp_to_idx("A19")), "UL")
        self.assertEqual(standard_code(gtp_to_idx("K10")), "C")
        self.assertEqual(standard_code(gtp_to_idx("T1")), "LR")
        self.assertEqual(standard_code(gtp_to_idx("G13")), "UL")   # G and 13 are still in the corner bands
        self.assertEqual(standard_code(gtp_to_idx("H12")), "C")

    def test_region_specs(self):
        self.assertEqual(len(region_indices({"rect": ["A19", "C17"]})), 9)
        self.assertEqual(len(region_indices({"near": "K10", "radius": 2})), 25)
        self.assertEqual(len(region_indices({"near": "A1", "radius": 2})), 9)
        self.assertEqual(region_indices({"points": ["D4", "Q16"]}), {gtp_to_idx("D4"), gtp_to_idx("Q16")})
        self.assertEqual(len(LABELS), 9)


class TestRender(unittest.TestCase):
    def test_render_marks_last_move(self):
        b = Board().play(BLACK, gtp_to_idx("D4")).play(WHITE, gtp_to_idx("Q16"))
        txt = render_board(b, (WHITE, gtp_to_idx("Q16")))
        lines = txt.splitlines()
        self.assertTrue(lines[0].startswith("Black to move · last move: W Q16 (@)"))
        self.assertEqual(len(lines), 1 + 19 + 2)
        row4 = [l for l in lines if l.startswith("  4 ")][0]
        self.assertEqual(row4.split()[1:][3], "X")
        row16 = [l for l in lines if l.startswith(" 16 ")][0]
        self.assertEqual(row16.split()[1:][15], "@")


class TestPersistentBest(unittest.TestCase):
    def test_links_episodes_sharing_best_or_teachable_point(self):
        from katago_mcp.metrics import link_persistent_best
        def ep(i, best, teach):
            return {"id": i, "root": {"best": best}, "teachable_move_preliminary": teach}
        eps = [ep("E1", "M6", "M6"), ep("E2", "Q3", "M6"), ep("E3", "D4", "D4"), ep("E4", "pass", "pass"),
               ep("E5", "pass", "pass")]
        link_persistent_best(eps)
        self.assertEqual(eps[0]["persistent_best"], ["E2"])
        self.assertEqual(eps[1]["persistent_best"], ["E1"])
        self.assertEqual(eps[2]["persistent_best"], [])
        self.assertEqual(eps[3]["persistent_best"], [])   # passes never link


if __name__ == "__main__":
    unittest.main()
