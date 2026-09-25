"""Limits of the DAG orchestrator (PAW-034).

Every size here bounds something a planner or an agent runtime controls. The
numbers are **provisional product choices** that the requirements do not settle;
``docs/decisions/0021-dag-orchestrator-policy.md`` (Proposed) lists them for the
human. The database repeats the ones that shape a column (key format, JSON
sizes) as CHECK constraints (migration ``0034``): ``tests/test_orchestrator_schema.py``
fails when the two disagree, so a limit that is written into the database is
changed with a new migration and a new Decision, not by editing the constant.
"""

# -- plan (what a planner may propose) -------------------------------------------
MAX_NODES = 32
MAX_DEPENDENCIES = 8  # per node
MAX_EDGES = 96
MAX_DEPTH = 16  # nodes on the longest dependency chain
MAX_KEY_LENGTH = 32
KEY_PATTERN = r"[a-z][a-z0-9_-]{0,31}"
MAX_TITLE_CHARS = 100
MAX_GOAL_CHARS = 4_000
MAX_NODE_INPUT_BYTES = 16 * 1024
MAX_NODE_INPUT_DEPTH = 8
MAX_PLAN_BYTES = 128 * 1024  # all texts and inputs of a plan together
MAX_NODE_CAPABILITIES = 16
MAX_NODE_REPOSITORIES = 16

# -- results (what a node passes on) ---------------------------------------------
MAX_RESULT_BYTES = 32 * 1024  # the JSON of one node's result
MAX_UPSTREAM_BYTES = MAX_DEPENDENCIES * MAX_RESULT_BYTES  # all inputs of one node
MAX_SUMMARY_CHARS = 2_000
MAX_LIST_ITEMS = 50
MAX_ITEM_CHARS = 1_000
MAX_CHANGED_FILES = 200
MAX_PATH_CHARS = 500
MAX_COMMIT_CHARS = 64
MAX_TEST_RESULT_BYTES = 4 * 1024
MAX_TEST_RESULT_DEPTH = 4
# The database refuses a JSON column larger than this (twice the service limit:
# the service is the rule, the database the backstop).
DB_MAX_JSON_BYTES = 2 * MAX_RESULT_BYTES

# -- scheduling ------------------------------------------------------------------
DEFAULT_MAX_PARALLEL_NODES = 4
MAX_PARALLEL_NODES = 16
DEFAULT_MAX_ATTEMPTS_PER_RUNG = 6  # attempts of one node on one agent, one run
MAX_ATTEMPTS_PER_RUNG = 20
MAX_LADDER_LENGTH = 4  # agents an escalation can climb through
DEFAULT_NODE_TIMEOUT_SECONDS = 1_800.0
MAX_NODE_TIMEOUT_SECONDS = 86_400.0
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_BACKOFF_SECONDS = 60.0
DEFAULT_MAX_PLAN_ATTEMPTS = 2
MAX_PLAN_ATTEMPTS = 5
MAX_ERROR_CLASS_CHARS = 100

# -- the project task-stop loop ----------------------------------------------------
DEFAULT_STOP_INTERVAL_SECONDS = 60.0
MIN_STOP_INTERVAL_SECONDS = 10.0
MAX_STOP_INTERVAL_SECONDS = 3_600.0
DEFAULT_PROJECTS_PER_CYCLE = 50
MAX_PROJECTS_PER_CYCLE = 500
DEFAULT_ROUNDS_PER_PROJECT = 5
MAX_ROUNDS_PER_PROJECT = 50
