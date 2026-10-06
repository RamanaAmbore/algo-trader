"""Fragment impact: $ref collection, transitive reach, the agents a change would touch, and the endpoint."""
from types import SimpleNamespace

import pytest

from backend.api.algo.agent_evaluator import (
    collect_refs, fragments_reaching, referencing_agents,
)
from backend.api.routes import agent_templates as at


def test_collect_refs_finds_nested_refs_in_conditions_and_events():
    tree = {"all": [{"$ref": "a"}, {"not": {"any": [{"$ref": "b"}]}}]}
    assert collect_refs(tree) == {"a", "b"}
    assert collect_refs([{"channel": "telegram"}, {"$ref": "ops"}]) == {"ops"}


def test_collect_refs_ignores_empty_and_non_string_refs():
    assert collect_refs({"$ref": ""}) == set()
    assert collect_refs({"$ref": 3}) == set()


def test_reach_includes_the_target_and_fragments_that_nest_it():
    bodies = {"loss": {"$ref": "base"}, "base": {"op": "<"}, "other": {"all": []},
              "outer": {"any": [{"$ref": "loss"}]}}
    assert fragments_reaching("base", bodies) == {"base", "loss", "outer"}


def test_reach_stops_on_cycles():
    bodies = {"a": {"$ref": "b"}, "b": {"$ref": "a"}}
    assert fragments_reaching("a", bodies) == {"a", "b"}


AGENTS = [
    {"slug": "loss-funds", "status": "active", "conditions": {"$ref": "loss"}, "events": []},
    {"slug": "outer-check", "status": "inactive", "conditions": {"any": [{"$ref": "outer"}]}, "events": []},
    {"slug": "unrelated", "status": "active", "conditions": {"op": "<"}, "events": [{"$ref": "loss"}]},
]


def test_condition_impact_lists_direct_and_transitive_agents():
    bodies = {"base": {"op": "<"}, "loss": {"$ref": "base"}, "outer": {"$ref": "loss"}}
    hits = referencing_agents("condition", "base", bodies, AGENTS)
    assert [h["slug"] for h in hits] == ["loss-funds", "outer-check"]
    assert hits[0]["via"] == ["loss"]


def test_notify_impact_reads_events_not_conditions():
    hits = referencing_agents("notify", "loss", {"loss": {}}, AGENTS)
    assert [h["slug"] for h in hits] == ["unrelated"]


def test_no_references_gives_empty_impact():
    assert referencing_agents("condition", "lonely", {"lonely": {}}, AGENTS) == []


@pytest.mark.asyncio
async def test_endpoint_returns_impact_for_a_fragment(monkeypatch):
    row = SimpleNamespace(id=7, kind="condition", name="base")

    async def load_fragment(frag_id):
        return row if frag_id == 7 else None

    async def load_bodies(kind):
        return {"base": {"op": "<"}, "loss": {"$ref": "base"}}

    async def load_agents():
        return AGENTS

    monkeypatch.setattr(at, "_load_fragment", load_fragment)
    monkeypatch.setattr(at, "_load_bodies", load_bodies)
    monkeypatch.setattr(at, "_load_agents", load_agents)
    out = await at.AgentTemplateController.fragment_references.fn(None, 7)
    assert out["fragment"] == {"id": 7, "kind": "condition", "name": "base"}
    assert out["fragments"] == ["loss"]
    assert [a["slug"] for a in out["agents"]] == ["loss-funds"]


@pytest.mark.asyncio
async def test_endpoint_404s_for_unknown_fragment(monkeypatch):
    from litestar.exceptions import HTTPException

    async def none(*a, **k):
        return None

    monkeypatch.setattr(at, "_load_fragment", none)
    with pytest.raises(HTTPException) as exc:
        await at.AgentTemplateController.fragment_references.fn(None, 99)
    assert exc.value.status_code == 404
