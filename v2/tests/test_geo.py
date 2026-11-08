from lead_engine import geo

LAT, LON = 25.9300, -80.1210

# ~50 m × 40 m rectangle around the point → ~2,000 m²
RECT = [(25.92980, -80.12125), (25.92980, -80.12075), (25.93016, -80.12075), (25.93016, -80.12125), (25.92980, -80.12125)]


def _way(i, ring, tags):
    return {"type": "way", "id": i, "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in ring]}


ELEMENTS = [
    _way(1, RECT, {"building": "apartments", "building:levels": "32", "roof:material": "concrete", "roof:shape": "flat"}),
    _way(2, [(25.9295, -80.1214), (25.9295, -80.1212), (25.9296, -80.1212), (25.9296, -80.1214), (25.9295, -80.1214)],
         {"building": "parking", "parking": "multi-storey"}),
    {"type": "node", "id": 3, "lat": 25.9301, "lon": -80.1205, "tags": {"leisure": "swimming_pool"}},
    {"type": "way", "id": 4, "center": {"lat": 25.9304, "lon": -80.1213}, "tags": {"leisure": "pitch", "sport": "tennis"}},
    {"type": "node", "id": 5, "lat": 25.9297, "lon": -80.1220, "tags": {"barrier": "lift_gate"}},
    _way(6, [(25.9300, -80.1195), (25.9310, -80.1194)], {"natural": "coastline"}),
    {"type": "node", "id": 7, "lat": 25.9500, "lon": -80.1300, "tags": {"leisure": "swimming_pool"}},   # too far
]


def test_area_and_point_in_polygon():
    area = geo.polygon_area_m2(RECT)
    assert 1900 < area < 2100
    assert geo.point_in_polygon(LAT, LON, RECT)
    assert not geo.point_in_polygon(25.94, LON, RECT)


def test_profile_from_overpass():
    p = geo.profile_from_overpass(ELEMENTS, LAT, LON)
    assert p["levels"] == 32 and p["roof"] == "concrete flat" and p["building_type"] == "apartments"
    assert 1900 < p["footprint_m2"] < 2100 and p["complex_footprint_m2"] > p["footprint_m2"]
    assert set(p["amenities"]) == {"parking_garage", "pool_deck", "tennis_court", "gatehouse"}
    assert p["coastal"] is True and p["coast_distance_m"] < 200


def test_levels_from_height_and_text_amenities_and_merge():
    assert geo._levels({"height": "64"}) == 20
    am = geo.amenities_from_text("Resort-style pool, covered parking garage, pickleball courts and a 24-hour guard gate.")
    assert set(am) == {"pool_deck", "parking_garage", "tennis_court", "gatehouse"}
    merged = geo.merge_profile({}, "Oceanfront living with a heated pool", "100 Collins Ave, Miami Beach, FL 33139", 164)
    assert merged["coastal"] is True and merged["coastal_source"] == "heuristic"
    assert merged["units"] == 164 and merged["large"] is True and "pool_deck" in merged["amenities"]
    small = geo.merge_profile({"levels": 3, "footprint_m2": 500}, "", "1 Main St, Doral, FL 33178", 12)
    assert small["large"] is False and "coastal" not in small


def test_fetch_profile_is_safe_without_coordinates():
    assert geo.fetch_profile(None, None) == {}
