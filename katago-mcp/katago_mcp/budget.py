"""plan_budget (tool contract §1.2): time budget -> concrete allocation."""
from __future__ import annotations

from .config import BudgetConfig, Unit


class BudgetError(ValueError):
    pass


# Line-search defaults of the probes (tools.py uses these as option defaults) and the searches per verified
# episode at line_node visits they imply (contract §1.2.2), p = u.plies:
INTENT_SEARCHES = 8                       # intent_probe
EXPECTATION_REFUTATION_PLIES = 4          # expectation_probe: engine moves played out after the misread
FORCED_RESISTANCE_NODES = 2               # forced_line: natural resistances tried by the opponent
FORCED_REFUTATION_PLIES = 3               # forced_line: engine moves refuting each resistance
PUNISH_PLIES = 3                          # analyze_line: opponent plies in the refutation probability
REFUTATION_PLIES = EXPECTATION_REFUTATION_PLIES     # older name


def line_node_searches(p: int) -> int:
    expectation = 2 * p + EXPECTATION_REFUTATION_PLIES + 1     # each imagined move: one search, one more when off the candidates
    forced = 2 * p + 3 + FORCED_RESISTANCE_NODES * (2 + FORCED_REFUTATION_PLIES)   # two per node, end features, resistances
    return INTENT_SEARCHES + expectation + 2 * forced      # forced lines from the better and the played move


def unit_visits(u: Unit) -> int:
    return u.root + sum(u.stability) * u.root + line_node_searches(u.plies) * u.line_node


def solve_visits(u: Unit) -> int:
    return 2 * u.solve


def minutes(visits: float, vps: float) -> float:
    return visits / vps / 60.0


def apply_step(u: Unit, step: str) -> Unit:
    v = u.copy()
    if step == "root_3000":
        v.root = max(v.root, 3000)
    elif step == "line_600":
        v.line_node = max(v.line_node, 600)
    elif step == "stability_16x":
        if 16 not in v.stability:
            v.stability = sorted(set(v.stability) | {16})
    elif step == "plies_8":
        v.plies = max(v.plies, 8)
    elif step == "plies_12":                              # pre-v0.3 configs
        v.plies = max(v.plies, 12)
    elif step == "solve_4000_all_ld":
        v.solve = max(v.solve, 4000)
    elif step == "root_6000":
        v.root = max(v.root, 6000)
    elif step == "line_1000":
        v.line_node = max(v.line_node, 1000)
    else:
        raise BudgetError(f"unknown ladder step {step!r}")
    return v


def survey_visits(cfg: BudgetConfig, vps: float, move_count: int, minutes: float | None = None) -> int:
    """Survey visits per move so that the survey takes about `minutes` (default: the survey target)."""
    m = cfg.survey_minutes_target if minutes is None else minutes
    return cap_clamp(int(vps * m * 60 / max(1, move_count)), cfg.survey_floor, cfg.survey_cap)


def search_profiles(survey_visits: int, u: Unit, quick: int = 200) -> dict:
    """Visits per budget profile (the `profiles` of a plan)."""
    return {
        "survey": survey_visits,
        "root": u.root,
        "line_node": u.line_node,
        "stability": [m * u.root for m in u.stability],
        "local_solve": u.solve,
        "quick": quick,
    }


def _per_episode(u: Unit) -> dict:
    return {"root_visits": u.root, "line_node_visits": u.line_node, "follow_pv_plies": u.plies,
            "forced_line_plies": u.plies, "expectation_plies": u.plies,
            "stability_multipliers": list(u.stability), "local_solve_visits": u.solve,
            "probes": ["intent_probe", "expectation_probe", "forced_line (better move)", "forced_line (played move)"]}


