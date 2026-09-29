"""Per-machine configuration (tool contract §6)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Unit:
    root: int = 1000
    line_node: int = 250
    plies: int = 6
    stability: list[int] = field(default_factory=lambda: [4])
    solve: int = 1500

    def copy(self) -> "Unit":
        return Unit(self.root, self.line_node, self.plies, list(self.stability), self.solve)


@dataclass
class BudgetConfig:
    overhead_minutes: float = 4.0
    claude_minutes_per_episode: float = 1.0
    self_review_minutes_default: float = 10.0
    survey_floor: int = 100
    survey_cap: int = 1000
    ld_reserve_episodes: int = 1
    max_episodes: int = 5
    min_episodes: int = 3
    unit_base: Unit = field(default_factory=Unit)
    unit_cap: Unit = field(default_factory=lambda: Unit(6000, 1000, 12, [4, 16], 4000))
    ladder: list[str] = field(default_factory=lambda: [
        "root_3000", "line_600", "stability_16x", "plies_12", "solve_4000_all_ld", "root_6000", "line_1000"])


@dataclass
class Thresholds:
    acceptable_margin: float = 1.0
    acceptable_min_visit_share: float = 0.05
    episode_min_loss: float = 2.0
    cluster_plies: int = 12
    cluster_distance: int = 4
    cluster_max_span: int = 24
    cluster_max_moves: int = 6
    alive: float = 0.6
    dead: float = -0.6
    settled_abs: float = 0.8
    opening_settledness: float = 0.35
    opening_max_move: int = 50
    endgame_settledness: float = 0.75
    endgame_hold: float = 0.70
    decided_winrate: float = 0.15
    recovery_winrate: float = 0.35
    decided_score_handicap: float = -15.0
    recovery_score_handicap: float = -8.0
    got_away_ratio: float = 0.6
    local_share_local: float = 0.7
    local_share_global: float = 0.4
    stability_visit_share: float = 0.35
    stability_margin: float = 0.5
    stability_unstable_margin: float = 0.3
    game_state_close: float = 5.0
    single_blunder_share: float = 0.40
    accumulation_share: float = 0.20
    reconciliation_tolerance: float = 2.5
    tag_min_loss: float = 3.0                # got-away-with-it
    # candidate-tag rules (calibrated in WS8 on 20 seed games)
    tag_plausible_min_loss: float = 2.0      # 3/4/5: root loss
    tag_plausible_min_peer: float = 0.20     # 3/4/5: peer-rank probability of the played move
    tag_plausible_ratio: float = 1.5         # 3/4/5: played >= ratio x best (peer probabilities)
    tag_intuition_best_min: float = 0.20     # 6/15/1: target-rank probability of the best move
    tag_intuition_played_max: float = 0.10   # 6/15/1: peer-rank probability of the played move
    tag_punish_min_loss: float = 5.0         # 13: the opponent's previous move lost at least this
    tag_direction_min_distance: int = 5      # 1 by ownership attribution: best this far from played
    quick_visits: int = 200
    stable_stop_delta: float = 0.5
    # causal evidence (contract v0.3): races, reply character
    race_max_liberties: int = 4              # capture race: adjacent unsettled groups, each at most this many liberties
    local_radius: int = 4                    # a reply within this Chebyshev distance of the move is "local"
    sharp_margin: float = 3.0                # local reply beats the best tenuki by this much -> "local_sharp"


@dataclass
class StudentConfig:
    username: str = "cwhay888"
    rank: str = "7k"
    target_offset_stones: int = 3
    horizon_rank: str = "rank_1d"


@dataclass
class KatagoConfig:
    binary: str = "katago"
    analysis_config: str = "config/analysis.cfg"
    model: str = "models/kata1-b18c384nbt-latest.bin.gz"
    human_model: str | None = "models/b18c384nbt-humanv0.bin.gz"
    perspective: str = "BLACK"              # must match reportAnalysisWinratesAs in analysis.cfg
    start_on_boot: bool = False             # False: KataGo starts on the first tool that needs it (Claude Desktop
                                            # launches two server instances; only the one in use should load a model)
    first_call_wait_seconds: float = 45.0   # how long a tool call waits for a starting engine before answering "starting"
    restart_after_queries: int = 6000       # a new survey restarts KataGo once this many queries have run since its start
                                            # (frees the NN cache; ~30 s on Metal). 0 disables.
    human_profile_key: str = "humanSLProfile"
    search_threads: int | None = None       # numSearchThreadsPerAnalysisThread for this machine (None: analysis.cfg)
    startup_timeout: float = 120.0
    query_timeout: float = 600.0
    report_every: float = 1.0


@dataclass
class OgsConfig:
    enabled: bool = True
    timeout: float = 20.0
    base_url: str = "https://online-go.com"


@dataclass
class Throughput:
    visits_per_second_cold: float = 0.0
    visits_per_second_sustained: float = 0.0
    measured_at: str = ""


@dataclass
class Config:
    machine: str = "unnamed"
    katago: KatagoConfig = field(default_factory=KatagoConfig)
    throughput: Throughput = field(default_factory=Throughput)
    student: StudentConfig = field(default_factory=StudentConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    reviews_dir: str = "reviews"
    games_dir: str = "games"          # SGF files given by name are looked up here; OGS downloads are cached here
    ogs: OgsConfig = field(default_factory=OgsConfig)
    log_level: str = "info"
    path: str | None = None
    root_dir: str | None = None        # repo root: relative paths in the TOML and analysis.cfg resolve against it

    @property
    def vps(self) -> float:
        return self.throughput.visits_per_second_sustained or self.throughput.visits_per_second_cold or 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _fill(dc, data: dict):
    for k, v in (data or {}).items():
        if hasattr(dc, k):
            cur = getattr(dc, k)
            if isinstance(cur, Unit) and isinstance(v, dict):
                _fill(cur, v)
            else:
                setattr(dc, k, v)
    return dc


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path is None:
        return cfg
    p = Path(path)
    with p.open("rb") as f:
        data = tomllib.load(f)
    cfg.path = str(p)
    cfg.machine = data.get("machine", {}).get("name", cfg.machine)
    _fill(cfg.katago, data.get("katago"))
    _fill(cfg.throughput, data.get("throughput"))
    _fill(cfg.student, data.get("student"))
    b = data.get("budget", {})
    _fill(cfg.budget, {k: v for k, v in b.items() if k not in ("unit_base", "unit_cap")})
    if "unit_base" in b:
        _fill(cfg.budget.unit_base, b["unit_base"])
    if "unit_cap" in b:
        _fill(cfg.budget.unit_cap, b["unit_cap"])
    _fill(cfg.thresholds, data.get("thresholds"))
    cfg.reviews_dir = data.get("paths", {}).get("reviews_dir", cfg.reviews_dir)
    cfg.games_dir = data.get("paths", {}).get("games_dir", cfg.games_dir)
    _fill(cfg.ogs, data.get("ogs"))
    cfg.log_level = data.get("logging", {}).get("level", cfg.log_level)
    # Relative paths are relative to the repo root (the parent of config/), not to the process cwd:
    # Claude Desktop launches servers from an arbitrary directory.
    cfg_dir = p.resolve().parent
    root = cfg_dir.parent if cfg_dir.name == "config" else cfg_dir
    cfg.root_dir = str(root)

    def absolute(v: str | None) -> str | None:
        if not v:
            return v
        q = Path(v).expanduser()
        return str(q if q.is_absolute() else (root / q).resolve())

    cfg.katago.analysis_config = absolute(cfg.katago.analysis_config)
    cfg.katago.model = absolute(cfg.katago.model)
    cfg.katago.human_model = absolute(cfg.katago.human_model)
    cfg.reviews_dir = absolute(cfg.reviews_dir)
    cfg.games_dir = absolute(cfg.games_dir)
    if "/" in cfg.katago.binary or "\\" in cfg.katago.binary:
        cfg.katago.binary = absolute(cfg.katago.binary)
    return cfg
