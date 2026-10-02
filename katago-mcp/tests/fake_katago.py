"""A stand-in for `katago analysis` that speaks its JSON protocol, for offline engine tests.

Query ids choose the behaviour: `warn*` sends a field warning before the result, `nores*` answers
as KataGo does for a query terminated before it was searched; anything else gets a plain result.
"""
import json
import sys

RESULT = {"isDuringSearch": False, "turnNumber": 0,
          "rootInfo": {"visits": 100, "winrate": 0.7, "scoreLead": 3.0, "currentPlayer": "B"},
          "moveInfos": [{"move": "D4", "order": 0, "visits": 100, "winrate": 0.7, "scoreLead": 3.0,
                         "prior": 0.2, "pv": ["D4"]}]}

for line in sys.stdin:
    q = json.loads(line)
    if q.get("action"):
        continue
    qid = q["id"]
    if qid.startswith("nores"):
        print(json.dumps({"id": qid, "isDuringSearch": False, "turnNumber": 0, "noResults": True}), flush=True)
        continue
    if qid.startswith("warn"):
        print(json.dumps({"id": qid, "field": "overrideSettings", "warning": "Unknown setting"}), flush=True)
    print(json.dumps({"id": qid, **RESULT}), flush=True)
