"""Single source of truth for Feishu Drive structural limits.

The scanner and the preflight both judge the same tree, so they must judge it
against the same numbers. When they disagreed the operator got the worst
possible outcome: a multi-hour inventory reported "no issues", and only the
preflight afterwards refused to migrate.
"""

from __future__ import annotations

# Feishu rejects object names longer than this.
MAX_FEISHU_NAME_LENGTH = 250

# Maximum depth of a full path inside Feishu Drive.
DRIVE_MAX_DEPTH = 15

# Inventory depths are relative to the selected local root. Every migration
# creates one dedicated wrapper folder in Drive, so local content may consume
# only the remaining levels.
DRIVE_MAX_LOCAL_DEPTH = DRIVE_MAX_DEPTH - 1

# Maximum number of children Feishu Drive accepts in a single folder.
DRIVE_MAX_CHILDREN = 1_500

# Guard against pathological trees before they reach the planner.
DRIVE_MAX_TREE_NODES = 400_000
