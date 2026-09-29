"""Derived metrics (tool contract §3).  Deterministic; no engine access here.

Conventions: `positions[k]` is the Analysis of P_k (after move k); move n is
`moves[n-1]`; score leads inside `Analysis` are Black-perspective.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .board import BLACK, WHITE, EMPTY, COLOR_CHAR, Board
from .config import Thresholds
from .coords import chebyshev, gtp_to_idx, idx_to_gtp, neighbors
from .engine import Analysis
from .regions import LABELS, STANDARD_CODES, standard_code, standard_partition, bbox


def sign_of(color: int) -> float:
    return 1.0 if color == BLACK else -1.0


@dataclass
class GameAnalysis:
    game_id: str
    size: int
    rules: str
    komi: float
    handicap: int
    student_color: int | None
    moves: list[tuple[int, int | None]]
    setup_black: list[int]
    setup_white: list[int]
    first_to_move: int
    positions: list[Analysis]                 # P_0 .. P_M (may be shorter if incomplete)
    refs: list[str]
    visits_per_move: int
    result: dict
    profiles: dict[str, str] = field(default_factory=dict)      # alias -> rank_xx
    after_best_ownership: dict[int, list[float]] = field(default_factory=dict)   # move n -> ownership after best move
    boards: list[Board] = field(default_factory=list, repr=False)

    def build_boards(self) -> None:
        if self.boards:
            return
        b = Board(self.size, to_move=self.first_to_move)
        for i in self.setup_black:
            b.place(BLACK, i)
        for i in self.setup_white:
            b.place(WHITE, i)
        self.boards = [b]
        for color, idx in self.moves:
            b = b.play(color, idx, self.rules)
            self.boards.append(b)

    @property
    def M(self) -> int:
        return len(self.positions) - 1


# ------------------------------------------------------------------ helpers
def policy_prob(a: Analysis, idx: int | None, size: int) -> float | None:
    if a.policy is None:
        return None
    i = size * size if idx is None else idx
    v = a.policy[i]
    return None if v is None or v < 0 else float(v)


def human_prob(a: Analysis, alias_to_profile: dict[str, str], idx: int | None, size: int) -> dict[str, float]:
    out = {}
    i = size * size if idx is None else idx
    for alias, prof in alias_to_profile.items():
        arr = a.human.get(prof)
        if arr is not None and 0 <= i < len(arr) and arr[i] >= 0:
            out[alias] = round(float(arr[i]), 4)
    return out


def best_candidate(a: Analysis) -> int | None:
    return a.candidates[0].move if a.candidates else None


def candidate_for(a: Analysis, idx: int | None):
    for c in a.candidates:
        if c.move == idx:
            return c
    return None


def acceptable_set(a: Analysis, mover: int, th: Thresholds) -> list[int | None]:
    if not a.candidates:
        return []
    s = sign_of(mover)
    root_visits = max(1, a.visits)
    best = max(s * c.score_lead for c in a.candidates if c.visits >= th.acceptable_min_visit_share * root_visits) \
        if any(c.visits >= th.acceptable_min_visit_share * root_visits for c in a.candidates) else s * a.candidates[0].score_lead
    return [c.move for c in a.candidates
            if c.visits >= th.acceptable_min_visit_share * root_visits and s * c.score_lead >= best - th.acceptable_margin]


# ------------------------------------------------------------------ move rows
def move_rows(ga: GameAnalysis, th: Thresholds) -> list[dict]:
    rows = []
    for n in range(1, ga.M + 1):
        color, idx = ga.moves[n - 1]
        before, after = ga.positions[n - 1], ga.positions[n]
        s = sign_of(color)
        pl = max(0.0, s * (before.score_lead - after.score_lead))
        best = best_candidate(before)
        acc = acceptable_set(before, color, th)
        rows.append({
            "n": n, "color": COLOR_CHAR[color], "move": idx_to_gtp(idx, ga.size), "idx": idx,
            "score_before": round(before.score_lead, 2), "score_after": round(after.score_lead, 2),
            "points_lost": round(pl, 2),
            "best": idx_to_gtp(best, ga.size), "best_idx": best,
            "prior_played": policy_prob(before, idx, ga.size), "prior_best": policy_prob(before, best, ga.size),
            "in_acceptable_set": idx in acc, "acceptable": acc,
            "visits": before.visits, "position_ref": ga.refs[n - 1] if n - 1 < len(ga.refs) else None,
        })
    return rows


# ------------------------------------------------------------------ phases
def settledness(a: Analysis, th: Thresholds) -> float | None:
    if a.ownership is None:
        return None
    n = len(a.ownership)
    return sum(1 for o in a.ownership if abs(o) >= th.settled_abs) / n


def phases(ga: GameAnalysis, th: Thresholds) -> dict:
    M = ga.M
    sett = [settledness(a, th) for a in ga.positions]
    if any(x is None for x in sett):
        o_end = min(th.opening_max_move, M)
        return {"opening": [1, o_end], "middlegame": [o_end + 1, int(M * 0.75)], "endgame": [int(M * 0.75) + 1, M],
                "method": "move_count_fallback"}
    o_end = th.opening_max_move
    for k in range(1, M + 1):
        if sett[k] >= th.opening_settledness:
            o_end = k
            break
    o_end = min(o_end, M)
    e_start = M + 1
    for k in range(o_end + 1, M + 1):
        if sett[k] >= th.endgame_settledness and all(sett[j] >= th.endgame_hold for j in range(k, M + 1)):
            e_start = k
            break
    return {"opening": [1, o_end], "middlegame": [o_end + 1, e_start - 1] if e_start - 1 > o_end else None,
            "endgame": [e_start, M] if e_start <= M else None, "method": "ownership_settledness"}


def phase_of(n: int, ph: dict) -> str:
    for name in ("opening", "middlegame", "endgame"):
        rng = ph.get(name)
        if rng and rng[0] <= n <= rng[1]:
            return name
    return "opening" if n <= ph["opening"][1] else "middlegame"


# ------------------------------------------------------------------ ownership attribution
def regional_attribution(own_a: list[float], own_b: list[float], mover: int, size: int, score_delta: float | None) -> dict:
    """Δownership by standard region, mover-perspective (negative = loss for mover)."""
    s = sign_of(mover)
    parts = standard_partition(size)
    regional = []
    total_loss = 0.0
    for code in STANDARD_CODES:
        d = s * sum(own_b[i] - own_a[i] for i in parts[code])
        regional.append({"region": code, "label": LABELS[code], "ownership_delta_points": round(d, 2)})
        total_loss += max(0.0, -d)
    primary = min(regional, key=lambda r: r["ownership_delta_points"])
    local_share = (-primary["ownership_delta_points"]) / total_loss if total_loss > 1e-6 else 0.0
    for r in regional:
        r["share"] = round(max(0.0, -r["ownership_delta_points"]) / total_loss, 3) if total_loss > 1e-6 else 0.0
    explained = min(1.0, total_loss / abs(score_delta)) if score_delta and abs(score_delta) > 1e-6 else None
    return {"regional": regional, "primary_region": primary["region"], "local_share": round(local_share, 3),
            "explained_share": None if explained is None else round(explained, 3)}


def classify_local(local_share: float, th: Thresholds) -> str:
    if local_share >= th.local_share_local:
        return "local"
    if local_share <= th.local_share_global:
        return "global"
    return "mixed"


# ------------------------------------------------------------------ groups
def group_status_label(mean_own_owner: float, th: Thresholds) -> str:
    if mean_own_owner >= th.alive:
        return "alive"
    if mean_own_owner <= th.dead:
        return "dead"
    return "unsettled"


def group_label(g, board_size: int) -> str:
    return f"{COLOR_CHAR[g.color]} {LABELS[standard_code(g.anchor, board_size)]} ({g.size})"


def group_records(board: Board, ownership: list[float], th: Thresholds, min_size: int = 1,
                  liberty_points: bool = False, race_anchors: set[int] | None = None) -> list[dict]:
    """Groups with status from ownership.  Liberty counts are left out unless the group is in a
    capturing race (`race_anchors`) or they are asked for (`liberty_points`): outside a race they
    are not what decides a position, and a count on the page invites commentary about it."""
    out = []
    for g in board.groups():
        if g.size < min_size:
            continue
        mean_black = sum(ownership[i] for i in g.stones) / g.size
        mean_owner = mean_black * sign_of(g.color)
        code = standard_code(g.anchor, board.size)
        rec = {
            "id": f"g_{COLOR_CHAR[g.color]}_{idx_to_gtp(g.anchor, board.size)}",
            "color": COLOR_CHAR[g.color], "anchor": idx_to_gtp(g.anchor, board.size),
            "stones": [idx_to_gtp(i, board.size) for i in g.stones], "size": g.size,
            "mean_ownership": round(mean_owner, 3),
            "status": group_status_label(mean_owner, th), "region": code,
            "label": group_label(g, board.size),
        }
        if liberty_points or (race_anchors and g.anchor in race_anchors):
            rec["liberties"] = len(g.liberties)
            rec["liberty_points"] = [idx_to_gtp(i, board.size) for i in g.liberties]
        out.append(rec)
    out.sort(key=lambda r: (r["status"] != "unsettled", -r["size"]))
    return out


def capture_races(board: Board, ownership: list[float] | None, th: Thresholds, min_size: int = 2) -> list[dict]:
    """Adjacent groups of opposite colour, both unsettled and both short of liberties
    (≤ `race_max_liberties`): the one situation where liberty counts are the lesson."""
    if ownership is None:
        return []
    weak = {}
    for g in board.groups():
        if g.size < min_size or len(g.liberties) > th.race_max_liberties:
            continue
        mean_owner = sign_of(g.color) * sum(ownership[i] for i in g.stones) / g.size
        if group_status_label(mean_owner, th) == "unsettled":
            weak[g.anchor] = g
    races, seen = [], set()
    for a, g in weak.items():
        for s in g.stones:
            for nb in neighbors(s, board.size):
                if board.cells[nb] in (EMPTY, g.color):
                    continue
                h = board.group_at(nb)
                if h is None or h.anchor not in weak or (min(a, h.anchor), max(a, h.anchor)) in seen:
                    continue
                seen.add((min(a, h.anchor), max(a, h.anchor)))
                races.append({"groups": [{"label": group_label(x, board.size), "anchor": idx_to_gtp(x.anchor, board.size),
                                          "color": COLOR_CHAR[x.color], "liberties": len(x.liberties),
                                          "liberty_points": [idx_to_gtp(i, board.size) for i in x.liberties]}
                                         for x in (g, h)]})
    return races


def race_anchor_set(races: list[dict], board_size: int) -> set[int]:
    return {gtp_to_idx(g["anchor"], board_size) for r in races for g in r["groups"]}


def group_changes(board_before: Board, own_before: list[float], own_after: list[float], th: Thresholds,
                  min_size: int = 3, top: int = 3) -> list[dict]:
    """Ownership change of the groups present before the move (captured stones count as -1 after)."""
    out = []
    for g in board_before.groups():
        if g.size < min_size:
            continue
        s = sign_of(g.color)
        mb = s * sum(own_before[i] for i in g.stones) / g.size
        ma = s * sum(own_after[i] for i in g.stones) / g.size
        code = standard_code(g.anchor, board_before.size)
        out.append({"group": f"{COLOR_CHAR[g.color]} {LABELS[code]} ({g.size})", "anchor": idx_to_gtp(g.anchor, board_before.size),
                    "before": f"{group_status_label(mb, th)} {mb:+.2f}", "after": f"{group_status_label(ma, th)} {ma:+.2f}",
                    "before_value": round(mb, 3), "after_value": round(ma, 3), "points": round(g.size * (ma - mb), 2),
                    "status_before": group_status_label(mb, th), "status_after": group_status_label(ma, th)})
    out.sort(key=lambda r: abs(r["after_value"] - r["before_value"]), reverse=True)
    return out[:top]


# ------------------------------------------------------------------ style axis
def style_axis(board_before: Board, own_played: list[float], own_best: list[float], student: int) -> dict:
    d_own = 0.0
    d_opp = 0.0
    for g in board_before.groups():
        s = sign_of(g.color)
        diff_best_minus_played = s * sum(own_best[i] - own_played[i] for i in g.stones)   # owner perspective, points
        if g.color == student:
            d_own += max(0.0, diff_best_minus_played)             # best protects my groups
        else:
            d_opp += max(0.0, -diff_best_minus_played)            # best hurts the opponent's groups
    tot = d_own + d_opp
    own_share = d_own / tot if tot > 1e-6 else 0.5
    label = "overplay" if own_share >= 0.6 else "slack" if own_share <= 0.4 else "neutral"
    if tot < 0.5:
        label = "neutral"
    return {"label": label, "own_share": round(own_share, 3), "opponent_share": round(1 - own_share, 3),
            "own_points": round(d_own, 2), "opponent_points": round(d_opp, 2)}


# ------------------------------------------------------------------ pattern hash
def pattern_hash(board: Board, idx: int, mover: int, window: int = 7) -> str:
    size = board.size
    half = window // 2
    r0, c0 = divmod(idx, size)
    cells = []
    for dr in range(-half, half + 1):
        row = []
        for dc in range(-half, half + 1):
            r, c = r0 + dr, c0 + dc
            if not (0 <= r < size and 0 <= c < size):
                row.append("#")
            else:
                v = board.cells[r * size + c]
                row.append("." if v == EMPTY else ("X" if v == mover else "O"))
        cells.append(row)

    def variants(m):
        out = []
        cur = m
        for _ in range(4):
            out.append(cur)
            out.append([list(reversed(row)) for row in cur])
            cur = [list(row) for row in zip(*cur[::-1])]   # rotate 90
        return out

    canon = min("".join("".join(r) for r in v) for v in variants(cells))
    return ("ph_" if window == 7 else f"ph{window}_") + hashlib.sha1(canon.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ stability
def stability_flag(before: Analysis, played: int | None, th: Thresholds) -> str:
    if len(before.candidates) < 2 or before.visits <= 0:
        return "unknown"
    s = sign_of(before.to_move)
    c0, c1 = before.candidates[0], before.candidates[1]
    share = c0.visits / before.visits
    margin = s * (c0.score_lead - c1.score_lead)
    cp = candidate_for(before, played)
    if margin < th.stability_unstable_margin or (cp is not None and s * cp.lcb > s * c0.lcb and cp.move != c0.move):
        return "unstable"
    if share >= th.stability_visit_share and margin >= th.stability_margin:
        return "stable"
    return "unknown"


# ------------------------------------------------------------------ decisive / last chance
def _student_series(ga: GameAnalysis, basis: str) -> list[float]:
    s = sign_of(ga.student_color)
    if basis == "winrate":
        return [a.winrate if ga.student_color == BLACK else 1 - a.winrate for a in ga.positions]
    return [s * a.score_lead for a in ga.positions]


def decisive_and_last_chance(ga: GameAnalysis, rows: list[dict], th: Thresholds) -> tuple[dict | None, dict | None]:
    if ga.student_color is None or ga.M < 2:
        return None, None
    wr = _student_series(ga, "winrate")
    degenerate = ga.handicap >= 2 or all(w < 0.05 or w > 0.95 for w in wr[:min(30, len(wr))])
    basis = "score" if degenerate else "winrate"
    series = _student_series(ga, basis)
    if basis == "winrate":
        lost_th, rec_th = th.decided_winrate, th.recovery_winrate
        won_th, won_rec = 1 - th.decided_winrate, 1 - th.recovery_winrate
    else:
        lost_th, rec_th = th.decided_score_handicap, th.recovery_score_handicap
        won_th, won_rec = -th.decided_score_handicap, -th.recovery_score_handicap
    winner = ga.result.get("winner")
    student_won = winner == COLOR_CHAR[ga.student_color] if winner else series[-1] > (0.5 if basis == "winrate" else 0)
    decisive = None
    M = ga.M
    for n in range(1, M + 1):
        mover = ga.moves[n - 1][0]
        if student_won:
            if mover == ga.student_color:
                continue
            if series[n] >= won_th and all(series[j] >= won_rec for j in range(n, M + 1)):
                decisive = {"move": n, "by": "opponent", "basis": basis, "before": round(series[n - 1], 3), "after": round(series[n], 3)}
                break
        else:
            if mover != ga.student_color:
                continue
            if series[n] <= lost_th and all(series[j] <= rec_th for j in range(n, M + 1)):
                decisive = {"move": n, "by": "student", "basis": basis, "before": round(series[n - 1], 3), "after": round(series[n], 3)}
                break
    last_chance = None
    if decisive and not student_won:
        s = sign_of(ga.student_color)
        for n in range(decisive["move"], 0, -1):
            if ga.moves[n - 1][0] != ga.student_color:
                continue
            before = ga.positions[n - 1]
            if not before.candidates:
                continue
            best = before.candidates[0]
            val = (best.winrate if ga.student_color == BLACK else 1 - best.winrate) if basis == "winrate" else s * best.score_lead
            if val >= rec_th:
                last_chance = {"move": n, "played": rows[n - 1]["move"], "best": idx_to_gtp(best.move, ga.size),
                               "eval_if_best": round(val, 3), "eval_actual": round(series[n], 3), "basis": basis}
                break
    return decisive, last_chance


# ------------------------------------------------------------------ episodes
def build_episodes(ga: GameAnalysis, rows: list[dict], ph: dict, th: Thresholds, max_episodes: int = 10) -> list[dict]:
    if ga.student_color is None:
        return []
    ga.build_boards()
    seeds = [r for r in rows if r["color"] == COLOR_CHAR[ga.student_color] and r["points_lost"] >= th.episode_min_loss]
    chains: list[list[dict]] = []
    for r in seeds:
        placed = False
        for ch in chains:
            last = ch[-1]
            if r["n"] - last["n"] > th.cluster_plies or r["n"] - ch[0]["n"] > th.cluster_max_span \
                    or len(ch) >= th.cluster_max_moves:
                continue
            pts = [x for x in (last["idx"], last["best_idx"]) if x is not None]
            mine = [x for x in (r["idx"], r["best_idx"]) if x is not None]
            near = any(chebyshev(a, b, ga.size) <= th.cluster_distance for a in pts for b in mine)
            same_region = last["idx"] is not None and r["idx"] is not None and r["n"] - last["n"] <= 6 and \
                standard_code(last["idx"], ga.size) == standard_code(r["idx"], ga.size)
            if near or same_region:
                ch.append(r)
                placed = True
                break
        if not placed:
            chains.append([r])
    chains.sort(key=lambda ch: -sum(x["points_lost"] for x in ch))
    episodes = []
    s = sign_of(ga.student_color)
    for k, ch in enumerate(chains[:max_episodes], 1):
        root = ch[0]
        n = root["n"]
        before, after = ga.positions[n - 1], ga.positions[n]
        board_before = ga.boards[n - 1]
        pts = {x["idx"] for x in ch if x["idx"] is not None}
        region_idx = pts or {root["best_idx"]}
        region_code = standard_code(root["idx"] if root["idx"] is not None else root["best_idx"], ga.size)
        bb = bbox(region_idx, ga.size)
        lead_before = s * before.score_lead
        state = "ahead" if lead_before > th.game_state_close else "behind" if lead_before < -th.game_state_close else "close"
        acc = root["acceptable"]
        best_idx = root["best_idx"]
        cp_best = candidate_for(before, best_idx)
        cp_played = candidate_for(before, root["idx"])
        attribution = None
        if before.ownership is not None and after.ownership is not None:
            attribution = regional_attribution(before.ownership, after.ownership, ga.student_color, ga.size,
                                               -root["points_lost"])
        style = {"label": "neutral", "own_share": 0.5, "opponent_share": 0.5, "note": "no after-best ownership"}
        if n in ga.after_best_ownership and after.ownership is not None:
            style = style_axis(board_before, after.ownership, ga.after_best_ownership[n], ga.student_color)
        human_played = human_prob(before, ga.profiles, root["idx"], ga.size)
        human_best = human_prob(before, ga.profiles, best_idx, ga.size)
        # preliminary teachable move: highest target probability within the acceptable set
        teach, learn = best_idx, human_best.get("target", 0.0)
        for m in acc:
            hp = human_prob(before, ga.profiles, m, ga.size).get("target", 0.0)
            if hp > learn:
                teach, learn = m, hp
        gaw = got_away_with_it(rows, n, th)
        sig = {
            "prior_played": root["prior_played"], "prior_best": root["prior_best"],
            "policy_top": idx_to_gtp(_policy_top(before, ga.size), ga.size) if before.policy else None,
            "search_best": root["best"],
            "local_loss_share": attribution["local_share"] if attribution else None,
            "explained_share": attribution["explained_share"] if attribution else None,
            "score_stdev_played": round(cp_played.score_stdev, 2) if cp_played else None,
            "score_stdev_best": round(cp_best.score_stdev, 2) if cp_best else None,
            "ko_present": board_before.ko_capture_available(ga.rules),
        }
        changes = group_changes(board_before, before.ownership, after.ownership, th) if (before.ownership and after.ownership) else []
        ep = {
            "id": f"E{k}", "moves": [n, ch[-1]["n"]], "student_moves": [x["n"] for x in ch],
            "root": {"move": n, "played": root["move"], "best": root["best"], "points_lost": root["points_lost"],
                     "position_ref_before": ga.refs[n - 1], "position_ref_after": ga.refs[n]},
            "points_lost_total": round(sum(x["points_lost"] for x in ch), 2),
            "region": {"standard": region_code, "label": LABELS[region_code],
                       "bbox": [idx_to_gtp(bb[0], ga.size), idx_to_gtp(bb[1], ga.size)]},
            "phase": phase_of(n, ph),
            "game_state_before": {"score_lead": round(lead_before, 2), "label": state},
            "acceptable_set": {"margin": th.acceptable_margin, "moves": [idx_to_gtp(m, ga.size) for m in acc],
                               "played_in_set": root["in_acceptable_set"],
                               "violation": round(max(0.0, s * ((cp_best.score_lead if cp_best else before.score_lead) - (cp_played.score_lead if cp_played else after.score_lead))), 2)},
            "signature": sig,
            "style_axis": style,
            "got_away_with_it": gaw,
            "human": {"played": human_played, "best": human_best},
            "learnability": round(learn, 4),
            "teachable_move_preliminary": idx_to_gtp(teach, ga.size),
            "stability": stability_flag(before, root["idx"], th),
            "pattern_hash": pattern_hash(board_before, root["idx"], ga.student_color, 7) if root["idx"] is not None else None,
            "pattern_hash_5": pattern_hash(board_before, root["idx"], ga.student_color, 5) if root["idx"] is not None else None,
            "group_status_change": changes,
        }
        ep["best_reply"] = reply_character(after, root["idx"], ga.size, th)
        ep["candidate_tags"] = candidate_tags(ep, rows, ga, n, before, after, th)
        episodes.append(ep)
    link_persistent_best(episodes)
    return episodes


def reply_character(after: Analysis, move_idx: int | None, size: int, th: Thresholds) -> dict | None:
    """The opponent's best answer to a move, from the search at the position after it: `tenuki` (the
    move did not need an answer), `local_sharp` (answering here is worth ≥ `sharp_margin` more than the
    best move elsewhere: the move started a fight or overplayed), `local_calm` (answered, but little rides
    on it). `gap` is how much the chosen kind of reply beats the best reply of the other kind."""
    if not after.candidates:
        return None
    best = after.candidates[0]
    s = sign_of(after.to_move)

    def is_local(c) -> bool:
        return c.move is not None and move_idx is not None and chebyshev(c.move, move_idx, size) <= th.local_radius

    local = is_local(best)
    other = next((c for c in after.candidates[1:] if is_local(c) != local), None)
    gap = None if other is None else round(s * (best.score_lead - other.score_lead), 2) + 0.0
    if not local:
        character = "tenuki"
    elif gap is None or gap >= th.sharp_margin:
        character = "local_sharp"
    else:
        character = "local_calm"
    return {"move": idx_to_gtp(best.move, size), "local": local, "character": character, "gap": gap,
            "score_stdev": round(best.score_stdev, 2)}


def link_persistent_best(episodes: list[dict]) -> None:
    """A point that is best (or teachable) at several episode roots is one big point left open across
    the game: list the other episodes sharing it (hint for 2 / 15, and one lesson candidate)."""
    for ep in episodes:
        mine = {ep["root"]["best"], ep["teachable_move_preliminary"]} - {None, "pass"}
        ep["persistent_best"] = [o["id"] for o in episodes if o is not ep
                                 and mine & ({o["root"]["best"], o["teachable_move_preliminary"]} - {None, "pass"})]


def _policy_top(a: Analysis, size: int) -> int | None:
    if not a.policy:
        return None
    best_i, best_v = None, -1.0
    for i, v in enumerate(a.policy[:size * size]):
        if v is not None and v > best_v:
            best_i, best_v = i, v
    return best_i


def got_away_with_it(rows: list[dict], n: int, th: Thresholds) -> dict | None:
    r = rows[n - 1]
    if r["points_lost"] < th.tag_min_loss:
        return None
    for k in (n + 1, n + 3):
        if k - 1 < len(rows):
            o = rows[k - 1]
            if o["color"] != r["color"] and o["points_lost"] >= th.got_away_ratio * r["points_lost"]:
                return {"opponent_move": k, "restored_points": o["points_lost"]}
    return None


def candidate_tags(ep: dict, rows: list[dict], ga: GameAnalysis, n: int, before: Analysis, after: Analysis,
                   th: Thresholds) -> list[str]:
    tags: list[str] = []
    root = rows[n - 1]
    sig = ep["signature"]
    pl = root["points_lost"]
    played_idx, best_idx = root["idx"], root["best_idx"]
    # "Plausible" is judged by the human model (what a player of the student's rank plays), not by
    # KataGo's policy, which almost always prefers the best move; fall back to the policy without it.
    hp, hb = ep["human"]["played"], ep["human"]["best"]
    if "peer" in hp and "peer" in hb:
        pp, pb = hp["peer"], hb["peer"]
        pb_target = hb.get("target", pb)
    else:
        pp, pb = sig["prior_played"] or 0.0, sig["prior_best"] or 0.0
        pb_target = pb
    changes = ep["group_status_change"]
    own_fell = any(c["group"].startswith(COLOR_CHAR[ga.student_color]) and c["after_value"] < c["before_value"] - 0.15 for c in changes)
    dist = chebyshev(played_idx, best_idx, ga.size) if played_idx is not None and best_idx is not None else None
    # 13 failure to punish
    if n >= 2:
        prev = rows[n - 2]
        if prev["color"] != root["color"] and prev["points_lost"] >= th.tag_punish_min_loss and pl >= th.got_away_ratio * prev["points_lost"]:
            tags.append("13")
    # 3 / 4 / 5: plausible move refuted by search
    plausible = pp >= th.tag_plausible_min_peer and pp >= th.tag_plausible_ratio * pb and pl >= th.tag_plausible_min_loss
    if plausible:
        if own_fell:
            tags.append("3")
        elif n in ga.after_best_ownership and after.ownership is not None:
            opp_hurt = sum(1 for g in ga.boards[n - 1].groups() if g.color != ga.student_color and g.size >= 3
                           and sign_of(g.color) * sum(ga.after_best_ownership[n][i] - after.ownership[i] for i in g.stones) / g.size < -0.15)
            tags.append("4" if opp_hurt else "5")
        else:
            tags.append("5")
    # 6 / 15 / 1 / 2: intuition failed — by KataGo's policy, or the target rank finds the best move
    # and the student's rank does not
    prior_pp, prior_pb = sig["prior_played"] or 0.0, sig["prior_best"] or 0.0
    by_policy = prior_pb >= th.tag_intuition_best_min and prior_pp <= th.tag_intuition_played_max
    by_human = not plausible and pb_target >= th.tag_intuition_best_min and pp <= th.tag_intuition_played_max
    if (by_policy or by_human) and dist is not None:
        if dist <= 2:
            tags.append("6")
        elif standard_code(played_idx, ga.size) == standard_code(best_idx, ga.size):
            tags.append("15")
        elif dist >= th.tag_direction_min_distance:
            tags.append("1")
            best_region = standard_code(best_idx, ga.size)
            if before.ownership is not None and any(
                    standard_code(g.anchor, ga.size) == best_region and g.size >= 2
                    and group_status_label(sign_of(g.color) * sum(before.ownership[i] for i in g.stones) / g.size, th) == "unsettled"
                    for g in ga.boards[n - 1].groups()):
                tags.append("2")
    # 1 whole-board by ownership attribution, only when the best move is genuinely elsewhere
    if sig.get("local_loss_share") is not None and sig["local_loss_share"] <= th.local_share_global \
            and "1" not in tags and not any(t in tags for t in ("6", "15")) \
            and dist is not None and dist >= th.tag_direction_min_distance:
        tags.append("1")
    # 9 choice of fight
    if ep["style_axis"]["label"] == "overplay" and sig.get("score_stdev_played") and sig.get("score_stdev_best") \
            and sig["score_stdev_played"] >= 1.5 * sig["score_stdev_best"]:
        tags.append("9")
    # 10 aji
    if before.ownership_stdev is not None and best_idx is not None and played_idx is not None \
            and standard_code(best_idx, ga.size) != standard_code(played_idx, ga.size):
        parts = standard_partition(ga.size)
        reg = parts[standard_code(best_idx, ga.size)]
        if sum(before.ownership_stdev[i] for i in reg) / len(reg) >= 0.35:
            tags.append("10")
    # 11 endgame
    if ep["phase"] == "endgame" and not any(t in tags for t in ("3", "4", "5")):
        tags.append("11")
    # 12 ko
    if sig.get("ko_present"):
        tags.append("12")
    # 7 joseki
    if ep["phase"] == "opening" and n <= 40 and best_idx is not None and standard_code(best_idx, ga.size) in ("UL", "UR", "LL", "LR"):
        tags.append("7")
    # 14 passive
    if n in ga.after_best_ownership and after.ownership is not None and before.ownership is not None:
        b = ga.boards[n - 1]
        own_up = sum(sign_of(g.color) * sum(after.ownership[i] - before.ownership[i] for i in g.stones)
                     for g in b.groups() if g.color == ga.student_color)
        opp_down = sum(-sign_of(g.color) * sum(ga.after_best_ownership[n][i] - before.ownership[i] for i in g.stones)
                       for g in b.groups() if g.color != ga.student_color)
        if own_up >= 1.0 and opp_down >= 3.0:
            tags.append("14")
    # dedupe, keep order, cap 3
    seen = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return seen[:3]


# ------------------------------------------------------------------ summaries
def points_lost_summary(rows: list[dict], ph: dict, color_char: str) -> dict:
    out = {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0, "total": 0.0, "per_move": 0.0}
    cnt = 0
    for r in rows:
        if r["color"] != color_char:
            continue
        p = phase_of(r["n"], ph)
        out[p] += r["points_lost"]
        out["total"] += r["points_lost"]
        cnt += 1
    for k in ("opening", "middlegame", "endgame", "total"):
        out[k] = round(out[k], 1)
    out["per_move"] = round(out["total"] / cnt, 2) if cnt else 0.0
    out["moves"] = cnt
    return out


def game_type(episodes: list[dict], student_total: float, th: Thresholds) -> dict:
    if not episodes or student_total <= 0:
        return {"type": "mixed", "top_episode_share": 0.0}
    share = episodes[0]["points_lost_total"] / student_total
    t = "single_blunder" if share >= th.single_blunder_share else "accumulation" if share <= th.accumulation_share else "mixed"
    return {"type": t, "top_episode_share": round(share, 3)}


def reconciliation(ga: GameAnalysis, th: Thresholds) -> dict:
    res = ga.result
    if res.get("method") != "score" or res.get("margin") is None or ga.M < 1:
        return {"status": "n/a", "engine_final_score": round(ga.positions[-1].score_lead, 2) if ga.positions else None,
                "sgf_margin": None, "diff": None}
    margin_black = res["margin"] if res.get("winner") == "B" else -res["margin"] if res.get("winner") == "W" else 0.0
    eng = ga.positions[-1].score_lead
    diff = eng - margin_black
    return {"status": "ok" if abs(diff) <= th.reconciliation_tolerance else "mismatch",
            "engine_final_score": round(eng, 2), "sgf_margin": margin_black, "diff": round(diff, 2)}


def positives(ga: GameAnalysis, rows: list[dict], th: Thresholds, top: int = 5) -> list[dict]:
    if ga.student_color is None:
        return []
    out = []
    for r in rows:
        if r["color"] != COLOR_CHAR[ga.student_color] or r["points_lost"] > 0.5:
            continue
        before = ga.positions[r["n"] - 1]
        hp = human_prob(before, ga.profiles, r["idx"], ga.size)
        peer = hp.get("peer")
        if peer is not None and peer <= 0.10:
            out.append({"move": r["n"], "played": r["move"], "points_lost": r["points_lost"],
                        "peer_probability": peer, "target_probability": hp.get("target")})
    out.sort(key=lambda d: d["peer_probability"])
    return out[:top]


def digest(ga: GameAnalysis, th: Thresholds, max_episodes: int = 10, include_positives: bool = True,
           complete: bool = True, job_id: str | None = None) -> dict:
    rows = move_rows(ga, th)
    ph = phases(ga, th)
    eps = build_episodes(ga, rows, ph, th, max_episodes)
    student_char = COLOR_CHAR[ga.student_color] if ga.student_color else None
    opp_char = ("W" if student_char == "B" else "B") if student_char else None
    pl_student = points_lost_summary(rows, ph, student_char) if student_char else None
    pl_opp = points_lost_summary(rows, ph, opp_char) if opp_char else None
    dec, lc = decisive_and_last_chance(ga, rows, th)
    low_visit = [r["n"] for r in rows if r["visits"] < 0.5 * ga.visits_per_move]
    return {
        "job_id": job_id, "game_id": ga.game_id, "complete": complete, "positions_analyzed": ga.M + 1,
        "visits_per_move": ga.visits_per_move,
        "game": {"student_color": student_char, "handicap": ga.handicap, "komi": ga.komi, "rules": ga.rules,
                 "result": ga.result.get("raw", ""), "reconciliation": reconciliation(ga, th)},
        "phases": ph,
        "game_type": game_type(eps, pl_student["total"] if pl_student else 0.0, th),
        "decisive": dec, "last_chance": lc,
        "points_lost": {"student": pl_student, "opponent": pl_opp},
        "episodes": eps,
        "positives": positives(ga, rows, th) if include_positives else [],
        "reliability": {"low_visit_positions": low_visit, "unstable_episodes": [e["id"] for e in eps if e["stability"] == "unstable"]},
        "profiles": ga.profiles,
    }
