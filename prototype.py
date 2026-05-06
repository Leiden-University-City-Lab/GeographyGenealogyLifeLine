"""
Life Paths – University History Visualization
================================================
Improved version of the prototype with:

Fixes:
  - MonetDB connection wrapped correctly for pandas
  - Floor map is optional (graceful fallback)
  - data refresh button (clears lru_cache)

New analysis tools:
  - Co-presence tab: who was in the same city at the same time?
  - Network graph tab: family/relational ties between professors
  - Faculty lens: filter and colour-code by faculty
  - Career flow: Sankey diagram of city-to-city moves

Usage:
    python app.py

Dependencies:
    pip install dash dash-bootstrap-components plotly pandas pymonetdb pillow networkx
"""

import os
from functools import lru_cache

import dash
from dash import Dash, dcc, html, Input, Output, State, dash_table, ctx, Patch
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import numpy as np

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pymonetdb
    HAS_MONETDB = True
except ImportError:
    HAS_MONETDB = False

try:
    import networkx as nx
    HAS_NX = True
except ImportError:
    HAS_NX = False

try:
    from edge_bundling import bundle_event_df
    HAS_BUNDLING = True
except ImportError:
    HAS_BUNDLING = False
    bundle_event_df = None


# ─── Config ───────────────────────────────────────────────────────────────────

DB_HOST     = os.getenv("MONETDB_HOST",     "localhost")
DB_PORT     = int(os.getenv("MONETDB_PORT", "50000"))
DB_NAME     = os.getenv("MONETDB_DATABASE", "peopledb")
DB_USER     = os.getenv("MONETDB_USER",     "monetdb")
DB_PASSWORD = os.getenv("MONETDB_PASSWORD", "monetdb")

FLOOR_MAP_PATH = "floor_map.png"

EVENT_ORDER = {"birth": 0, "education": 1, "career": 2, "death": 3}
EVENT_COLORS = {
    "birth":     "#2ca02c",
    "education": "#1f77b4",
    "career":    "#ff7f0e",
    "death":     "#d62728",
}

FACULTY_PALETTE = [
    "#e63946", "#457b9d", "#2a9d8f", "#e9c46a",
    "#f4a261", "#264653", "#a8dadc", "#6d6875",
]

# Distinct colours for individually selected people (never gold — that is reserved
# for shared-location segments).
PERSON_PALETTE = [
    "#e63946",  # red
    "#457b9d",  # steel blue
    "#2a9d8f",  # teal
    "#9b5de5",  # purple
    "#f77f00",  # orange
    "#06d6a0",  # mint
    "#118ab2",  # ocean blue
    "#ef476f",  # pink
    "#073b4c",  # dark navy
    "#b5838d",  # mauve
]
GOLD = "#f1c40f"

# Dutch names that all mean "the Netherlands" — cities with these countries are NL.
# Also includes province names that occasionally appear as the "country" half of
# cache entries (e.g. "SomeVillage, Gelderland") and must not become cluster nodes.
NL_COUNTRY_DUTCH = {
    "nederland", "netherlands", "nl", "holland", "the netherlands",
    # Dutch provinces
    "gelderland", "zeeland", "friesland", "groningen", "drenthe",
    "overijssel", "flevoland", "utrecht", "noord-holland", "zuid-holland",
    "noord-brabant", "limburg",
}

# Netherlands lat/lon bounding box — used only as a LAST RESORT when the
# geocache has no entry at all for a location.
_NL_LAT = (50.75, 53.55)
_NL_LON = (3.36, 7.23)

# ── Build (rounded_lat, rounded_lon) → country from geocode_cache.json ────────
# Strings that appear as the "country" half of "City, Country" geocache keys but
# are actually city names (bad entries like "Genève, Parijs").
_GEOCACHE_CITY_BLACKLIST = {
    "parijs", "genève", "cambridge", "westminster", "aken", "algiers",
    "algier", "londen",
}

_COUNTRY_LOOKUP: dict = {}
# city name (lowercase) → country, built from "City, Country" cache keys
_CITY_COUNTRY_LOOKUP: dict = {}
try:
    import json as _json
    with open("geocode_cache.json") as _f:
        _gc = _json.load(_f)

    for _k, _v in _gc.items():
        if not _v or "," not in _k:
            continue
        _lat, _lon = _v.get("lat"), _v.get("lon")
        if _lat is None or _lon is None:
            continue
        _country = _k.rsplit(",", 1)[1].strip()
        if _country.lower() in _GEOCACHE_CITY_BLACKLIST:
            continue
        # Skip NL province names appearing as country so they don't pollute lookup
        if _country.lower() not in NL_COUNTRY_DUTCH:
            for _r in [4, 3, 2]:
                _COUNTRY_LOOKUP.setdefault((round(_lat, _r), round(_lon, _r)), _country)
        # City-name → country lookup (first-seen wins per city name)
        _city_part = _k.split(",", 1)[0].strip().lower()
        if _city_part and _country.lower() not in NL_COUNTRY_DUTCH:
            _CITY_COUNTRY_LOOKUP.setdefault(_city_part, _country)
except Exception:
    pass


def _get_country(lat: float, lon: float) -> str | None:
    """Return country name for lat/lon, or None if unknown.

    Cache lookup runs first (precise); NL bounding box is last resort only,
    so Belgian border cities like Antwerpen are correctly identified via cache.
    """
    for r in [4, 3, 2, 1]:
        c = _COUNTRY_LOOKUP.get((round(float(lat), r), round(float(lon), r)))
        if c:
            # Normalise NL variants to "Nederland"
            return "Nederland" if c.lower() in NL_COUNTRY_DUTCH else c
    # Last resort: NL bounding box for cities with no cache entry
    if _NL_LAT[0] <= lat <= _NL_LAT[1] and _NL_LON[0] <= lon <= _NL_LON[1]:
        return "Nederland"
    return None


def _cluster_label(city: str, country, expanded_countries: set) -> str:
    """Return the Sankey node label.

    • country is None/NaN or an NL variant → show city name (individual NL node)
    • country is in expanded_countries      → show city name (user expanded it)
    • otherwise                             → show country name (cluster node)
    """
    city_s = (city or "").strip()
    # pandas stores Python None as float NaN in object columns
    if not country or not isinstance(country, str):
        return city_s or "Unknown"
    if country.lower() in NL_COUNTRY_DUTCH:
        return city_s or "Unknown"
    if country in expanded_countries:
        return city_s or country
    return country


def person_color_map(selected_ids: list) -> dict:
    """Return {person_id: hex_color} for each selected id."""
    return {pid: PERSON_PALETTE[i % len(PERSON_PALETTE)]
            for i, pid in enumerate(selected_ids)}


def draw_person_path_map(fig, pdf, pid, pname, person_color, shared_locs,
                         direction_mode="off"):
    """
    Draw a single person's path on a Scattermap.
    Segments touching a shared location are drawn in GOLD;
    all other segments use person_color.
    """
    from itertools import groupby as _groupby
    pdf = pdf.sort_values(["event_date", "event_order"]).reset_index(drop=True)
    if len(pdf) < 2:
        return

    segs = []
    for i in range(len(pdf) - 1):
        p0 = (float(pdf.at[i,   "latitude"]), float(pdf.at[i,   "longitude"]))
        p1 = (float(pdf.at[i+1, "latitude"]), float(pdf.at[i+1, "longitude"]))
        r0 = (round(p0[0], 5), round(p0[1], 5))
        r1 = (round(p1[0], 5), round(p1[1], 5))
        # Gold only when BOTH endpoints are shared locations
        c  = GOLD if (r0 in shared_locs and r1 in shared_locs) else person_color
        segs.append((p0, p1, c))

    for color, grp in _groupby(segs, key=lambda s: s[2]):
        grp   = list(grp)
        lats  = [pt for s in grp for pt in (s[0][0], s[1][0], None)]
        lons  = [pt for s in grp for pt in (s[0][1], s[1][1], None)]
        tip   = f"<b>{pname}</b>" + (" · shared location" if color == GOLD else "")
        fig.add_trace(go.Scattermap(
            lat=lats, lon=lons,
            mode="lines",
            line={"width": 5 if color == GOLD else 4, "color": color},
            opacity=1.0, showlegend=False,
            customdata=[[int(pid), pname, "path"]] * len(lats),
            hovertemplate=tip + "<extra></extra>",
        ))

    if direction_mode in {"selected", "all"}:
        _add_direction(fig, pdf)


def draw_person_path_3d(fig, pdf, pid, pname, person_color, shared_locs):
    """
    Draw a single person's path in the space-time cube (Scatter3d).
    Segments touching a shared location are drawn in GOLD.
    """
    from itertools import groupby as _groupby
    pdf = pdf.sort_values(["event_date", "event_order"]).reset_index(drop=True)
    if len(pdf) < 2:
        return

    segs = []
    for i in range(len(pdf) - 1):
        loc0 = (float(pdf.at[i,   "latitude"]), float(pdf.at[i,   "longitude"]))
        loc1 = (float(pdf.at[i+1, "latitude"]), float(pdf.at[i+1, "longitude"]))
        r0   = (round(loc0[0], 5), round(loc0[1], 5))
        r1   = (round(loc1[0], 5), round(loc1[1], 5))
        # Gold only when BOTH endpoints are shared locations
        c    = GOLD if (r0 in shared_locs and r1 in shared_locs) else person_color
        segs.append((pdf.at[i,   "longitude"], pdf.at[i,   "latitude"], pdf.at[i,   "year"],
                     pdf.at[i+1, "longitude"], pdf.at[i+1, "latitude"], pdf.at[i+1, "year"],
                     c))

    for color, grp in _groupby(segs, key=lambda s: s[6]):
        grp  = list(grp)
        xs   = [v for s in grp for v in (s[0], s[3], None)]
        ys   = [v for s in grp for v in (s[1], s[4], None)]
        zs   = [v for s in grp for v in (s[2], s[5], None)]
        fig.add_trace(go.Scatter3d(
            x=xs, y=ys, z=zs,
            mode="lines",
            line={"width": 7 if color == GOLD else 5, "color": color},
            opacity=1.0,
            hoverinfo="skip",
            showlegend=False,
        ))


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_connection():
    if not HAS_MONETDB:
        raise RuntimeError("pymonetdb not installed")
    return pymonetdb.connect(
        hostname=DB_HOST, port=DB_PORT, database=DB_NAME,
        username=DB_USER, password=DB_PASSWORD,
    )


