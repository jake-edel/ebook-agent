"""The core interface stays transport-agnostic: everything crossing it is plain data."""

import dataclasses
import json
import typing
from pathlib import Path

import pytest

import core
from core import (
    AgentStatus,
    BotMessage,
    Request,
    RequestKind,
    RequestState,
    RequestUpdated,
    StatusChanged,
    to_dict,
)

STATUS = AgentStatus(connected=True, server="irc.irchighway.net", nick="ebooks_cli",
                     channel="#ebooks", peer="142.44.0.1", tls_fingerprint="ab12")
REQ = Request(id="r1", kind=RequestKind.BOOK, line="!Bsk Dune.epub", bot="Bsk",
              state=RequestState.DONE, created_at=1.0, updated_at=2.0,
              filename="Dune.epub", size=10, received=10, path=Path("/books/Dune.epub"))

# Types allowed in core dataclass fields: plain values a JSON transport can carry
_ALLOWED = {str, int, float, bool, type(None), Path, RequestKind, RequestState,
            AgentStatus, Request}


def _leaf_types(hint):
    args = typing.get_args(hint)
    if not args:
        yield hint
    for a in args:
        yield from _leaf_types(a)


@pytest.mark.parametrize("cls", [Request, AgentStatus, StatusChanged, RequestUpdated, BotMessage])
def test_fields_are_plain_values(cls):
    for name, hint in typing.get_type_hints(cls).items():
        for t in _leaf_types(hint):
            assert t in _ALLOWED, f"{cls.__name__}.{name} uses {t!r}"


@pytest.mark.parametrize("cls", [Request, AgentStatus, StatusChanged, RequestUpdated, BotMessage])
def test_snapshots_are_immutable(cls):
    assert cls.__dataclass_params__.frozen


def test_event_union_covers_every_event_class():
    assert set(typing.get_args(core.Event)) == {StatusChanged, RequestUpdated, BotMessage}


@pytest.mark.parametrize("event", [
    StatusChanged(STATUS, reason="joined"),
    RequestUpdated(REQ),
    BotMessage("Bsk", "You are in queue position 2", request_id="r1"),
])
def test_events_round_trip_through_json(event):
    data = json.loads(json.dumps(to_dict(event)))
    assert data["type"] == type(event).__name__


def test_request_serialization():
    data = to_dict(REQ)
    assert data["kind"] == "book"
    assert data["state"] == "done"
    assert data["path"] == "/books/Dune.epub"
    assert data["error"] is None


def test_finished_states():
    assert {s for s in RequestState if s.finished} == {
        RequestState.DONE, RequestState.FAILED, RequestState.CANCELLED}


def test_to_dict_rejects_non_core_values():
    with pytest.raises(TypeError):
        to_dict(object())


def test_replace_produces_a_new_snapshot():
    newer = dataclasses.replace(REQ, state=RequestState.FAILED, error="gone")
    assert REQ.state is RequestState.DONE and newer.error == "gone"