def plan(cfg: BudgetConfig, vps: float, move_count: int, total_minutes,
         self_review_minutes: float | None = None, episodes_requested: int | None = None,
         interview_minutes: float | None = None,
         expected_ld_episodes: int | None = None, selected: list[dict] | None = None,
         elapsed_minutes: float = 0.0, survey_visits_existing: int | None = None, quick_visits: int = 200) -> dict:
    """Return the allocation dict of tool contract §1.2.  Pure function."""
    if vps <= 0:
        raise BudgetError("throughput unknown: run `katago-mcp benchmark` first")
    if move_count <= 0:
        raise BudgetError("move_count must be positive")
    O = cfg.overhead_minutes
    C_ep = cfg.claude_minutes_per_episode
    S = cfg.self_review_minutes_default if self_review_minutes is None else float(self_review_minutes)   # blind self-review
    I = cfg.interview_minutes_default if interview_minutes is None else float(interview_minutes)       # episode interviews
    # the survey is sized by its own target, so a short blind review does not cut its visits; given an
    # explicit self_review_minutes (e.g. 0: "review just this sequence") it is sized to that, as before v0.3
    S_survey = cfg.survey_minutes_target if self_review_minutes is None else S
    E_req = min(cfg.max_episodes, episodes_requested or cfg.max_episodes)
    LD = cfg.ld_reserve_episodes if expected_ld_episodes is None else int(expected_ld_episodes)
    base, cap = cfg.unit_base, cfg.unit_cap
    unlimited = total_minutes in ("unlimited", None) or (isinstance(total_minutes, str))
    T = None if unlimited else float(total_minutes)
    notes: list[str] = []
    replan = selected is not None

    def ep_minutes(u: Unit) -> float:
        return minutes(unit_visits(u), vps) + C_ep

    def verification_minutes(u: Unit, n: int, ld: int) -> float:
        return n * ep_minutes(u) + min(ld, n) * minutes(solve_visits(u), vps)

    # ---------------------------------------------------------------- survey
    if replan and survey_visits_existing:
        v_s = survey_visits_existing
    else:
        v_s = survey_visits(cfg, vps, move_count, S_survey)
    if unlimited and not replan:
        v_s = cfg.survey_cap
    t_s = minutes(move_count * v_s, vps)
    c_s = max(0.0, t_s - S)
    if c_s > 0:
        notes.append(f"the survey at {v_s} visits/move needs {t_s:.1f} min on this machine; {c_s:.1f} min run past "
                     f"the blind self-review (ask the optional questions meanwhile) and are charged to the budget")

    # ---------------------------------------------------------------- minimum for three episodes
    v_min = cfg.survey_floor
    c_min = max(0.0, minutes(move_count * v_min, vps) - S)
    minimum_three = O + S + I + c_min + verification_minutes(base, cfg.min_episodes, LD)

    # ---------------------------------------------------------------- unlimited
    if unlimited:
        n = E_req if not replan else min(cfg.max_episodes, len(selected))
        ld = LD if not replan else sum(1 for s in selected if s.get("needs_local_solve"))
        u = cap
        expected_total = O + max(S, t_s) + I + verification_minutes(u, n, ld)
        return {
            "feasible": True, "mode": "unlimited", "total_minutes": "unlimited", "elapsed_minutes": elapsed_minutes,
            "throughput_vps": vps,
            "reserved": {"overhead_minutes": O, "self_review_minutes": S, "interview_minutes": I},
            "survey": {"visits_per_move": v_s, "expected_minutes": round(t_s, 2), "runs_past_self_review": c_s > 0,
                       "charged_minutes": round(c_s, 2)},
            "verification": {"wall_minutes_available": None, "episodes": n, "per_episode": _per_episode(u),
                             "ld_episodes_budgeted": min(ld, n), "ladder_steps_applied": list(cfg.ladder),
                             "expected_minutes": round(verification_minutes(u, n, ld), 2), "slack_minutes": None},
            "minimum_minutes_for_three_episodes": round(minimum_three, 1),
            "expected_total_minutes": round(expected_total, 1),
            "profiles": search_profiles(v_s, u, quick_visits), "notes": notes,
        }

    # ---------------------------------------------------------------- wall clock for verification
    if replan:
        W = T - O - elapsed_minutes - I                    # the interviews come after the re-plan
        n_target = min(cfg.max_episodes, len(selected))
        ld = sum(1 for s in selected if s.get("needs_local_solve"))
        if len(selected) > cfg.max_episodes:
            notes.append(f"{len(selected)} episodes selected; only {cfg.max_episodes} are budgeted")
    else:
        W = T - O - S - I - c_s
        n_target = E_req
        ld = LD

    # ---------------------------------------------------------------- episode count at base rigor
    n = 0
    for m in range(n_target, 0, -1):
        if verification_minutes(base, m, ld) <= W:
            n = m
            break
    if replan:
        feasible = n == n_target
        if not feasible:
            notes.append(f"only {n} of {n_target} selected episodes fit at base rigor in the remaining "
                         f"{W:.1f} min; drop episodes or extend the budget")
    else:
        feasible = n >= cfg.min_episodes
        if not feasible:
            notes.append(f"fewer than {cfg.min_episodes} episodes fit; a three-episode review needs about "
                         f"{minimum_three:.0f} min on this machine")
    if W <= 0:
        notes.append("no time left for verification after overhead, self-review and interviews")

    # ---------------------------------------------------------------- depth ladder
    u = base.copy()
    applied: list[str] = []
    used = verification_minutes(u, n, ld) if n > 0 else 0.0
    surplus = W - used
    if n > 0:
        for step in cfg.ladder:
            nu = apply_step(u, step)
            cost = verification_minutes(nu, n, ld) - verification_minutes(u, n, ld)
            if cost <= surplus:
                u = nu
                surplus -= cost
                applied.append(step)
            else:
                break
    expected_verif = verification_minutes(u, n, ld) if n > 0 else 0.0
    expected_total = O + (S + c_s if not replan else elapsed_minutes) + I + expected_verif
    return {
        "feasible": feasible, "mode": "replan" if replan else "plan", "total_minutes": T,
        "elapsed_minutes": elapsed_minutes, "throughput_vps": vps,
        "reserved": {"overhead_minutes": O, "self_review_minutes": S, "interview_minutes": I},
        "survey": {"visits_per_move": v_s, "expected_minutes": round(t_s, 2), "runs_past_self_review": c_s > 0,
                   "charged_minutes": round(c_s, 2)},
        "verification": {"wall_minutes_available": round(W, 2), "episodes": n, "per_episode": _per_episode(u),
                         "ld_episodes_budgeted": min(ld, n), "ladder_steps_applied": applied,
                         "expected_minutes": round(expected_verif, 2), "slack_minutes": round(max(surplus, 0.0), 2)},
        "minimum_minutes_for_three_episodes": round(minimum_three, 1),
        "expected_total_minutes": round(expected_total, 1),
        "profiles": search_profiles(v_s, u, quick_visits), "notes": notes,
    }


def cap_clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