def read_sql(query: str) -> pd.DataFrame:
    """Execute query and return DataFrame. Works with MonetDB cursors."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        cols = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=cols)
    finally:
        conn.close()


# ─── Queries ──────────────────────────────────────────────────────────────────

EVENTS_QUERY = """
SELECT
    p.person_id,
    TRIM(
        COALESCE(p.first_name, '') || ' ' ||
        COALESCE(p.affix || ' ', '') ||
        COALESCE(p.last_name, '')
    ) AS person_name,
    p.faculty,
    et.event_type_name,
    e.begin_date,
    e.end_date,
    e.description,
    l.location_id,
    l.country,
    l.city,
    l.latitude,
    l.longitude
FROM event e
JOIN person p ON p.person_id = e.person_id
JOIN event_type et ON et.event_type_id = e.event_type_id
LEFT JOIN location l ON l.location_id = e.location_id
WHERE et.event_type_name IN ('birth', 'death', 'education', 'career')
  AND l.latitude IS NOT NULL
  AND l.longitude IS NOT NULL
ORDER BY p.person_id, e.begin_date, e.end_date
"""

RELATIONS_QUERY = """
SELECT
    r.person_id_1,
    r.person_id_2,
    rt.relation_type_name AS relation_type,
    TRIM(
        COALESCE(p1.first_name, '') || ' ' ||
        COALESCE(p1.affix || ' ', '') ||
        COALESCE(p1.last_name, '')
    ) AS name_1,
    TRIM(
        COALESCE(p2.first_name, '') || ' ' ||
        COALESCE(p2.affix || ' ', '') ||
        COALESCE(p2.last_name, '')
    ) AS name_2
