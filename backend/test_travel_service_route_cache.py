import os
import sys

sys.path.append(os.path.dirname(__file__))

import pytest

import database
import travel_service
from travel_service import TravelService


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "features": [
                {
                    "properties": {
                        "summary": {
                            "duration": 600,
                        }
                    }
                }
            ]
        }


def test_route_minutes_uses_persistent_cache_before_ors(monkeypatch):
    service = TravelService(api_key="test-key")

    monkeypatch.setattr(database, "get_route_cache", lambda *args, **kwargs: 17)

    def fail_post(*_args, **_kwargs):
        raise AssertionError("ORS should not be called when persistent route cache hits")

    monkeypatch.setattr(travel_service.requests, "post", fail_post)

    minutes = service.route_minutes((2.9264123, 101.6412123), (3.15785, 101.71165))

    assert minutes == 17
    stats = service.stats_snapshot()
    assert stats["route_api_calls"] == 0
    assert stats["route_cache_hits"] == 1
    assert stats["route_persistent_cache_hits"] == 1
    assert stats["route_cache_misses"] == 0


def test_route_minutes_saves_persistent_cache_after_ors_miss(monkeypatch):
    service = TravelService(api_key="test-key")
    saved_payloads = []

    monkeypatch.setattr(database, "get_route_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(database, "save_route_cache", lambda *args, **kwargs: saved_payloads.append((args, kwargs)) or True)
    monkeypatch.setattr(travel_service.requests, "post", lambda *args, **kwargs: FakeResponse())

    minutes = service.route_minutes((2.9264123, 101.6412123), (3.15785, 101.71165))

    assert minutes == 10
    stats = service.stats_snapshot()
    assert stats["route_api_calls"] == 1
    assert stats["route_cache_misses"] == 1
    assert saved_payloads
    args, kwargs = saved_payloads[0]
    assert args[4] == 10
    assert kwargs["ttl_days"] == service.route_cache_ttl_days
