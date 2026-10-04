"""Per-machine configuration (tool contract §6)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SearchConfig:
    """Fixed search sizes, per machine (no time budget: a review takes as long as it needs). Each profile
    name is what a tool's `budget: {"profile": ...}` resolves to."""
    survey_minutes_target: float = 10.0     # the survey is sized from the measured visits/s to finish in about this long
    survey_floor: int = 100                 # survey visits per move, at least
    survey_cap: int = 1000                  # ... and at most
    root: int = 3000                        # a position's own search (the best move, the candidates)
    line_node: int = 600                    # each search inside a line or a probe
    plies: int = 8                          # forced lines, imagined lines, PV continuations
    stability: int = 4                      # the stability search runs at root x this
    local_solve: int = 4000                 # each playout move of a life-and-death solve
    quick: int = 200                        # quick checks (a recall-quiz answer, an overlay)


@dataclass
class Thresholds:
    acceptable_margin: float = 1.0
    acceptable_min_visit_share: float = 0.05
    episode_min_loss: float = 2.0            # a move losing at least this is a swing worth naming in the story
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
    stability_visit_share: float = 0.35
    stability_margin: float = 0.5
    stability_unstable_margin: float = 0.3
    game_state_close: float = 5.0
    reconciliation_tolerance: float = 2.5
    positive_min_choice: float = 0.5         # positives: the second candidate is this many points worse than the best
    group_event_min_size: int = 4            # story: groups of at least this many stones have their fate told
    group_event_hold: int = 6                # story: a group's new status must still hold this many moves later
    stable_stop_delta: float = 0.5
    # causal evidence: races, reply character, lines, end comparisons
    race_max_liberties: int = 4              # capture race: adjacent unsettled groups, each at most this many liberties
    local_radius: int = 4                    # a reply within this Chebyshev distance of the move is "local"
    sharp_margin: float = 3.0                # local reply beats the best tenuki by this much -> "local_sharp"
    forced_margin: float = 3.0               # forced_line: a reply is forced when the second-best loses more than this
    human_margin: float = 1.0                # forced_line: a human-profile move within this of the best replaces it
    territory_diff_min: float = 2.0          # terminal comparison: report regions that differ by at least this
    group_change_min: float = 0.2            # terminal comparison: a status change needs this much ownership movement
    defend_radius: int = 2                   # intent_probe: a group "defended" by a move has a stone this close to it
    neighborhood_radius: int = 3             # intent_probe: local answers / local attacks are confined to this radius
    misread_margin: float = 3.0              # expectation_probe: the first imagined move losing more than this is the misread


@dataclass
class StudentConfig:
    username: str = ""                      # the student's OGS name, set per machine in [student]
    rank: str = "7k"                        # fallback when the SGF has no rank for the student
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
    restart_above_mb: int = 4000            # before a new survey, restart KataGo if its resident memory is above this many
                                            # MB (~30 s model reload on Metal). Normal use levels off near 2 GB, so this is
                                            # a guard, not a routine step. 0 disables. See README §5a.
    human_profile_key: str = "humanSLProfile"
    search_threads: int | None = None       # numSearchThreadsPerAnalysisThread for this machine (None: analysis.cfg)
    startup_timeout: float = 120.0
    query_timeout: float = 600.0
    report_every: float = 1.0


@dataclass
class PrefetchConfig:
    """Background explain_moment work (contract §1.20): Claude's identical later call returns at once."""
    moments: int = 3                        # when a survey finishes, prepare this many of its key moments (0: off)
    priority: int = 5                       # KataGo priority of background work: above the survey (0), below Claude's calls (10)


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
    search: SearchConfig = field(default_factory=SearchConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    prefetch: PrefetchConfig = field(default_factory=PrefetchConfig)
    reviews_dir: str = "reviews"
    games_dir: str = "games"          # SGF files given by name are looked up here; OGS downloads are cached here
    ogs: OgsConfig = field(default_factory=OgsConfig)
    log_level: str = "info"
    path: str | None = None
    root_dir: str | None = None        # repo root: relative paths in the TOML and analysis.cfg resolve against it

    @property
    def vps(self) -> float:
        return self.throughput.visits_per_second_sustained or self.throughput.visits_per_second_cold or 0.0

    @property
    def throughput_path(self) -> Path | None:
        """The measured-throughput sidecar next to the machine config (<machine>.throughput.json)."""
        return Path(self.path).with_suffix(".throughput.json") if self.path else None


def _fill(dc, data: dict):
    for k, v in (data or {}).items():
        if hasattr(dc, k):
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
    _fill(cfg.search, data.get("search"))
    _fill(cfg.thresholds, data.get("thresholds"))
    _fill(cfg.prefetch, data.get("prefetch"))
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