FROM relation r
JOIN relation_type rt ON rt.relation_type_id = r.relation_type_id
JOIN person p1 ON p1.person_id = r.person_id_1
JOIN person p2 ON p2.person_id = r.person_id_2
"""


# ─── Data loading ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_events() -> pd.DataFrame:
    df = read_sql(EVENTS_QUERY)
    if df.empty:
        return df

    df["begin_date"] = pd.to_datetime(df["begin_date"], errors="coerce")
    df["end_date"]   = pd.to_datetime(df["end_date"],   errors="coerce")
    df["event_date"] = df["begin_date"].fillna(df["end_date"])
    df = df.dropna(subset=["event_date", "latitude", "longitude"]).copy()

    df["year"]        = df["event_date"].dt.year.astype(int)
    df["event_order"] = df["event_type_name"].map(EVENT_ORDER).fillna(99).astype(int)
    df["city_label"]  = df["city"].fillna("")
    df["country_label"] = df["country"].fillna("")

    df["location_label"] = df["city_label"]
    mask = df["country_label"].ne("") & df["location_label"].ne("")
    df.loc[mask, "location_label"] = df["city_label"] + ", " + df["country_label"]
    df.loc[df["location_label"].eq(""), "location_label"] = df["country_label"]
    df.loc[df["location_label"].eq(""), "location_label"] = "Unknown"

    df["hover_text"] = (
        "<b>" + df["person_name"] + "</b><br>"
        + df["event_type_name"].str.title() + "<br>"
        + df["location_label"] + "<br>"
        + df["event_date"].dt.strftime("%Y-%m-%d")
        + df["description"].fillna("").map(lambda x: f"<br>{x}" if x else "")
    )

    df = df.sort_values(
        ["person_id", "event_date", "event_order", "location_id"]
    ).reset_index(drop=True)

    return df


@lru_cache(maxsize=1)
def load_relations() -> pd.DataFrame:
    try:
        return read_sql(RELATIONS_QUERY)
    except Exception:
        return pd.DataFrame(columns=["person_id_1", "person_id_2",
                                     "relation_type", "name_1", "name_2"])


def refresh_data():
    """Clear caches so next load re-queries the DB."""
    load_events.cache_clear()
    load_relations.cache_clear()


# ─── Derived globals ──────────────────────────────────────────────────────────

DF        = load_events()
RELATIONS = load_relations()

FACULTIES = sorted(DF["faculty"].dropna().unique().tolist()) if not DF.empty else []
FACULTY_COLOR_MAP = {f: FACULTY_PALETTE[i % len(FACULTY_PALETTE)]
                     for i, f in enumerate(FACULTIES)}

if not DF.empty:
    MIN_YEAR = int(DF["year"].min())
    MAX_YEAR = int(DF["year"].max())
    YEARS    = list(range(MIN_YEAR, MAX_YEAR + 1))
else:
    MIN_YEAR, MAX_YEAR = 1575, 2000
    YEARS = list(range(MIN_YEAR, MAX_YEAR + 1))

DEFAULT_RANGE = [YEARS[0], YEARS[-1]]

if not DF.empty:
    LAT_MIN = int(DF["latitude"].min()  - 2)
    LAT_MAX = int(DF["latitude"].max()  + 2)
    LON_MIN = int(DF["longitude"].min() - 2)
    LON_MAX = int(DF["longitude"].max() + 2)
else:
    LAT_MIN, LAT_MAX = 30, 75
    LON_MIN, LON_MAX = -20, 50
PERSON_OPTIONS = (
    DF[["person_id", "person_name", "faculty"]]
    .drop_duplicates("person_id")
    .sort_values("person_name")
    .to_dict("records")
    if not DF.empty else []
)


# ─── Figure helpers ───────────────────────────────────────────────────────────

def empty_fig(msg="No data", h=400):
    fig = go.Figure()
    fig.add_annotation(text=msg, xref="paper", yref="paper",
                       x=0.5, y=0.5, showarrow=False,
                       font={"size": 16, "color": "#888"})
    fig.update_layout(template="plotly_white",
                      margin={"l": 10, "r": 10, "t": 10, "b": 10},
                      height=h)
    return fig


def build_marks(years):
    if len(years) <= 12:
        return {int(y): str(int(y)) for y in years}
    step = max(1, len(years) // 10)
    sampled = years[::step]
    if years[-1] not in sampled:
        sampled = list(sampled) + [years[-1]]
    return {int(y): str(int(y)) for y in sampled}


def filter_df(df, year_range, faculties=None, people=None):
    s, e = sorted(year_range)
    out = df[(df["year"] >= s) & (df["year"] <= e)]
    if faculties:
        out = out[out["faculty"].isin(faculties)]
    if people:
        out = out[out["person_id"].isin(people)]
    return out.copy()


def person_color(person_id, selected_ids, faculty_map, use_faculty=False):
    if selected_ids and person_id in selected_ids:
        return "#c0392b"
    if use_faculty:
        return faculty_map.get(person_id, "#7f8c8d")
    return "#7f8c8d"


# ─── Map building ─────────────────────────────────────────────────────────────

def build_map(df, year_range, selected_people, display_mode,
              direction_mode, bg_opacity, use_faculty_colors,
              use_bundling=False):
    if df.empty:
        return empty_fig("No geocoded events in this range.", h=700)

    selected_people = selected_people or []
    _, end_year = sorted(year_range)

    path_df     = DF[DF["year"] <= end_year].copy()
    interval_df = filter_df(DF, year_range)

    # Apply faculty filter from df
    if len(df) < len(DF):
        fids = df["person_id"].unique()
        path_df     = path_df[path_df["person_id"].isin(fids)]
        interval_df = interval_df[interval_df["person_id"].isin(fids)]

    # Build faculty id→color map
    fac_color_map = {}
    if use_faculty_colors and not DF.empty:
        for _, row in DF[["person_id", "faculty"]].drop_duplicates().iterrows():
            fac_color_map[int(row["person_id"])] = \
                FACULTY_COLOR_MAP.get(row["faculty"], "#7f8c8d")

    sel_color_map  = person_color_map(selected_people)  # {pid: unique_color}
    fig = go.Figure()

    # Legend entry per selected person (invisible trace, just for the colour key)
    if selected_people:
        for _pid in selected_people:
            _rows = path_df[path_df["person_id"] == _pid]
            if _rows.empty:
                continue
            _pname = _rows["person_name"].iloc[0]
            _color = sel_color_map.get(int(_pid), "#7f8c8d")
            fig.add_trace(go.Scattermap(
                lat=[None], lon=[None], mode="lines",
                line={"width": 4, "color": _color},
                name=_pname, showlegend=True,
            ))

    # ── Edge bundling for background paths ────────────────────────────────────
    # Bundle only unselected paths to reduce visual clutter.
    # Selected people always keep exact coordinates for accuracy.
    if use_bundling and HAS_BUNDLING and not path_df.empty:
        unsel_ids = path_df[
            ~path_df["person_id"].isin(selected_people)
        ]["person_id"].unique()
        cap = min(50, len(unsel_ids))

        # Use FULL event history (DF, not path_df) for better bundling —
        # more points = better curvature. Then crop back to visible range.
        full_unsel = DF[DF["person_id"].isin(unsel_ids[:cap])].copy()
        bundled_full = bundle_event_df(full_unsel, max_paths=cap, min_events=3)

        # Crop bundled result back to the visible year range
        bundled_visible = bundled_full[bundled_full["year"] <= end_year]

        path_df = pd.concat([
            path_df[path_df["person_id"].isin(selected_people)],
            bundled_visible,
            path_df[path_df["person_id"].isin(unsel_ids[cap:])],
        ], ignore_index=True)

    # ── Paths ─────────────────────────────────────────────────────────────────
    _interval_pids  = set(interval_df["person_id"].astype(int).unique())

    if selected_people:
        # Use path_df (all events up to end_year) so we check real temporal overlap —
        # two people are co-present only if they have events at the same (loc, year).
        shared_locs = get_shared_locations(
            path_df[path_df["person_id"].isin(selected_people)]
        )
    else:
        shared_locs = set()

    if display_mode == "all":
        # Selected people: individual traces with gold co-presence highlighting
        for pid, pdf in path_df[path_df["person_id"].isin(selected_people)].groupby("person_id"):
            pdf = pdf.sort_values(["event_date", "event_order"]).reset_index(drop=True)
            pname = pdf["person_name"].iloc[0]
            pcolor = sel_color_map[int(pid)]
            draw_person_path_map(fig, pdf, pid, pname, pcolor, shared_locs,
                                 direction_mode if direction_mode != "off" else "off")

        # Unselected people: merge into one trace per color instead of one per person.
        # Reduces O(n_people) traces to O(1-8) traces — major speedup in Python + browser.
        color_lats: dict = {}
        color_lons: dict = {}
        color_text: dict = {}
        for pid, pdf in path_df[~path_df["person_id"].isin(selected_people)].groupby("person_id"):
            if not use_bundling and int(pid) not in _interval_pids:
                continue
            pdf   = pdf.sort_values(["event_date", "event_order"]).reset_index(drop=True)
            pname = pdf["person_name"].iloc[0]
            color = fac_color_map.get(int(pid), "#7f8c8d") if use_faculty_colors else "#7f8c8d"
            color_lats.setdefault(color, []).extend(pdf["latitude"].tolist() + [None])
            color_lons.setdefault(color, []).extend(pdf["longitude"].tolist() + [None])
            color_text.setdefault(color, []).extend([pname] * len(pdf) + [None])

        for color, lats in color_lats.items():
            fig.add_trace(go.Scattermap(
                lat=lats, lon=color_lons[color],
                mode="lines",
                line={"width": 1, "color": color},
                opacity=bg_opacity,
                text=color_text[color],
                hovertemplate="%{text}<extra></extra>",
                showlegend=False,
                name="paths",
            ))

    elif selected_people:
        for pid, pdf in path_df[path_df["person_id"].isin(selected_people)].groupby("person_id"):
            pdf    = pdf.sort_values(["event_date", "event_order"]).reset_index(drop=True)
            pname  = pdf["person_name"].iloc[0]
            pcolor = sel_color_map[int(pid)]
            draw_person_path_map(fig, pdf, pid, pname, pcolor, shared_locs,
                                 direction_mode)

    # Event markers
    marker_df = interval_df if not selected_people else                 interval_df[interval_df["person_id"].isin(selected_people)]
    for etype, grp in marker_df.groupby("event_type_name"):
        sizes = [12 if int(p) in selected_people else 7 for p in grp["person_id"]]
        fig.add_trace(go.Scattermap(
            lat=grp["latitude"], lon=grp["longitude"],
            mode="markers",
            marker={"size": sizes,
                    "color": EVENT_COLORS.get(etype, "#636efa"),
                    "opacity": 0.9},
            name=etype.title(),
            customdata=grp[["person_id", "person_name"]].assign(kind="event").values,
            hovertext=grp["hover_text"],
            hovertemplate="%{hovertext}<extra></extra>",
            showlegend=True,
        ))

    # Gold markers at shared locations (vectorised — no apply)
    if shared_locs and selected_people:
        sel_events = path_df[path_df["person_id"].isin(selected_people)].copy()
        sel_events["lat_r"] = sel_events["latitude"].round(5)
        sel_events["lon_r"] = sel_events["longitude"].round(5)
        shared_df   = pd.DataFrame(list(shared_locs), columns=["lat_r", "lon_r"])
        gold_events = sel_events.merge(shared_df, on=["lat_r", "lon_r"])                                 .drop_duplicates(subset=["lat_r", "lon_r"])
        if not gold_events.empty:
            fig.add_trace(go.Scattermap(
                lat=gold_events["latitude"], lon=gold_events["longitude"],
                mode="markers",
                marker={"size": 14, "color": GOLD, "opacity": 1.0,
                        "allowoverlap": True},
                name="Co-present location",
                hovertext=gold_events["location_label"],
                hovertemplate="<b>%{hovertext}</b><br>Co-present location<extra></extra>",
                showlegend=True,
            ))

    # Location cluster markers
    grouped = _aggregate_locations(interval_df, year_range)
    if not grouped.empty:
        fig.add_trace(go.Scattermap(
            lat=grouped["latitude"], lon=grouped["longitude"],
            mode="markers",
            marker={"size": grouped["marker_size"], "color": "#111", "opacity": 0.75},
            text=grouped["hover_text"],
            hovertemplate="%{text}<extra></extra>",
            customdata=grouped[["latitude","longitude","location_label","start_year","end_year"]].values,
            name="Events in interval", showlegend=True,
        ))

    center = {"lat": float(path_df["latitude"].mean()),
              "lon": float(path_df["longitude"].mean())} if not path_df.empty \
             else {"lat": 52, "lon": 10}

    fig.update_layout(
        template="plotly_white",
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        legend={"orientation": "h", "y": 0.01, "x": 0.01,
                "bgcolor": "rgba(255,255,255,0.8)"},
        map={"style": "carto-positron", "center": center, "zoom": 3},
        height=700,
    )
    return fig


def _add_direction(fig, person_df):
    person_df = person_df.sort_values(["event_date", "event_order"]).copy()
    if person_df.empty:
        return
    person_df["seq"] = range(1, len(person_df) + 1)
    fig.add_trace(go.Scattermap(
        lat=person_df["latitude"], lon=person_df["longitude"],
        mode="markers+text",
        marker={"size": 18, "color": "#111", "opacity": 0.9},
        text=person_df["seq"].astype(str),
        textfont={"size": 10, "color": "#fff"},
        textposition="middle center",
        showlegend=False,
        hoverinfo="skip",
    ))


def _aggregate_locations(events_df: pd.DataFrame, year_range) -> pd.DataFrame:
    """Aggregate events by location — vectorised, no Python loops."""
    if events_df.empty:
        return pd.DataFrame()
    s, e = sorted(year_range)

    grp = events_df.groupby(["latitude", "longitude", "location_label"])
    n_people = grp["person_id"].nunique().rename("n_people")
    n_events = grp.size().rename("n_events")

    agg = pd.concat([n_people, n_events], axis=1).reset_index()
    agg["start_year"]  = s
    agg["end_year"]    = e
    agg["marker_size"] = (10
        + 4 * (agg["n_people"] - 1)
        + 2 * (agg["n_events"] - agg["n_people"]).clip(lower=0))

    # Build hover text per row (still a loop but only over unique locations)
    top_visitors = (
        events_df.groupby(["latitude", "longitude", "person_name"])
        .size().reset_index(name="v")
        .sort_values(["latitude","longitude","v"], ascending=[True,True,False])
    )
    visitor_map = top_visitors.groupby(["latitude","longitude"]).apply(
        lambda g: "<br>".join(f"• {r['person_name']} — {r['v']}"
                              for _, r in g.head(8).iterrows())
    ).reset_index(name="visitor_lines")

    agg = agg.merge(visitor_map, on=["latitude","longitude"], how="left")
    agg["hover_text"] = (
        "<b>" + agg["location_label"] + "</b><br>"
        + s.__str__() + "–" + e.__str__() + "<br>"
        + "People: " + agg["n_people"].astype(str) + "<br>"
        + "Events: " + agg["n_events"].astype(str) + "<br><br>"
        + agg["visitor_lines"].fillna("")
    )
    return agg[["latitude","longitude","location_label",
                "start_year","end_year","marker_size","hover_text"]]


# ─── Co-presence analysis ─────────────────────────────────────────────────────

def build_copresence(df, year_range, min_overlap=1):
    """
    Find pairs of people who were in the same city in overlapping years.
    Returns a DataFrame of pairs only (no heatmap).
    """
    if df.empty:
        return pd.DataFrame()

    s, e = sorted(year_range)
    sub = df[(df["year"] >= s) & (df["year"] <= e)].copy()
    if sub.empty:
        return pd.DataFrame()

    person_city = (
        sub.groupby(["person_id", "person_name", "city_label"])["year"]
        .apply(set).reset_index(name="years")
    )
    person_city = person_city[person_city["city_label"].ne("")]

    pairs = []
    records = person_city.to_dict("records")
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            a, b = records[i], records[j]
            if a["person_id"] == b["person_id"] or a["city_label"] != b["city_label"]:
                continue
            overlap = a["years"] & b["years"]
            if len(overlap) >= min_overlap:
                pairs.append({
                    "person_a":      a["person_name"],
                    "person_b":      b["person_name"],
                    "city":          a["city_label"],
                    "overlap_years": len(overlap),
                    "years":         ", ".join(str(y) for y in sorted(overlap)[:5])
                                     + ("…" if len(overlap) > 5 else ""),
                })

    if not pairs:
        return pd.DataFrame()

    return pd.DataFrame(pairs).sort_values("overlap_years", ascending=False)


def get_shared_locations(selected_df):
    """
    Return (latitude, longitude) pairs where ≥2 DIFFERENT selected people
    were present in the SAME YEAR — i.e. genuinely co-present, not just
    visited the same city at different times.

    Uses rounded coordinates (5 decimal places) to avoid float precision
    mismatches when comparing path points to this set.
    """
    if selected_df.empty or selected_df["person_id"].nunique() < 2:
        return set()

    # Round coordinates to avoid float precision drift
    df = selected_df.copy()
    df["lat_r"] = df["latitude"].round(5)
    df["lon_r"] = df["longitude"].round(5)

    # For each (location, year) count distinct people present
    counts = (
        df.groupby(["lat_r", "lon_r", "year"])["person_id"]
        .nunique()
        .reset_index(name="n")
    )
    shared = counts[counts["n"] >= 2][["lat_r", "lon_r"]].drop_duplicates()
    return set(zip(shared["lat_r"], shared["lon_r"]))


# ─── Career flow (Sankey) ─────────────────────────────────────────────────────

def build_career_sankey(df, year_range, faculty_filter=None, top_n=20,
                        expanded_countries=None, trace_node=None,
                        selected_people=None):
    """
    Sankey of all life-event city flows (birth, education, career, death).

    • Non-NL cities are collapsed to their country name.  Click a country node
      to expand it into individual cities; click again to collapse.
    • selected_people: list of person_ids whose flows are drawn in their person
      colour; all other flows are dimmed.
    • trace_node: node label — only flows touching this node are highlighted;
      everything else is dimmed.

    Returns (fig, list_of_country_cluster_labels).
    """
    expanded_countries = set(expanded_countries or [])
    selected_people    = list(map(int, selected_people or []))

    if df.empty:
        return empty_fig("No career data available."), [], None

    s, e = sorted(year_range)
    career = df[
        (df["year"] >= s) & (df["year"] <= e)
    ].copy()

    if faculty_filter:
        career = career[career["faculty"].isin(faculty_filter)]
    if career.empty:
        return empty_fig("No events in this range."), [], None

    career = career.sort_values(["person_id", "event_date", "event_order"])
    # Derive country from lat/lon, with city-name fallback for cache misses
    career["detected_country"] = career.apply(
        lambda r: (
            _get_country(r["latitude"], r["longitude"])
            or _CITY_COUNTRY_LOOKUP.get((r["city_label"] or "").strip().lower())
        ),
        axis=1,
    )
    career["node_label"] = career.apply(
        lambda r: _cluster_label(r["city_label"], r["detected_country"], expanded_countries),
        axis=1,
    )
    # True when the label is the country name (cluster), False when it's the city name
    career["is_clustered"] = career.apply(
        lambda r: (
            isinstance(r["detected_country"], str)
            and r["detected_country"].lower() not in NL_COUNTRY_DUTCH
            and r["detected_country"] not in expanded_countries
        ),
        axis=1,
    )

    # Build per-person flows (retain person info for trace highlighting)
    raw = []
    for pid, pdf in career.groupby("person_id"):
        pname  = pdf["person_name"].iloc[0]
        labels = pdf["node_label"].tolist()
        for i in range(len(labels) - 1):
            src, tgt = labels[i], labels[i + 1]
            if src and tgt and src != tgt:
                raw.append({"from": src, "to": tgt,
                            "person_id": int(pid), "person_name": pname})

    if not raw:
        return empty_fig("No location transitions found."), [], None

    flows_df_all = pd.DataFrame(raw)
    flows_agg_all = flows_df_all.groupby(["from", "to"]).size().reset_index(name="count")

    # Top-N nodes by total flow volume
    city_vol = (
        pd.concat([
            flows_agg_all[["from", "count"]].rename(columns={"from": "city"}),
            flows_agg_all[["to",   "count"]].rename(columns={"to":   "city"}),
        ])
        .groupby("city")["count"].sum()
        .nlargest(top_n)
    )
    top_cities = set(city_vol.index)

    # Always include every location visited by the selected person(s)
    if selected_people:
        sel_rows = flows_df_all[flows_df_all["person_id"].isin(selected_people)]
        top_cities |= set(sel_rows["from"]) | set(sel_rows["to"])

    flows_agg = flows_agg_all[
        flows_agg_all["from"].isin(top_cities) & flows_agg_all["to"].isin(top_cities)
    ]
    flows_df = flows_df_all[
        flows_df_all["from"].isin(top_cities) & flows_df_all["to"].isin(top_cities)
    ]

    if flows_agg.empty:
        return empty_fig(f"No flows between top {top_n} nodes in this range."), [], None

    # Identify country-cluster nodes: a node is a cluster when the label was
    # produced by the country name (is_clustered=True), not the city name.
    # We track this directly during label assignment rather than inferring it
    # post-hoc (which fails when the city_label IS the country name, e.g. "Duitsland").
    all_labels    = set(flows_agg["from"]) | set(flows_agg["to"])
    clustered_set = set(
        career[career["is_clustered"] & career["node_label"].isin(all_labels)]["node_label"]
    )
    country_nodes = sorted(clustered_set)

    # Node ordering: pure sources → both → pure sinks
    src_set    = set(flows_agg["from"])
    tgt_set    = set(flows_agg["to"])
    both_set   = src_set & tgt_set
    node_order = sorted(src_set - both_set) + sorted(both_set) + sorted(tgt_set - src_set)
    node_idx   = {n: i for i, n in enumerate(node_order)}

    # Node colours: Leiden = red, country cluster = grey, NL city = blue
    node_colors = [
        "#c0392b" if "leiden" in n.lower() else
        ("#6c757d" if n in country_nodes else "#457b9d")
        for n in node_order
    ]

    # Node hover text
    def _node_hover(n):
        if n in country_nodes:
            cities = sorted(
                career[career["node_label"] == n]["city_label"].dropna().unique()
            )
            lines  = "<br>".join(f"• {c}" for c in cities[:12])
            extra  = f"<br>… and {len(cities) - 12} more" if len(cities) > 12 else ""
            return f"<b>{n}</b> — cluster ({len(cities)} cities)<br>{lines}{extra}<br><i>Click to expand</i>"
        suffix = "<br><i>Click to collapse country</i>" if n in expanded_countries else ""
        return f"<b>{n}</b>{suffix}"

    node_hover = [_node_hover(n) for n in node_order]

    sel_cmap = person_color_map(selected_people)
    sel_set  = set(selected_people)

    # Per-person flow counts — how many times each person moved A→B
    person_flows = (
        flows_df.groupby(["from", "to", "person_id"])
        .size()
        .reset_index(name="count")
    )

    def _hex_rgba(hex_c: str, alpha: float) -> str:
        h = hex_c.lstrip("#")
        rv, gv, bv = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({rv},{gv},{bv},{alpha})"

    def _active(src, tgt) -> bool:
        return not trace_node or src == trace_node or tgt == trace_node

    link_src, link_tgt, link_val, link_col = [], [], [], []

    if sel_set:
        # Unselected people — grey aggregate per (from, to)
        unsel = person_flows[~person_flows["person_id"].isin(sel_set)]
        if not unsel.empty:
            for (src, tgt), grp in unsel.groupby(["from", "to"]):
                if src not in node_idx or tgt not in node_idx:
                    continue
                link_src.append(node_idx[src])
                link_tgt.append(node_idx[tgt])
                link_val.append(int(grp["count"].sum()))
                link_col.append("rgba(180,180,180,0.15)" if _active(src, tgt)
                                else "rgba(180,180,180,0.04)")

        # Selected people — one proportional band per person per link
        for _, r in person_flows[person_flows["person_id"].isin(sel_set)].iterrows():
            src, tgt = r["from"], r["to"]
            if src not in node_idx or tgt not in node_idx:
                continue
            link_src.append(node_idx[src])
            link_tgt.append(node_idx[tgt])
            link_val.append(int(r["count"]))
            pid = int(r["person_id"])
            alpha = 0.85 if _active(src, tgt) else 0.06
            link_col.append(_hex_rgba(sel_cmap.get(pid, "#e63946"), alpha))
    else:
        # No selection — single aggregated link per pair, uniform blue
        for _, r in flows_agg.iterrows():
            src, tgt = r["from"], r["to"]
            alpha = 0.28 if _active(src, tgt) else 0.06
            link_src.append(node_idx[src])
            link_tgt.append(node_idx[tgt])
            link_val.append(int(r["count"]))
            link_col.append(f"rgba(69,123,157,{alpha})")

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            label=node_order,
            color=node_colors,
            customdata=node_hover,
            hovertemplate="%{customdata}<extra></extra>",
            pad=14, thickness=18,
        ),
        link=dict(
            source=link_src,
            target=link_tgt,
            value=link_val,
            color=link_col,
            hovertemplate="%{source.label} → %{target.label}<br>%{value} moves<extra></extra>",
        ),
    ))

    title = f"Life flows — top {top_n} nodes  |  grey = country cluster (click to expand)"
    if trace_node:
        title += f"  |  tracing: {trace_node}"
    if selected_people:
        title += f"  |  {len(selected_people)} person(s) highlighted"

    fig_height = max(600, len(node_order) * 40)
    fig.update_layout(
        title=dict(text=title, font=dict(size=13)),
        template="plotly_white",
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        height=fig_height,
        font=dict(size=11),
    )
    # Build flows_data for fast Patch-based recoloring (stores node indices)
    flows_data = {
        "per_person": [
            {"si": node_idx[r["from"]], "ti": node_idx[r["to"]],
             "pid": int(r["person_id"]), "n": int(r["count"])}
            for _, r in person_flows.iterrows()
            if r["from"] in node_idx and r["to"] in node_idx
        ],
        "aggregated": [
            {"si": node_idx[r["from"]], "ti": node_idx[r["to"]], "n": int(r["count"])}
            for _, r in flows_agg.iterrows()
            if r["from"] in node_idx and r["to"] in node_idx
        ],
    }

    return fig, country_nodes, flows_data


# ─── Network graph ────────────────────────────────────────────────────────────

def build_network(selected_people, relations_df, events_df):
    """
    Force-directed network of family/relational ties.
    Nodes = people, edges = relation type (spouse/parent/child/sibling).
    """
    if not HAS_NX:
        return empty_fig("networkx not installed. pip install networkx")

    if relations_df.empty:
        return empty_fig("No relational data available.")

    rel = relations_df.copy()
    if selected_people:
        # Show ego network: selected people + their direct connections
        connected = set(selected_people)
        for pid in selected_people:
            nb1 = set(rel[rel["person_id_1"] == pid]["person_id_2"].tolist())
            nb2 = set(rel[rel["person_id_2"] == pid]["person_id_1"].tolist())
            connected |= nb1 | nb2
        rel = rel[
            rel["person_id_1"].isin(connected) |
            rel["person_id_2"].isin(connected)
        ]

    if rel.empty:
        return empty_fig("No connections found for selected people.")

    # Cap at 200 nodes for performance
    all_ids = set(rel["person_id_1"]) | set(rel["person_id_2"])
    if len(all_ids) > 200:
        rel = rel.head(400)
        all_ids = set(rel["person_id_1"]) | set(rel["person_id_2"])

    G = nx.Graph()
    id_to_name = {}

    for _, row in rel.iterrows():
        G.add_edge(row["person_id_1"], row["person_id_2"],
                   relation=row.get("relation_type", "related"))
        id_to_name[row["person_id_1"]] = row["name_1"]
        id_to_name[row["person_id_2"]] = row["name_2"]

    pos = nx.spring_layout(G, seed=42, k=1.5/max(1, len(G.nodes)**0.5))

    # Faculty color for nodes
    fac_map = {}
    if not events_df.empty:
        for _, row in events_df[["person_id","faculty"]].drop_duplicates().iterrows():
            fac_map[row["person_id"]] = FACULTY_COLOR_MAP.get(row["faculty"], "#aaa")

    edge_traces = []
    rel_types = rel["relation_type"].dropna().unique().tolist() if "relation_type" in rel else ["related"]

    rel_colors = {"spouse": "#e63946", "parent": "#457b9d", "child": "#2a9d8f",
                  "sibling": "#e9c46a", "related": "#aaa"}

    for rtype in rel_types:
        sub_rel = rel[rel.get("relation_type", pd.Series(["related"]*len(rel))) == rtype] \
                  if "relation_type" in rel.columns else rel
        ex, ey = [], []
        for _, row in sub_rel.iterrows():
            p1, p2 = row["person_id_1"], row["person_id_2"]
            if p1 in pos and p2 in pos:
                x1, y1 = pos[p1]
                x2, y2 = pos[p2]
                ex += [x1, x2, None]
                ey += [y1, y2, None]
        if ex:
            edge_traces.append(go.Scatter(
                x=ex, y=ey, mode="lines",
                line={"width": 1.5, "color": rel_colors.get(rtype, "#aaa")},
                hoverinfo="none", name=rtype.title(), showlegend=True,
            ))

    node_x = [pos[n][0] for n in G.nodes()]
    node_y = [pos[n][1] for n in G.nodes()]
    node_colors = [
        "#c0392b" if n in (selected_people or []) else fac_map.get(n, "#7f8c8d")
        for n in G.nodes()
    ]
    node_sizes = [
        16 if n in (selected_people or []) else
        8 + 2 * G.degree(n)
        for n in G.nodes()
    ]
    node_text = [id_to_name.get(n, str(n)) for n in G.nodes()]

    node_trace = go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        marker={"size": node_sizes, "color": node_colors,
                "line": {"width": 1, "color": "#fff"}},
        text=node_text,
        textposition="top center",
        textfont={"size": 9},
        hovertemplate="<b>%{text}</b><extra></extra>",
        showlegend=False,
    )

    fig = go.Figure(data=edge_traces + [node_trace])
    fig.update_layout(
        template="plotly_white",
        title="Relational network",
        showlegend=True,
        hovermode="closest",
        xaxis={"showgrid": False, "zeroline": False, "showticklabels": False},
        yaxis={"showgrid": False, "zeroline": False, "showticklabels": False},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        height=600,
        legend={"orientation": "h", "y": -0.05},
    )
    return fig


# ─── Space-time cube ──────────────────────────────────────────────────────────

def build_cube(df, selected_people, show_copresence=False):
    if df.empty:
        return empty_fig("No data for space-time cube.", h=500)

    selected_people = selected_people or []
    cube_df = df if not selected_people else df[df["person_id"].isin(selected_people)]
    if cube_df.empty:
        return empty_fig("No events for selected people.", h=500)

    fig = go.Figure()

    # Floor map
    if HAS_PIL and os.path.exists(FLOOR_MAP_PATH):
        img = Image.open(FLOOR_MAP_PATH).convert("L")
        arr = np.array(img)
        lons = np.linspace(float(cube_df["longitude"].min()) - 1,
                           float(cube_df["longitude"].max()) + 1, arr.shape[1])
        lats = np.linspace(float(cube_df["latitude"].min()) - 1,
                           float(cube_df["latitude"].max()) + 1, arr.shape[0])
        X, Y = np.meshgrid(lons, lats)
        Z    = np.full_like(X, float(cube_df["year"].min()))
        fig.add_trace(go.Surface(x=X, y=Y, z=Z, surfacecolor=arr,
                                 colorscale="Greys", cmin=0, cmax=255,
                                 showscale=False, opacity=0.5, hoverinfo="skip"))

    sel_color_map_cube = person_color_map(selected_people)
    # Use the FULL df (all time) so we only mark locations as shared when
    # the people were literally there in the same year — not just same interval.
    if selected_people and len(selected_people) >= 2:
        shared_locs_cube = get_shared_locations(
            df[df["person_id"].isin(selected_people)]
        )
    else:
        shared_locs_cube = set()

    for pid, pdf in cube_df.groupby("person_id"):
        pdf    = pdf.sort_values(["event_date", "event_order"]).reset_index(drop=True)
        is_sel = int(pid) in selected_people
        pname  = pdf["person_name"].iloc[0] if not pdf.empty else str(pid)

        if len(pdf) < 2:
            continue

        if is_sel:
            draw_person_path_3d(fig, pdf, pid, pname,
                                sel_color_map_cube[int(pid)], shared_locs_cube)
        else:
            fig.add_trace(go.Scatter3d(
                x=pdf["longitude"], y=pdf["latitude"], z=pdf["year"],
                mode="lines",
                line={"width": 2, "color": "#95a5a6"},
                opacity=0.25,
                hoverinfo="skip", showlegend=False,
            ))

    for etype, grp in cube_df.groupby("event_type_name"):
        fig.add_trace(go.Scatter3d(
            x=grp["longitude"], y=grp["latitude"], z=grp["year"],
            mode="markers",
            marker={"size": 5, "color": EVENT_COLORS.get(etype,"#636efa"), "opacity": 0.9},
            name=etype.title(),
            hovertext=grp["hover_text"],
            hovertemplate="%{hovertext}<br>Year %{z}<extra></extra>",
        ))

    # Gold markers at shared nodes in the cube
    if shared_locs_cube and selected_people:
        all_sel = df[df["person_id"].isin(selected_people)].copy()
        all_sel["lat_r"] = all_sel["latitude"].round(5)
        all_sel["lon_r"] = all_sel["longitude"].round(5)
        shared_df_c = pd.DataFrame(list(shared_locs_cube), columns=["lat_r", "lon_r"])
        gold_pts    = all_sel.merge(shared_df_c, on=["lat_r", "lon_r"])                              .drop_duplicates(subset=["lat_r", "lon_r", "year"])
        if not gold_pts.empty:
            fig.add_trace(go.Scatter3d(
                x=gold_pts["longitude"], y=gold_pts["latitude"], z=gold_pts["year"],
                mode="markers",
                marker={"size": 8, "color": GOLD, "opacity": 1.0,
                        "symbol": "diamond"},
                name="Co-present location",
                hovertext=gold_pts["location_label"],
                hovertemplate="<b>%{hovertext}</b><br>Year %{z}<extra></extra>",
                showlegend=True,
            ))

    # ── Co-presence overlay ───────────────────────────────────────────────────
    if show_copresence and not cube_df.empty:
        # Find (location, year) pairs where ≥2 different people were present
        cp = (
            cube_df.groupby(["longitude", "latitude", "year"])["person_id"]
            .nunique()
            .reset_index(name="n_people")
        )
        cp = cp[cp["n_people"] >= 2].copy()
        if not cp.empty:
            # Hover: list who was there
            def _cp_hover(row):
                ppl = cube_df[
                    (cube_df["longitude"] == row["longitude"]) &
                    (cube_df["latitude"]  == row["latitude"])  &
                    (cube_df["year"]      == row["year"])
                ]["person_name"].unique().tolist()
                loc = cube_df[
                    (cube_df["longitude"] == row["longitude"]) &
                    (cube_df["latitude"]  == row["latitude"])
                ]["city_label"].iloc[0] if not cube_df[
                    (cube_df["longitude"] == row["longitude"]) &
                    (cube_df["latitude"]  == row["latitude"])
                ].empty else "?"
                return f"<b>{loc} — {int(row['year'])}</b><br>" +                        "<br>".join(f"• {p}" for p in ppl[:10])
            cp["hover"] = cp.apply(_cp_hover, axis=1)

            fig.add_trace(go.Scatter3d(
                x=cp["longitude"], y=cp["latitude"], z=cp["year"],
                mode="markers",
                marker=dict(
                    size=cp["n_people"] * 3 + 4,
                    color="#f1c40f",
                    opacity=0.85,
                    line=dict(width=1, color="#e67e22"),
                    symbol="diamond",
                ),
                name="Co-presence",
                hovertext=cp["hover"],
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=True,
            ))

    lon_r = [float(cube_df["longitude"].min())-1, float(cube_df["longitude"].max())+1]
    lat_r = [float(cube_df["latitude"].min())-1,  float(cube_df["latitude"].max())+1]
    yr_r  = [int(cube_df["year"].min()),          int(cube_df["year"].max())]

    fig.update_layout(
        template="plotly_white",
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
        height=550,
        scene=dict(
            xaxis=dict(title="Longitude", range=lon_r),
            yaxis=dict(title="Latitude",  range=lat_r),
            zaxis=dict(title="Year",      range=yr_r),
            aspectmode="manual",
            aspectratio=dict(x=1.2, y=1.0, z=1.3),
            camera=dict(eye=dict(x=1.4, y=1.4, z=1.1)),
        ),
        legend=dict(orientation="h", y=0.01),
    )
    return fig


# ─── Event count bar ─────────────────────────────────────────────────────────

def build_event_bar(df, year_range, selected_people=None):
    selected_people = selected_people or []
    s, e = sorted(year_range)
    base = df[df["person_id"].isin(selected_people)] if selected_people else df
    counts = base.groupby("year").size().reindex(YEARS, fill_value=0).reset_index()
    counts.columns = ["year", "count"]
    colors = ["#c0392b" if s <= y <= e else "#b2bec3" for y in counts["year"]]
    fig = go.Figure(go.Bar(
        x=counts["year"], y=counts["count"],
        marker={"color": colors},
        hovertemplate="Year %{x}<br>Events %{y}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_white",
        margin={"l": 35, "r": 10, "t": 5, "b": 30},
        height=160,
        xaxis_title=None, yaxis_title="Events",
        bargap=0.05, dragmode=False,
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(rangemode="tozero")
    return fig


# ─── Layout ───────────────────────────────────────────────────────────────

# ── Pop-out button helper ─────────────────────────────────────────────────────
def popout_btn(panel_id: str, label: str) -> html.A:
    """A small link that opens a panel in a new browser window."""
    return html.A(
        f"⧉ Pop out {label}",
        href=f"/panel/{panel_id}",
        target="_blank",
        className="btn btn-outline-secondary btn-sm ms-2",
        style={"fontSize": "11px", "verticalAlign": "middle"},
    )

# ── Full-window panel layout builder ─────────────────────────────────────────
def panel_layout(graph_id: str, extra_controls=None):
    """Minimal layout for a detached panel — fills the entire browser window."""
    children = []
    if extra_controls:
        children.append(
            html.Div(extra_controls,
                     style={"padding": "6px 12px", "background": "#f8f9fa",
                            "borderBottom": "1px solid #dee2e6"})
        )
    children.append(
        dcc.Graph(
            id=graph_id,
            style={"height": "calc(100vh - 44px)" if extra_controls
                   else "100vh",
                   "width": "100vw"},
            config={"displayModeBar": True, "responsive": True},
        )
    )
    return html.Div(
        children,
        style={"margin": 0, "padding": 0, "overflow": "hidden",
               "fontFamily": "Georgia, serif"},
    )


CONTROLS = dbc.Card([
    dbc.CardBody([
        dbc.Row([
            dbc.Col([
                html.Label("Events over time", className="fw-bold small"),
                dcc.Graph(id="event-bar", config={"displayModeBar": False},
                          style={"height": "160px"}),
            ], width=12),
        ], className="mb-2"),
        dbc.Row([
            dbc.Col([
                html.Label("Year range", className="fw-bold small"),
                dcc.RangeSlider(
                    id="year-slider", min=YEARS[0], max=YEARS[-1], step=1,
                    value=DEFAULT_RANGE, marks=build_marks(YEARS),
                    allowCross=False,
                    tooltip={"placement": "bottom", "always_visible": False},
                    updatemode="mouseup",  # only fire on release, not every pixel
                ),
            ], width=12),
        ], className="mb-3"),
        dbc.Row([
            dbc.Col([
                html.Label("Highlight people", className="fw-bold small"),
                dcc.Dropdown(
                    id="person-dropdown",
                    options=[{"label": f"{r['person_name']} ({r.get('faculty') or '—'})",
                              "value": int(r["person_id"])}
                             for r in PERSON_OPTIONS],
                    value=[], multi=True,
                    placeholder="Click a path, or select here…",
                ),
            ], width=6),
            dbc.Col([
                html.Label("Faculty filter", className="fw-bold small"),
                dcc.Dropdown(
                    id="faculty-dropdown",
                    options=[{"label": f, "value": f} for f in FACULTIES],
                    value=[], multi=True,
                    placeholder="All faculties",
                ),
            ], width=4),
            dbc.Col([
                html.Label("Options", className="fw-bold small"),
                dbc.Checklist(
                    id="options-checks",
                    options=[
                        {"label": "Faculty colours",  "value": "faculty_colors"},
                        {"label": "Direction cues",   "value": "directions"},
                        {"label": "Bundle paths",     "value": "bundle"},
                    ],
                    value=[],
                    inline=True,
                    switch=True,
                ),
                dbc.RadioItems(
                    id="display-mode",
                    options=[{"label": "All paths",  "value": "all"},
                             {"label": "Selected",   "value": "selected"}],
                    value="all", inline=True, className="mt-1",
                ),
            ], width=2),
        ], className="mb-1"),
        dbc.Row([
            dbc.Col([
                html.Label("Background density", className="fw-bold small"),
                dcc.Slider(id="bg-opacity", min=0.02, max=0.35, step=0.01,
                           value=0.08,
                           marks={0.05: "light", 0.15: "med", 0.30: "dense"},
                           updatemode="mouseup"),
            ], width=4),
            dbc.Col([
                html.Label("Animation", className="fw-bold small"),
                dbc.ButtonGroup([
                    dbc.Button("▶ Play",  id="anim-play-btn",  color="primary",
                               size="sm", outline=True),
                    dbc.Button("⏸ Pause", id="anim-pause-btn", color="secondary",
                               size="sm", outline=True),
                    dbc.Button("↩ Reset", id="anim-reset-btn", color="secondary",
                               size="sm", outline=True),
                ], className="mt-1"),
                html.Span(id="anim-status", className="ms-2 small text-muted"),
            ], width=3),
            dbc.Col([
                html.Label("Yrs / step", className="fw-bold small"),
                dcc.Slider(id="anim-speed", min=1, max=10, step=1, value=3,
                           marks={1: "1", 5: "5", 10: "10"},
                           updatemode="mouseup"),
            ], width=2),
            dbc.Col([
                dbc.Button("↺ Refresh data", id="refresh-btn",
                           color="secondary", size="sm", outline=True,
                           className="mt-3"),
                html.Span(id="refresh-status", className="ms-2 small text-muted"),
            ], width=3),
        ]),
    ])
], className="mb-3 shadow-sm")


TABS = dbc.Tabs([

    # ── Map tab ──────────────────────────────────────────────────────────────
    dbc.Tab(label="🗺 Life paths map", tab_id="tab-map", children=[
        dbc.Row([
            dbc.Col([
                html.Div(popout_btn("map", "map"), className="text-end mb-1"),
                dcc.Loading(
                    id="map-loading",
                    type="circle",
                    children=dcc.Graph(id="lifepath-map",
                                       config={"displayModeBar": True,
                                               "responsive": True}),
                ),
            ], width=8),
            dbc.Col([
                html.H5("Selection", className="mt-2"),
                html.Div(id="selection-summary", className="mb-2 small"),
                dash_table.DataTable(
                    id="person-table",
                    columns=[
                        {"name": "Person",         "id": "person_name"},
                        {"name": "Faculty",        "id": "faculty"},
                        {"name": "Events",         "id": "visible_events"},
                        {"name": "First year",     "id": "first_year"},
                        {"name": "Last location",  "id": "latest_event"},
                    ],
                    data=[], page_size=10,
                    style_cell={"fontSize": "12px", "padding": "5px",
                                "textAlign": "left"},
                    style_header={"fontWeight": "bold"},
                ),
                html.H5("Timeline", className="mt-3"),
                html.Div(id="person-timeline",
                         style={"maxHeight": "35vh", "overflowY": "auto"}),
                html.H5("Hovered location", className="mt-3"),
                html.Div(id="hover-summary",
                         style={"maxHeight": "18vh", "overflowY": "auto"},
                         className="small"),
            ], width=4),
        ]),
    ]),

    # ── Space-time cube tab ───────────────────────────────────────────────────
    dbc.Tab(label="🧊 Space-time cube", tab_id="tab-cube", children=[
        dbc.Row([
            dbc.Col([
                dbc.Checklist(
                    id="cube-options",
                    options=[{"label": " Highlight co-presence locations", "value": "copresence"}],
                    value=[],
                    inline=True,
                    switch=True,
                    className="mt-2 mb-1 small",
                ),
                html.P(
                    "Co-presence markers appear as gold spheres where ≥2 professors "
                    "were present in the same year.",
                    className="text-muted small mb-0",
                ),
            ], width=4),
            dbc.Col([
                html.Label("Latitude range (spatial zoom)", className="fw-bold small"),
                dcc.RangeSlider(
                    id="cube-lat-range",
                    min=LAT_MIN, max=LAT_MAX, step=1,
                    value=[LAT_MIN, LAT_MAX],
                    marks={LAT_MIN: str(LAT_MIN), LAT_MAX: str(LAT_MAX)},
                    tooltip={"placement": "bottom", "always_visible": False},
                    updatemode="mouseup",
                ),
                html.Label("Longitude range", className="fw-bold small mt-1"),
                dcc.RangeSlider(
                    id="cube-lon-range",
                    min=LON_MIN, max=LON_MAX, step=1,
                    value=[LON_MIN, LON_MAX],
                    marks={LON_MIN: str(LON_MIN), LON_MAX: str(LON_MAX)},
                    tooltip={"placement": "bottom", "always_visible": False},
                    updatemode="mouseup",
                ),
            ], width=6),
            dbc.Col([
                dbc.Button("Reset spatial filter", id="cube-reset-btn",
                           color="secondary", size="sm", outline=True,
                           className="mt-4"),
            ], width=2),
        ], className="mb-2"),
        html.Div(popout_btn("cube", "cube"), className="text-end mb-1"),
        dcc.Graph(id="space-time-cube", config={"displayModeBar": True,
                                                 "responsive": True}),
    ]),

    # ── Co-presence tab ───────────────────────────────────────────────────────
    dbc.Tab(label="📍 Co-presence", tab_id="tab-copresence", children=[
        html.P(
            "Professors who were in the same city during overlapping years. "
            "Select people on the map to see their shared locations highlighted in gold.",
            className="text-muted small mt-2 mb-2",
        ),
        html.Div(id="copresence-pairs",
                 style={"maxHeight": "70vh", "overflowY": "auto",
                        "columnCount": 2, "columnGap": "1rem"}),
    ]),

    # ── Career flow tab ───────────────────────────────────────────────────────
    dbc.Tab(label="➡ Life flows", tab_id="tab-sankey", children=[
        dbc.Row([
            dbc.Col([
                html.P(
                    "City-to-city career moves. Non-NL cities are grouped by country "
                    "(grey nodes). Expand countries with the dropdown below, or click "
                    "any city node to trace flows through it. "
                    "Select people on the map to highlight their personal paths.",
                    className="text-muted small mt-2 mb-1",
                ),
                html.Div(id="sankey-trace-info", className="small text-info"),
            ], width=5),
            dbc.Col([
                html.Label("Expand countries", className="fw-bold small mt-2"),
                dcc.Dropdown(
                    id="sankey-expand-dropdown",
                    options=[],
                    value=[],
                    multi=True,
                    placeholder="Select country clusters to expand…",
                ),
            ], width=4),
            dbc.Col([
                html.Label("Max nodes shown", className="fw-bold small mt-2"),
                dcc.Slider(
                    id="sankey-top-n",
                    min=5, max=40, step=5, value=20,
                    marks={5: "5", 10: "10", 20: "20", 30: "30", 40: "40"},
                    tooltip={"placement": "bottom", "always_visible": False},
                ),
                dbc.Button("✕ Clear", id="sankey-clear-btn",
                           color="secondary", size="sm", outline=True,
                           className="mt-2"),
            ], width=3),
        ], className="mb-2"),
        html.Div(popout_btn("sankey", "life flows"), className="text-end mb-1"),
        html.Div(
            dcc.Graph(id="career-sankey",
                      config={"displayModeBar": True}),
            style={"overflowY": "auto", "height": "calc(100vh - 260px)"},
        ),
    ]),

    # ── Network tab ───────────────────────────────────────────────────────────
    dbc.Tab(label="🔗 Network", tab_id="tab-network", children=[
        html.P("Relational network of professors. Select people to see their "
               "ego network. Colours = faculty. Red = selected.",
               className="text-muted small mt-2"),
        dcc.Graph(id="network-graph"),
    ]),

], id="main-tabs", active_tab="tab-map")


app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY],
    title="Life Paths — Leiden University History",
    suppress_callback_exceptions=True,
)
app.title = "Life Paths"

# ── Shared state stores (available on all pages) ──────────────────────────────
_STORES = [
    dcc.Store(id="selected-people-store", data=[]),
    dcc.Location(id="url", refresh=False),

    # ── New feature stores ────────────────────────────────────────────────────
    dcc.Store(id="anim-playing",          data=False),
    dcc.Interval(id="anim-tick",          interval=2000, disabled=True, n_intervals=0),
    dcc.Store(id="sankey-trace-node",     data=None),  # node being traced
    dcc.Store(id="sankey-country-nodes",  data=[]),    # labels that are country clusters
    dcc.Store(id="sankey-flows-store",    data=None),  # per-link person_ids for fast recolor

    # ── Hidden fallback components ────────────────────────────────────────────
    # Panel pages only render a subset of the layout; these ensure every
    # callback input/state ID always exists in the DOM.
    dcc.Checklist(id="cube-options",    value=[], options=[],
                  style={"display": "none"}),
    html.Div(dcc.Slider(id="sankey-top-n", value=20, min=5, max=40),
             style={"display": "none"}),
    html.Div(dcc.RangeSlider(id="year-slider", value=DEFAULT_RANGE,
             min=YEARS[0], max=YEARS[-1]), style={"display": "none"}),
    dcc.Dropdown(id="faculty-dropdown", value=[], options=[],
                  style={"display": "none"}),
    dcc.RadioItems(id="display-mode",   value="all", options=[],
                  style={"display": "none"}),
    dcc.Checklist(id="options-checks",  value=[], options=[
                      {"label": "Faculty colours", "value": "faculty_colors"},
                      {"label": "Direction cues",  "value": "directions"},
                      {"label": "Bundle paths",    "value": "bundle"},
                  ], style={"display": "none"}),
    html.Div(dcc.Slider(id="bg-opacity", value=0.08, min=0.02, max=0.35),
             style={"display": "none"}),
    dbc.Tabs(id="main-tabs", active_tab=None, children=[],
             style={"display": "none"}),
    # Animation controls fallbacks (only in CONTROLS on main page)
    html.Div([
        dbc.Button(id="anim-play-btn"),
        dbc.Button(id="anim-pause-btn"),
        dbc.Button(id="anim-reset-btn"),
        dcc.Slider(id="anim-speed", value=3, min=1, max=10),
        html.Span(id="anim-status"),
    ], style={"display": "none"}),
    # Cube spatial filter fallbacks (only in cube tab on main page)
    html.Div([
        dcc.RangeSlider(id="cube-lat-range", value=[LAT_MIN, LAT_MAX],
                        min=LAT_MIN, max=LAT_MAX),
        dcc.RangeSlider(id="cube-lon-range", value=[LON_MIN, LON_MAX],
                        min=LON_MIN, max=LON_MAX),
        dbc.Button(id="cube-reset-btn"),
    ], style={"display": "none"}),
    # Sankey interaction fallbacks (only in sankey tab on main page)
    html.Div([
        dbc.Button(id="sankey-clear-btn"),
        html.Div(id="sankey-trace-info"),
        dcc.Dropdown(id="sankey-expand-dropdown", value=[], options=[]),
    ], style={"display": "none"}),
]

# ── Root layout — router shell ────────────────────────────────────────────────
app.layout = html.Div([
    *_STORES,
    html.Div(id="page-content"),
])

# ── Main app layout ───────────────────────────────────────────────────────────
MAIN_LAYOUT = dbc.Container([
    dbc.Row(dbc.Col([
        html.H3("Life Paths — Leiden University", className="mt-3 mb-0"),
        html.P("Historical geography of professors, 1575–present",
               className="text-muted mb-2"),
    ])),
    CONTROLS,
    TABS,
], fluid=True, style={"fontFamily": "Georgia, serif", "maxWidth": "1800px"})


# ─── Callbacks ────────────────────────────────────────────────────────────────

# Sync person selection (map click ↔ dropdown)
@app.callback(
    Output("selected-people-store", "data"),
    Output("person-dropdown", "value"),
    Input("lifepath-map", "clickData"),
    Input("person-dropdown", "value"),
    State("selected-people-store", "data"),
    prevent_initial_call=True,
)
def sync_selection(click_data, dropdown_val, stored):
    stored = stored or []
    dropdown_val = dropdown_val or []
    trigger = ctx.triggered_id

    if trigger == "person-dropdown":
        cleaned = sorted({int(x) for x in dropdown_val})
        return cleaned, cleaned

    if trigger == "lifepath-map" and click_data:
        cd = (click_data.get("points") or [{}])[0].get("customdata")
        if cd and len(cd) >= 2:
            try:
                pid = int(cd[0])
                if cd[2] if len(cd) > 2 else True:  # not a location marker
                    new = [p for p in stored if p != pid] if pid in stored \
                          else sorted(stored + [pid])
                    return new, new
            except Exception:
                pass

    return stored, stored


# Refresh data
@app.callback(
    Output("refresh-status", "children"),
    Input("refresh-btn", "n_clicks"),
    prevent_initial_call=True,
)
def refresh(_):
    refresh_data()
    return "Refreshed ✓"


# Event bar (always on)
@app.callback(
    Output("event-bar", "figure"),
    Input("year-slider", "value"),
    Input("selected-people-store", "data"),
    Input("faculty-dropdown", "value"),
)
def update_bar(year_range, selected, faculties):
    df = filter_df(DF, [MIN_YEAR, MAX_YEAR], faculties or None)
    return build_event_bar(df, year_range, selected)


# Map
@app.callback(
    Output("lifepath-map", "figure"),
    Output("selection-summary", "children"),
    Output("person-table", "data"),
    Output("person-timeline", "children"),
    Input("year-slider", "value"),
    Input("selected-people-store", "data"),
    Input("display-mode", "value"),
    Input("options-checks", "value"),
    Input("bg-opacity", "value"),
    Input("faculty-dropdown", "value"),
)
def update_map(year_range, selected, display_mode, options, bg_opacity, faculties):
    if not year_range:
        raise dash.exceptions.PreventUpdate
    selected = selected or []
    df = filter_df(DF, year_range, faculties or None)

    use_faculty = "faculty_colors" in (options or [])
    directions  = "directions"      in (options or [])
    use_bundling = "bundle"         in (options or [])

    map_fig = build_map(df, year_range, selected, display_mode,
                        "selected" if directions else "off",
                        bg_opacity, use_faculty,
                        use_bundling=use_bundling)

    s, e = sorted(year_range)
    path_df = (DF if not faculties else DF[DF["faculty"].isin(faculties)])
    path_df = path_df[path_df["year"] <= e]

    table_df = (
        (path_df[path_df["person_id"].isin(selected)] if selected else path_df)
        .sort_values(["person_name","event_date"])
        .groupby(["person_id","person_name","faculty"], as_index=False)
        .agg(visible_events=("event_type_name","count"),
             first_year=("year","min"),
             latest_event=("location_label","last"))
        .sort_values("visible_events", ascending=False)
        .head(20)
    )

    summary = html.Div([
        html.Span(f"Interval: {s}–{e}  "),
        html.Span(f"People: {path_df['person_id'].nunique()}  "),
        html.Span(f"Selected: {len(selected)}"),
    ], className="small text-muted")

    timeline = _build_timeline(path_df, selected)
    return map_fig, summary, table_df.to_dict("records"), timeline


def _build_timeline(df, selected):
    if not selected:
        return html.P("Select people to see their timeline.", className="text-muted small")
    sdf = df[df["person_id"].isin(selected)].sort_values(["person_name","event_date","event_order"])
    blocks = []
    for pname, pdf in sdf.groupby("person_name"):
        rows = []
        for _, row in pdf.iterrows():
            when = row["event_date"].strftime("%Y") if pd.notna(row["event_date"]) else "?"
            desc = row["description"] or row["event_type_name"].title()
            rows.append(html.Div([
                html.Span(when, style={"minWidth":"50px","fontWeight":"bold","display":"inline-block"}),
                html.Span(row["event_type_name"].title() + " — ",
                          style={"color": EVENT_COLORS.get(row["event_type_name"],"#888")}),
                html.Span(f"{desc} · {row['location_label']}"),
            ], style={"borderBottom":"1px solid #eee","padding":"3px 0","fontSize":"12px"}))
        blocks.append(dbc.Card(dbc.CardBody([
            html.Strong(pname, className="small"),
            html.Div(rows),
        ]), className="mb-2"))
    return blocks


# Hover summary
@app.callback(
    Output("hover-summary", "children"),
    Input("lifepath-map", "hoverData"),
    State("year-slider", "value"),
)
def hover_summary(hover_data, year_range):
    if not hover_data:
        return html.P("Hover a black marker.", className="text-muted")
    cd = (hover_data.get("points") or [{}])[0].get("customdata")
    if not cd or len(cd) != 5:
        return html.P("Hover a black marker.", className="text-muted")
    lat, lon, loc, sy, ey = cd
    sub = DF[(DF["latitude"]==lat)&(DF["longitude"]==lon)&
             (DF["year"]>=int(sy))&(DF["year"]<=int(ey))]
    if sub.empty:
        return html.P("No detail available.")
    names = sub["person_name"].unique().tolist()
    return html.Div([
        html.Strong(loc), html.Br(),
        html.Span(f"{int(sy)}–{int(ey)} · {len(names)} people", className="text-muted"),
        html.Ul([html.Li(n, style={"fontSize":"11px"}) for n in names[:15]]),
    ])


# ── Lazy tab callbacks — use url to detect panel vs main app ─────────────────
# All four use prevent_initial_call=True and check url.pathname so they work
# both in the tabbed main app AND in detached panel windows.

@app.callback(
    Output("space-time-cube", "figure"),
    Input("year-slider", "value"),
    Input("selected-people-store", "data"),
    Input("cube-options", "value"),
    Input("faculty-dropdown", "value"),
    Input("cube-lat-range", "value"),
    Input("cube-lon-range", "value"),
    State("url", "pathname"),
    prevent_initial_call=False,
)
def update_cube(year_range, selected, cube_opts, faculties, lat_range, lon_range, pathname):
    yr      = year_range or DEFAULT_RANGE
    df      = filter_df(DF, yr, faculties or None)
    show_cp = "copresence" in (cube_opts or [])

    # Apply spatial filter from the lat/lon sliders
    if lat_range and len(lat_range) == 2:
        lo, hi = sorted(lat_range)
        df = df[(df["latitude"] >= lo) & (df["latitude"] <= hi)]
    if lon_range and len(lon_range) == 2:
        lo, hi = sorted(lon_range)
        df = df[(df["longitude"] >= lo) & (df["longitude"] <= hi)]

    return build_cube(df, selected or [], show_copresence=show_cp)


@app.callback(
    Output("copresence-pairs", "children"),
    Input("year-slider", "value"),
    Input("selected-people-store", "data"),
    Input("faculty-dropdown", "value"),
    prevent_initial_call=False,
)
def update_copresence(year_range, selected, faculties):
    yr = year_range or DEFAULT_RANGE
    df = filter_df(DF, yr, faculties or None)
    if selected:
        df = df[df["person_id"].isin(selected)]
    pairs_df = build_copresence(df, yr)
    if pairs_df.empty:
        return html.P("No co-present pairs found in this range.", className="text-muted")
    return [
        dbc.Card(dbc.CardBody([
            html.Strong(f"{r['person_a']} & {r['person_b']}",
                        style={"fontSize": "12px"}),
            html.Div(f"{r['city']} · {r['overlap_years']} yrs · {r['years']}",
                     className="text-muted", style={"fontSize": "11px"}),
        ]), className="mb-1 break-inside-avoid")
        for _, r in pairs_df.head(60).iterrows()
    ]


@app.callback(
    Output("career-sankey", "figure"),
    Output("sankey-country-nodes", "data"),
    Output("sankey-flows-store", "data"),
    Input("year-slider", "value"),
    Input("faculty-dropdown", "value"),
    Input("sankey-top-n", "value"),
    Input("sankey-expand-dropdown", "value"),
    State("sankey-trace-node", "data"),
    State("selected-people-store", "data"),
    prevent_initial_call=False,
)
def update_sankey(year_range, faculties, top_n, expanded, trace_node, selected):
    yr  = year_range or DEFAULT_RANGE
    fig, country_nodes, flows_data = build_career_sankey(
        DF, yr,
        faculty_filter=faculties or None,
        top_n=top_n or 20,
        expanded_countries=expanded or [],
        trace_node=trace_node,
        selected_people=selected or [],
    )
    return fig, country_nodes, flows_data


@app.callback(
    Output("career-sankey", "figure", allow_duplicate=True),
    Input("selected-people-store", "data"),
    Input("sankey-trace-node", "data"),
    State("sankey-flows-store", "data"),
    prevent_initial_call=True,
)
def recolor_sankey(selected, trace_node, flows_data):
    if not flows_data or not flows_data.get("per_person") and not flows_data.get("aggregated"):
        raise dash.exceptions.PreventUpdate

    selected_people = list(map(int, selected or []))
    sel_set  = set(selected_people)
    sel_cmap = person_color_map(selected_people)

    def _hex_rgba(hex_c, alpha):
        h = hex_c.lstrip("#")
        rv, gv, bv = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({rv},{gv},{bv},{alpha})"

    link_src, link_tgt, link_val, link_col = [], [], [], []

    if sel_set:
        # Unselected aggregate
        unsel_agg = {}
        for p in flows_data.get("per_person", []):
            if p["pid"] not in sel_set:
                k = (p["si"], p["ti"])
                unsel_agg[k] = unsel_agg.get(k, 0) + p["n"]
        for (si, ti), n in unsel_agg.items():
            link_src.append(si); link_tgt.append(ti)
            link_val.append(n);  link_col.append("rgba(180,180,180,0.15)")

        # Per-person proportional bands
        for p in flows_data.get("per_person", []):
            if p["pid"] in sel_set:
                link_src.append(p["si"]); link_tgt.append(p["ti"])
                link_val.append(p["n"])
                link_col.append(_hex_rgba(sel_cmap.get(p["pid"], "#e63946"), 0.85))
    else:
        for p in flows_data.get("aggregated", []):
            link_src.append(p["si"]); link_tgt.append(p["ti"])
            link_val.append(p["n"]); link_col.append("rgba(69,123,157,0.28)")

    patched = Patch()
    patched["data"][0]["link"]["source"] = link_src
    patched["data"][0]["link"]["target"] = link_tgt
    patched["data"][0]["link"]["value"]  = link_val
    patched["data"][0]["link"]["color"]  = link_col
    return patched


@app.callback(
    Output("sankey-expand-dropdown", "options"),
    Output("sankey-expand-dropdown", "value", allow_duplicate=True),
    Input("sankey-country-nodes", "data"),
    State("sankey-expand-dropdown", "value"),
    prevent_initial_call="initial_duplicate",
)
def sync_expand_options(country_nodes, current_value):
    """Keep the expand dropdown options in sync with detected country clusters."""
    nodes = country_nodes or []
    options = [{"label": n, "value": n} for n in sorted(nodes)]
    # Remove any selected values that no longer exist as cluster nodes
    value = [v for v in (current_value or []) if v in nodes]
    return options, value


@app.callback(
    Output("sankey-trace-node", "data"),
    Output("sankey-trace-info", "children"),
    Output("sankey-expand-dropdown", "value", allow_duplicate=True),
    Input("career-sankey", "clickData"),
    Input("sankey-clear-btn", "n_clicks"),
    State("sankey-country-nodes", "data"),
    State("sankey-expand-dropdown", "value"),
    prevent_initial_call=True,
)
def handle_sankey_click(click_data, clear_clicks, country_nodes, expanded):
    """
    Click a city node → set it as trace node (highlights flows through it).
    Click a country node → add it to the expand dropdown (expand the cluster).
    Clear button → reset trace and collapse all.
    """
    tid = ctx.triggered_id

    if tid == "sankey-clear-btn":
        return None, "", []

    if tid == "career-sankey" and click_data:
        pt    = (click_data.get("points") or [{}])[0]
        label = pt.get("label", "")
        if not label:
            raise dash.exceptions.PreventUpdate

        color      = pt.get("color") or ""
        nodes      = list(country_nodes or [])
        is_cluster = color == "#6c757d" or label in nodes

        if is_cluster:
            # Add to expand dropdown (toggle: remove if already there)
            exp = list(expanded or [])
            if label in exp:
                exp = [c for c in exp if c != label]
                info = f"Collapsed '{label}'."
            else:
                exp.append(label)
                info = f"Expanded '{label}' into individual cities."
            return dash.no_update, info, exp
        else:
            info = f"Tracing flows through: {label}  —  click 'Clear' to reset"
            return label, info, dash.no_update

    raise dash.exceptions.PreventUpdate


@app.callback(
    Output("network-graph", "figure"),
    Input("selected-people-store", "data"),
    State("main-tabs", "active_tab"),
    prevent_initial_call=False,
)
def update_network(selected, active_tab):
    return build_network(selected or [], RELATIONS, DF)


# ─── Animation ────────────────────────────────────────────────────────────────

@app.callback(
    Output("year-slider",   "value",    allow_duplicate=True),
    Output("anim-playing",  "data",     allow_duplicate=True),
    Output("anim-tick",     "disabled", allow_duplicate=True),
    Output("anim-status",   "children"),
    Input("anim-play-btn",  "n_clicks"),
    Input("anim-pause-btn", "n_clicks"),
    Input("anim-reset-btn", "n_clicks"),
    Input("anim-tick",      "n_intervals"),
    State("year-slider",    "value"),
    State("anim-playing",   "data"),
    State("anim-speed",     "value"),
    prevent_initial_call=True,
)
def animation_driver(play, pause, reset, n_tick, year_range, playing, speed):
    """Single callback handles play / pause / reset / tick advance."""
    tid  = ctx.triggered_id
    yr   = year_range or DEFAULT_RANGE
    s, e = sorted(yr)

    if tid == "anim-play-btn":
        return yr, True, False, "▶ Playing…"

    if tid == "anim-pause-btn":
        return yr, False, True, "⏸ Paused"

    if tid == "anim-reset-btn":
        return DEFAULT_RANGE, False, True, ""

    if tid == "anim-tick":
        if not playing:
            raise dash.exceptions.PreventUpdate
        spd   = int(speed or 3)
        new_e = min(e + spd, MAX_YEAR)
        if new_e >= MAX_YEAR:
            return [s, MAX_YEAR], False, True, "⏹ Done"
        return [s, new_e], True, False, f"▶ {s} – {new_e}"

    raise dash.exceptions.PreventUpdate


# ─── Cube spatial-filter reset ────────────────────────────────────────────────

@app.callback(
    Output("cube-lat-range", "value"),
    Output("cube-lon-range", "value"),
    Input("cube-reset-btn",  "n_clicks"),
    prevent_initial_call=True,
)
def reset_cube_spatial(_):
    return [LAT_MIN, LAT_MAX], [LON_MIN, LON_MAX]


# ─── Router ──────────────────────────────────────────────────────────────────

@app.callback(
    Output("page-content", "children"),
    Input("url", "pathname"),
)
def route(pathname):
    """Serve the main app or a full-window panel based on URL path."""
    if not pathname or pathname == "/":
        return MAIN_LAYOUT

    if pathname == "/panel/map":
        return panel_layout("lifepath-map")

    if pathname == "/panel/cube":
        return panel_layout(
            "space-time-cube",
            extra_controls=[
                dbc.Checklist(
                    id="cube-options",
                    options=[{"label": " Highlight co-presence",
                              "value": "copresence"}],
                    value=[], inline=True, switch=True,
                    className="small d-inline-block me-3",
                ),
            ],
        )

    if pathname == "/panel/sankey":
        return panel_layout(
            "career-sankey",
            extra_controls=[
                html.Label("Max cities:", className="small me-2 fw-bold"),
                html.Div(dcc.Slider(
                    id="sankey-top-n",
                    min=5, max=40, step=5, value=20,
                    marks={5:"5",10:"10",20:"20",30:"30",40:"40"},
                    tooltip={"placement": "bottom", "always_visible": False},
                ), style={"width": "260px", "display": "inline-block",
                          "verticalAlign": "middle"}),
            ],
        )

    # 404 fallback
    return html.Div([
        html.H4("Panel not found", className="mt-5 text-center text-muted"),
        html.P(pathname, className="text-center text-muted small"),
    ])


# Panel pages need their own instances of the graph callbacks.
# We re-use the same callback functions but they trigger on the panel graph ids.
# Because suppress_callback_exceptions=True, undefined ids on other pages are fine.

# Panel rendering is handled by the unified callbacks above.


if __name__ == "__main__":
    app.run(debug=True)