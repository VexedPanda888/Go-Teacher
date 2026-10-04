"""Derived metrics (tool contract §3).  Deterministic; no engine access here.

Conventions: `positions[k]` is the Analysis of P_k (after move k); move n is
`moves[n-1]`; score leads inside `Analysis` are Black-perspective.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .board import BLACK, EMPTY, COLOR_CHAR, Board, opponent
from .config import Thresholds
from .coords import chebyshev, gtp_to_idx, idx_to_gtp, neighbors
from .engine import Analysis
from .regions import LABELS, standard_code, standard_partition


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
    boards: list[Board] = field(default_factory=list, repr=False)

    def build_boards(self) -> None:
        if self.boards:
            return
        b = Board.from_setup(self.size, self.first_to_move, self.setup_black, self.setup_white)
        self.boards = [b]
        for color, idx in self.moves:
            b = b.play(color, idx, self.rules)
            self.boards.append(b)

    @property
    def M(self) -> int:
        return len(self.positions) - 1


# ------------------------------------------------------------------ helpers
def policy_prob(a: Analysis, idx: int | None, size: int) -> float | None:
    return None if a.policy is None else prob_at(a.policy, idx, size)


def human_prob(a: Analysis, alias_to_profile: dict[str, str], idx: int | None, size: int) -> dict[str, float]:
    out = {}
    i = size * size if idx is None else idx
    for alias, prof in alias_to_profile.items():
        arr = a.human.get(prof)
        if arr is not None and 0 <= i < len(arr) and arr[i] >= 0:
            out[alias] = round(float(arr[i]), 4)
    return out


def prob_at(arr: list[float], idx: int | None, size: int) -> float | None:
    """A policy-array entry (pass is the last), None for illegal (-1) points."""
    v = arr[size * size if idx is None else idx]
    return None if v is None or v < 0 else float(v)


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


# ------------------------------------------------------------------ groups
def group_status_label(mean_own_owner: float, th: Thresholds) -> str:
    if mean_own_owner >= th.alive:
        return "alive"
    if mean_own_owner <= th.dead:
        return "dead"
    return "unsettled"


def group_mean_ownership(g, own: list[float], board: Board | None = None) -> float:
    """Owner-perspective mean ownership of the stones of g; with `board`, stones no longer on it count -1."""
    s = sign_of(g.color)
    if board is None:
        return s * sum(own[i] for i in g.stones) / g.size
    return sum(s * own[i] if board.cells[i] == g.color else -1.0 for i in g.stones) / g.size


def groups_near(board: Board, color: int, idx: int, radius: int, min_size: int = 2) -> list:
    return [g for g in board.groups() if g.color == color and g.size >= min_size
            and any(chebyshev(s, idx, board.size) <= radius for s in g.stones)]


def near_empty(board: Board, idx: int, radius: int) -> list[str]:
    """Empty points (GTP) within Chebyshev `radius` of idx."""
    size = board.size
    r0, c0 = divmod(idx, size)
    return [idx_to_gtp(r * size + c, size) for r in range(max(0, r0 - radius), min(size, r0 + radius + 1))
            for c in range(max(0, c0 - radius), min(size, c0 + radius + 1)) if board.cells[r * size + c] == EMPTY]


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
        mean_owner = group_mean_ownership(g, ownership)
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
        if group_status_label(group_mean_ownership(g, ownership), th) == "unsettled":
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


def territory_by_region(board: Board, ownership: list[float], persp: int) -> dict[str, dict]:
    """Expected points per standard region for each side: ownership summed over the points that are not
    the owner's own stones (empty points and the other side's stones, which count as dead)."""
    s = sign_of(persp)
    own_color, opp_color = persp, opponent(persp)
    out = {}
    for code, idxs in standard_partition(board.size).items():
        you = opp = 0.0
        for i in idxs:
            v = s * ownership[i]
            if v > 0 and board.cells[i] != own_color:
                you += v
            elif v < 0 and board.cells[i] != opp_color:
                opp -= v
        out[code] = {"label": LABELS[code], "you": round(you, 1), "opponent": round(opp, 1)}
    return out


def race_anchor_set(races: list[dict], board_size: int) -> set[int]:
    return {gtp_to_idx(g["anchor"], board_size) for r in races for g in r["groups"]}


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


# ------------------------------------------------------------------ the story: groups that live or die
def group_events(ga: GameAnalysis, th: Thresholds, max_events: int = 15) -> list[dict]:
    """Moves at which a group of at least `group_event_min_size` stones changes status (alive / unsettled /
    dead, from ownership; captured stones count as dead) and the new status still holds `group_event_hold`
    moves later. The fates of the groups are the plot of a game: who lived, who died, when."""
    if ga.student_color is None or ga.M < 1 or any(a.ownership is None for a in ga.positions):
        return []
    ga.build_boards()
    M, hold = ga.M, th.group_event_hold
    reported: list[tuple[set[int], str]] = []
    events = []
    for k in range(1, M + 1):
        prev, own_prev, own_now = ga.boards[k - 1], ga.positions[k - 1].ownership, ga.positions[k].ownership
        for g in prev.groups():
            if g.size < th.group_event_min_size:
                continue
            before = group_mean_ownership(g, own_prev)
            after = group_mean_ownership(g, own_now, ga.boards[k])
            s_before, s_after = group_status_label(before, th), group_status_label(after, th)
            if s_before == s_after:
                continue
            if k >= 2 and group_status_label(group_mean_ownership(g, ga.positions[k - 2].ownership), th) != s_before:
                continue                                  # the old status was itself a one-move wobble
            j = min(M, k + hold)
            if group_status_label(group_mean_ownership(g, ga.positions[j].ownership, ga.boards[j]), th) != s_after:
                continue
            stones = set(g.stones)
            last = next((to for st, to in reversed(reported) if st & stones), None)
            if last == s_after:
                continue                              # the same group told again with the same fate
            reported.append((stones, s_after))
            mover = ga.moves[k - 1][0]
            events.append({"move": k, "by": "you" if mover == ga.student_color else "opponent",
                           "group": group_label(g, ga.size), "whose": "yours" if g.color == ga.student_color else "opponent's",
                           "anchor": idx_to_gtp(g.anchor, ga.size), "size": g.size, "from": s_before, "to": s_after,
                           "ownership_before": round(before, 2), "ownership_after": round(after, 2)})
    if len(events) > max_events:
        keep = sorted(events, key=lambda e: -e["size"])[:max_events]
        events = [e for e in events if e in keep]
    return events


def lead_timeline(ga: GameAnalysis, step: int = 20) -> list[dict]:
    """The student's score lead every `step` moves and at the end: the shape of the game at a glance."""
    if ga.student_color is None:
        return []
    s = sign_of(ga.student_color)
    ks = list(range(0, ga.M + 1, step))
    if ks[-1] != ga.M:
        ks.append(ga.M)
    return [{"move": k, "lead": round(s * ga.positions[k].score_lead, 1)} for k in ks]


