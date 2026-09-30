"""The thresholds and bounds of System Health (PAW-066, Decision 0059 Proposed).

Each value is the recommendation of the Decision; ``REQUIREMENTS.md`` fixes only
the downsampling tiers ("Observability retention / downsampling"). The
thresholds are constants for now: a later Issue may make them settings.
"""

# -- the checks ----------------------------------------------------------------

# How long one source may take before it counts as ``check_failed``.
CHECK_TIMEOUT_SECONDS = 5.0
# A report is reused for this long by the API (every source is read at most this
# often on its behalf; the sampling loop refreshes it anyway).
REPORT_MAX_AGE_SECONDS = 10.0
# The scheduled jobs are read from ``audit_events`` (no index for it): at most
# this often.
JOB_STATUS_MAX_AGE_SECONDS = 300.0

# PostgreSQL: the readiness check itself taking longer than this is a warning.
DATABASE_SLOW_MS = 1_000

# Tasks that failed in the last hour: one is a warning (a single failure), this
# many an error (a continuing failure).
TASK_FAILURES_ERROR = 5

# Shared connections: failed calls in the last hour are a warning when there
# are at least this many and they are at least half of the calls.
CONNECTION_FAILURES_WARNING = 5

# The connection reaper: this many failed cycles in a row is an error (fewer, a
# warning). Settling a row at all is a warning: a process died mid-call.
REAPER_FAILURES_ERROR = 3

# Memory consolidation jobs that went to the dead letter in the last day.
MEMORY_DEAD_WARNING = 1

# A scheduled job whose runs failed this many times in a row is an error (one
# failure is a warning).
JOB_FAILURES_ERROR = 3


# (warning, error) ages of the last successful run, in seconds, per job. The
# timers of ``deploy/systemd``: the backup every 30 minutes, the projection every
# 5 minutes, the audit retention daily.
RECOVERY_BACKUP_STALE = (2 * 3_600, 24 * 3_600)
MEMORY_PROJECTION_STALE = (3_600, 24 * 3_600)
AUDIT_RETENTION_STALE = (2 * 86_400, 7 * 86_400)

# At most this many consecutive failures are counted (a bound on the query).
MAX_COUNTED_FAILURES = 100

# -- the time series -----------------------------------------------------------

# The sampling interval (``PAW_HEALTH_SAMPLE_INTERVAL_SECONDS``): the requirements'
# "last 24 hours: 10-30 seconds".
DEFAULT_SAMPLE_INTERVAL_SECONDS = 30
MIN_SAMPLE_INTERVAL_SECONDS = 10
MAX_SAMPLE_INTERVAL_SECONDS = 300

# (resolution in seconds, how long rows of that resolution are kept before
# they are rolled up into the next one). 0 is the raw samples.
RAW_RESOLUTION = 0
TIERS: tuple[tuple[int, int], ...] = (
    (RAW_RESOLUTION, 86_400),  # raw samples for 24 hours
    (60, 7 * 86_400),  # 1 minute for 7 days
    (300, 30 * 86_400),  # 5 minutes for 30 days
)
HOURLY_RESOLUTION = 3_600  # older: 1 hour
# How long the hourly aggregates and the health events are kept
# (``PAW_HEALTH_RETENTION_DAYS``; the requirements: important operational
# events "1年以上").
DEFAULT_RETENTION_DAYS = 400
MIN_RETENTION_DAYS = 366
MAX_RETENTION_DAYS = 3_650
# The roll-up runs every this many sampling cycles (and at the first one).
ROLLUP_EVERY_CYCLES = 20
# At most this many rows per statement of the roll-up and purge.
MAX_ROLLUP_ROWS = 50_000

# -- the API -------------------------------------------------------------------

METRIC_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,40}(\.[a-z][a-z0-9_]{0,40}){1,3}$"
MAX_SERIES_POINTS = 1_000
MAX_SERIES_SPAN_SECONDS = 400 * 86_400
DEFAULT_SERIES_SPAN_SECONDS = 86_400
MAX_EVENTS = 500
DEFAULT_EVENTS = 100
