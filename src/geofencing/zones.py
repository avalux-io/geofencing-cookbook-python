"""Geofencing primitives for fleet operations.

Single-file library. Copy into your codebase and own it.
No numpy, shapely, geopandas, or postgis required.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Union

EARTH_RADIUS_M = 6_371_000


@dataclass(frozen=True)
class Point:
    """A latitude/longitude coordinate in WGS84 decimal degrees."""
    lat: float
    lon: float


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance between two points, in meters."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial bearing from a to b in degrees (0=N, 90=E, 180=S, 270=W)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


@dataclass(frozen=True)
class CircleZone:
    """A circular geofence defined by center and radius in meters."""
    zone_id: str
    center: Point
    radius_m: float

    def contains(self, p: Point) -> bool:
        return haversine_m(self.center, p) <= self.radius_m


@dataclass(frozen=True)
class PolygonZone:
    """A polygonal geofence. Vertices in order, last connects to first.

    Ray-casting containment. Good for zones smaller than ~50 km across.
    For larger areas, project to UTM or web mercator first.
    """
    zone_id: str
    vertices: tuple[Point, ...]

    def contains(self, p: Point) -> bool:
        inside = False
        n = len(self.vertices)
        for i in range(n):
            a = self.vertices[i]
            b = self.vertices[(i + 1) % n]
            if (a.lon > p.lon) != (b.lon > p.lon):
                cross = (p.lon - a.lon) * (b.lat - a.lat) - (b.lon - a.lon) * (p.lat - a.lat)
                if cross == 0:
                    return True
                if (cross < 0) != (b.lon < a.lon):
                    inside = not inside
        return inside


Zone = Union[CircleZone, PolygonZone]


@dataclass(frozen=True)
class ZoneEvent:
    """An entry or exit event for a vehicle and zone."""
    vehicle_id: str
    zone_id: str
    kind: str  # "enter" or "exit"
    at: datetime
    point: Point


@dataclass
class ZoneTracker:
    """Tracks which zones each vehicle currently sits inside.

    Feed location pings with track(); receive zone enter/exit events back.
    State is held in memory. Persist it yourself if you need durability.
    """
    zones: tuple[Zone, ...]
    _state: dict[str, set[str]] = field(default_factory=dict)

    def track(self, vehicle_id: str, point: Point, at: datetime | None = None) -> list[ZoneEvent]:
        at = at or datetime.now(timezone.utc)
        current = {z.zone_id for z in self.zones if z.contains(point)}
        prior = self._state.get(vehicle_id, set())
        events: list[ZoneEvent] = []
        for entered in current - prior:
            events.append(ZoneEvent(vehicle_id, entered, "enter", at, point))
        for exited in prior - current:
            events.append(ZoneEvent(vehicle_id, exited, "exit", at, point))
        self._state[vehicle_id] = current
        return events

    def snapshot(self) -> dict[str, set[str]]:
        """Return a deep copy of current state for persistence."""
        return {v: set(zs) for v, zs in self._state.items()}

    def restore(self, state: dict[str, set[str]]) -> None:
        """Replace internal state, typically after a worker restart."""
        self._state = {v: set(zs) for v, zs in state.items()}


def dwell_time_s(events: Iterable[ZoneEvent], vehicle_id: str, zone_id: str) -> float:
    """Sum of seconds the vehicle spent inside the zone, from an event log.

    Pairs each enter with the next exit. Open intervals are skipped.
    """
    relevant = sorted(
        (e for e in events if e.vehicle_id == vehicle_id and e.zone_id == zone_id),
        key=lambda e: e.at,
    )
    total = 0.0
    open_enter: datetime | None = None
    for e in relevant:
        if e.kind == "enter" and open_enter is None:
            open_enter = e.at
        elif e.kind == "exit" and open_enter is not None:
            total += (e.at - open_enter).total_seconds()
            open_enter = None
    return total


if __name__ == "__main__":
    yard = CircleZone("yard-01", Point(40.7128, -74.0060), radius_m=150)
    customer = PolygonZone(
        "customer-acme",
        vertices=(
            Point(40.7580, -73.9855),
            Point(40.7582, -73.9810),
            Point(40.7540, -73.9810),
            Point(40.7540, -73.9855),
        ),
    )
    tracker = ZoneTracker(zones=(yard, customer))

    pings = [
        Point(40.7128, -74.0060),  # at the yard
        Point(40.7200, -74.0000),  # rolling out
        Point(40.7560, -73.9830),  # arrived at customer
        Point(40.7560, -73.9830),  # still at customer, no duplicate event
        Point(40.8000, -73.9000),  # left customer, no active zone
    ]
    for ping in pings:
        for ev in tracker.track("truck-17", ping):
            print(ev)