def swings(ga: GameAnalysis, rows: list[dict], ph: dict, th: Thresholds, top: int = 10) -> list[dict]:
    """The biggest single-move losses of both players, in move order: where the points changed hands. An
    opponent's swing says how much of it the student's next move gave back (`gave_back`)."""
    if ga.student_color is None:
        return []
    s = sign_of(ga.student_color)
    you = COLOR_CHAR[ga.student_color]
    big = sorted((r for r in rows if r["points_lost"] >= th.episode_min_loss), key=lambda r: -r["points_lost"])[:top]
    out = []
    for r in sorted(big, key=lambda r: r["n"]):
        e = {"move": r["n"], "by": "you" if r["color"] == you else "opponent", "played": r["move"], "best": r["best"],
             "points_lost": r["points_lost"], "lead_after": round(s * r["score_after"], 1),
             "region": LABELS[standard_code(r["idx"], ga.size)] if r["idx"] is not None else None,
             "phase": phase_of(r["n"], ph)}
        if e["by"] == "opponent" and r["n"] < len(rows):
            e["gave_back"] = rows[r["n"]]["points_lost"]
        out.append(e)
    return out


# ------------------------------------------------------------------ the story: key moments
def build_moments(ga: GameAnalysis, rows: list[dict], ph: dict, th: Thresholds, events: list[dict] | None = None,
                  max_moments: int = 6) -> list[dict]:
    """The student's costly sequences (a move losing at least episode_min_loss, chained with the student's
    nearby losses that follow), ranked by the first move's loss. Candidates for the review's key moments."""
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
    # ranked by the first move's loss: a chain sum counts the same group again each time both sides swing it
    chains.sort(key=lambda ch: -ch[0]["points_lost"])
    s = sign_of(ga.student_color)
    moments = []
    for k, ch in enumerate(chains[:max_moments], 1):
        root = ch[0]
        n, last_n = root["n"], ch[-1]["n"]
        before = ga.positions[n - 1]
        lead_before = s * before.score_lead
        state = "ahead" if lead_before > th.game_state_close else "behind" if lead_before < -th.game_state_close else "close"
        human_played = human_prob(before, ga.profiles, root["idx"], ga.size)
        human_best = human_prob(before, ga.profiles, root["best_idx"], ga.size)
        # the findable move: within a point of the best, the one a player a few stones stronger most often plays
        findable, findable_p = root["best_idx"], human_best.get("target", 0.0)
        for m in root["acceptable"]:
            hp = human_prob(before, ga.profiles, m, ga.size).get("target", 0.0)
            if hp > findable_p:
                findable, findable_p = m, hp
        after_chain = min(ga.M, last_n + 1)
        reply = reply_character(ga.positions[n], root["idx"], ga.size, th)
        moments.append({
            "id": f"M{k}", "moves": [n, last_n], "student_moves": [x["n"] for x in ch],
            "move": n, "played": root["move"], "best": root["best"], "points_lost": root["points_lost"],
            "net_loss": round(max(0.0, lead_before - s * ga.positions[after_chain].score_lead), 1),
            "position_ref_before": ga.refs[n - 1],
            "region": LABELS[standard_code(root["idx"] if root["idx"] is not None else root["best_idx"], ga.size)],
            "phase": phase_of(n, ph),
            "lead_before": {"score_lead": round(lead_before, 1), "label": state},
            "acceptable_moves": [idx_to_gtp(m, ga.size) for m in root["acceptable"]],
            "played_in_acceptable_set": root["in_acceptable_set"],
            "human": {"played": {k: v for k, v in human_played.items() if k in ("peer", "target")},
                      "best": {k: v for k, v in human_best.items() if k in ("peer", "target")}},
            "findable_move": idx_to_gtp(findable, ga.size), "findable_probability": round(findable_p, 4),
            "opponent_best_reply": None if reply is None else {"move": reply["move"], "character": reply["character"]},
            "group_events": [e for e in (events or []) if n <= e["move"] <= last_n + th.group_event_hold],
            "stability": stability_flag(before, root["idx"], th),
        })
    return moments


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


