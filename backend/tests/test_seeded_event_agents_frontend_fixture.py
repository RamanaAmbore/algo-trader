"""
Guards `frontend/src/lib/__tests__/fixtures/seededEventAgents.json` — a
static snapshot of every real `SEEDED_AGENTS` entry's `(slug, name,
conditions, events, actions)`, used by the frontend's Wave 2 byte-identical
round-trip test (`agentsGrammar.test.js` — originally "23 real seeded agents
compile to byte-identical JSON under the new `WHEN log(...) DO render(...)`
syntax"; 2026-10-25 alert-channel collapse reduced the real count to 10 —
see `event_agents.SEED_VERSION`'s own comment).

The fixture is a STATIC file (not regenerated at test time) so the frontend
suite needs no Python/DB dependency. This test is the drift guard: if a
future change to `event_agents.py` adds/renames/reshapes a seeded agent
without updating the fixture, this fails loudly instead of letting the
frontend round-trip test silently validate against a stale shape.

To regenerate the fixture after an intentional `SEEDED_AGENTS` change:

    venv/bin/python3 -c "
    import json
    import backend.api.algo.event_agents as ea
    out = [{'slug': a['slug'], 'name': a['name'], 'conditions': a.get('conditions'),
            'events': a.get('events') or [], 'actions': a.get('actions')}
           for a in ea.SEEDED_AGENTS]
    with open('frontend/src/lib/__tests__/fixtures/seededEventAgents.json', 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False); f.write('\\n')
    "
"""
import json
from pathlib import Path

from backend.api.algo import event_agents

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend" / "src" / "lib" / "__tests__" / "fixtures" / "seededEventAgents.json"
)


def _live_dump() -> list[dict]:
    return [
        {
            "slug": a["slug"],
            "name": a["name"],
            "conditions": a.get("conditions"),
            "events": a.get("events") or [],
            "actions": a.get("actions"),
        }
        for a in event_agents.SEEDED_AGENTS
    ]


def test_frontend_fixture_matches_live_seeded_agents():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture = json.load(f)
    live = _live_dump()
    assert fixture == live, (
        "frontend/src/lib/__tests__/fixtures/seededEventAgents.json is stale — "
        "a SEEDED_AGENTS entry changed shape without the fixture being "
        "regenerated (see this file's own module docstring for the "
        "regeneration command)."
    )


def test_fixture_has_all_10_agents_and_covers_both_log_and_any_shapes():
    # 2026-10-25: 23 -> 10 — the alert-channel collapse retires 16
    # per-event detector agents into 3 generic channel agents (see
    # event_agents.SEED_VERSION's own comment).
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture = json.load(f)
    assert len(fixture) == 10
    has_plain_log = any("log" in a["conditions"] for a in fixture)
    has_any_of_logs = any("any" in a["conditions"] for a in fixture)
    has_nonempty_events = any(a["events"] for a in fixture)
    has_gate_true = any(a["actions"][0].get("gate") is True for a in fixture)
    assert has_plain_log, "fixture must cover a bare {'log': {...}} condition shape"
    assert has_any_of_logs, "fixture must cover an {'any': [{'log':...}, {'log':...}]} shape"
    assert has_nonempty_events, "fixture must cover a real agent with a non-empty events/channel list"
    assert has_gate_true, "fixture must cover the action-level gate=true shape (error-alert)"
