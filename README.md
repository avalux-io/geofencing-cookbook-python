> Geofencing primitives for fleet operations, built and maintained by [Avalux](https://avalux.io). This repo gives logistics engineers a small, dependency-free Python toolkit for the building blocks every fleet platform reinvents: distance math, point-in-polygon tests, circular zones, and an entry/exit tracker that turns raw GPS pings into clean dwell events.

## Why this exists

Most fleet platforms started life with a single TODO labelled "implement geofencing" and grew tendrils around it. By the third quarter the codebase has four copies of `haversine`, two competing zone schemas, and a tracker that emits duplicate enter events whenever the GPS jitters across a fence line.

We kept running into the same situation when helping clients integrate telematics data. Either they had no in-house geofencing logic and were paying a SaaS vendor 40 dollars per truck per month for a feature that fits in 200 lines, or they had four versions of it and nobody knew which one the dispatcher dashboard actually called.

This cookbook is the version we ship. It is small on purpose. It runs without numpy, shapely, geopandas, or postgis, so you can drop it into a Lambda, a Cloud Run service, a Celery worker, or a Jupyter notebook without dragging a binary wheel along. If you need PostGIS-grade geometry later, swap the implementations behind the same interface and your callers will not notice.

## Who this is for

This is for engineers who:

- own a fleet of trucks, vans, last-mile couriers, service vehicles, or trailers and want to compute "is vehicle X inside zone Y right now" without buying a platform
- already pull GPS pings from Samsara, Geotab, Verizon Connect, Motive, or a Ruptela or Teltonika device and need to turn those pings into business events
- want to log time-on-site for billing, detect unauthorized excursions, trigger arrival webhooks, or compute service-area coverage stats
- need a baseline geofencing implementation to benchmark against a vendor or to plug into a custom dispatch app

It is not for hobbyists building a hiking app, autonomous-vehicle teams who need centimeter accuracy, or anyone who needs to handle polygons that span hemispheres. Fleet zones are usually under fifty kilometres across, and the ray-casting and haversine math here is tuned for that scale.

## What is in the box

```
geofencing-cookbook-python/
├── README.md
├── LICENSE
├── .env.example
├── .gitignore
└── src/
    └── geofencing/
        └── zones.py        # Point, haversine_m, CircleZone, PolygonZone, ZoneTracker, dwell_time_s
```

The whole library is one file. That is not a mistake, it is a feature. You can read it end to end in fifteen minutes, fork it into your repo, and own it. There is no plugin system, no abstract base class hierarchy, and no `GeofenceProvider` factory.

## Quick start

```bash
git clone https://github.com/avalux-io/geofencing-cookbook-python.git
cd geofencing-cookbook-python
python3 -m venv .venv && source .venv/bin/activate
python3 src/geofencing/zones.py
```

That last command runs the demo at the bottom of `zones.py`, which seeds a yard zone, a customer polygon, and a fake truck whose pings march through both. You should see `enter` and `exit` events printed for each transition.

To use the library in your own project, copy `src/geofencing/zones.py` into your codebase. There is no PyPI release on purpose: this is a cookbook, not a framework.

## How the geofencing math works

### Haversine distance

The `haversine_m` function returns the great-circle distance between two points on a sphere of radius 6,371,000 metres. That is the mean Earth radius in WGS84. The error versus a true geodesic on the WGS84 ellipsoid is under 0.3 percent at any latitude, which is well under the noise floor of consumer GPS. For fleet use cases where you are testing "is this truck within 200 metres of the yard gate," haversine is the right tool.

```python
from src.geofencing.zones import Point, haversine_m

depot = Point(40.7128, -74.0060)
truck = Point(40.7200, -74.0010)
print(haversine_m(depot, truck))  # ~868
```

If you need higher accuracy for survey-grade work, use `pyproj` and call `Geod(ellps='WGS84').inv(...)` instead. For dispatch radius checks, haversine is plenty.

### Circular geofences

`CircleZone` takes a centre point and a radius in metres. Containment is a single haversine call against the radius. This is the right primitive for:

- depot yards, where the gate coordinates are known and trucks should register arrival within roughly 150 metres
- customer sites where you do not have a polygon, only a pin
- speed traps, school zones, no-idle areas around residential blocks

```python
yard = CircleZone("yard-01", Point(40.7128, -74.0060), radius_m=150)
yard.contains(Point(40.7129, -74.0061))  # True
```

### Polygon geofences

`PolygonZone` takes an ordered tuple of vertices and uses ray-casting to decide containment. The algorithm is the standard "cast a horizontal ray and count crossings" approach. Edge cases like a point exactly on the boundary return `True`, which is what you want for billing purposes: a truck parked on the line is on the site.

Vertices should be in either clockwise or counter-clockwise order, with the last vertex connecting back to the first implicitly. Self-intersecting polygons will give nonsense results, so if your dispatcher draws a figure-eight, sanitize it first.

```python
loading_dock = PolygonZone(
    "dock-7",
    vertices=(
        Point(40.7580, -73.9855),
        Point(40.7582, -73.9810),
        Point(40.7540, -73.9810),
        Point(40.7540, -73.9855),
    ),
)
```

For zones that span more than about fifty kilometres, the lat/lon plane stops being a good approximation and you should project to UTM or web mercator first. We deliberately did not add reprojection because fleet zones rarely need it, and adding it would mean depending on `pyproj`.

### Zone tracker

`ZoneTracker` is the part most teams write four times. It holds the set of zones each vehicle is currently inside, and on each new ping it diffs the old set against the new set to emit `enter` and `exit` events. That diffing logic is the whole reason this repo exists. If you write it naively, you get duplicate enter events every time a noisy GPS reading flickers across a boundary.

```python
tracker = ZoneTracker(zones=(yard, loading_dock))
for ping in stream_of_gps_pings:
    for event in tracker.track(ping.vehicle_id, ping.point, ping.timestamp):
        publish_to_kafka(event)
```

The tracker holds state in memory. For production you will want to persist `_state` to Redis or a database keyed by vehicle, so a worker restart does not cause every truck to fire `enter` events the next time they ping. That persistence layer is yours to design, and we do not bake an opinion in here because every team has a different stack.

### Dwell time

`dwell_time_s` walks an event log for one vehicle and one zone, pairs `enter` with the next `exit`, and returns total seconds spent inside. This is the function that bills "time on site" or computes detention. Open intervals (an `enter` with no matching `exit` yet) are skipped, so make sure you query after the visit is complete or you will undercount.

## Real-world fleet examples

A few patterns we have shipped using this exact code:

**Detention billing.** A regional carrier was eating two hours of unpaid detention per drop because their shipper required 30-minute free time and the carrier had no defensible record of arrival and departure. Wrapping `ZoneTracker` around their Samsara webhook stream and persisting `ZoneEvent` rows to Postgres gave them a billable audit trail. Detention recovery went from zero to about 14,000 dollars per month off one shipper.

**Service-area coverage maps.** A field-service company wanted to know which postcodes their techs actually visit, versus the marketing pages claiming they cover the whole county. Polygon zones per postcode, fed by raw GPS history, produced a heatmap that retired three "we serve this area" claims from their site within a week.

**Geofence-triggered dispatch.** When a tow truck enters a 500-metre radius around a stranded vehicle, the dispatcher app marks the job in-progress automatically. No more "did you arrive yet" texts.

**Yard exit alarms.** Trailers leaving the yard outside business hours emit a Slack alert. The whole thing is a `CircleZone` and a webhook, but the customer had been quoted 18,000 dollars by a vendor for the same feature.

## Integration notes

Most telematics providers expose either a webhook (push) or a REST endpoint (pull) for GPS data. The shape of a ping is essentially `(vehicle_id, lat, lon, timestamp)` regardless of vendor. Mapping examples:

- **Samsara** `/fleet/vehicles/stats/feed` returns `gps` objects with `latitude` and `longitude` fields, paginated.
- **Geotab** uses MyGeotab `LogRecord` entities with `latitude`, `longitude`, and `dateTime`.
- **Motive** `/v3/vehicle_locations` returns `current_location.lat` and `current_location.lon`.
- **Verizon Connect** XML feeds expose `Latitude` and `Longitude` per vehicle event.

Build one adapter per vendor that normalizes their payload into our `Point` and pushes it through `ZoneTracker.track()`. The `.env.example` file in this repo lists the variables you typically need.

## Why we built this

Avalux helps small and mid-size businesses turn operational data into automations that pay for themselves. Fleet ops is a recurring theme because the data is messy, the vendors are expensive, and the wins are easy to measure in fuel, hours, and detention dollars.

We publish these cookbooks so that engineers evaluating us, or building in-house, do not have to start from a blank file. If you want help wiring this into a real backend, integrating with your telematics provider, or building the dispatcher dashboard that sits on top, reach us at [avalux.io](https://avalux.io).

## Open source is not a finished product

This repo is scaffolding. It is the layer we would always write before building anything fleet-shaped. To run it in production you still need:

- a telematics adapter for your provider
- a database to persist `_state` and `ZoneEvent` history
- a worker process to consume the live feed, probably on Cloud Run, Lambda, or a small Kubernetes deployment
- alerting, retry logic, GPS noise filtering, and idle detection
- a dashboard your dispatchers will actually use

The math here is correct. The plumbing is not included. That is the difference between an open-source primitive and a deployed system.

## Avalux's other open source projects

We maintain a small portfolio of focused tooling for SMB ops:

- [freight-eta-toolkit](https://github.com/avalux-io/freight-eta-toolkit): ETA calculation utilities for freight dispatch
- [avalux-open-source](https://github.com/avalux-io/avalux-open-source): index of every public Avalux repo

## License

MIT, see `LICENSE`. Copy, fork, ship. Attribution to Avalux is appreciated but not required.

## Keywords

geofencing python, fleet geofence library, haversine geofence, point in polygon python, fleet management geofencing, gps zone tracker, fleet telematics python, samsara geofence integration, geotab geofence, motive geofence, polygon geofence python, circular geofence, dwell time calculation, geofence entry exit events, fleet operations open source, logistics python library, gps tracking python, fleet engineer toolkit