def positives(ga: GameAnalysis, rows: list[dict], th: Thresholds, top: int = 3) -> list[dict]:
    """Correct moves a player of the student's rank rarely finds, where the position offered a real choice."""
    if ga.student_color is None:
        return []
    s = sign_of(ga.student_color)
    out = []
    for r in rows:
        if r["color"] != COLOR_CHAR[ga.student_color] or r["points_lost"] > 0.5 or r["idx"] is None:
            continue
        before = ga.positions[r["n"] - 1]
        # a real choice: the second candidate is clearly worse (otherwise every move was as good: dame)
        values = sorted((s * c.score_lead for c in before.candidates), reverse=True)
        if len(values) < 2 or values[0] - values[1] < th.positive_min_choice:
            continue
        hp = human_prob(before, ga.profiles, r["idx"], ga.size)
        peer = hp.get("peer")
        if peer is not None and peer <= 0.10:
            out.append({"move": r["n"], "played": r["move"], "points_lost": r["points_lost"],
                        "peer_probability": peer, "target_probability": hp.get("target")})
    out.sort(key=lambda d: d["peer_probability"])
    return out[:top]


def story(ga: GameAnalysis, th: Thresholds, max_moments: int = 6, complete: bool = True, job_id: str | None = None) -> dict:
    """The survey read as a teacher's first pass (contract §1.6): how the lead moved, which groups lived or
    died and when, where the points changed hands, where the game was decided, and the candidate key moments."""
    rows = move_rows(ga, th)
    ph = phases(ga, th)
    events = group_events(ga, th)
    moments = build_moments(ga, rows, ph, th, events, max_moments)
    student_char = COLOR_CHAR[ga.student_color] if ga.student_color else None
    opp_char = COLOR_CHAR[opponent(ga.student_color)] if ga.student_color else None
    dec, lc = decisive_and_last_chance(ga, rows, th)
    low_visit = [r["n"] for r in rows if r["visits"] < 0.5 * ga.visits_per_move]
    return {
        "job_id": job_id, "game_id": ga.game_id, "complete": complete, "positions_analyzed": ga.M + 1,
        "visits_per_move": ga.visits_per_move,
        "game": {"student_color": student_char, "handicap": ga.handicap, "komi": ga.komi, "rules": ga.rules,
                 "result": ga.result.get("raw", ""), "reconciliation": reconciliation(ga, th)},
        "phases": ph,
        "lead": lead_timeline(ga),
        "group_events": events,
        "swings": swings(ga, rows, ph, th),
        "decisive": dec, "last_chance": lc,
        "points_lost": {"you": points_lost_summary(rows, ph, student_char) if student_char else None,
                        "opponent": points_lost_summary(rows, ph, opp_char) if opp_char else None},
        "moments": moments,
        "positives": positives(ga, rows, th),
        "reliability": {"low_visit_positions": low_visit,
                        "unstable_moments": [m["id"] for m in moments if m["stability"] == "unstable"]},
        "profiles": ga.profiles,
    }
