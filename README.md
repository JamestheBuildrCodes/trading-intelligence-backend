[object Object]

**History response cap safeguard:** the worker recursively subdivides a requested time range if MetaApi returns the 1,000-row maximum. If a one-second interval remains saturated, the sync is marked with `history_response_limit_reached` and the cursor is not advanced past that interval. This avoids silently claiming complete history after a capped response.
