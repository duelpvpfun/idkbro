"""Central configuration loaded from environment / .env."""

from __future__ import annotations

from enum import Enum

from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Trading
    trading_mode: TradingMode = TradingMode.PAPER
    starting_bankroll_usd: float = 100.0

    # Data providers
    helius_api_key: str = ""
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"

    # --- OpenAI (optional, ONLY for pfp/banner image generation) ---
    # Note: ChatGPT Plus does NOT include API access. This is separate pay-per-use billing.
    openai_api_key: str = ""
    openai_image_model: str = "gpt-image-1"

    # --- Birdeye (richer market data: candles, holders, security) ---
    birdeye_api_key: str = ""
    birdeye_max_calls_per_day: int = 40_000   # Lite = 2.5M credits/mo; ~40k/day is safe
    birdeye_chain: str = "solana"

    # --- Helius credit budget (protects your monthly credit) ---
    # Hard cap on Helius API calls per day across the whole agent. Generous early so the
    # agent can study a lot and learn fast; still a safety brake vs a runaway loop.
    helius_max_calls_per_day: int = 800_000
    # Wallet discovery loop pacing (faster now that the budget is roomy).
    wallet_study_batch: int = 20          # wallets studied per discovery cycle
    wallet_study_interval_seconds: float = 45.0   # gap between discovery cycles

    # LLM
    anthropic_api_key: str = ""
    # Cheap model for high-volume calls (coin thesis, position re-checks).
    anthropic_fast_model: str = "claude-haiku-4-5"
    # Stronger model for entry conviction + periodic reflection.
    anthropic_smart_model: str = "claude-sonnet-4-5"
    # Hard daily cap on Claude calls (safety backstop against a cost runaway).
    llm_max_calls_per_day: int = 8000

    # --- Memory backup (so his brain is never lost) ---
    backup_enabled: bool = True
    backup_interval_seconds: float = 1800.0   # snapshot every 30 min
    backup_keep: int = 20                      # keep the most recent N snapshots
    backup_git_push: bool = False             # also push snapshots to a git branch (off-machine)
    backup_git_branch: str = "memory-backup"

    # --- X / Twitter (the agent posts its trading journey) ---
    x_enabled: bool = False               # master switch
    x_api_key: str = ""                   # consumer key
    x_api_secret: str = ""                # consumer secret
    x_access_token: str = ""
    x_access_secret: str = ""
    x_bearer_token: str = ""
    x_dry_run: bool = True                # log tweets instead of posting (safe default)
    x_min_minutes_between: float = 20.0   # rate/credit guard: min gap between tweets
    x_max_per_day: int = 15               # hard daily post cap
    x_max_per_month: int = 400            # hard MONTHLY cap so $98 can't burn in 2-3 days
    x_max_thread_tweets: int = 4          # max tweets in one thread (counts against caps)
    x_post_buys: bool = True
    x_post_closes: bool = True
    x_post_reflections: bool = True
    # Discovery/read quota protection (Basic tier reads are very limited).
    x_reads_per_day: int = 40             # hard cap on read calls/day (profiles + timelines)
    x_posts_per_kol: int = 3              # how many recent posts to sample per KOL
    x_follow_trusted: bool = True         # follow KOLs the agent decides to trust
    x_max_follows_per_day: int = 10       # cap follow actions/day

    # --- Observation window ---
    # How long to watch a fresh launch before deciding (seconds).
    observation_seconds: float = 20.0

    # --- Redeploy filter (skip already-migrated names) ---
    # When a coin migrates (graduates), its name+ticker is 'spent'; copycats spam it.
    # Skip fresh launches reusing a name+ticker that migrated within this window.
    skip_migrated_redeploys: bool = True
    migrated_name_ttl_days: float = 30.0

    # --- Safety (pump.fun specific) ---
    max_dev_holding_pct: float = 8.0
    max_sniped_pct: float = 15.0          # snipers/bundlers in the first moments
    max_top10_holding_pct: float = 70.0   # holder concentration via Helius; high on purpose

    # --- Hard stop-loss (SURVIVAL floor, not strategy) ---
    # The agent freely decides take-profit and can cut earlier whenever it wants, but it
    # can NEVER hold past these floors. This is the catastrophe brake against rugs.
    hard_stop_enabled: bool = True
    hard_stop_loss_pct: float = 0.40      # force exit if down this much from entry
    hard_trailing_giveback_pct: float = 0.55  # force exit if it gives back this much from peak (once in profit)
    hard_stop_min_peak_x: float = 1.5     # trailing floor only arms after reaching this multiple
    # A fresh buy that goes nowhere is dead money. After this long roughly flat, force exit
    # (the agent can still cut earlier). And if we never even get a live price, exit sooner.
    max_hold_minutes: float = 45.0        # force exit a stagnant position after this long
    flat_band_pct: float = 0.15           # within +/- this of entry counts as 'nothing moved'
    untracked_exit_minutes: float = 12.0  # if we never got a price, exit this fast

    # --- Sizing (fractions of equity) ---
    size_normal_min: float = 0.05
    size_normal_max: float = 0.10
    size_high_min: float = 0.11
    size_high_max: float = 0.18
    size_hard_cap: float = 0.20           # never exceed on a single entry
    max_concurrent_positions: int = 8

    # --- Meta tracker (learn what's running now) ---
    meta_refresh_seconds: float = 600.0       # re-read the live meta every 10 min

    # --- Old-coin revival scanner ---
    scanner_enabled: bool = True
    scanner_interval_seconds: float = 60.0    # how often to sweep the market
    scanner_max_checks_per_sweep: int = 25    # cap DexScreener lookups per sweep
    revival_min_age_hours: float = 1.0        # must be older than a fresh launch
    revival_min_liquidity_usd: float = 8_000  # tradeable floor
    revival_vol_spike_mult: float = 3.0       # 1h vol vs avg hour to count as a spike
    revival_min_h1_change: float = 15.0       # % 1h move to flag a breakout
    revival_trigger_strength: float = 0.4     # detector strength needed to call Claude
    missed_runner_multiple: float = 3.0       # a skipped coin up this much = "one that got away"

    # --- Exploration / curiosity ---
    # Buy score threshold (lower = tries more coins). On paper money in the exploration
    # phase we keep this modest so the agent actually takes shots and learns from outcomes,
    # then it can tighten its own bar via lessons once it has data.
    entry_score_threshold: float = 0.40
    # Chance to take a small "learning" position on a borderline coin it would otherwise
    # skip, so it keeps gathering data on setups it's unsure about.
    exploration_rate: float = 0.25
    exploration_size_pct: float = 0.03    # tiny probe size for exploratory buys

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Simulator (fallback when no live feed / for offline dev)
    use_simulator: bool = False
    sim_tokens_per_minute: int = 8

    @property
    def has_live_feed(self) -> bool:
        return not self.use_simulator

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
