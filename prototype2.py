"""
Life Paths v2 — Leiden University Professor Geography
======================================================
Stack: Dash 4 · Mantine 2.7 · AG Grid · deck.gl · Cytoscape · Plotly
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import os
from functools import lru_cache

import dash
from dash import Dash, dcc, html, Input, Output, State, ctx, ALL, no_update
import dash_mantine_components as dmc
import dash_ag_grid as dag
from person_card_component.person_card import PersonCard  # kept for future use
import numpy as np
import plotly.graph_objects as go
import pandas as pd

try:
    import dash_deck
    HAS_DECK = True
except Exception:
    HAS_DECK = False

try:
    import dash_cytoscape as cyto
    cyto.load_extra_layouts()
    HAS_CYTO = True
except Exception:
    HAS_CYTO = False

try:
    import pymonetdb
    HAS_MONETDB = True
except ImportError:
    HAS_MONETDB = False


# ── Database config ───────────────────────────────────────────────────────────

DB_HOST     = os.getenv("MONETDB_HOST",     "localhost")
DB_PORT     = int(os.getenv("MONETDB_PORT", "50000"))
DB_NAME     = os.getenv("MONETDB_DATABASE", "peopledb")
DB_USER     = os.getenv("MONETDB_USER",     "monetdb")
DB_PASSWORD = os.getenv("MONETDB_PASSWORD", "monetdb")

EVENT_COLORS = {
    "birth":     "#2ca02c",
    "education": "#1f77b4",
    "career":    "#ff7f0e",
    "death":     "#d62728",
}

FACULTY_COLORS = {
    "Theologie":   "#2ca02c",
    "Geneeskunde": "#1f77b4",
    "Rechten":     "#ff7f0e",
    "Letteren":    "#d62728",
    "Wijsbegeerte":"#9467bd",
    "Filosofie":   "#8c564b",
    "Wiskunde":    "#e377c2",
    "Curatoren":   "#bcbd22",
}
FACULTY_COLORS_DEFAULT = "#aaa"

# Palette for up to 12 simultaneously-selected people
SEL_PALETTE = [
    "#e63946","#457b9d","#2a9d8f","#e9c46a",
    "#f4a261","#264653","#a8dadc","#6d6875",
    "#8338ec","#fb5607","#06d6a0","#118ab2",
]

CARTO_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"


# ── Data layer ────────────────────────────────────────────────────────────────

def _read_sql(query: str) -> pd.DataFrame:
    conn = pymonetdb.connect(
        hostname=DB_HOST, port=DB_PORT, database=DB_NAME,
        username=DB_USER, password=DB_PASSWORD,
    )
    try:
        cur = conn.cursor()
        cur.execute(query)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)
    finally:
        conn.close()


EVENTS_QUERY = """
SELECT
    p.person_id,
    TRIM(
        COALESCE(p.first_name, '') || ' ' ||
        COALESCE(p.affix || ' ', '') ||
        COALESCE(p.last_name, '')
    ) AS person_name,
    p.faculty,
    et.event_type_name  AS event_type,
    e.begin_date,
    e.end_date,
    e.description,
    l.city              AS location,
    l.country,
    l.latitude          AS lat,
    l.longitude         AS lon
FROM event e
JOIN person p  ON p.person_id       = e.person_id
JOIN event_type et ON et.event_type_id = e.event_type_id
LEFT JOIN location l ON l.location_id  = e.location_id
WHERE et.event_type_name IN ('birth', 'death', 'education', 'career')
  AND p.type_of_person = 3
ORDER BY p.person_id, e.begin_date, e.end_date
"""

PEOPLE_QUERY = """
SELECT
    p.person_id,
    TRIM(
        COALESCE(p.first_name, '') || ' ' ||
        COALESCE(p.affix || ' ', '') ||
        COALESCE(p.last_name, '')
    ) AS name,
    p.first_name,
    p.last_name,
    p.gender,
    p.faculty,
    CAST(EXTRACT(YEAR FROM p.birth_date_begin) AS INTEGER) AS birth_year,
    CAST(EXTRACT(YEAR FROM p.death_date_begin) AS INTEGER) AS death_year,
    p.birth_city,
    p.birth_country,
    p.death_city,
    p.death_country
FROM person p
WHERE p.type_of_person = 3
ORDER BY p.person_id
"""


@lru_cache(maxsize=1)
def load_data():
    """Load events and people from MonetDB peopledb."""
    events_df = _read_sql(EVENTS_QUERY)
    people_df = _read_sql(PEOPLE_QUERY)

    if not events_df.empty:
        events_df["begin_date"] = pd.to_datetime(events_df["begin_date"], errors="coerce")
        events_df["end_date"]   = pd.to_datetime(events_df["end_date"],   errors="coerce")
        events_df["date"]       = events_df["begin_date"].fillna(events_df["end_date"])
        events_df["year"]       = events_df["date"].dt.year.astype("Int64")
        events_df["event_order"] = events_df["event_type"].map(
            {"birth": 0, "education": 1, "career": 2, "death": 3}
        ).fillna(9).astype(int)
        events_df["location"] = (
            events_df["location"].fillna("") + ", " + events_df["country"].fillna("")
        ).str.strip(", ")
        events_df = events_df.dropna(subset=["lat", "lon"]).reset_index(drop=True)

    return people_df, events_df


# Load once at startup
PEOPLE_DF, EVENTS_DF = load_data()

YEAR_MIN = int(EVENTS_DF["year"].min()) if not EVENTS_DF.empty else 1575
YEAR_MAX = int(EVENTS_DF["year"].max()) if not EVENTS_DF.empty else 2000
PEOPLE   = PEOPLE_DF.to_dict("records") if not PEOPLE_DF.empty else []

# Per-person events lookup keyed by person_id (int)
PERSON_EVENTS: dict = {}
if not EVENTS_DF.empty:
    for _r in EVENTS_DF.itertuples():
        _pid = int(_r.person_id)
        PERSON_EVENTS.setdefault(_pid, []).append({
            "year":        int(_r.year) if pd.notna(_r.year) else None,
            "type":        _r.event_type,
            "location":    _r.location if _r.location else None,
            "description": _r.description if _r.description else None,
        })

# City → country lookup built from rows that have explicit country data
_NL_VARIANTS = {"nederland", "netherlands", "holland", "the netherlands"}
_CITY_COUNTRY_MAP: dict[str, str] = {}
if not EVENTS_DF.empty:
    for _r in EVENTS_DF.itertuples():
        if pd.notna(_r.country) and _r.country and pd.notna(_r.location) and _r.location:
            _c = str(_r.country).strip()
            if _c.lower() not in _NL_VARIANTS:
                _city = str(_r.location).split(",")[0].strip()
                if _city and _city not in _CITY_COUNTRY_MAP:
                    _CITY_COUNTRY_MAP[_city] = _c

# Fallback: well-known non-Dutch cities without country in DB
_KNOWN_FOREIGN: dict[str, str] = {
    "Genève": "Zwitserland", "Geneva": "Zwitserland", "Genf": "Zwitserland",
    "Paris": "Frankrijk", "Parijs": "Frankrijk",
    "Heidelberg": "Duitsland", "Frankfurt": "Duitsland", "Berlin": "Duitsland",
    "Hamburg": "Duitsland", "Köln": "Duitsland", "München": "Duitsland",
    "Leuven": "België", "Brussel": "België", "Brugge": "België", "Gent": "België",
    "Londen": "Engeland", "London": "Engeland", "Oxford": "Engeland",
    "Cambridge": "Engeland", "Edinburgh": "Schotland",
    "Bologna": "Italië", "Padua": "Italië", "Padova": "Italië",
    "Rome": "Italië", "Roma": "Italië", "Florence": "Italië", "Venezia": "Italië",
    "Venetië": "Italië", "Napels": "Italië", "Naples": "Italië",
    "Straatsburg": "Frankrijk", "Strasbourg": "Frankrijk", "Lyon": "Frankrijk",
    "Bordeaux": "Frankrijk", "Montpellier": "Frankrijk",
    "Bazel": "Zwitserland", "Basel": "Zwitserland", "Zürich": "Zwitserland",
    "Bern": "Zwitserland",
    "Wien": "Oostenrijk", "Vienna": "Oostenrijk",
    "Praag": "Tsjechië", "Prague": "Tsjechië", "Praha": "Tsjechië",
    "Wittenberg": "Duitsland", "Marburg": "Duitsland", "Jena": "Duitsland",
    "Leipzig": "Duitsland", "Tübingen": "Duitsland", "Freiburg": "Duitsland",
    "Göttingen": "Duitsland", "Halle": "Duitsland", "Rostock": "Duitsland",
    "Greifswald": "Duitsland", "Kiel": "Duitsland", "Erlangen": "Duitsland",
    "Breslau": "Duitsland", "Königsberg": "Duitsland",
    "Madrid": "Spanje", "Salamanca": "Spanje",
    "Kopenhagen": "Denemarken", "Copenhagen": "Denemarken",
    "Stockholm": "Zweden", "Uppsala": "Zweden",
    "Edinburgh": "Schotland", "Glasgow": "Schotland",
    "Dublin": "Ierland",
    "Warschau": "Polen", "Cracow": "Polen", "Krakow": "Polen",
    "Konstantinopel": "Turkije", "Istanbul": "Turkije",
    "Moskou": "Rusland", "Moscow": "Rusland", "Sint-Petersburg": "Rusland",
}
for _city, _cntry in _KNOWN_FOREIGN.items():
    if _city not in _CITY_COUNTRY_MAP:
        _CITY_COUNTRY_MAP[_city] = _cntry


# Pre-computed city and country centroids (lat/lon) for the Flow Map tab
CITY_COORDS: dict[str, tuple[float, float]] = {}
if not EVENTS_DF.empty and "lat" in EVENTS_DF.columns:
    _cd = EVENTS_DF.dropna(subset=["lat", "lon", "location"]).copy()
    _cd["_city"] = _cd["location"].apply(lambda x: str(x).split(",")[0].strip())
    for _city, _grp in _cd.groupby("_city"):
        CITY_COORDS[str(_city)] = (float(_grp["lat"].mean()), float(_grp["lon"].mean()))

COUNTRY_COORDS: dict[str, tuple[float, float]] = {}
_cnt_lats: dict[str, list] = {}
_cnt_lons: dict[str, list] = {}
for _cy, (_la, _lo) in CITY_COORDS.items():
    _cn = _CITY_COUNTRY_MAP.get(_cy, "")
    if _cn:
        _cnt_lats.setdefault(_cn, []).append(_la)
        _cnt_lons.setdefault(_cn, []).append(_lo)
for _cn in _cnt_lats:
    COUNTRY_COORDS[_cn] = (
        sum(_cnt_lats[_cn]) / len(_cnt_lats[_cn]),
        sum(_cnt_lons[_cn]) / len(_cnt_lons[_cn]),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def sel_color_map(selected_ids: list) -> dict:
    return {pid: SEL_PALETTE[i % len(SEL_PALETTE)]
            for i, pid in enumerate(selected_ids or [])}


def hex_rgba(h: str, a: int = 200) -> list:
    h = h.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a]


def empty_fig(msg="No data", height=300) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        annotations=[{
            "text": msg, "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
            "font": {"size": 13, "color": "#aaa"},
        }],
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
    )
    return fig


def filter_events(year_range=None, person_ids=None) -> pd.DataFrame:
    df = EVENTS_DF.copy()
    if year_range:
        s, e = sorted(year_range)
        df = df[df["year"].between(s, e, inclusive="both")]
    if person_ids:
        df = df[df["person_id"].isin(person_ids)]
    return df


def _effective_year_range(year_range, anim: dict) -> list:
    """Return effective [start, end] taking animation state into account."""
    base_s, base_e = sorted(year_range or [YEAR_MIN, YEAR_MAX])
    if not anim or not anim.get("active"):
        return [base_s, base_e]
    yr   = int(anim.get("year", base_s))
    mode = anim.get("mode", "cumulative")
    if mode == "snapshot":
        return [yr, yr]
    if mode == "window":
        w = int(anim.get("window_size", 10))
        return [max(base_s, yr - w), yr]
    # cumulative (default)
    return [base_s, yr]


# ── Deck.gl map builder ───────────────────────────────────────────────────────

def build_map(year_range, selected_ids, visible_types=None, focus_mode="all"):
    if not HAS_DECK or EVENTS_DF.empty:
        return {}

    visible_types = set(visible_types or EVENT_COLORS.keys())
    df    = filter_events(year_range)
    s_ids = set(selected_ids or [])
    cmap  = sel_color_map(selected_ids)
    # When no one selected, focus mode has no effect
    active_focus = focus_mode if s_ids else "all"

    layers = []

    # ── Background paths (all people, low opacity) ────────────────────────────
    if active_focus != "hide":
        bg_alpha = 8 if active_focus == "dim" else 30
        bg_segs  = []
        for pid, pdf in df.groupby("person_id"):
            if int(pid) in s_ids:
                continue
            pdf = pdf.sort_values(["date", "event_order"], na_position="last")
            pts = [[float(r.lon), float(r.lat)] for r in pdf.itertuples()
                   if r.lon is not None]
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                bg_segs.append({
                    "path":        [pts[i], pts[i + 1]],
                    "color":       [120, 120, 120, bg_alpha],
                    "person_id":   int(pid),
                    "person_name": str(pdf["person_name"].iloc[0]),
                    "tooltip":     str(pdf["person_name"].iloc[0]),
                })
        if bg_segs:
            layers.append({
                "@@type": "PathLayer", "id": "bg-paths", "data": bg_segs,
                "getPath": "@@=path", "getColor": "@@=color",
                "getWidth": 2, "widthUnits": "pixels",
                "pickable": True,
            })

    # ── Selected people paths (bright, per-segment) ───────────────────────────
    for pid in s_ids:
        pdf = df[df["person_id"] == pid].sort_values(
            ["date", "event_order"], na_position="last"
        )
        pts = [[float(r.lon), float(r.lat)] for r in pdf.itertuples()
               if r.lon is not None]
        if len(pts) < 2:
            continue
        pname = str(pdf["person_name"].iloc[0]) if not pdf.empty else str(pid)
        color = hex_rgba(cmap.get(pid, "#7f8c8d"), 230)
        segs  = [{"path": [pts[i], pts[i + 1]], "color": color,
                  "person_id": pid, "person_name": pname, "tooltip": pname}
                 for i in range(len(pts) - 1)]
        if segs:
            layers.append({
                "@@type": "PathLayer", "id": f"sel-{pid}", "data": segs,
                "getPath": "@@=path", "getColor": "@@=color",
                "getWidth": 5, "widthUnits": "pixels",
                "pickable": True,
            })

    # ── Event scatter markers ─────────────────────────────────────────────────
    for etype, grp in df.groupby("event_type"):
        if etype not in visible_types:
            continue
        if active_focus == "hide":
            grp = grp[grp["person_id"].isin(s_ids)]
            if grp.empty:
                continue
        base_alpha = 20 if active_focus == "dim" else 210
        rgba = hex_rgba(EVENT_COLORS.get(etype, "#888"), base_alpha)
        data = [{
            "position":    [float(r.lon), float(r.lat)],
            "color":       hex_rgba(cmap.get(int(r.person_id), EVENT_COLORS.get(etype, "#888")), 240)
                           if int(r.person_id) in s_ids else rgba,
            "radius":      9 if int(r.person_id) in s_ids else 5,
            "person_id":   int(r.person_id),
            "person_name": str(r.person_name),
            "tooltip":     f"{r.person_name} · {etype} · {r.year or '?'} · {r.location}",
        } for r in grp.itertuples() if r.lon is not None]
        if data:
            layers.append({
                "@@type": "ScatterplotLayer",
                "id": f"ev-{etype}", "data": data,
                "getPosition": "@@=position",
                "getFillColor": "@@=color",
                "getRadius": "@@=radius",
                "radiusUnits": "pixels",
                "pickable": True, "stroked": False,
            })

    # Center view
    lats = df["lat"].dropna()
    lons = df["lon"].dropna()
    clat = float(lats.mean()) if not lats.empty else 52.0
    clon = float(lons.mean()) if not lons.empty else 10.0

    return {
        "initialViewState": {
            "latitude": clat, "longitude": clon,
            "zoom": 4, "pitch": 0, "bearing": 0,
        },
        "layers": layers,
        "mapProvider": "carto",
        "mapStyle": CARTO_STYLE,
        "views": [{"@@type": "MapView", "controller": True}],
    }


# ── Timeline chart ────────────────────────────────────────────────────────────

def build_timeline(year_range=None, selected_ids=None, focus_mode="all"):
    s, e = sorted(year_range) if year_range else (YEAR_MIN, YEAR_MAX)
    active_focus = focus_mode if selected_ids else "all"

    # ── Per-person life-event chart when professors are selected ──────────────
    if selected_ids:
        df = filter_events(year_range, selected_ids)
        if df.empty:
            return empty_fig("No events for selected professors.", 300)

        cmap         = sel_color_map(list(map(int, selected_ids)))
        pid_to_name  = {r["person_id"]: r["name"] for r in PEOPLE}
        pid_to_row   = {r["person_id"]: r          for r in PEOPLE}

        fig = go.Figure()

        # Legend anchors — one invisible trace per event type so the legend is complete
        for etype in ["birth", "education", "career", "death"]:
            fig.add_trace(go.Scatter(
                x=[], y=[], mode="markers",
                name=etype.title(), legendgroup=etype,
                marker=dict(color=EVENT_COLORS[etype], size=10),
                showlegend=True,
            ))

        for pid in map(int, selected_ids):
            color  = cmap.get(pid, SEL_PALETTE[0])
            pname  = pid_to_name.get(pid, f"Person {pid}")
            pdata  = df[df["person_id"] == pid]
            if pdata.empty:
                continue

            # Life-span bar (birth → death from PEOPLE_DF when available)
            pr = pid_to_row.get(pid, {})
            by = pr.get("birth_year"); by = int(by) if pd.notna(by) and by else None
            dy = pr.get("death_year"); dy = int(dy) if pd.notna(dy) and dy else None
            if by and dy:
                fig.add_trace(go.Scatter(
                    x=[by, dy], y=[pname, pname],
                    mode="lines",
                    line=dict(color=color, width=4, dash="dot"),
                    showlegend=False, hoverinfo="skip",
                ))

            # Event markers per type
            for etype in ["birth", "education", "career", "death"]:
                sub = pdata[pdata["event_type"] == etype].dropna(subset=["year"])
                if sub.empty:
                    continue
                fig.add_trace(go.Scatter(
                    x=sub["year"].astype(int).tolist(),
                    y=[pname] * len(sub),
                    mode="markers",
                    name=etype.title(), legendgroup=etype, showlegend=False,
                    marker=dict(
                        color=EVENT_COLORS[etype], size=11,
                        symbol="circle",
                        line=dict(color=color, width=2),
                    ),
                    customdata=sub[["location", "description"]].fillna("").values.tolist(),
                    hovertemplate=(
                        f"<b>{pname}</b><br>{etype.title()} %{{x}}<br>"
                        "%{customdata[0]}<br>%{customdata[1]}<extra></extra>"
                    ),
                ))

        n_persons = len(selected_ids)
        fig.update_layout(
            margin={"l": 8, "r": 8, "t": 40, "b": 8},
            template="plotly_white",
            title={"text": f"Life events — {n_persons} selected professor{'s' if n_persons > 1 else ''}",
                   "font": {"size": 13}, "x": 0.01},
            legend={"orientation": "h", "y": 1.12, "font": {"size": 11}},
            xaxis={"range": [s, e], "tickfont": {"size": 10}, "title": None},
            yaxis={"tickfont": {"size": 11}, "title": None, "autorange": "reversed"},
            hovermode="closest",
        )
        return fig

    # ── Stacked histogram for all professors ─────────────────────────────────
    df = filter_events(year_range, None)
    if df.empty:
        return empty_fig("No events in range.", 300)

    fig = go.Figure()
    for etype in ["birth", "education", "career", "death"]:
        sub = df[df["event_type"] == etype]
        if sub.empty:
            continue
        counts = (
            sub.groupby("year").size()
            .reindex(range(s, e + 1), fill_value=0)
            .reset_index(name="n")
        )
        fig.add_trace(go.Bar(
            x=counts["year"], y=counts["n"],
            name=etype.title(), marker_color=EVENT_COLORS[etype],
            hovertemplate=f"{etype.title()} %{{x}}: %{{y}}<extra></extra>",
        ))

    fig.update_layout(
        barmode="stack",
        margin={"l": 8, "r": 8, "t": 32, "b": 8},
        template="plotly_white",
        title={"text": "All events", "font": {"size": 13}, "x": 0.01},
        legend={"orientation": "h", "y": 1.08, "font": {"size": 11},
                "itemsizing": "constant"},
        xaxis={"tickfont": {"size": 10}, "title": None},
        yaxis={"tickfont": {"size": 10}, "title": "# events"},
        hovermode="x unified",
    )
    return fig


# ── Event bar chart ───────────────────────────────────────────────────────────

def build_event_bar(year_range):
    df = filter_events(year_range)
    if df.empty:
        return empty_fig("No events in range.", 120)

    s, e = sorted(year_range)
    fig = go.Figure()
    for etype in ["birth", "education", "career", "death"]:
        sub = df[df["event_type"] == etype]
        if sub.empty:
            continue
        counts = (sub.groupby("year").size()
                  .reindex(range(s, e + 1), fill_value=0)
                  .reset_index(name="n"))
        fig.add_trace(go.Bar(
            x=counts["year"], y=counts["n"],
            name=etype.title(),
            marker_color=EVENT_COLORS[etype],
            hovertemplate=f"{etype.title()} %{{x}}: %{{y}}<extra></extra>",
        ))

    fig.update_layout(
        barmode="stack", height=120,
        margin={"l": 4, "r": 4, "t": 4, "b": 4},
        template="plotly_white",
        legend={"orientation": "h", "y": 1.15, "font": {"size": 9}},
        xaxis={"tickfont": {"size": 8}},
        yaxis={"tickfont": {"size": 8}},
    )
    return fig


# ── Co-presence builders ─────────────────────────────────────────────────────

def _copresence_top(year_range):
    """Top 50 professor pairs by number of shared cities (no selection needed)."""
    df = filter_events(year_range)
    if df.empty:
        return []
    df = df[df["location"].str.strip() != ""].copy()
    if df.empty:
        return []

    pid_to_name = {r["person_id"]: r["name"] for r in PEOPLE}

    # Self-join on location to find pairs
    loc_cols = df[["person_id", "location"]].copy()
    joined = pd.merge(
        loc_cols.rename(columns={"person_id": "pid_a"}),
        loc_cols.rename(columns={"person_id": "pid_b"}),
        on="location",
    )
    joined = joined[joined["pid_a"] < joined["pid_b"]]
    if joined.empty:
        return []

    pair_cities = (
        joined.groupby(["pid_a", "pid_b"])["location"]
        .apply(lambda x: sorted(set(x)))
        .reset_index()
    )
    pair_cities["overlap_yrs"] = pair_cities["location"].apply(len)
    pair_cities = pair_cities.sort_values("overlap_yrs", ascending=False).head(50)

    rows = []
    for _, row in pair_cities.iterrows():
        pid_a = int(row["pid_a"])
        pid_b = int(row["pid_b"])
        cities = row["location"]
        rows.append({
            "person_a":    pid_to_name.get(pid_a, str(pid_a)),
            "person_b":    pid_to_name.get(pid_b, str(pid_b)),
            "city":        ", ".join(cities[:4]) + ("…" if len(cities) > 4 else ""),
            "years":       "",
            "overlap_yrs": len(cities),
            "pid_a":       pid_a,
            "pid_b":       pid_b,
        })
    return rows


def _copresence_one(pid: int, year_range):
    """Professors who most often shared a city with a single selected person."""
    df = filter_events(year_range)
    if df.empty:
        return []
    df = df[df["location"].str.strip() != ""].copy()
    pid_to_name = {r["person_id"]: r["name"] for r in PEOPLE}
    a_locs = set(df[df["person_id"] == pid]["location"].tolist())
    if not a_locs:
        return []
    others = df[(df["person_id"] != pid) & (df["location"].isin(a_locs))]
    if others.empty:
        return []
    pair_data = (
        others.groupby("person_id")["location"]
        .apply(lambda x: sorted(set(x)))
        .reset_index()
    )
    pair_data["overlap_yrs"] = pair_data["location"].apply(len)
    pair_data = pair_data.sort_values("overlap_yrs", ascending=False).head(50)
    rows = []
    for _, row in pair_data.iterrows():
        pid_b  = int(row["person_id"])
        cities = row["location"]
        rows.append({
            "person_a":    pid_to_name.get(pid, str(pid)),
            "person_b":    pid_to_name.get(pid_b, str(pid_b)),
            "city":        ", ".join(cities[:4]) + ("…" if len(cities) > 4 else ""),
            "years":       "",
            "overlap_yrs": len(cities),
            "pid_a":       pid,
            "pid_b":       pid_b,
        })
    return rows


def _copresence_selected(selected, year_range):
    """Co-presence for a specific set of selected professors."""
    df = filter_events(year_range, selected)
    if df.empty:
        return []
    df = df.copy()
    df["lat_r"] = df["lat"].round(3)
    df["lon_r"] = df["lon"].round(3)

    pid_to_name = {r["person_id"]: r["name"] for r in PEOPLE}
    rows = []
    ids = list(selected)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a = df[df["person_id"] == ids[i]]
            b = df[df["person_id"] == ids[j]]
            if a.empty or b.empty:
                continue
            shared = pd.merge(
                a[["lat_r", "lon_r", "location", "year"]],
                b[["lat_r", "lon_r", "year"]],
                on=["lat_r", "lon_r"],
                suffixes=("_a", "_b"),
            )
            for loc, grp in shared.groupby("location"):
                years = sorted(set(
                    grp["year_a"].dropna().tolist() +
                    grp["year_b"].dropna().tolist()
                ))
                yr_str = ", ".join(str(int(y)) for y in years[:8])
                rows.append({
                    "person_a":    pid_to_name.get(ids[i], str(ids[i])),
                    "person_b":    pid_to_name.get(ids[j], str(ids[j])),
                    "city":        str(loc),
                    "years":       yr_str,
                    "overlap_yrs": len(years),
                    "pid_a":       ids[i],
                    "pid_b":       ids[j],
                })
    return rows


# ── Sankey helpers ───────────────────────────────────────────────────────────

def _sankey_node_label(location: str, country, expanded_countries: set) -> tuple[str, bool, str]:
    """Return (label, is_country_node, resolved_country) for a location row.

    Dutch cities → (city, False, "").  Foreign cities → (country, True, country).
    Expanded countries → (city, False, country) so they show as individual nodes
    while still carrying the resolved country for downstream filtering.

    Country resolution priority:
      1. Explicit `country` column in the DB row
      2. Suffix parsed from "City, Country" location string
      3. _CITY_COUNTRY_MAP lookup (built from rows that do have country data
         plus _KNOWN_FOREIGN fallback table)
    """
    loc  = str(location) if pd.notna(location) and location else ""
    city = loc.split(",")[0].strip() if "," in loc else loc.strip()

    # Level 1: explicit column
    c = (str(country) if pd.notna(country) and country else "").strip()

    # Level 2: parse suffix from location string e.g. "Genève, Zwitserland"
    if (not c or c.lower() in _NL_VARIANTS) and "," in loc:
        suffix = loc.split(",", 1)[1].strip()
        if suffix and suffix.lower() not in _NL_VARIANTS:
            c = suffix

    # Level 3: lookup table
    if not c or c.lower() in _NL_VARIANTS:
        c = _CITY_COUNTRY_MAP.get(city, "")

    if not c or c.lower() in _NL_VARIANTS:
        return city or "Unknown", False, ""

    if c in expanded_countries:
        return city or c, False, c

    return c, True, c


# ── Network (Cytoscape) builder ──────────────────────────────────────────────

def build_network(selected_ids, year_range):
    """Return Cytoscape elements: selected people + location-sharing neighbours."""
    if not HAS_CYTO or EVENTS_DF.empty:
        return []

    s_ids = set(int(x) for x in (selected_ids or []))
    if not s_ids:
        return []

    df = filter_events(year_range)
    if df.empty:
        return []

    cmap        = sel_color_map(list(s_ids))
    pid_to_name = {r["person_id"]: r["name"] for r in PEOPLE}

    # location → set of person_ids
    loc_people = (
        df[df["location"].str.strip() != ""]
        .groupby("location")["person_id"]
        .apply(lambda x: set(int(i) for i in x))
    )

    neighbour_pids: set = set()
    raw_edges: list    = []

    for loc, pids in loc_people.items():
        if not (s_ids & pids):
            continue
        neighbour_pids |= pids
        pids_list = sorted(pids)
        for i in range(len(pids_list)):
            for j in range(i + 1, len(pids_list)):
                raw_edges.append((pids_list[i], pids_list[j], loc))

    all_pids = (s_ids | neighbour_pids) - {0}

    # Limit graph size to stay readable
    if len(all_pids) > 80:
        degree: dict = {}
        for a, b, _ in raw_edges:
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        keep = s_ids | set(
            sorted(neighbour_pids - s_ids, key=lambda p: -degree.get(p, 0))[:50]
        )
        all_pids  = keep
        raw_edges = [(a, b, loc) for a, b, loc in raw_edges
                     if a in all_pids and b in all_pids]

    nodes = [
        {"data": {
            "id":     str(pid),
            "label":  (pid_to_name.get(pid, str(pid)).split()[-1])[:16],
            "color":  cmap.get(pid, "#adb5bd"),
            "border": cmap.get(pid, "#495057") if pid in s_ids else "#ced4da",
            "bw":     3 if pid in s_ids else 1,
            "size":   32 if pid in s_ids else 20,
            "pid":    pid,
        }}
        for pid in all_pids
    ]

    seen: set  = set()
    edges: list = []
    for a, b, loc in raw_edges:
        if (a, b) not in seen:
            seen.add((a, b))
            edges.append({"data": {"source": str(a), "target": str(b),
                                   "location": loc}})

    return nodes + edges


# ── Person detail body ────────────────────────────────────────────────────────

def build_detail_body(pid: int):
    p_rows = PEOPLE_DF[PEOPLE_DF["person_id"] == pid]
    if p_rows.empty:
        return dmc.Text("Person not found.", c="dimmed")
    p = p_rows.iloc[0]

    def safe_year(v):
        return str(int(v)) if pd.notna(v) else "?"

    stats = dmc.SimpleGrid(
        cols=4,
        children=[
            dmc.Stack(gap=0, children=[
                dmc.Text("Born", size="xs", c="dimmed"),
                dmc.Text(safe_year(p["birth_year"]), fw=700, size="lg"),
                dmc.Text(p["birth_city"] or "—", size="xs"),
            ]),
            dmc.Stack(gap=0, children=[
                dmc.Text("Died", size="xs", c="dimmed"),
                dmc.Text(safe_year(p["death_year"]), fw=700, size="lg"),
                dmc.Text(p["death_city"] or "—", size="xs"),
            ]),
            dmc.Stack(gap=0, children=[
                dmc.Text("Gender", size="xs", c="dimmed"),
                dmc.Text(p["gender"] or "—", fw=600),
            ]),
            dmc.Stack(gap=0, children=[
                dmc.Text("Origin", size="xs", c="dimmed"),
                dmc.Text(p["birth_country"] or "—", fw=600),
            ]),
        ],
    )

    # Mini timeline for this person only
    timeline_fig = build_timeline(selected_ids=[pid])
    timeline_fig.update_layout(height=200, margin={"l": 4, "r": 4, "t": 4, "b": 32})

    person_events = EVENTS_DF[EVENTS_DF["person_id"] == pid].copy()
    table_data = [
        {
            "event_type":  row["event_type"],
            "year":        int(row["year"]) if pd.notna(row["year"]) else None,
            "location":    row["location"],
            "description": row["description"],
        }
        for _, row in person_events.iterrows()
    ]

    return dmc.Stack(gap="md", children=[
        stats,
        dmc.Divider(),
        dmc.Text("Life Timeline", size="sm", fw=600),
        dcc.Graph(
            id="detail-timeline",
            figure=timeline_fig,
            config={"displayModeBar": False, "responsive": True},
            style={"height": "200px"},
        ),
        dmc.Text("All Events", size="sm", fw=600),
        dag.AgGrid(
            id="detail-events-grid",
            columnDefs=[
                {"field": "event_type",  "headerName": "Type",        "flex": 1,
                 "cellStyle": {"fontWeight": "500"}},
                {"field": "year",        "headerName": "Year",        "flex": 1,
                 "type": "numericColumn"},
                {"field": "location",    "headerName": "Location",    "flex": 2},
                {"field": "description", "headerName": "Description", "flex": 3},
            ],
            rowData=table_data,
            defaultColDef={
                "sortable": True, "filter": True, "resizable": True,
                "floatingFilter": False,
            },
            dashGridOptions={"animateRows": True},
            style={"height": "280px"},
            className="ag-theme-quartz",
        ),
    ])


# ── Cytoscape stylesheet ──────────────────────────────────────────────────────

CYTO_STYLESHEET = [
    {"selector": "node", "style": {
        "background-color": "data(color)",
        "border-color": "data(border)",
        "border-width": "data(bw)",
        "width": "data(size)", "height": "data(size)",
        "label": "data(label)",
        "font-size": "9px", "text-valign": "bottom",
        "text-margin-y": "4px", "color": "#333",
        "text-outline-width": 1, "text-outline-color": "white",
    }},
    {"selector": "edge", "style": {
        "line-color": "#ccc", "width": 1.5,
        "curve-style": "bezier",
    }},
    {"selector": ":selected", "style": {
        "border-width": 4, "border-color": "#e63946",
    }},
]


# ── AG Grid column defs ───────────────────────────────────────────────────────

PEOPLE_COLS = [
    {"field": "name",       "headerName": "Name",       "flex": 2,
     "checkboxSelection": True, "headerCheckboxSelection": True},
    {"field": "birth_year", "headerName": "Born",       "flex": 1,
     "type": "numericColumn"},
    {"field": "death_year", "headerName": "Died",       "flex": 1,
     "type": "numericColumn"},
    {"field": "birth_city", "headerName": "Birthplace", "flex": 2},
    {"field": "death_city", "headerName": "Died in",    "flex": 2},
    {"field": "faculty",    "headerName": "Faculty",    "flex": 2},
]

EVENT_COLS = [
    {"field": "person_name", "headerName": "Person",      "flex": 2},
    {"field": "event_type",  "headerName": "Type",        "flex": 1,
     "cellStyle": {"fontWeight": "500"}},
    {"field": "year",        "headerName": "Year",        "flex": 1,
     "type": "numericColumn"},
    {"field": "location",    "headerName": "Location",    "flex": 2},
    {"field": "description", "headerName": "Description", "flex": 3},
]


# ── App ───────────────────────────────────────────────────────────────────────

app = Dash(
    __name__,
    external_stylesheets=[
        dmc.styles.ALL,          # Mantine CSS
    ],
    suppress_callback_exceptions=True,
)
app.title = "Life Paths — Leiden University"


# ── Layout helpers ────────────────────────────────────────────────────────────

def _selection_chips(selected_ids):
    """Coloured badge per selected person, with an info button to open detail."""
    if not selected_ids:
        return dmc.Text("None selected", size="xs", c="dimmed")
    cmap = sel_color_map(selected_ids)
    pid_to_name = {r["person_id"]: r["name"] for r in PEOPLE}
    chips = []
    for pid in selected_ids:
        name  = pid_to_name.get(pid, str(pid))
        color = cmap.get(pid, "#888")
        chips.append(
            dmc.Group(gap=4, wrap="nowrap", children=[
                dmc.Badge(
                    name, id={"type": "sel-badge", "index": pid},
                    color=color, variant="filled", size="sm",
                    style={"cursor": "pointer", "userSelect": "none"},
                ),
                dmc.ActionIcon(
                    dmc.Text("ℹ", size="xs"),
                    id={"type": "detail-btn", "index": pid},
                    variant="subtle", color="gray", size="xs",
                    style={"cursor": "pointer"},
                ),
            ])
        )
    return dmc.Stack(chips, gap=4)


def _navbar():
    nav_links = html.Div(
        style={"display": "flex", "flexDirection": "column", "gap": "4px"},
        children=[
            dcc.Link(label, href=path, style={
                "display": "block", "padding": "7px 10px",
                "borderRadius": "6px", "textDecoration": "none",
                "fontSize": "13px", "fontWeight": "500", "color": "#495057",
                "border": "1px solid #dee2e6", "backgroundColor": "#f8f9fa",
            })
            for path, label in PAGES
        ],
    )

    return html.Div(
        style={
            "position": "fixed",
            "top": "50px", "left": 0,
            "width": "260px",
            "height": "calc(100vh - 50px)",
            "overflowY": "auto",
            "borderRight": "1px solid #dee2e6",
            "backgroundColor": "#fff",
            "padding": "16px",
            "zIndex": 100,
            "boxSizing": "border-box",
        },
        children=[
            dmc.Stack(gap="lg", children=[

                # Title / subtitle
                dmc.Stack(gap=2, children=[
                    dmc.Text("Life Paths", fw=700, size="lg"),
                    dmc.Text("Leiden University · 1575–present",
                             size="xs", c="dimmed"),
                ]),

                # ── Page navigation ────────────────────────────────────────
                dmc.Stack(gap="xs", children=[
                    dmc.Text("Views", size="sm", fw=600),
                    nav_links,
                ]),

                dmc.Divider(),

                # Year range
                dmc.Stack(gap="xs", children=[
                    dmc.Text("Year range", size="sm", fw=600),
                    dmc.RangeSlider(
                        id="year-slider",
                        min=YEAR_MIN, max=YEAR_MAX,
                        value=[YEAR_MIN, YEAR_MAX],
                        step=5,
                        minRange=5,
                        marks=[
                            {"value": y, "label": str(y)}
                            for y in range(
                                (YEAR_MIN // 50) * 50,
                                YEAR_MAX + 50, 50,
                            )
                        ],
                        styles={"root": {"paddingBottom": "24px"}},
                    ),
                    dmc.Group(
                        [dmc.Text(id="year-label", size="xs", c="dimmed")],
                    ),
                ]),

                dmc.Divider(),

                # ── Animation player ───────────────────────────────────────
                dmc.Stack(gap="xs", children=[
                    dmc.Text("Animation", size="sm", fw=600),

                    # Mode selector
                    dmc.SegmentedControl(
                        id="anim-mode",
                        value="cumulative",
                        size="xs",
                        fullWidth=True,
                        data=[
                            {"value": "snapshot",   "label": "Snapshot"},
                            {"value": "cumulative", "label": "Cumulative"},
                            {"value": "window",     "label": "Window"},
                        ],
                    ),

                    # Window size (shown only in Window mode)
                    html.Div(
                        id="anim-window-row",
                        style={"display": "none"},
                        children=[
                            dmc.Text(id="anim-window-label",
                                     children="Window  (10 yrs)",
                                     size="xs", c="dimmed"),
                            dmc.Slider(
                                id="anim-window-size",
                                min=1, max=50, step=1, value=10,
                                styles={"root": {"paddingBottom": "16px"}},
                            ),
                        ],
                    ),

                    # Speed selector
                    dmc.Group(gap="xs", children=[
                        dmc.Text("Speed", size="xs", c="dimmed",
                                 style={"whiteSpace": "nowrap"}),
                        dmc.Select(
                            id="anim-speed",
                            value="800",
                            size="xs",
                            style={"flex": 1},
                            data=[
                                {"value": "2000", "label": "Slow"},
                                {"value": "800",  "label": "Normal"},
                                {"value": "400",  "label": "Fast"},
                                {"value": "150",  "label": "Very fast"},
                            ],
                        ),
                    ]),

                    # Year display
                    dmc.Text(id="anim-year-display",
                             children="–",
                             size="xs", c="dimmed", ta="center"),

                    # Play / Pause / Stop buttons
                    dmc.Group(gap="xs", justify="center", children=[
                        dmc.Button(
                            "▶ Play",
                            id="anim-play-btn",
                            size="xs",
                            variant="filled",
                            color="blue",
                            style={"flex": 1},
                        ),
                        dmc.Button(
                            "■ Stop",
                            id="anim-stop-btn",
                            size="xs",
                            variant="outline",
                            color="gray",
                            style={"flex": 1},
                        ),
                    ]),
                ]),

                dmc.Divider(),

                # Event type filter
                dmc.Stack(gap="xs", children=[
                    dmc.Text("Show event types", size="sm", fw=600),
                    dmc.CheckboxGroup(
                        id="type-filter",
                        value=list(EVENT_COLORS.keys()),
                        children=dmc.Stack(gap=4, children=[
                            dmc.Checkbox(
                                label=t.title(), value=t,
                                color=EVENT_COLORS[t],
                            )
                            for t in EVENT_COLORS
                        ]),
                    ),
                ]),

                dmc.Divider(),

                # Selection panel
                dmc.Stack(gap="xs", children=[
                    dmc.Group([
                        dmc.Text("Selected people", size="sm", fw=600),
                        dmc.ActionIcon(
                            dmc.Text("✕", size="xs"),
                            id="clear-btn",
                            variant="subtle", color="gray", size="sm",
                        ),
                    ], justify="space-between"),
                    html.Div(id="selection-chips"),

                    # Focus mode — only meaningful when people are selected
                    dmc.Stack(gap=4, children=[
                        dmc.Text("Others", size="xs", c="dimmed"),
                        dmc.SegmentedControl(
                            id="focus-mode",
                            value="all",
                            size="xs",
                            fullWidth=True,
                            data=[
                                {"value": "all",  "label": "Show"},
                                {"value": "dim",  "label": "Dim"},
                                {"value": "hide", "label": "Hide"},
                            ],
                        ),
                    ]),
                ]),

                dmc.Divider(),

                # Quick-search dropdown
                dmc.Stack(gap="xs", children=[
                    dmc.Text("Quick-select", size="sm", fw=600),
                    dmc.MultiSelect(
                        id="person-search",
                        data=[{"value": str(r["person_id"]), "label": r["name"]}
                              for r in PEOPLE],
                        placeholder="Search by name…",
                        searchable=True,
                        clearable=True,
                        maxDropdownHeight=200,
                        value=[],
                    ),
                ]),

            ]),
        ],
    )  # end html.Div navbar


GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}

# Panel builder functions — called lazily from the tab-content callback
# so only the active tab's components are ever in the DOM.

def _panel_map():
    deck_init = {
        "initialViewState": {"latitude": 52.0, "longitude": 10.0,
                             "zoom": 4, "pitch": 0, "bearing": 0},
        "layers": [],
        "views": [{"@@type": "MapView", "controller": True}],
    }
    map_el = (
        dash_deck.DeckGL(
            id="life-map",
            mapboxKey="",
            enableEvents=["click"],
            tooltip={
                "html": "{tooltip}",
                "style": {"background": "rgba(255,255,255,.95)",
                          "color": "#222", "fontSize": "12px",
                          "padding": "6px 10px", "borderRadius": "4px",
                          "boxShadow": "0 2px 6px rgba(0,0,0,.2)"},
            },
            data=deck_init,
            style={"height": "100%", "width": "100%"},
        )
        if HAS_DECK else
        dcc.Graph(id="life-map", config=GRAPH_CONFIG, style={"height": "100%"})
    )
    return html.Div(
        style={"display": "flex", "flexDirection": "column",
               "height": "calc(100vh - 74px)"},
        children=[
            dcc.Graph(id="event-bar", config=GRAPH_CONFIG,
                      style={"height": "110px", "flexShrink": 0}),
            html.Div(
                style={"position": "relative", "flex": 1, "overflow": "hidden"},
                children=[map_el],
            ),
        ],
    )


def _panel_timeline():
    return html.Div([
        html.P(
            "Events per year, stacked by type. "
            "Select people in the sidebar to filter to their events only.",
            style={"fontSize": "12px", "color": "#868e96", "margin": "0 0 6px"},
        ),
        dcc.Graph(
            id="timeline-chart",
            config=GRAPH_CONFIG,
            style={"height": "calc(100vh - 160px)"},
        ),
    ])


def _panel_network():
    if HAS_CYTO:
        return cyto.Cytoscape(
            id="network-graph",
            layout={"name": "cose", "animate": False,
                    "nodeRepulsion": 8000, "idealEdgeLength": 80},
            stylesheet=CYTO_STYLESHEET,
            elements=[],
            style={"height": "calc(100vh - 160px)", "width": "100%",
                   "border": "1px solid #e9ecef", "borderRadius": "8px"},
            responsive=True,
        )
    return html.P("Install dash-cytoscape to enable the network view.",
                  style={"color": "#888", "padding": "24px"})


def _panel_copresence():
    return html.Div([
        html.P(
            "Top 50 professor pairs by shared cities. "
            "Select 2+ professors in the sidebar to filter to that pair.",
            style={"fontSize": "13px", "color": "#666", "margin": "0 0 8px"},
        ),
        dag.AgGrid(
            id="copresence-grid",
            columnDefs=[
                {"field": "person_a",    "headerName": "Person A",      "flex": 2},
                {"field": "person_b",    "headerName": "Person B",      "flex": 2},
                {"field": "city",        "headerName": "City",          "flex": 2},
                {"field": "years",       "headerName": "Overlap years", "flex": 3},
                {"field": "overlap_yrs", "headerName": "# yrs",         "flex": 1,
                 "type": "numericColumn"},
            ],
            rowData=[],
            defaultColDef={"sortable": True, "filter": True, "resizable": True,
                           "floatingFilter": True},
            dashGridOptions={"rowSelection": "single", "animateRows": True},
            style={"height": "calc(100vh - 200px)"},
            className="ag-theme-quartz",
        ),
    ])


def _panel_people():
    return html.Div(
        style={"display": "flex", "flexDirection": "column",
               "height": "calc(100vh - 74px)"},
        children=[
            dag.AgGrid(
                id="people-grid",
                columnDefs=PEOPLE_COLS,
                rowData=PEOPLE_DF.to_dict("records") if not PEOPLE_DF.empty else [],
                defaultColDef={
                    "sortable": True, "filter": True,
                    "resizable": True, "floatingFilter": True,
                },
                dashGridOptions={"rowSelection": "multiple", "animateRows": True},
                style={"flex": 1, "width": "100%"},
                className="ag-theme-quartz",
            ),
        ],
    )


# ── Sankey (Life Flows) builder ──────────────────────────────────────────────

def build_sankey(year_range, selected_ids, faculty_filter,
                 event_type_filter, top_n, min_flow,
                 expanded_countries, trace_node,
                 color_mode, arrangement, focus_mode="all"):
    yr = sorted(year_range or [YEAR_MIN, YEAR_MAX])
    expanded_countries = set(expanded_countries or [])
    selected_ids = list(map(int, selected_ids or []))
    active_focus = focus_mode if selected_ids else "all"

    # 1. Filter events
    df = filter_events(yr, None)
    if faculty_filter:
        df = df[df["faculty"].isin(faculty_filter)]
    if event_type_filter:
        df = df[df["event_type"].isin(event_type_filter)]
    if df.empty:
        return empty_fig("No events in range."), [], {}, {}

    df = df.dropna(subset=["location"]).copy()

    # 2. Apply node labels (country clustering); track which labels are country nodes
    _label_results = df.apply(
        lambda r: _sankey_node_label(r["location"], r["country"], expanded_countries),
        axis=1,
    )
    df["node"]           = _label_results.apply(lambda t: t[0])
    _is_country_node     = _label_results.apply(lambda t: t[1])
    _country_node_labels = set(df.loc[_is_country_node, "node"].unique())
    # City nodes unlocked by expanding a country — must survive the top-N cut
    _expanded_city_nodes = {t[0] for t in _label_results if not t[1] and t[2] in expanded_countries}

    # 3. Build per-person sequential flows
    raw_flows = []   # list of dicts: {from, to, person_id, person_name, faculty, year_from}
    for pid, pdf in df.sort_values(["date", "event_order"], na_position="last").groupby("person_id"):
        pdf = pdf.reset_index(drop=True)
        pname = str(pdf["person_name"].iloc[0])
        fac   = str(pdf["faculty"].iloc[0]) if pd.notna(pdf["faculty"].iloc[0]) else ""
        nodes = pdf["node"].tolist()
        years = pdf["year"].tolist()
        for i in range(len(nodes) - 1):
            src, tgt = nodes[i], nodes[i+1]
            if src and tgt and src != tgt:
                raw_flows.append({
                    "from": src, "to": tgt,
                    "person_id": int(pid), "person_name": pname,
                    "faculty": fac,
                    "year_from": int(years[i]) if pd.notna(years[i]) else yr[0],
                })

    if not raw_flows:
        return empty_fig("No location transitions found."), [], {}, {}

    flows_df = pd.DataFrame(raw_flows)

    # When hiding unselected: restrict diagram to selected people's flows only
    if active_focus == "hide" and selected_ids:
        flows_df = flows_df[flows_df["person_id"].isin(selected_ids)]
        if flows_df.empty:
            return empty_fig("No flows for selected people."), [], {}, {}

    flows_agg = flows_df.groupby(["from", "to"]).size().reset_index(name="count")

    # 4. Top-N nodes by total volume
    vol = (
        pd.concat([
            flows_agg[["from","count"]].rename(columns={"from":"city"}),
            flows_agg[["to","count"]].rename(columns={"to":"city"}),
        ])
        .groupby("city")["count"].sum()
        .nlargest(int(top_n or 20))
    )
    top_nodes = set(vol.index)

    # Always include selected people's nodes
    if selected_ids:
        sel_rows = flows_df[flows_df["person_id"].isin(selected_ids)]
        top_nodes |= set(sel_rows["from"]) | set(sel_rows["to"])

    # Always include every city from an expanded country, regardless of volume
    top_nodes |= _expanded_city_nodes

    flows_agg = flows_agg[flows_agg["from"].isin(top_nodes) & flows_agg["to"].isin(top_nodes)]
    flows_df  = flows_df [flows_df ["from"].isin(top_nodes) & flows_df ["to"].isin(top_nodes)]

    # 5. Min-flow threshold
    if min_flow and min_flow > 1:
        keep = flows_agg[flows_agg["count"] >= min_flow][["from","to"]]
        flows_agg = flows_agg[flows_agg["count"] >= min_flow]
        flows_df  = flows_df.merge(keep, on=["from","to"])

    if flows_agg.empty:
        return empty_fig("No flows meet the minimum threshold."), [], {}, {}

    # 6. Identify country-cluster nodes (derived from labeling step, not DB column)
    all_in_diagram = set(flows_agg["from"]) | set(flows_agg["to"])
    country_nodes  = sorted(all_in_diagram & _country_node_labels)

    # 7. Node ordering: sources-only → both → sinks-only (alphabetical within each group)
    src_set    = set(flows_agg["from"])
    tgt_set    = set(flows_agg["to"])
    both_set   = src_set & tgt_set
    node_order = (sorted(src_set - both_set) + sorted(both_set) + sorted(tgt_set - src_set))
    node_idx   = {n: i for i, n in enumerate(node_order)}

    # 8. Node colours
    def _node_color(n):
        if "leiden" in n.lower():
            return "#c0392b"
        if n in country_nodes:
            return "#6c757d"
        return "#457b9d"
    node_colors = [_node_color(n) for n in node_order]

    # 9. Node hover text
    pid_to_name = {r["person_id"]: r["name"] for r in PEOPLE}
    def _node_hover(n):
        pids = set(flows_df[flows_df["from"] == n]["person_id"].tolist() +
                   flows_df[flows_df["to"]   == n]["person_id"].tolist())
        names = sorted(pid_to_name.get(p, str(p)) for p in pids)
        snippet = "<br>".join(f"• {nm}" for nm in names[:10])
        more    = f"<br>… +{len(names)-10}" if len(names) > 10 else ""
        expand_hint = "<br><i>Click to expand</i>" if n in country_nodes else "<br><i>Click to trace flows</i>"
        return f"<b>{n}</b> ({len(pids)} professors)<br>{snippet}{more}{expand_hint}"
    node_hover = [_node_hover(n) for n in node_order]

    # 10. Link colors
    def _hex_rgba_str(h, alpha):
        h2 = h.lstrip("#")
        r,g,b = int(h2[0:2],16), int(h2[2:4],16), int(h2[4:6],16)
        return f"rgba({r},{g},{b},{alpha})"

    sel_set  = set(selected_ids)
    cmap     = sel_color_map(selected_ids)

    def _link_active(src, tgt):
        return not trace_node or src == trace_node or tgt == trace_node

    link_src, link_tgt, link_val, link_col, link_custom = [], [], [], [], []

    def _add_link(src, tgt, val, col, display=""):
        si, ti = node_idx[src], node_idx[tgt]
        link_src.append(si); link_tgt.append(ti)
        link_val.append(val); link_col.append(col)
        # customdata: [index_key, src_label, tgt_label, display_label]
        link_custom.append([f"{si}-{ti}", src, tgt, display])

    if color_mode == "faculty":
        fac_flows = flows_df.groupby(["from","to","faculty"]).size().reset_index(name="count")
        for _, r in fac_flows.iterrows():
            src, tgt = r["from"], r["to"]
            if src not in node_idx or tgt not in node_idx: continue
            alpha   = 0.55 if _link_active(src, tgt) else 0.05
            fac_col = FACULTY_COLORS.get(r["faculty"], FACULTY_COLORS_DEFAULT)
            _add_link(src, tgt, int(r["count"]), _hex_rgba_str(fac_col, alpha),
                      f" ({r['faculty']})")
    elif sel_set:
        unsel = flows_df[~flows_df["person_id"].isin(sel_set)].groupby(["from","to"]).size().reset_index(name="count")
        for _, r in unsel.iterrows():
            src, tgt = r["from"], r["to"]
            if src not in node_idx or tgt not in node_idx: continue
            alpha = (0.02 if active_focus == "dim" else 0.12) if _link_active(src, tgt) else 0.01
            _add_link(src, tgt, int(r["count"]), f"rgba(180,180,180,{alpha})")
        sel_pf = flows_df[flows_df["person_id"].isin(sel_set)].groupby(["from","to","person_id"]).size().reset_index(name="count")
        for _, r in sel_pf.iterrows():
            src, tgt = r["from"], r["to"]
            if src not in node_idx or tgt not in node_idx: continue
            alpha = 0.85 if _link_active(src, tgt) else 0.06
            _add_link(src, tgt, int(r["count"]),
                      _hex_rgba_str(cmap.get(int(r["person_id"]), "#e63946"), alpha))
    else:
        for _, r in flows_agg.iterrows():
            src, tgt = r["from"], r["to"]
            if src not in node_idx or tgt not in node_idx: continue
            alpha = 0.35 if _link_active(src, tgt) else 0.05
            _add_link(src, tgt, int(r["count"]), f"rgba(69,123,157,{alpha})")

    # 11. Build figure
    fig = go.Figure(go.Sankey(
        arrangement=arrangement or "snap",
        node=dict(
            label=node_order,
            color=node_colors,
            customdata=node_hover,
            hovertemplate="%{customdata}<extra></extra>",
            pad=16, thickness=20,
        ),
        link=dict(
            source=link_src, target=link_tgt,
            value=link_val, color=link_col,
            customdata=link_custom,
            hovertemplate="%{source.label} → %{target.label}: %{value} moves%{customdata[3]}<extra></extra>",
        ),
    ))

    title_parts = [f"Life flows — top {len(node_order)} nodes"]
    if trace_node:  title_parts.append(f"tracing: {trace_node}")
    if selected_ids: title_parts.append(f"{len(selected_ids)} highlighted")
    if expanded_countries: title_parts.append(f"expanded: {', '.join(sorted(expanded_countries))}")

    fig.update_layout(
        title=dict(text="  |  ".join(title_parts), font=dict(size=12)),
        template="plotly_white",
        margin={"l": 10, "r": 10, "t": 50, "b": 10},
        height=max(550, len(node_order) * 38),
        font=dict(size=11),
    )

    # 12. Build detail dicts for click callbacks
    flow_detail = {}
    for (src, tgt), grp in flows_df.groupby(["from","to"]):
        if src not in node_idx or tgt not in node_idx:
            continue
        key     = f"{node_idx[src]}-{node_idx[tgt]}"
        entries = grp.sort_values("year_from")[["person_id","person_name","year_from"]].drop_duplicates()
        flow_detail[key] = {
            "labels": [
                f"{row['person_name']} ({int(row['year_from'])})"
                for _, row in entries.head(30).iterrows()
            ],
            "pids": sorted({int(p) for p in grp["person_id"].tolist()}),
        }

    node_detail = {}
    for n in node_order:
        pids = sorted(set(
            flows_df[flows_df["from"]==n]["person_id"].tolist() +
            flows_df[flows_df["to"]  ==n]["person_id"].tolist()
        ))
        node_detail[n] = {
            "labels": sorted(pid_to_name.get(p, str(p)) for p in pids),
            "pids":   [int(p) for p in pids],
        }

    return fig, country_nodes, flow_detail, node_detail


# ── Flow Map builder ──────────────────────────────────────────────────────────

def _arc_points(lat1: float, lon1: float, lat2: float, lon2: float,
                n: int = 40, curvature: float = 0.22):
    """Quadratic bezier arc between two lat/lon points, offset perpendicularly."""
    t    = np.linspace(0, 1, n)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    dist = (dlat**2 + dlon**2) ** 0.5 or 1
    mlat = (lat1 + lat2) / 2 + curvature * (-dlon / dist)
    mlon = (lon1 + lon2) / 2 + curvature * (dlat  / dist)
    lats = (1-t)**2 * lat1 + 2*(1-t)*t * mlat + t**2 * lat2
    lons = (1-t)**2 * lon1 + 2*(1-t)*t * mlon + t**2 * lon2
    return lats.tolist(), lons.tolist()


def _empty_flowmap(msg: str = "No flows in range"):
    """Mapbox figure with carto-positron base but no data traces."""
    fig = go.Figure()
    fig.update_layout(
        map=dict(style="carto-positron", center=dict(lat=52.2, lon=5.3), zoom=4),
        margin=dict(l=0, r=0, t=40, b=0),
        height=600,
        showlegend=False,
        uirevision="flowmap",
        annotations=[dict(
            text=msg, xref="paper", yref="paper",
            x=0.5, y=0.05, showarrow=False,
            font=dict(size=13, color="#555"),
            bgcolor="rgba(255,255,255,0.75)",
            bordercolor="#ccc", borderwidth=1, borderpad=6,
        )],
    )
    return fig


def build_flowmap(year_range, selected_ids, faculty_filter, event_type_filter,
                  top_n, min_flow, expanded_countries, color_mode, focus_mode="all"):
    yr = sorted(year_range or [YEAR_MIN, YEAR_MAX])
    expanded_countries = set(expanded_countries or [])
    selected_ids = list(map(int, selected_ids or []))
    active_focus = focus_mode if selected_ids else "all"

    # 1. Filter events
    df = filter_events(yr, None)
    if faculty_filter:
        df = df[df["faculty"].isin(faculty_filter)]
    if event_type_filter:
        df = df[df["event_type"].isin(event_type_filter)]
    if df.empty:
        return _empty_flowmap("No events in range."), [], {}

    df = df.dropna(subset=["location"]).copy()

    # 2. Node labels + country tracking (shared logic with Sankey)
    _lr = df.apply(
        lambda r: _sankey_node_label(r["location"], r["country"], expanded_countries), axis=1
    )
    df["node"]           = _lr.apply(lambda t: t[0])
    _is_country_node     = _lr.apply(lambda t: t[1])
    _country_node_labels = set(df.loc[_is_country_node, "node"].unique())
    _expanded_city_nodes = {t[0] for t in _lr if not t[1] and t[2] in expanded_countries}

    # 3. Per-person sequential flows
    raw_flows = []
    for pid, pdf in df.sort_values(["date", "event_order"], na_position="last").groupby("person_id"):
        pdf   = pdf.reset_index(drop=True)
        pname = str(pdf["person_name"].iloc[0])
        fac   = str(pdf["faculty"].iloc[0]) if pd.notna(pdf["faculty"].iloc[0]) else ""
        nodes = pdf["node"].tolist()
        years = pdf["year"].tolist()
        for i in range(len(nodes) - 1):
            src, tgt = nodes[i], nodes[i+1]
            if src and tgt and src != tgt:
                raw_flows.append({
                    "from": src, "to": tgt,
                    "person_id": int(pid), "person_name": pname, "faculty": fac,
                    "year_from": int(years[i]) if pd.notna(years[i]) else yr[0],
                })
    if not raw_flows:
        return _empty_flowmap("No location transitions found."), [], {}

    flows_df = pd.DataFrame(raw_flows)

    # When hiding unselected: restrict to selected people's flows only
    if active_focus == "hide" and selected_ids:
        flows_df = flows_df[flows_df["person_id"].isin(selected_ids)]
        if flows_df.empty:
            return _empty_flowmap("No flows for selected people."), [], {}

    flows_agg = flows_df.groupby(["from", "to"]).size().reset_index(name="count")

    # 4. Top-N + expand override
    vol = (
        pd.concat([
            flows_agg[["from","count"]].rename(columns={"from":"city"}),
            flows_agg[["to","count"]].rename(columns={"to":"city"}),
        ]).groupby("city")["count"].sum().nlargest(int(top_n or 20))
    )
    top_nodes = set(vol.index)
    if selected_ids:
        sr = flows_df[flows_df["person_id"].isin(selected_ids)]
        top_nodes |= set(sr["from"]) | set(sr["to"])
    top_nodes |= _expanded_city_nodes

    flows_agg = flows_agg[flows_agg["from"].isin(top_nodes) & flows_agg["to"].isin(top_nodes)]
    flows_df  = flows_df [flows_df ["from"].isin(top_nodes) & flows_df ["to"].isin(top_nodes)]

    # 5. Min-flow threshold
    if min_flow and min_flow > 1:
        keep      = flows_agg[flows_agg["count"] >= min_flow][["from","to"]]
        flows_agg = flows_agg[flows_agg["count"] >= min_flow]
        flows_df  = flows_df.merge(keep, on=["from","to"])

    if flows_agg.empty:
        return _empty_flowmap("No flows meet the minimum threshold."), [], {}

    all_in_diagram = set(flows_agg["from"]) | set(flows_agg["to"])
    country_nodes  = sorted(all_in_diagram & _country_node_labels)

    # 6. Node coordinates (city centroid or country centroid)
    def _get_coords(n):
        return COUNTRY_COORDS.get(n) if n in country_nodes else CITY_COORDS.get(n)

    node_coords = {n: _get_coords(n) for n in all_in_diagram}
    has_coords  = {n for n, c in node_coords.items() if c is not None}
    flows_agg   = flows_agg[flows_agg["from"].isin(has_coords) & flows_agg["to"].isin(has_coords)]
    flows_df    = flows_df [flows_df ["from"].isin(has_coords) & flows_df ["to"].isin(has_coords)]

    if flows_agg.empty:
        return _empty_flowmap("No flows with known coordinates."), [], {}

    all_in_diagram = set(flows_agg["from"]) | set(flows_agg["to"])
    pid_to_name    = {r["person_id"]: r["name"] for r in PEOPLE}
    sel_set        = set(selected_ids)
    cmap           = sel_color_map(selected_ids)
    max_count      = int(flows_agg["count"].max()) or 1

    # 7. Arc traces (one per flow)
    flow_detail = {}
    arc_traces  = []

    for _, row in flows_agg.sort_values("count", ascending=False).iterrows():
        src, tgt, cnt = row["from"], row["to"], int(row["count"])
        slat, slon = node_coords[src]
        tlat, tlon = node_coords[tgt]
        arc_lats, arc_lons = _arc_points(slat, slon, tlat, tlon)

        # V-shaped arrowhead appended to the same trace so it inherits colour/width.
        # Placed at ~75 % along the arc so it's clear of the destination node.
        _n  = len(arc_lats)
        _hi = int(0.75 * (_n - 1))
        _bi = max(0, _hi - 4)
        _hlat, _hlon = arc_lats[_hi], arc_lons[_hi]
        _dlat = _hlat - arc_lats[_bi]
        _dlon = _hlon - arc_lons[_bi]
        _dist = (_dlat**2 + _dlon**2) ** 0.5 or 1e-9
        _ux, _uy = _dlat / _dist, _dlon / _dist   # unit tangent
        _px, _py = -_uy, _ux                        # perpendicular
        _arc_len = ((tlat - slat)**2 + (tlon - slon)**2) ** 0.5
        _wing = max(0.10, min(0.45, _arc_len * 0.14))
        _w1lat = _hlat - _wing * _ux + _wing * 0.45 * _px
        _w1lon = _hlon - _wing * _uy + _wing * 0.45 * _py
        _w2lat = _hlat - _wing * _ux - _wing * 0.45 * _px
        _w2lon = _hlon - _wing * _uy - _wing * 0.45 * _py
        arc_lats = arc_lats + [None, _w1lat, _hlat, None, _w2lat, _hlat]
        arc_lons = arc_lons + [None, _w1lon, _hlon, None, _w2lon, _hlon]

        fkey = f"{src}|{tgt}"
        grp  = flows_df[(flows_df["from"] == src) & (flows_df["to"] == tgt)]
        entries = grp.sort_values("year_from")[["person_id","person_name","year_from"]].drop_duplicates()
        flow_detail[fkey] = {
            "labels": [f"{r['person_name']} ({int(r['year_from'])})"
                       for _, r in entries.head(30).iterrows()],
            "pids":   sorted({int(p) for p in grp["person_id"].tolist()}),
        }

        # Colour
        if color_mode == "faculty":
            fc = grp["faculty"].value_counts()
            dom = fc.index[0] if not fc.empty else ""
            color = FACULTY_COLORS.get(dom, FACULTY_COLORS_DEFAULT)
        elif sel_set and not grp[grp["person_id"].isin(sel_set)].empty:
            pid_in_flow = next(iter(set(grp["person_id"].tolist()) & sel_set))
            color = cmap.get(pid_in_flow, "#e63946")
        else:
            color = "#457b9d"

        alpha = 0.10 + 0.75 * (cnt / max_count) ** 0.5
        if sel_set and grp[grp["person_id"].isin(sel_set)].empty:
            alpha *= (0.04 if active_focus == "dim" else 0.2)
        h = color.lstrip("#")
        rgba = f"rgba({int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)},{alpha:.2f})"
        lw   = 1.5 + 6 * (cnt / max_count) ** 0.5

        cd = [fkey, src, tgt, cnt]
        arc_traces.append(go.Scattermap(
            lat=arc_lats, lon=arc_lons,
            mode="lines",
            line=dict(width=lw, color=rgba),
            hovertemplate=(
                f"<b>{src} → {tgt}</b><br>"
                f"{cnt} move{'s' if cnt!=1 else ''}<br>"
                "<i>Click to select</i><extra></extra>"
            ),
            customdata=[cd] * len(arc_lats),
            showlegend=False,
        ))

    # 8. Node marker trace
    node_list = sorted(all_in_diagram)
    node_lats = [node_coords[n][0] for n in node_list]
    node_lons = [node_coords[n][1] for n in node_list]

    vol_map = (
        pd.concat([
            flows_agg[["from","count"]].rename(columns={"from":"node"}),
            flows_agg[["to","count"]].rename(columns={"to":"node"}),
        ]).groupby("node")["count"].sum()
    )
    vmax = vol_map.max() or 1
    node_sizes = [6 + 18 * (vol_map.get(n, 1) / vmax) ** 0.5 for n in node_list]

    def _node_col(n):
        if "leiden" in n.lower(): return "#c0392b"
        if n in country_nodes:   return "#6c757d"
        return "#457b9d"
    node_colors = [_node_col(n) for n in node_list]

    node_detail = {}
    pids_per_node = {}
    for n in node_list:
        pids = sorted(set(
            flows_df[flows_df["from"] == n]["person_id"].tolist() +
            flows_df[flows_df["to"]   == n]["person_id"].tolist()
        ))
        pids_per_node[n] = pids
        node_detail[n] = {
            "labels": sorted(pid_to_name.get(p, str(p)) for p in pids),
            "pids":   [int(p) for p in pids],
        }

    node_hover = [
        f"<b>{n}</b><br>"
        f"{int(vol_map.get(n, 0))} total moves · "
        f"{len(pids_per_node[n])} professor{'s' if len(pids_per_node[n])!=1 else ''}"
        + ("<br><i>Click to expand</i>" if n in country_nodes else "<br><i>Click to select professors</i>")
        for n in node_list
    ]

    node_trace = go.Scattermap(
        lat=node_lats, lon=node_lons,
        mode="markers+text",
        marker=dict(size=node_sizes, color=node_colors, opacity=0.92),
        text=node_list,
        textposition="top right",
        textfont=dict(size=9, color="#333"),
        hovertemplate=[h + "<extra></extra>" for h in node_hover],
        customdata=[{"type": "node", "label": n} for n in node_list],
        showlegend=False,
    )

    # 9. Assemble figure
    all_lats = [c[0] for n in all_in_diagram if (c := node_coords.get(n))]
    all_lons = [c[1] for n in all_in_diagram if (c := node_coords.get(n))]
    center   = dict(lat=sum(all_lats)/len(all_lats), lon=sum(all_lons)/len(all_lons))

    title_parts = [f"Flow map — top {len(all_in_diagram)} locations"]
    if selected_ids:        title_parts.append(f"{len(selected_ids)} highlighted")
    if expanded_countries:  title_parts.append(f"expanded: {', '.join(sorted(expanded_countries))}")

    fig = go.Figure(data=arc_traces + [node_trace])
    fig.update_layout(
        title=dict(text="  |  ".join(title_parts), font=dict(size=12)),
        map=dict(style="carto-positron", center=center, zoom=4),
        margin=dict(l=0, r=0, t=40, b=0),
        height=600,
        showlegend=False,
        hovermode="closest",
        uirevision="flowmap",
    )

    detail = {"flows": flow_detail, "nodes": node_detail}
    return fig, country_nodes, detail


# ── Space-Time Cube builder ───────────────────────────────────────────────────

CAMERA_PRESETS = {
    "3d":   {"eye": {"x": 1.4, "y": 1.4, "z": 1.1}},
    "map":  {"eye": {"x": 0,   "y": 0,   "z": 2.5}, "up": {"x": 0, "y": 1, "z": 0}},
    "tlon": {"eye": {"x": 2.5, "y": 0,   "z": 0.3}},
    "tlat": {"eye": {"x": 0,   "y": -2.5,"z": 0.3}},
}

# Marker shape and size per event type
_CUBE_SYMBOLS = {
    "birth":     "circle",
    "education": "diamond",
    "career":    "square",
    "death":     "cross",
}
_CUBE_SIZES = {
    "birth":     7,
    "education": 5,
    "career":    5,
    "death":     8,
}


def build_cube(year_range, selected_ids, color_mode, type_filter,
               show_proj, show_copresence, camera_preset, focus_mode="all"):
    yr = sorted(year_range or [YEAR_MIN, YEAR_MAX])
    cube_df = filter_events(yr, None)
    if cube_df.empty:
        return empty_fig("No data in range.")

    cube_df = cube_df.dropna(subset=["lat", "lon", "year"]).copy()
    cube_df["year_int"] = cube_df["year"].astype(int)

    s_ids        = set(selected_ids or [])
    cmap         = sel_color_map(selected_ids)
    floor_z      = yr[0] - 10
    active_focus = focus_mode if s_ids else "all"

    fig = go.Figure()

    # ── Layer A — background paths (unselected, batched with None separators) ──
    xs_bg, ys_bg, zs_bg = [], [], []
    ht_bg = []  # hover text per point (None for gap points)
    for pid, pdf in cube_df[~cube_df["person_id"].isin(s_ids)].groupby("person_id"):
        pdf   = pdf.sort_values(["date", "event_order"], na_position="last")
        pname = str(pdf["person_name"].iloc[0])
        yr0   = int(pdf["year_int"].min())
        yr1   = int(pdf["year_int"].max())
        hover = f"<b>{pname}</b><br>{yr0}–{yr1}"
        if len(pdf) < 2:
            continue
        xs_bg  += list(pdf["lon"])   + [None]
        ys_bg  += list(pdf["lat"])   + [None]
        zs_bg  += list(pdf["year_int"]) + [None]
        ht_bg  += [hover] * len(pdf) + [None]
    if xs_bg and active_focus != "hide":
        bg_opacity = 0.07 if active_focus == "dim" else 0.30
        fig.add_trace(go.Scatter3d(
            x=xs_bg, y=ys_bg, z=zs_bg,
            mode="lines",
            line={"color": "#adb5bd", "width": 1.5},
            opacity=bg_opacity,
            text=ht_bg,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
            name="Paths",
        ))

    # ── Layer B — floor projection shadows ────────────────────────────────────
    if show_proj and active_focus != "hide":
        xs_fl, ys_fl, zs_fl = [], [], []
        for pid, pdf in cube_df[~cube_df["person_id"].isin(s_ids)].groupby("person_id"):
            pdf = pdf.sort_values(["date", "event_order"], na_position="last")
            if len(pdf) < 2:
                continue
            xs_fl += list(pdf["lon"])  + [None]
            ys_fl += list(pdf["lat"])  + [None]
            zs_fl += [floor_z] * len(pdf) + [None]
        if xs_fl:
            proj_opacity = 0.04 if active_focus == "dim" else 0.12
            fig.add_trace(go.Scatter3d(
                x=xs_fl, y=ys_fl, z=zs_fl,
                mode="lines",
                line={"color": "#95a5a6", "width": 1},
                opacity=proj_opacity,
                hoverinfo="none",
                showlegend=False,
                name="_proj",
            ))

    # ── Layer B2 — vertical pillars (event → floor) ───────────────────────────
    if show_proj and active_focus != "hide":
        xs_pl, ys_pl, zs_pl = [], [], []
        for _, row in cube_df[~cube_df["person_id"].isin(s_ids)].iterrows():
            xs_pl += [row["lon"], row["lon"], None]
            ys_pl += [row["lat"], row["lat"], None]
            zs_pl += [row["year_int"], floor_z,     None]
        if xs_pl:
            pill_opacity = 0.05 if active_focus == "dim" else 0.18
            fig.add_trace(go.Scatter3d(
                x=xs_pl, y=ys_pl, z=zs_pl,
                mode="lines",
                line={"color": "#ced4da", "width": 0.8},
                opacity=pill_opacity,
                hoverinfo="none",
                showlegend=False,
                name="_pillars",
            ))

    # ── Layer C — event markers (distinct shape per event type) ───────────────
    type_filter = list(type_filter or EVENT_COLORS.keys())
    for etype in type_filter:
        sub = cube_df[cube_df["event_type"] == etype]
        if sub.empty:
            continue

        sym = _CUBE_SYMBOLS.get(etype, "circle")
        sz  = _CUBE_SIZES.get(etype, 5)

        # Split into selected vs unselected for independent opacity
        for is_sel in ([True, False] if s_ids else [False]):
            if is_sel:
                chunk = sub[sub["person_id"].isin(s_ids)]
            else:
                if active_focus == "hide":
                    continue
                chunk = sub[~sub["person_id"].isin(s_ids)]
            if chunk.empty:
                continue

            if color_mode == "event":
                mc = EVENT_COLORS.get(etype, "#888")
            elif color_mode == "faculty":
                mc = [FACULTY_COLORS.get(fac, FACULTY_COLORS_DEFAULT)
                      for fac in chunk["faculty"]]
            else:
                mc = [cmap.get(int(p), "#aaa") for p in chunk["person_id"]]

            opacity = 0.85 if is_sel else (0.12 if active_focus == "dim" else 0.82)
            fig.add_trace(go.Scatter3d(
                x=chunk["lon"], y=chunk["lat"], z=chunk["year_int"],
                mode="markers",
                marker=dict(
                    size=sz, symbol=sym, color=mc,
                    opacity=opacity,
                    line=dict(color="white", width=0.5),
                ),
                text=chunk["person_name"] + " · " + chunk["location"],
                customdata=chunk["person_id"],
                hovertemplate=(
                    f"<b>{etype.title()}</b><br>"
                    "%{text}<br>%{z}<extra></extra>"
                ),
                name=etype.title(),
                legendgroup=etype,
                showlegend=not is_sel,
            ))

    # ── Layer D — selected professor paths ────────────────────────────────────
    for pid in selected_ids or []:
        pdf = cube_df[cube_df["person_id"] == pid].sort_values(
            ["date", "event_order"], na_position="last"
        )
        if pdf.empty:
            continue
        color = cmap.get(int(pid), "#7f8c8d")
        pname = str(pdf["person_name"].iloc[0])

        # Shared-location keys for other selected people (gold segment highlight)
        others_df = cube_df[cube_df["person_id"].isin(s_ids - {pid})]
        loc_keys  = set(zip(others_df["lat"].round(3), others_df["lon"].round(3)))

        # ── D1: path line (segment-batched, with gold co-presence highlight) ──
        if len(pdf) >= 2:
            pts = list(pdf[["lon", "lat", "year_int"]].itertuples(index=False, name=None))
            gold    = "#f1c40f"
            batches: dict = {}
            for i in range(len(pts) - 1):
                a, b  = pts[i], pts[i + 1]
                a_key = (round(float(a[1]), 3), round(float(a[0]), 3))
                b_key = (round(float(b[1]), 3), round(float(b[0]), 3))
                seg_c = gold if (a_key in loc_keys and b_key in loc_keys) else color
                batches.setdefault(seg_c, ([], [], []))
                batches[seg_c][0].extend([a[0], b[0], None])
                batches[seg_c][1].extend([a[1], b[1], None])
                batches[seg_c][2].extend([a[2], b[2], None])

            first = True
            for seg_c, (bx, by, bz) in batches.items():
                fig.add_trace(go.Scatter3d(
                    x=bx, y=by, z=bz,
                    mode="lines",
                    line={"color": seg_c, "width": 5 if seg_c != gold else 7},
                    opacity=1.0,
                    hoverinfo="none",
                    showlegend=first,
                    legendgroup=f"sel-{pid}",
                    name=pname,
                ))
                first = False

        # ── D2: vertical pillars for this person ─────────────────────────────
        if show_proj:
            xp, yp, zp = [], [], []
            for _, row in pdf.iterrows():
                xp += [row["lon"], row["lon"], None]
                yp += [row["lat"], row["lat"], None]
                zp += [row["year_int"], floor_z, None]
            fig.add_trace(go.Scatter3d(
                x=xp, y=yp, z=zp,
                mode="lines",
                line={"color": color, "width": 1},
                opacity=0.35,
                hoverinfo="none",
                showlegend=False,
                legendgroup=f"sel-{pid}",
            ))

        # ── D3: event markers + year labels on the path ───────────────────────
        hover_pts = [
            f"<b>{pname}</b><br>{et.title()} · {yr}<br>{loc}"
            for et, yr, loc in zip(
                pdf["event_type"], pdf["year_int"], pdf["location"]
            )
        ]
        fig.add_trace(go.Scatter3d(
            x=pdf["lon"], y=pdf["lat"], z=pdf["year_int"],
            mode="markers+text",
            marker=dict(
                size=[_CUBE_SIZES.get(et, 5) * 1.8 for et in pdf["event_type"]],
                symbol=[_CUBE_SYMBOLS.get(et, "circle") for et in pdf["event_type"]],
                color=color,
                opacity=1.0,
                line=dict(color="white", width=1.5),
            ),
            text=[str(y) for y in pdf["year_int"]],
            textposition="top center",
            textfont=dict(size=9, color=color),
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hover_pts,
            showlegend=False,
            legendgroup=f"sel-{pid}",
        ))

    # ── Layer E — co-presence markers ─────────────────────────────────────────
    if show_copresence:
        cube_df["_lonr"] = cube_df["lon"].round(3)
        cube_df["_latr"] = cube_df["lat"].round(3)
        grp = cube_df.groupby(["_lonr", "_latr", "year_int"])
        co_rows = []
        for (lon_r, lat_r, yr_i), g in grp:
            pids_here = g["person_id"].unique()
            if len(pids_here) >= 2:
                names = ", ".join(sorted(g["person_name"].unique()))
                co_rows.append({
                    "lon": lon_r, "lat": lat_r, "year": yr_i,
                    "n": len(pids_here), "names": names,
                })
        if co_rows:
            co_df = pd.DataFrame(co_rows)
            fig.add_trace(go.Scatter3d(
                x=co_df["lon"], y=co_df["lat"], z=co_df["year"],
                mode="markers",
                marker=dict(
                    symbol="diamond",
                    size=(co_df["n"] * 3).clip(upper=20).tolist(),
                    color="#f1c40f",
                    opacity=0.9,
                    line=dict(color="#e67e22", width=1),
                ),
                text=co_df["names"],
                hovertemplate="Co-presence: %{text}<extra></extra>",
                name="Co-presence",
                showlegend=True,
            ))

    # ── Layout ─────────────────────────────────────────────────────────────────
    fig.update_layout(
        template="plotly_white",
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        uirevision=camera_preset,
        scene=dict(
            xaxis=dict(title="Longitude",  gridcolor="#e9ecef", showbackground=True,
                       backgroundcolor="#f8f9fa"),
            yaxis=dict(title="Latitude",   gridcolor="#e9ecef", showbackground=True,
                       backgroundcolor="#f1f3f5"),
            zaxis=dict(title="Year",       gridcolor="#e9ecef", showbackground=True,
                       backgroundcolor="#f8f9fa"),
            bgcolor="#fff",
            aspectmode="manual",
            aspectratio={"x": 1.2, "y": 1.0, "z": 1.4},
            camera=CAMERA_PRESETS.get(camera_preset, CAMERA_PRESETS["3d"]),
        ),
        legend=dict(orientation="h", y=-0.02, font={"size": 11}),
    )
    return fig


def build_cube_map(year_range, selected_ids, focus_mode="all"):
    """2D Scattergeo companion map for the Space-Time Cube."""
    df = filter_events(year_range)
    s_ids = set(map(int, selected_ids or []))
    active_focus = focus_mode if s_ids else "all"
    cmap = sel_color_map(list(s_ids))

    fig = go.Figure()

    # Unselected background events
    if active_focus != "hide":
        bg = df[~df["person_id"].isin(s_ids)].dropna(subset=["lat", "lon"])
        if not bg.empty:
            opacity = 0.12 if active_focus == "dim" else 0.40
            fig.add_trace(go.Scattergeo(
                lat=bg["lat"], lon=bg["lon"],
                mode="markers",
                marker=dict(size=4, color="#adb5bd", opacity=opacity),
                hoverinfo="skip",
                showlegend=False,
                name="",
            ))

    # Selected person paths
    for pid in s_ids:
        pdf = df[df["person_id"] == pid].dropna(subset=["lat", "lon"]).sort_values("year")
        if pdf.empty:
            continue
        color = cmap.get(int(pid), "#7f8c8d")
        name_val = pdf.iloc[0].get("name", f"Person {pid}") if "name" in pdf.columns else f"Person {pid}"
        # Path line
        fig.add_trace(go.Scattergeo(
            lat=pdf["lat"], lon=pdf["lon"],
            mode="lines+markers",
            line=dict(color=color, width=1.5),
            marker=dict(
                size=[_CUBE_SIZES.get(str(et), 5) * 1.4 for et in pdf["event_type"]],
                color=color,
                symbol="circle",
                opacity=0.85,
            ),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "%{customdata[1]} · %{customdata[2]}<br>"
                "%{lat:.2f}, %{lon:.2f}<extra></extra>"
            ),
            customdata=list(zip(
                pdf["name"].fillna(f"Person {pid}") if "name" in pdf.columns else [f"Person {pid}"] * len(pdf),
                pdf["event_type"].fillna(""),
                pdf["year"].fillna("").astype(str),
            )),
            name=name_val,
            showlegend=True,
        ))

    # Geo layout
    lats = df["lat"].dropna()
    lons = df["lon"].dropna()
    if not lats.empty:
        pad = 3.0
        lat_min, lat_max = float(lats.min()) - pad, float(lats.max()) + pad
        lon_min, lon_max = float(lons.min()) - pad, float(lons.max()) + pad
    else:
        lat_min, lat_max, lon_min, lon_max = 40, 65, -10, 30

    fig.update_layout(
        geo=dict(
            showland=True, landcolor="#f0ede8",
            showocean=True, oceancolor="#d6eaf8",
            showcoastlines=True, coastlinecolor="#aaa",
            showcountries=True, countrycolor="#ccc",
            showlakes=True, lakecolor="#d6eaf8",
            showrivers=True, rivercolor="#d6eaf8",
            projection_type="mercator",
            lataxis_range=[lat_min, lat_max],
            lonaxis_range=[lon_min, lon_max],
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        height=210,
        showlegend=bool(s_ids),
        legend=dict(orientation="h", y=1.02, font=dict(size=10)),
        uirevision="cube-geomap",
    )
    return fig


def _panel_cube():
    return html.Div(style={"display": "flex", "flexDirection": "column",
                            "height": "calc(100vh - 74px)", "gap": "6px"}, children=[
        # Controls bar
        html.Div(style={
            "display": "flex", "flexWrap": "wrap", "gap": "20px",
            "alignItems": "center", "padding": "6px 4px",
            "borderBottom": "1px solid #e9ecef", "flexShrink": 0,
        }, children=[
            # Color mode
            dmc.Stack(gap=2, children=[
                dmc.Text("Colour by", size="xs", c="dimmed"),
                dmc.SegmentedControl(
                    id="cube-color-mode", value="event", size="xs",
                    data=[{"value": "event",   "label": "Event type"},
                          {"value": "person",  "label": "Person"},
                          {"value": "faculty", "label": "Faculty"}],
                ),
            ]),
            # Event type filter
            dmc.Stack(gap=2, children=[
                dmc.Text("Show events", size="xs", c="dimmed"),
                dmc.CheckboxGroup(
                    id="cube-type-filter",
                    value=list(EVENT_COLORS.keys()),
                    children=dmc.Group(gap=8, children=[
                        dmc.Checkbox(label=t.title(), value=t,
                                     color=EVENT_COLORS[t], size="xs")
                        for t in EVENT_COLORS
                    ]),
                ),
            ]),
            # Camera presets
            dmc.Stack(gap=2, children=[
                dmc.Text("View", size="xs", c="dimmed"),
                dmc.SegmentedControl(
                    id="cube-camera", value="3d", size="xs",
                    data=[{"value": "3d",   "label": "3D"},
                          {"value": "map",  "label": "Map"},
                          {"value": "tlon", "label": "Time×Lat"},
                          {"value": "tlat", "label": "Time×Lon"}],
                ),
            ]),
            # Toggles
            dmc.Stack(gap=4, children=[
                dmc.Switch(id="cube-projections", checked=True,
                           label="Floor shadows", size="xs"),
                dmc.Switch(id="cube-copresence", checked=False,
                           label="Co-presence", size="xs"),
            ]),
            # Stats
            dmc.Text(id="cube-stats", size="xs", c="dimmed"),
        ]),
        # 3D chart
        dcc.Graph(
            id="cube-chart",
            config={"displayModeBar": True,
                    "modeBarButtonsToRemove": ["toImage"],
                    "scrollZoom": True, "responsive": True},
            style={"flex": 1},
        ),
        # 2D geographic companion map
        dcc.Graph(
            id="cube-geomap",
            config=GRAPH_CONFIG,
            style={"height": "220px", "flexShrink": 0},
        ),
    ])


def _panel_sankey():
    faculties = sorted(PEOPLE_DF["faculty"].dropna().unique().tolist())
    return html.Div(
        style={"display":"flex","flexDirection":"column","height":"calc(100vh - 74px)","gap":"4px"},
        children=[
            # ── Controls bar ──────────────────────────────────────────────
            html.Div(style={
                "display":"flex","flexWrap":"wrap","gap":"20px","alignItems":"flex-end",
                "padding":"6px 4px","borderBottom":"1px solid #e9ecef","flexShrink":0,
            }, children=[
                dmc.Stack(gap=2, children=[
                    dmc.Text("Event types in flow", size="xs", c="dimmed"),
                    dmc.CheckboxGroup(
                        id="sankey-event-filter",
                        value=list(EVENT_COLORS.keys()),
                        children=dmc.Group(gap=8, children=[
                            dmc.Checkbox(label=t.title(), value=t, color=EVENT_COLORS[t], size="xs")
                            for t in EVENT_COLORS
                        ]),
                    ),
                ]),
                dmc.Stack(gap=2, children=[
                    dmc.Text("Faculty", size="xs", c="dimmed"),
                    dmc.MultiSelect(
                        id="sankey-faculty-filter", data=faculties,
                        placeholder="All faculties", searchable=True,
                        clearable=True, value=[], maxDropdownHeight=200,
                        style={"minWidth":"160px"},
                    ),
                ]),
                dmc.Stack(gap=2, children=[
                    dmc.Text(id="sankey-topn-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="sankey-top-n", value=20, min=5, max=60, step=5,
                        marks=[{"value":v,"label":str(v)} for v in [5,20,40,60]],
                        style={"width":"160px"},
                    ),
                ]),
                dmc.Stack(gap=2, children=[
                    dmc.Text(id="sankey-minflow-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="sankey-min-flow", value=1, min=1, max=20, step=1,
                        marks=[{"value":v,"label":str(v)} for v in [1,5,10,20]],
                        style={"width":"130px"},
                    ),
                ]),
                dmc.Stack(gap=2, children=[
                    dmc.Text("Colour links", size="xs", c="dimmed"),
                    dmc.SegmentedControl(
                        id="sankey-color-mode", value="uniform", size="xs",
                        data=[{"value":"uniform","label":"Uniform"},
                              {"value":"faculty","label":"Faculty"}],
                    ),
                ]),
                dmc.Stack(gap=2, children=[
                    dmc.Text("Layout", size="xs", c="dimmed"),
                    dmc.SegmentedControl(
                        id="sankey-arrangement", value="snap", size="xs",
                        data=[{"value":"snap",        "label":"Snap"},
                              {"value":"perpendicular","label":"Perp"},
                              {"value":"fixed",        "label":"Fixed"}],
                    ),
                ]),
                dmc.Stack(gap=2, children=[
                    dmc.Text("Expand countries", size="xs", c="dimmed"),
                    dmc.MultiSelect(
                        id="sankey-expand-select",
                        value=[], data=[],
                        placeholder="Select country to expand…",
                        clearable=True, searchable=True,
                        style={"minWidth": "180px"},
                    ),
                ]),
                dmc.Button("✕ Clear trace", id="sankey-clear-btn", size="xs",
                           variant="subtle", color="gray"),
            ]),
            # ── Sankey chart (scrollable) ─────────────────────────────────
            html.Div(style={"flex":1,"overflowY":"auto","overflowX":"hidden"}, children=[
                dcc.Graph(
                    id="sankey-chart",
                    config={"displayModeBar":True,"modeBarButtonsToRemove":["toImage"],
                            "responsive":True},
                    style={"minHeight":"500px"},
                ),
            ]),
            # ── Info panel ────────────────────────────────────────────────
            html.Div(
                id="sankey-info",
                style={"flexShrink":0,"maxHeight":"160px","overflowY":"auto",
                       "padding":"6px 8px","borderTop":"1px solid #e9ecef",
                       "fontSize":"12px","color":"#495057","backgroundColor":"#f8f9fa"},
                children="Click a node or link to see details.",
            ),
        ],
    )


def _panel_flowmap():
    faculties = sorted({r["faculty"] for r in PEOPLE if r.get("faculty")})
    return html.Div(
        style={"display":"flex","flexDirection":"column","height":"100%"},
        children=[
            html.Div(
                style={"display":"flex","flexWrap":"wrap","gap":"12px",
                       "padding":"8px 12px","borderBottom":"1px solid #e9ecef",
                       "background":"#f8f9fa","alignItems":"flex-end","flexShrink":0},
                children=[
                    dmc.Stack(gap=2, children=[
                        dmc.Text("Event types", size="xs", c="dimmed"),
                        dmc.CheckboxGroup(
                            id="flowmap-event-filter",
                            value=list(EVENT_COLORS.keys()),
                            children=dmc.Group(gap=8, children=[
                                dmc.Checkbox(label=t.title(), value=t, color=EVENT_COLORS[t], size="xs")
                                for t in EVENT_COLORS
                            ]),
                        ),
                    ]),
                    dmc.Stack(gap=2, children=[
                        dmc.Text("Faculty", size="xs", c="dimmed"),
                        dmc.MultiSelect(
                            id="flowmap-faculty-filter", data=faculties,
                            placeholder="All faculties", searchable=True,
                            clearable=True, value=[], maxDropdownHeight=200,
                            style={"minWidth":"160px"},
                        ),
                    ]),
                    dmc.Stack(gap=2, children=[
                        dmc.Text(id="flowmap-topn-label", size="xs", c="dimmed"),
                        dmc.Slider(
                            id="flowmap-top-n", value=20, min=5, max=60, step=5,
                            marks=[{"value":v,"label":str(v)} for v in [5,20,40,60]],
                            style={"width":"160px"},
                        ),
                    ]),
                    dmc.Stack(gap=2, children=[
                        dmc.Text(id="flowmap-minflow-label", size="xs", c="dimmed"),
                        dmc.Slider(
                            id="flowmap-min-flow", value=1, min=1, max=20, step=1,
                            marks=[{"value":v,"label":str(v)} for v in [1,5,10,20]],
                            style={"width":"130px"},
                        ),
                    ]),
                    dmc.Stack(gap=2, children=[
                        dmc.Text("Colour", size="xs", c="dimmed"),
                        dmc.SegmentedControl(
                            id="flowmap-color-mode", value="uniform", size="xs",
                            data=[{"value":"uniform","label":"Uniform"},
                                  {"value":"faculty","label":"Faculty"}],
                        ),
                    ]),
                    dmc.Stack(gap=2, children=[
                        dmc.Text("Expand countries", size="xs", c="dimmed"),
                        dmc.MultiSelect(
                            id="flowmap-expand-select",
                            value=[], data=[],
                            placeholder="Select to expand…",
                            clearable=True, searchable=True,
                            style={"minWidth":"180px"},
                        ),
                    ]),
                ],
            ),
            html.Div(style={"flex":1,"overflow":"hidden"}, children=[
                dcc.Graph(
                    id="flowmap-chart",
                    config={"displayModeBar":True,"responsive":True},
                    style={"height":"600px","minHeight":"500px"},
                ),
            ]),
            html.Div(
                id="flowmap-info",
                style={"flexShrink":0,"maxHeight":"160px","overflowY":"auto",
                       "padding":"6px 8px","borderTop":"1px solid #e9ecef",
                       "fontSize":"12px","color":"#495057","backgroundColor":"#f8f9fa"},
                children="Click a flow arc or city node to see details.",
            ),
        ],
    )


# ── Experimental Flow Map ────────────────────────────────────────────────────
#
# Techniques (all toggleable):
#   • Line type       — Straight / Cubic-Bézier / Great-circle geodesic
#   • Edge bundling   — Off / FDEB (Holten & van Wijk 2009) / Hierarchical (faculty hubs)
#   • Directed arrows — dot marker at destination
#   • Semantic zoom   — manual slider or auto-adjust from map viewport
#   • Color           — uniform / faculty / count gradient


def _great_circle_pts(lat1, lon1, lat2, lon2, n=50):
    """Points along the great-circle arc between two coordinates."""
    la1, lo1, la2, lo2 = np.radians([lat1, lon1, lat2, lon2])
    cos_d = np.clip(np.sin(la1)*np.sin(la2) + np.cos(la1)*np.cos(la2)*np.cos(lo2-lo1), -1, 1)
    d = np.arccos(cos_d)
    if d < 1e-9:
        return [lat1]*n, [lon1]*n
    t  = np.linspace(0, 1, n)
    A  = np.sin((1-t)*d) / np.sin(d)
    B  = np.sin(t*d)     / np.sin(d)
    x  = A*np.cos(la1)*np.cos(lo1) + B*np.cos(la2)*np.cos(lo2)
    y  = A*np.cos(la1)*np.sin(lo1) + B*np.cos(la2)*np.sin(lo2)
    z  = A*np.sin(la1)              + B*np.sin(la2)
    return (np.degrees(np.arctan2(z, np.sqrt(x**2+y**2))).tolist(),
            np.degrees(np.arctan2(y, x)).tolist())


def _bezier_pts(lat1, lon1, lat2, lon2, n=50, curvature=0.22):
    """
    Cubic Bézier arc: two control points placed at 1/3 and 2/3 of the chord,
    offset perpendicularly by a fraction of the chord length.
    Produces smoother, more geographic-looking curves than the quadratic version.
    """
    t    = np.linspace(0, 1, n)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    dist = (dlat**2 + dlon**2)**0.5 or 1
    perp_lat, perp_lon = -dlon / dist, dlat / dist      # CCW perpendicular unit vector
    off = curvature * dist
    c1lat = lat1 + dlat / 3 + off * perp_lat
    c1lon = lon1 + dlon / 3 + off * perp_lon
    c2lat = lat1 + 2*dlat / 3 + off * 0.85 * perp_lat
    c2lon = lon1 + 2*dlon / 3 + off * 0.85 * perp_lon
    lats = (1-t)**3*lat1 + 3*(1-t)**2*t*c1lat + 3*(1-t)*t**2*c2lat + t**3*lat2
    lons = (1-t)**3*lon1 + 3*(1-t)**2*t*c1lon + 3*(1-t)*t**2*c2lon + t**3*lon2
    return lats.tolist(), lons.tolist()


def _fdeb(edge_endpoints, strength=0.5, n_cycles=3, n_iter=15, ks=0.05):
    """
    Holten & van Wijk (2009) Force-Directed Edge Bundling.

    Algorithm:
      1. Pre-compute pairwise compatibility: angle × scale × position.
      2. Start with edges as straight 2-point segments.
      3. Each cycle: subdivide (double interior points) then run n_iter force steps.
         Spring forces hold adjacent control points together (prevents collapse).
         Electrostatic forces pull compatible edges toward each other.
      4. Endpoints are always kept fixed.

    edge_endpoints : [(slat, slon, dlat, dlon), ...]
    strength       : 0-1 global bundling pull multiplier
    n_cycles       : subdivision cycles (more → tighter bundles, slower)
    n_iter         : gradient-descent steps per cycle
    ks             : spring stiffness constant
    """
    n = len(edge_endpoints)
    if n < 2 or strength < 0.01:
        return [(np.linspace(ep[0], ep[2], 9).tolist(),
                 np.linspace(ep[1], ep[3], 9).tolist())
                for ep in edge_endpoints]

    eps   = np.array(edge_endpoints, dtype=float)      # (n, 4)
    s_pts = eps[:, :2]                                  # (n, 2) starts
    d_pts = eps[:, 2:]                                  # (n, 2) ends
    vecs  = d_pts - s_pts                               # (n, 2)
    lens  = np.linalg.norm(vecs, axis=1).clip(1e-9)    # (n,)
    dirs  = vecs / lens[:, None]                        # (n, 2) unit dirs
    mids  = (s_pts + d_pts) / 2                         # (n, 2) midpoints

    # ── Compatibility matrix (angle × scale × position) ──────────────────────
    Ca   = np.abs(dirs @ dirs.T)                        # (n, n) — |cos θ|
    lavg = (lens[:, None] + lens[None, :]) / 2
    lmin = np.minimum(lens[:, None], lens[None, :])
    lmax = np.maximum(lens[:, None], lens[None, :])
    Cs   = 2 / (lavg / lmin + lavg / lmax)             # harmonic scale ratio
    d_m  = np.linalg.norm(mids[:, None, :] - mids[None, :, :], axis=2)
    Cp   = lavg / (lavg + d_m)                         # position closeness
    C    = Ca * Cs * Cp
    np.fill_diagonal(C, 0)
    C[C < 0.4] = 0                                      # prune weak pairs

    # ── Initialize: (n, 2, 2) — just start and end ───────────────────────────
    pts   = np.stack([s_pts, d_pts], axis=1).copy().astype(float)
    step0 = 0.04 * strength

    for cycle in range(n_cycles):
        # Subdivide: linear interpolation doubles interior resolution
        n_old = pts.shape[1]
        t_new = np.linspace(0, 1, n_old * 2 - 1)
        t_old = np.linspace(0, 1, n_old)
        new_lat = np.array([np.interp(t_new, t_old, pts[i, :, 0]) for i in range(n)])
        new_lon = np.array([np.interp(t_new, t_old, pts[i, :, 1]) for i in range(n)])
        pts     = np.stack([new_lat, new_lon], axis=2)  # (n, n_ctrl, 2)

        cur_step = step0 / (2 ** cycle)
        cur_ks   = ks   / (2 ** cycle)

        for _ in range(n_iter):
            F = np.zeros_like(pts)
            # Spring: pull each interior point toward both neighbors
            F[:, 1:-1] += cur_ks * (
                pts[:, :-2, :] - pts[:, 1:-1, :] +
                pts[:,  2:, :] - pts[:, 1:-1, :]
            )
            # Electrostatic: compatible edges pull corresponding control points
            for i in range(n):
                w = C[i]
                mask = w > 0
                if not mask.any():
                    continue
                wk     = w[mask][:, None, None]        # (k, 1, 1)
                pull   = (wk * (pts[mask] - pts[i])).sum(axis=0)  # (n_ctrl, 2)
                F[i, 1:-1] += pull[1:-1]
            pts[:, 1:-1] += cur_step * F[:, 1:-1]

    return [(pts[i, :, 0].tolist(), pts[i, :, 1].tolist()) for i in range(n)]


def _hierarchical_bundle(edge_endpoints, faculty_list, faculty_centroids, strength=0.5):
    """
    Hierarchical edge bundling via faculty geographic hubs.

    Each flow is re-routed as a quadratic Bézier through the centroid of all
    events belonging to the flow's dominant faculty.  Flows with the same
    faculty naturally converge; strength blends between direct and hub-routed.

    faculty_centroids : {faculty_name: (lat, lon)}
    """
    t     = np.linspace(0, 1, 50)
    paths = []
    for (slat, slon, dlat, dlon), fac in zip(edge_endpoints, faculty_list):
        hub = faculty_centroids.get(fac) if fac else None
        if hub is None:
            paths.append(_bezier_pts(slat, slon, dlat, dlon))
            continue
        hlat, hlon = hub
        hub_lats = (1-t)**2 * slat + 2*(1-t)*t * hlat + t**2 * dlat
        hub_lons = (1-t)**2 * slon + 2*(1-t)*t * hlon + t**2 * dlon
        if strength < 1.0:
            dl, dln = np.array(_bezier_pts(slat, slon, dlat, dlon))
            hub_lats = strength * hub_lats + (1 - strength) * dl
            hub_lons = strength * hub_lons + (1 - strength) * dln
        paths.append((hub_lats.tolist(), hub_lons.tolist()))
    return paths


def _viewport_semantic_pct(relayout_data):
    """Derive an automatic semantic threshold from the current map viewport span."""
    if not relayout_data:
        return None
    lat_r = relayout_data.get("geo.lataxis.range")
    lon_r = relayout_data.get("geo.lonaxis.range")
    if not (lat_r and lon_r):
        return None
    span = max(abs(lat_r[1] - lat_r[0]), abs(lon_r[1] - lon_r[0]))
    # Zoomed out (span≥60°) → 70% threshold; zoomed in (span≤5°) → 0%
    return max(0, min(70, int((span - 5) * 70 / 55)))


TEMPORAL_PALETTE = ["#3a86ff", "#4cc9f0", "#06d6a0", "#ffd166", "#ef476f"]


def _temporal_color(bin_idx, n_bins):
    """Interpolated color across TEMPORAL_PALETTE for a given temporal bin index."""
    t  = bin_idx / max(n_bins - 1, 1)
    p  = t * (len(TEMPORAL_PALETTE) - 1)
    lo = int(p)
    hi = min(lo + 1, len(TEMPORAL_PALETTE) - 1)
    f  = p - lo
    def _h(h): return [int(h.lstrip("#")[i:i+2], 16) for i in (0, 2, 4)]
    c0, c1 = _h(TEMPORAL_PALETTE[lo]), _h(TEMPORAL_PALETTE[hi])
    r, g, b = (int(c0[i] + f*(c1[i]-c0[i])) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def _temporal_bundle(edge_endpoints, year_per_flow, n_bins=5, strength=0.5):
    """
    Temporal edge bundling: run FDEB independently within each time period.

    Flows in different eras are never merged together, so temporal patterns
    (e.g. early-career vs late-career migration routes) are preserved.
    Each returned path belongs to a numbered bin used for color encoding.

    Returns (paths, bin_indices).
    """
    n = len(edge_endpoints)
    if n == 0:
        return [], []

    y       = np.array(year_per_flow, dtype=float)
    y_min, y_max = y.min(), y.max()
    span    = max(y_max - y_min, 1)
    bin_idx = np.floor((y - y_min) / span * n_bins).clip(0, n_bins - 1).astype(int)

    all_paths = [None] * n
    for b in range(n_bins):
        sel = np.where(bin_idx == b)[0]
        if len(sel) == 0:
            continue
        sub_eps   = [edge_endpoints[i] for i in sel]
        sub_paths = _fdeb(sub_eps, strength=strength, n_cycles=2, n_iter=10)
        for j, i in enumerate(sel):
            all_paths[i] = sub_paths[j]

    for i in range(n):
        if all_paths[i] is None:
            ep = edge_endpoints[i]
            all_paths[i] = (np.linspace(ep[0], ep[2], 9).tolist(),
                            np.linspace(ep[1], ep[3], 9).tolist())

    return all_paths, bin_idx.tolist()


def build_expflowmap(year_range, selected_ids,
                     line_type="bezier",
                     bundling_mode="off", bundling_strength=0.5,
                     show_arrows=True,
                     semantic_pct=0,
                     top_n=20, min_flow=1,
                     color_mode="uniform",
                     faculty_filter=None,
                     expanded_countries=None,
                     n_time_bins=5):
    yr = sorted(year_range or [YEAR_MIN, YEAR_MAX])
    selected_ids = list(map(int, selected_ids or []))
    sel_set      = set(selected_ids)
    cmap         = sel_color_map(selected_ids)

    # ── 1. Filter events and build flows ─────────────────────────────────────
    df = filter_events(yr)
    if faculty_filter:
        df = df[df["faculty"].isin(faculty_filter)]
    df = df.dropna(subset=["location"]).copy()
    if df.empty:
        return empty_fig("No events in range.", 600), []

    # Node labels with country expansion support
    expanded_set    = set(expanded_countries or [])
    _lr             = df.apply(lambda r: _sankey_node_label(r["location"], r["country"], expanded_set), axis=1)
    df["node"]      = _lr.apply(lambda t: t[0])
    _country_labels = {t[0] for t in _lr if t[1]}

    raw_flows = []
    for pid, pdf in df.sort_values(["date","event_order"], na_position="last").groupby("person_id"):
        pdf   = pdf.reset_index(drop=True)
        pname = str(pdf["person_name"].iloc[0])
        fac   = str(pdf["faculty"].iloc[0]) if pd.notna(pdf["faculty"].iloc[0]) else ""
        nodes = pdf["node"].tolist()
        years = pdf["year"].tolist()
        for i in range(len(nodes)-1):
            s, t_ = nodes[i], nodes[i+1]
            if s and t_ and s != t_:
                raw_flows.append({"from": s, "to": t_,
                                   "person_id": int(pid), "person_name": pname,
                                   "faculty": fac,
                                   "year_from": int(years[i]) if pd.notna(years[i]) else yr[0]})
    if not raw_flows:
        return empty_fig("No location transitions found.", 600), []

    flows_df  = pd.DataFrame(raw_flows)
    flows_agg = (
        flows_df.groupby(["from", "to"])
        .agg(count=("person_id", "count"), year_med=("year_from", "median"))
        .reset_index()
    )

    # ── 2. Top-N + min-flow ──────────────────────────────────────────────────
    vol = (pd.concat([
        flows_agg[["from","count"]].rename(columns={"from":"city"}),
        flows_agg[["to","count"]].rename(columns={"to":"city"}),
    ]).groupby("city")["count"].sum().nlargest(int(top_n or 20)))
    top_nodes = set(vol.index)
    if selected_ids:
        sr = flows_df[flows_df["person_id"].isin(selected_ids)]
        top_nodes |= set(sr["from"]) | set(sr["to"])

    flows_agg = flows_agg[flows_agg["from"].isin(top_nodes) & flows_agg["to"].isin(top_nodes)]
    if min_flow and min_flow > 1:
        flows_agg = flows_agg[flows_agg["count"] >= min_flow]
    if flows_agg.empty:
        return empty_fig("No flows after filtering.", 600), []

    # ── 3. Semantic threshold ────────────────────────────────────────────────
    if semantic_pct and semantic_pct > 0:
        thresh = flows_agg["count"].max() * (semantic_pct / 100)
        flows_agg = flows_agg[flows_agg["count"] >= thresh]
    if flows_agg.empty:
        return empty_fig("All flows below semantic threshold.", 600), []

    # ── 4. Resolve node coordinates ──────────────────────────────────────────
    all_nodes     = set(flows_agg["from"]) | set(flows_agg["to"])
    country_nodes = sorted(all_nodes & _country_labels)
    node_coords   = {n: CITY_COORDS.get(n) or COUNTRY_COORDS.get(n) for n in all_nodes}
    valid         = {n for n, c in node_coords.items() if c is not None}
    flows_agg     = flows_agg[flows_agg["from"].isin(valid) & flows_agg["to"].isin(valid)]
    if flows_agg.empty:
        return empty_fig("No flows with known coordinates.", 600), []

    max_cnt = int(flows_agg["count"].max()) or 1

    # ── 5. Compute raw paths ─────────────────────────────────────────────────
    edge_endpoints, raw_paths = [], []
    for _, row in flows_agg.iterrows():
        slat, slon = node_coords[row["from"]]
        dlat, dlon = node_coords[row["to"]]
        edge_endpoints.append((slat, slon, dlat, dlon))

        if line_type == "straight":
            lats = np.linspace(slat, dlat, 50).tolist()
            lons = np.linspace(slon, dlon, 50).tolist()
        elif line_type == "greatcircle":
            lats, lons = _great_circle_pts(slat, slon, dlat, dlon)
        else:                                     # bezier (default)
            lats, lons = _bezier_pts(slat, slon, dlat, dlon)
        raw_paths.append((lats, lons))

    # ── 6. Optional edge bundling ─────────────────────────────────────────────
    bstrength = float(bundling_strength or 0.5)

    if bundling_mode == "fdeb":
        # Full Holten & van Wijk FDEB with compatibility scoring and subdivision
        final_paths = _fdeb(edge_endpoints, strength=bstrength, n_cycles=3, n_iter=15)
        temporal_bin_indices = []

    elif bundling_mode == "hier":
        # Hierarchical: route each flow through its faculty's geographic centroid
        fac_evts = df.dropna(subset=["lat", "lon", "faculty"])
        fac_centroids = (
            fac_evts.groupby("faculty")[["lat", "lon"]]
            .mean()
            .apply(lambda r: (float(r["lat"]), float(r["lon"])), axis=1)
            .to_dict()
        )
        # Dominant faculty for each aggregated flow
        fac_per_flow = []
        for _, row in flows_agg.iterrows():
            grp = flows_df[(flows_df["from"] == row["from"]) & (flows_df["to"] == row["to"])]
            dom = grp["faculty"].value_counts()
            fac_per_flow.append(dom.index[0] if not dom.empty else "")
        final_paths = _hierarchical_bundle(
            edge_endpoints, fac_per_flow, fac_centroids, strength=bstrength
        )
        temporal_bin_indices = []

    elif bundling_mode == "temporal":
        # Temporal: bundle flows within each time period independently
        year_per_flow = flows_agg["year_med"].fillna(yr[0]).astype(int).tolist()
        final_paths, temporal_bin_indices = _temporal_bundle(
            edge_endpoints, year_per_flow, n_bins=int(n_time_bins or 5), strength=bstrength
        )

    else:
        # No bundling — use raw line-type paths
        final_paths = raw_paths
        temporal_bin_indices = []

    # ── 7. Build Scattergeo traces ────────────────────────────────────────────
    fig      = go.Figure()
    n_bins_eff = int(n_time_bins or 5)

    for flow_idx, ((_, row), (lats, lons)) in enumerate(zip(flows_agg.iterrows(), final_paths)):
        src, tgt, cnt = row["from"], row["to"], int(row["count"])
        alpha = 0.12 + 0.75 * (cnt / max_cnt) ** 0.5
        lw    = 1.0  + 5.0  * (cnt / max_cnt) ** 0.5

        # Colour — temporal mode assigns hue by time period, overriding color_mode
        if bundling_mode == "temporal" and temporal_bin_indices:
            base = _temporal_color(temporal_bin_indices[flow_idx], n_bins_eff)
        elif color_mode == "faculty":
            grp   = flows_df[(flows_df["from"]==src) & (flows_df["to"]==tgt)]
            dom   = grp["faculty"].value_counts()
            fac   = dom.index[0] if not dom.empty else ""
            base  = FACULTY_COLORS.get(fac, FACULTY_COLORS_DEFAULT)
        elif color_mode == "count":
            # Blue (low) → red (high)
            r_ = int(255 * (cnt/max_cnt))
            b_ = int(255 * (1 - cnt/max_cnt))
            base = f"#{r_:02x}33{b_:02x}"
        else:
            base = "#457b9d"

        # Dim if selection active and this flow doesn't involve selected person
        if sel_set:
            grp_f = flows_df[(flows_df["from"]==src) & (flows_df["to"]==tgt)]
            if grp_f[grp_f["person_id"].isin(sel_set)].empty:
                alpha *= 0.12
                lw    *= 0.5

        h = base.lstrip("#")
        rgba = f"rgba({int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)},{alpha:.2f})"

        fig.add_trace(go.Scattergeo(
            lat=lats + [None], lon=lons + [None],
            mode="lines",
            line=dict(width=lw, color=rgba),
            hoverinfo="skip",
            showlegend=False,
        ))

        # Directed arrow: extra marker at destination end
        if show_arrows:
            fig.add_trace(go.Scattergeo(
                lat=[lats[-1]], lon=[lons[-1]],
                mode="markers",
                marker=dict(size=5 + 4*(cnt/max_cnt), color=rgba,
                            symbol="circle", opacity=min(1.0, alpha*1.8)),
                hovertemplate=f"<b>{src} → {tgt}</b><br>{cnt} move{'s' if cnt!=1 else ''}<extra></extra>",
                showlegend=False,
            ))

    # ── 8. Node markers ───────────────────────────────────────────────────────
    node_list  = sorted(valid)
    cn_set     = set(country_nodes)
    city_nodes = [n for n in node_list if n not in cn_set]
    node_vols  = {}
    for n in node_list:
        v = (flows_agg[flows_agg["from"]==n]["count"].sum() +
             flows_agg[flows_agg["to"]  ==n]["count"].sum())
        node_vols[n] = int(v)
    max_vol = max(node_vols.values()) or 1

    if city_nodes:
        fig.add_trace(go.Scattergeo(
            lat=[node_coords[n][0] for n in city_nodes],
            lon=[node_coords[n][1] for n in city_nodes],
            mode="markers+text",
            marker=dict(
                size=[6 + 14*(node_vols[n]/max_vol)**0.5 for n in city_nodes],
                color=["#c0392b" if "leiden" in n.lower() else "#2c3e50" for n in city_nodes],
                symbol="circle", opacity=0.9,
                line=dict(color="white", width=1),
            ),
            text=city_nodes,
            textposition="top center",
            textfont=dict(size=9, color="#333"),
            hovertemplate="<b>%{text}</b><br>%{customdata} total moves<extra></extra>",
            customdata=[node_vols[n] for n in city_nodes],
            showlegend=False,
        ))

    if country_nodes:
        fig.add_trace(go.Scattergeo(
            lat=[node_coords[n][0] for n in country_nodes],
            lon=[node_coords[n][1] for n in country_nodes],
            mode="markers+text",
            marker=dict(
                size=[10 + 14*(node_vols.get(n, 0)/max_vol)**0.5 for n in country_nodes],
                color="#6c757d", symbol="diamond", opacity=0.9,
                line=dict(color="white", width=1.5),
            ),
            text=country_nodes,
            textposition="top center",
            textfont=dict(size=9, color="#555"),
            hovertemplate="<b>%{text}</b><br>%{customdata} total moves<br><i>Click to expand</i><extra></extra>",
            customdata=[node_vols.get(n, 0) for n in country_nodes],
            showlegend=False,
        ))

    # ── 9. Layout ─────────────────────────────────────────────────────────────
    all_lats = [c[0] for c in node_coords.values() if c]
    all_lons = [c[1] for c in node_coords.values() if c]
    pad = 3
    technique_labels = [line_type.title()]
    bundle_label = {"fdeb": "FDEB", "hier": "Hierarchical",
                    "temporal": f"Temporal ({n_bins_eff})"}.get(bundling_mode)
    if bundle_label:
        technique_labels.append(f"{bundle_label} (s={bundling_strength:.2f})")
    if show_arrows:
        technique_labels.append("Directed")

    fig.update_layout(
        title=dict(text="Experimental Flow Map  ·  " + "  ·  ".join(technique_labels),
                   font=dict(size=12)),
        geo=dict(
            showland=True,    landcolor="#f0ede8",
            showocean=True,   oceancolor="#d6eaf8",
            showcoastlines=True, coastlinecolor="#bbb",
            showcountries=True, countrycolor="#ddd",
            showlakes=True,   lakecolor="#d6eaf8",
            projection_type="mercator",
            lataxis_range=[min(all_lats)-pad, max(all_lats)+pad] if all_lats else [40, 65],
            lonaxis_range=[min(all_lons)-pad, max(all_lons)+pad] if all_lons else [-10, 30],
        ),
        margin=dict(l=0, r=0, t=32, b=0),
        showlegend=False,
        uirevision="expflowmap",
    )
    return fig, country_nodes


def _panel_expflowmap():
    faculties = sorted({r["faculty"] for r in PEOPLE if r.get("faculty")})
    return html.Div(
        style={"display": "flex", "flexDirection": "column", "height": "calc(100vh - 74px)"},
        children=[
            # ── Controls ──────────────────────────────────────────────────────
            # ── Controls ──────────────────────────────────────────────────────
            html.Div(style={
                "display": "flex", "flexWrap": "wrap", "gap": "8px",
                "padding": "4px 10px", "borderBottom": "1px solid #e9ecef",
                "background": "#f8f9fa", "alignItems": "flex-end",
                "flexShrink": 0, "overflowX": "auto",
            }, children=[

                # Line type
                dmc.Stack(gap=1, children=[
                    dmc.Text("Line type", size="xs", c="dimmed"),
                    dmc.SegmentedControl(
                        id="expflow-line-type", value="bezier", size="xs",
                        data=[{"value": "straight",    "label": "Straight"},
                              {"value": "bezier",      "label": "Bezier"},
                              {"value": "greatcircle", "label": "Great circle"}],
                    ),
                ]),

                # Edge bundling — Off / FDEB / Hierarchical / Temporal
                dmc.Stack(gap=1, children=[
                    dmc.Text("Edge bundling", size="xs", c="dimmed"),
                    dmc.SegmentedControl(
                        id="expflow-bundling", value="off", size="xs",
                        data=[{"value": "off",      "label": "Off"},
                              {"value": "fdeb",     "label": "FDEB"},
                              {"value": "hier",     "label": "Hierarchical"},
                              {"value": "temporal", "label": "Temporal"}],
                    ),
                ]),
                dmc.Stack(gap=1, children=[
                    dmc.Text(id="expflow-strength-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="expflow-bundling-strength", value=50, min=10, max=90, step=10,
                        marks=[{"value": v, "label": str(v)} for v in [10, 50, 90]],
                        style={"width": "100px"},
                    ),
                ]),
                dmc.Stack(gap=1, children=[
                    dmc.Text(id="expflow-timebins-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="expflow-time-bins", value=5, min=2, max=10, step=1,
                        marks=[{"value": v, "label": str(v)} for v in [2, 5, 10]],
                        style={"width": "90px"},
                    ),
                ]),

                # Directed arrows
                dmc.Stack(gap=1, children=[
                    dmc.Text("Direction", size="xs", c="dimmed"),
                    dmc.Switch(id="expflow-arrows", checked=True, label="Arrows", size="xs"),
                ]),

                # Semantic threshold + auto-zoom
                dmc.Stack(gap=1, children=[
                    dmc.Text(id="expflow-semantic-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="expflow-semantic-pct", value=0, min=0, max=80, step=10,
                        marks=[{"value": v, "label": f"{v}%" } for v in [0, 40, 80]],
                        style={"width": "110px"},
                    ),
                ]),
                dmc.Stack(gap=1, children=[
                    dmc.Text("Sem. zoom", size="xs", c="dimmed"),
                    dmc.Switch(id="expflow-auto-zoom", checked=False, label="Auto", size="xs"),
                ]),

                # Top-N locations
                dmc.Stack(gap=1, children=[
                    dmc.Text(id="expflow-topn-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="expflow-top-n", value=20, min=5, max=60, step=5,
                        marks=[{"value": v, "label": str(v)} for v in [5, 20, 60]],
                        style={"width": "110px"},
                    ),
                ]),

                # Min flow
                dmc.Stack(gap=1, children=[
                    dmc.Text(id="expflow-minflow-label", size="xs", c="dimmed"),
                    dmc.Slider(
                        id="expflow-min-flow", value=1, min=1, max=20, step=1,
                        marks=[{"value": v, "label": str(v)} for v in [1, 10, 20]],
                        style={"width": "90px"},
                    ),
                ]),

                # Colour mode
                dmc.Stack(gap=1, children=[
                    dmc.Text("Colour", size="xs", c="dimmed"),
                    dmc.SegmentedControl(
                        id="expflow-color-mode", value="uniform", size="xs",
                        data=[{"value": "uniform", "label": "Uniform"},
                              {"value": "faculty",  "label": "Faculty"},
                              {"value": "count",    "label": "Count"}],
                    ),
                ]),

                # Faculty filter
                dmc.Stack(gap=1, children=[
                    dmc.Text("Faculty", size="xs", c="dimmed"),
                    dmc.MultiSelect(
                        id="expflow-faculty-filter", data=faculties,
                        placeholder="All", searchable=True, clearable=True,
                        value=[], maxDropdownHeight=200, style={"minWidth": "115px"},
                    ),
                ]),

                # Expand countries (populated from country nodes in the map)
                dmc.Stack(gap=1, children=[
                    dmc.Text("Expand countries", size="xs", c="dimmed"),
                    dmc.MultiSelect(
                        id="expflow-expand-select",
                        value=[], data=[],
                        placeholder="Click diamond node on map",
                        clearable=True, searchable=True,
                        style={"minWidth": "155px"},
                    ),
                ]),
            ]),

            # ── Map — fills all remaining vertical space ───────────────────────
            dcc.Graph(
                id="expflowmap-chart",
                config={"displayModeBar": True, "responsive": True},
                style={"flex": 1, "minHeight": 0},
            ),
        ],
    )


PAGES = [
    ("/",            "Map"),
    ("/timeline",    "Timeline"),
    ("/cube",        "Space-Time Cube"),
    ("/sankey",      "Life Flows"),
    ("/flowmap",     "Flow Map"),
    ("/expflowmap",  "Flow Map ✦"),
    ("/copresence",  "Co-presence"),
    ("/people",      "People"),
]

NAV_BASE = {
    "display": "inline-block",
    "padding": "6px 16px",
    "marginRight": "4px",
    "borderRadius": "6px",
    "textDecoration": "none",
    "fontSize": "14px",
    "fontWeight": "500",
    "color": "#495057",
    "border": "1px solid #dee2e6",
    "backgroundColor": "#f8f9fa",
    "cursor": "pointer",
}
NAV_ACTIVE = {**NAV_BASE, "backgroundColor": "#228be6", "color": "#fff",
              "border": "1px solid #228be6"}


def _nav_bar(current_path):
    links = []
    for path, label in PAGES:
        style = NAV_ACTIVE if current_path == path else NAV_BASE
        links.append(dcc.Link(label, href=path, style=style))
    return html.Div(links, style={"marginBottom": "12px"})


# ── Root layout ───────────────────────────────────────────────────────────────

app.layout = dmc.MantineProvider(
    children=[
        # Animation interval (ticks when animation is active)
        dcc.Interval(id="anim-interval", interval=800, n_intervals=0, disabled=True),

        # Shared state stores
        dcc.Store(id="store-selection",       data=[]),
        dcc.Store(id="store-year",       data=[YEAR_MIN, YEAR_MAX]),
        dcc.Store(id="store-anim",       data={"active": False, "year": YEAR_MIN,
                                               "mode": "cumulative", "window_size": 10}),
        dcc.Store(id="store-focus",      data="all"),
        dcc.Store(id="store-copresence", data=[]),
        dcc.Store(id="store-sankey-expanded",      data=[]),
        dcc.Store(id="store-sankey-trace",         data=None),
        dcc.Store(id="store-sankey-country-nodes", data=[]),
        dcc.Store(id="store-sankey-detail",        data={"flows":{}, "nodes":{}}),
        dcc.Store(id="store-flowmap-expanded",          data=[]),
        dcc.Store(id="store-flowmap-country-nodes",    data=[]),
        dcc.Store(id="store-flowmap-detail",           data={"flows":{}, "nodes":{}}),
        dcc.Store(id="store-expflowmap-expanded",      data=[]),
        dcc.Store(id="store-expflowmap-country-nodes", data=[]),
        dcc.Store(id="page-render-trigger",        data="/"),

        # Person detail modal (portal-rendered, not in flow)
        dmc.Modal(
            id="detail-modal",
            title="",
            size="xl",
            children=[html.Div(id="detail-body")],
            opened=False,
        ),

        # ── Fixed header bar ───────────────────────────────────────────────
        html.Div(
            style={
                "position": "fixed", "top": 0, "left": 0, "right": 0,
                "height": "50px",
                "backgroundColor": "#fff",
                "borderBottom": "1px solid #dee2e6",
                "display": "flex", "alignItems": "center",
                "padding": "0 16px", "gap": "12px",
                "zIndex": 200,
                "boxSizing": "border-box",
            },
            children=[
                dmc.Text("Life Paths · Leiden University", fw=600, size="sm"),
                html.Div(style={"flex": 1}),
                dmc.Badge(
                    f"{len(PEOPLE)} professors · {len(EVENTS_DF)} events",
                    variant="light",
                ),
            ],
        ),

        # ── Fixed left sidebar ─────────────────────────────────────────────
        _navbar(),

        # URL router
        dcc.Location(id="url", refresh=False),

        # ── Scrollable main content ────────────────────────────────────────
        html.Div(
            id="page-content",
            style={
                "marginTop": "50px",
                "marginLeft": "260px",
                "padding": "12px 16px",
                "minHeight": "calc(100vh - 50px)",
                "boxSizing": "border-box",
            },
        ),
    ]
)


# ── Callbacks ─────────────────────────────────────────────────────────────────

# URL → page content
_PATH_BUILDERS = {
    "/":            _panel_map,
    "/timeline":    _panel_timeline,
    "/cube":        _panel_cube,
    "/sankey":      _panel_sankey,
    "/flowmap":     _panel_flowmap,
    "/expflowmap":  _panel_expflowmap,
    "/copresence":  _panel_copresence,
    "/people":      _panel_people,
}

@app.callback(
    Output("page-content",       "children"),
    Output("page-render-trigger","data"),
    Input("url", "pathname"),
)
def render_page(pathname):
    path    = (pathname or "/").rstrip("/") or "/"
    builder = _PATH_BUILDERS.get(path, _panel_map)
    return builder(), path


# Year slider → store + label
@app.callback(
    Output("store-year", "data"),
    Output("year-label", "children"),
    Input("year-slider", "value"),
)
def update_year(value):
    s, e = sorted(value or [YEAR_MIN, YEAR_MAX])
    return [s, e], f"{s} – {e}"


# Focus mode control → store
@app.callback(
    Output("store-focus", "data"),
    Input("focus-mode", "value"),
)
def update_focus(value):
    return value or "all"


# Sidebar selection sources → store (clear btn, badge click, search dropdown)
# Kept separate from map/chart clicks so lazy-loaded Input components
# don't block this callback from firing on non-map pages.
@app.callback(
    Output("store-selection", "data"),
    Output("person-search", "value"),
    Input("clear-btn", "n_clicks"),
    Input({"type": "sel-badge", "index": ALL}, "n_clicks"),
    Input("person-search", "value"),
    State("store-selection", "data"),
    prevent_initial_call=True,
)
def update_selection(clear_n, badge_clicks, search_val, stored):
    stored  = list(stored or [])
    trigger = ctx.triggered_id

    if trigger == "clear-btn":
        return [], []

    if isinstance(trigger, dict) and trigger.get("type") == "sel-badge":
        pid = int(trigger["index"])
        stored = [p for p in stored if p != pid]
        return stored, [str(p) for p in stored]

    if trigger == "person-search" and search_val:
        pids   = [int(v) for v in search_val]
        merged = sorted(set(stored) | set(pids))
        return merged, [str(p) for p in merged]

    return stored, [str(p) for p in stored]


# Map click → toggle selection  (separate callback: life-map is lazy-loaded)
if HAS_DECK:
    @app.callback(
        Output("store-selection", "data", allow_duplicate=True),
        Output("person-search", "value", allow_duplicate=True),
        Input("life-map", "clickInfo"),
        State("store-selection", "data"),
        prevent_initial_call=True,
    )
    def map_click_select(map_click, stored):
        if not map_click:
            raise dash.exceptions.PreventUpdate
        obj = (map_click if isinstance(map_click, dict) else {}).get("object") or {}
        pid = obj.get("person_id")
        if pid is None:
            raise dash.exceptions.PreventUpdate
        pid    = int(pid)
        stored = list(stored or [])
        stored = [p for p in stored if p != pid] if pid in stored \
                 else sorted(stored + [pid])
        return stored, [str(p) for p in stored]


# Selection → chips display
@app.callback(
    Output("selection-chips", "children"),
    Input("store-selection", "data"),
)
def update_chips(selected):
    return _selection_chips(selected or [])


# Map update
if HAS_DECK:
    @app.callback(
        Output("life-map", "data"),
        Input("page-render-trigger", "data"),
        Input("store-year", "data"),
        Input("store-selection", "data"),
        Input("type-filter", "value"),
        Input("store-anim", "data"),
        Input("store-focus", "data"),
        State("url", "pathname"),
    )
    def update_map_cb(page, year_range, selected, visible_types, anim, focus, pathname):
        if (pathname or "/") not in ("/", ""):
            raise dash.exceptions.PreventUpdate
        yr = _effective_year_range(year_range, anim)
        return build_map(yr, selected, visible_types, focus or "all")


# Event bar
@app.callback(
    Output("event-bar", "figure"),
    Input("page-render-trigger", "data"),
    Input("store-year", "data"),
    Input("store-anim", "data"),
    State("url", "pathname"),
)
def update_event_bar(page, year_range, anim, pathname):
    if (pathname or "/") not in ("/", ""):
        raise dash.exceptions.PreventUpdate
    yr = _effective_year_range(year_range, anim)
    return build_event_bar(yr)


# Timeline histogram
@app.callback(
    Output("timeline-chart", "figure"),
    Input("page-render-trigger", "data"),
    Input("store-year", "data"),
    Input("store-selection", "data"),
    Input("store-anim", "data"),
    Input("store-focus", "data"),
    State("url", "pathname"),
)
def update_timeline(page, year_range, selected, anim, focus, pathname):
    if (pathname or "/").rstrip("/") != "/timeline":
        raise dash.exceptions.PreventUpdate
    yr = _effective_year_range(year_range, anim)
    return build_timeline(yr, selected or None, focus or "all")


# Co-presence calculation
@app.callback(
    Output("store-copresence", "data"),
    Output("copresence-grid", "rowData"),
    Input("page-render-trigger", "data"),
    Input("store-selection", "data"),
    Input("store-year", "data"),
    Input("store-anim", "data"),
    State("url", "pathname"),
)
def update_copresence(page, selected, year_range, anim, pathname):
    if (pathname or "/").rstrip("/") != "/copresence":
        return no_update, no_update
    yr = _effective_year_range(year_range, anim)
    if selected and len(selected) >= 2:
        rows = _copresence_selected(selected, yr)
    elif selected and len(selected) == 1:
        rows = _copresence_one(int(selected[0]), yr)
    else:
        rows = _copresence_top(yr)
    return rows, rows


# Co-presence row click → select that pair
@app.callback(
    Output("store-selection", "data", allow_duplicate=True),
    Output("person-search", "value", allow_duplicate=True),
    Input("copresence-grid", "selectedRows"),
    prevent_initial_call=True,
)
def copresence_select(rows):
    if not rows:
        raise dash.exceptions.PreventUpdate
    row  = rows[0]
    pids = sorted({int(row["pid_a"]), int(row["pid_b"])})
    return pids, [str(p) for p in pids]


# People grid → select (table row selection)
@app.callback(
    Output("store-selection", "data",  allow_duplicate=True),
    Output("person-search",   "value", allow_duplicate=True),
    Input("people-grid", "selectedRows"),
    State("store-selection", "data"),
    prevent_initial_call=True,
)
def people_grid_selection(selected_rows, stored):
    if not selected_rows:
        raise dash.exceptions.PreventUpdate
    new_pids = {int(r["person_id"]) for r in selected_rows if "person_id" in r}
    current  = set(map(int, stored or []))
    merged   = sorted(current | new_pids)
    if merged == sorted(current):
        raise dash.exceptions.PreventUpdate
    return merged, [str(p) for p in merged]


# Network graph (Cytoscape elements + node-click selection)
if HAS_CYTO:
    @app.callback(
        Output("store-selection", "data", allow_duplicate=True),
        Output("person-search", "value", allow_duplicate=True),
        Input("network-graph", "tapNodeData"),
        State("store-selection", "data"),
        prevent_initial_call=True,
    )
    def network_node_click(node_data, stored):
        if not node_data:
            raise dash.exceptions.PreventUpdate
        pid = node_data.get("pid")
        if pid is None:
            raise dash.exceptions.PreventUpdate
        pid    = int(pid)
        stored = list(stored or [])
        stored = [p for p in stored if p != pid] if pid in stored \
                 else sorted(stored + [pid])
        return stored, [str(p) for p in stored]


# Person detail modal — opened by the ℹ button next to each selection chip
@app.callback(
    Output("detail-modal", "opened"),
    Output("detail-modal", "title"),
    Output("detail-body", "children"),
    Input({"type": "detail-btn", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_detail_modal(n_clicks_list):
    if not any(n for n in (n_clicks_list or []) if n):
        raise dash.exceptions.PreventUpdate
    triggered = ctx.triggered_id
    if not isinstance(triggered, dict):
        raise dash.exceptions.PreventUpdate
    pid   = int(triggered["index"])
    name_rows = PEOPLE_DF[PEOPLE_DF["person_id"] == pid]
    title = name_rows.iloc[0]["name"] if not name_rows.empty else f"Person {pid}"
    return True, title, build_detail_body(pid)


# Space-Time Cube update
@app.callback(
    Output("cube-chart", "figure"),
    Output("cube-stats", "children"),
    Output("cube-geomap", "figure"),
    Input("page-render-trigger", "data"),
    Input("store-year", "data"),
    Input("store-selection", "data"),
    Input("cube-color-mode", "value"),
    Input("cube-type-filter", "value"),
    Input("cube-projections", "checked"),
    Input("cube-copresence", "checked"),
    Input("cube-camera", "value"),
    Input("store-anim", "data"),
    Input("store-focus", "data"),
    State("url", "pathname"),
)
def update_cube(page, year_range, selected, color_mode,
                type_filter, show_proj, show_copresence, camera_preset,
                anim, focus, pathname):
    if (pathname or "/").rstrip("/") != "/cube":
        raise dash.exceptions.PreventUpdate
    yr = _effective_year_range(year_range, anim)
    fig = build_cube(yr, selected or [], color_mode or "event",
                     type_filter or list(EVENT_COLORS.keys()),
                     show_proj, show_copresence, camera_preset or "3d",
                     focus or "all")
    df = filter_events(yr, selected if selected else None)
    n_people = df["person_id"].nunique()
    n_events = len(df)
    stat_text = f"{n_people} professors · {n_events} events · {yr[0]}–{yr[1]}"
    geomap_fig = build_cube_map(yr, selected or [], focus or "all")
    return fig, stat_text, geomap_fig


# Space-Time Cube click → select person
@app.callback(
    Output("store-selection", "data", allow_duplicate=True),
    Output("person-search", "value", allow_duplicate=True),
    Input("cube-chart", "clickData"),
    State("store-selection", "data"),
    prevent_initial_call=True,
)
def cube_click_select(click_data, stored):
    if not click_data:
        raise dash.exceptions.PreventUpdate
    pts = click_data.get("points", [])
    if not pts:
        raise dash.exceptions.PreventUpdate
    cd = pts[0].get("customdata")
    if cd is None:
        raise dash.exceptions.PreventUpdate
    pid = int(cd)
    stored = list(stored or [])
    stored = [p for p in stored if p != pid] if pid in stored else sorted(stored + [pid])
    return stored, [str(p) for p in stored]


# Life Flows (Sankey) — main update
@app.callback(
    Output("sankey-chart", "figure"),
    Output("store-sankey-country-nodes", "data"),
    Output("store-sankey-detail", "data"),
    Output("sankey-topn-label", "children"),
    Output("sankey-minflow-label", "children"),
    Input("page-render-trigger", "data"),
    Input("store-year", "data"),
    Input("store-selection", "data"),
    Input("sankey-event-filter", "value"),
    Input("sankey-faculty-filter", "value"),
    Input("sankey-top-n", "value"),
    Input("sankey-min-flow", "value"),
    Input("store-sankey-expanded", "data"),
    Input("store-sankey-trace", "data"),
    Input("sankey-color-mode", "value"),
    Input("sankey-arrangement", "value"),
    Input("store-anim", "data"),
    Input("store-focus", "data"),
    State("url", "pathname"),
)
def update_sankey(page, year_range, selected, event_filter,
                  faculty_filter, top_n, min_flow, expanded,
                  trace_node, color_mode, arrangement, anim, focus, pathname):
    if (pathname or "/").rstrip("/") != "/sankey":
        raise dash.exceptions.PreventUpdate
    yr    = _effective_year_range(year_range, anim)
    fig, country_nodes, flow_detail, node_detail = build_sankey(
        yr, selected or [], faculty_filter or [],
        event_filter or list(EVENT_COLORS.keys()),
        top_n or 20, min_flow or 1,
        expanded or [], trace_node,
        color_mode or "uniform", arrangement or "snap",
        focus or "all",
    )
    detail = {"flows": flow_detail, "nodes": node_detail}
    topn_label    = f"Top-N nodes  ({top_n or 20})"
    minflow_label = f"Min flow  (≥{min_flow or 1})"
    return fig, country_nodes, detail, topn_label, minflow_label


# Life Flows (Sankey) — click handler
@app.callback(
    Output("store-sankey-expanded", "data"),
    Output("store-sankey-trace",    "data"),
    Output("sankey-info",           "children"),
    Output("store-selection",       "data",  allow_duplicate=True),
    Output("person-search",         "value", allow_duplicate=True),
    Input("sankey-chart",  "clickData"),
    Input("sankey-clear-btn", "n_clicks"),
    State("store-sankey-country-nodes", "data"),
    State("store-sankey-expanded",      "data"),
    State("store-sankey-trace",         "data"),
    State("store-sankey-detail",        "data"),
    State("store-selection",            "data"),
    prevent_initial_call=True,
)
def sankey_click(click_data, clear_clicks, country_nodes, expanded,
                 trace_node, detail, current_sel):
    triggered = ctx.triggered_id

    if triggered == "sankey-clear-btn":
        return expanded or [], None, "Trace cleared.", no_update, no_update

    if not click_data:
        raise dash.exceptions.PreventUpdate

    pt            = (click_data.get("points") or [{}])[0]
    expanded      = list(expanded or [])
    country_nodes = list(country_nodes or [])

    # ── Link click → select all professors in that flow ───────────────
    if isinstance(pt.get("source"), int):
        cd        = pt.get("customdata") or []
        flow_key  = cd[0] if isinstance(cd, list) and cd else f"{pt['source']}-{pt['target']}"
        src_label = cd[1] if isinstance(cd, list) and len(cd) > 1 else "?"
        tgt_label = cd[2] if isinstance(cd, list) and len(cd) > 2 else "?"
        entry  = (detail.get("flows") or {}).get(flow_key, {})
        labels = entry.get("labels", []) if isinstance(entry, dict) else []
        pids   = entry.get("pids",   []) if isinstance(entry, dict) else []
        if not labels:
            return (expanded, trace_node,
                    f"{src_label} → {tgt_label}: no detail available.",
                    no_update, no_update)
        header  = html.B(f"{src_label} → {tgt_label}  ({len(labels)} moves):")
        items   = html.Ul([html.Li(n) for n in labels],
                          style={"margin": "4px 0", "paddingLeft": "18px"})
        note    = html.I(f" — {len(pids)} professor{'s' if len(pids)!=1 else ''} selected",
                         style={"color": "#888", "fontSize": "11px"})
        new_sel = sorted(pids)
        return (expanded, trace_node, [header, note, items],
                new_sel, [str(p) for p in new_sel])

    # ── Node click ────────────────────────────────────────────────────
    label = pt.get("label", "")
    if not label:
        raise dash.exceptions.PreventUpdate

    if label in country_nodes:
        if label in expanded:
            expanded.remove(label)
            info = f"Collapsed {label}."
        else:
            expanded.append(label)
            info = f"Expanded {label} into individual cities."
        return expanded, trace_node, info, no_update, no_update

    # City node — toggle trace AND select all professors at that node
    new_trace = None if trace_node == label else label
    entry  = (detail.get("nodes") or {}).get(label, {})
    labels = entry.get("labels", []) if isinstance(entry, dict) else entry or []
    pids   = entry.get("pids",   []) if isinstance(entry, dict) else []
    if not labels:
        return expanded, new_trace, f"Tracing {new_trace or 'cleared'}.", no_update, no_update
    header = html.B(f"{'Tracing: ' if new_trace else 'Cleared — '}{label}  ({len(labels)} professors):")
    items  = html.Ul([html.Li(n) for n in labels[:20]],
                     style={"margin": "4px 0", "paddingLeft": "18px"})
    note   = html.I(f" — {len(pids)} professor{'s' if len(pids)!=1 else ''} selected",
                    style={"color": "#888", "fontSize": "11px"})
    new_sel = sorted(pids)
    return (expanded, new_trace, [header, note, items],
            new_sel, [str(p) for p in new_sel])


# Sankey — keep expand-select options in sync with the current country nodes
@app.callback(
    Output("sankey-expand-select", "data"),
    Output("sankey-expand-select", "value"),
    Input("store-sankey-country-nodes", "data"),
    State("store-sankey-expanded",      "data"),
    prevent_initial_call=True,
)
def sync_expand_select_options(country_nodes, expanded):
    options = [{"value": n, "label": n} for n in sorted(country_nodes or [])]
    # Keep only values still present in the new options
    valid = {o["value"] for o in options}
    value = [v for v in (expanded or []) if v in valid]
    return options, value


# Sankey — MultiSelect drives store-sankey-expanded
@app.callback(
    Output("store-sankey-expanded", "data", allow_duplicate=True),
    Input("sankey-expand-select", "value"),
    State("store-sankey-expanded", "data"),
    prevent_initial_call=True,
)
def expand_select_changed(value, current):
    new = sorted(value or [])
    if new == sorted(current or []):
        raise dash.exceptions.PreventUpdate
    return new


# ── Flow Map callbacks ────────────────────────────────────────────────────────

@app.callback(
    Output("flowmap-chart",               "figure"),
    Output("store-flowmap-country-nodes", "data"),
    Output("store-flowmap-detail",        "data"),
    Output("flowmap-topn-label",          "children"),
    Output("flowmap-minflow-label",       "children"),
    Input("page-render-trigger",    "data"),
    Input("store-year",             "data"),
    Input("store-selection",        "data"),
    Input("flowmap-event-filter",   "value"),
    Input("flowmap-faculty-filter", "value"),
    Input("flowmap-top-n",          "value"),
    Input("flowmap-min-flow",       "value"),
    Input("store-flowmap-expanded", "data"),
    Input("flowmap-color-mode",     "value"),
    Input("store-anim",             "data"),
    Input("store-focus",            "data"),
    State("url", "pathname"),
)
def update_flowmap(page, year_range, selected, event_filter, faculty_filter,
                   top_n, min_flow, expanded, color_mode, anim, focus, pathname):
    if (pathname or "/").rstrip("/") != "/flowmap":
        raise dash.exceptions.PreventUpdate
    yr = _effective_year_range(year_range, anim)
    fig, country_nodes, detail = build_flowmap(
        yr, selected or [], faculty_filter or [],
        event_filter or list(EVENT_COLORS.keys()),
        top_n or 20, min_flow or 1,
        expanded or [], color_mode or "uniform",
        focus or "all",
    )
    return (fig, country_nodes, detail,
            f"Top-N nodes  ({top_n or 20})",
            f"Min flow  (≥{min_flow or 1})")


@app.callback(
    Output("store-flowmap-expanded", "data"),
    Output("flowmap-info",           "children"),
    Output("store-selection",        "data",  allow_duplicate=True),
    Output("person-search",          "value", allow_duplicate=True),
    Input("flowmap-chart",    "clickData"),
    State("store-flowmap-country-nodes", "data"),
    State("store-flowmap-expanded",      "data"),
    State("store-flowmap-detail",        "data"),
    State("store-selection",             "data"),
    prevent_initial_call=True,
)
def flowmap_click(click_data, country_nodes, expanded, detail, current_sel):
    if not click_data:
        raise dash.exceptions.PreventUpdate
    pt = (click_data.get("points") or [{}])[0]
    cd = pt.get("customdata")
    expanded      = list(expanded or [])
    country_nodes = list(country_nodes or [])

    # Node click — customdata is a dict {"type": "node", "label": "..."}
    if isinstance(cd, dict) and cd.get("type") == "node":
        label = cd.get("label", "")
        if label in country_nodes:
            if label in expanded:
                expanded.remove(label)
                return expanded, f"Collapsed {label}.", no_update, no_update
            expanded.append(label)
            return expanded, f"Expanded {label} — cities now visible.", no_update, no_update
        entry  = (detail.get("nodes") or {}).get(label, {})
        labels = entry.get("labels", [])
        pids   = entry.get("pids", [])
        if not labels:
            return no_update, f"No detail for {label}.", no_update, no_update
        header = html.B(f"{label}  ({len(labels)} professors):")
        items  = html.Ul([html.Li(n) for n in labels[:20]],
                         style={"margin":"4px 0","paddingLeft":"18px"})
        note   = html.I(f" — {len(pids)} selected",
                        style={"color":"#888","fontSize":"11px"})
        return (no_update, [header, note, items],
                sorted(pids), [str(p) for p in sorted(pids)])

    # Arc click — customdata is a list [fkey, src, tgt, count]
    if isinstance(cd, list) and len(cd) >= 3:
        fkey, src, tgt = str(cd[0]), str(cd[1]), str(cd[2])
        cnt   = int(cd[3]) if len(cd) > 3 else "?"
        entry  = (detail.get("flows") or {}).get(fkey, {})
        labels = entry.get("labels", [])
        pids   = entry.get("pids", [])
        if not labels:
            return (no_update, f"{src} → {tgt}: no detail available.",
                    no_update, no_update)
        header = html.B(f"{src} → {tgt}  ({cnt} moves):")
        items  = html.Ul([html.Li(n) for n in labels],
                         style={"margin":"4px 0","paddingLeft":"18px"})
        note   = html.I(f" — {len(pids)} professor{'s' if len(pids)!=1 else ''} selected",
                        style={"color":"#888","fontSize":"11px"})
        return (no_update, [header, note, items],
                sorted(pids), [str(p) for p in sorted(pids)])

    raise dash.exceptions.PreventUpdate


@app.callback(
    Output("flowmap-expand-select", "data"),
    Output("flowmap-expand-select", "value"),
    Input("store-flowmap-country-nodes", "data"),
    State("store-flowmap-expanded",      "data"),
    prevent_initial_call=True,
)
def sync_flowmap_expand_options(country_nodes, expanded):
    options = [{"value": n, "label": n} for n in sorted(country_nodes or [])]
    valid   = {o["value"] for o in options}
    value   = [v for v in (expanded or []) if v in valid]
    return options, value


@app.callback(
    Output("store-flowmap-expanded", "data", allow_duplicate=True),
    Input("flowmap-expand-select", "value"),
    State("store-flowmap-expanded", "data"),
    prevent_initial_call=True,
)
def flowmap_expand_select_changed(value, current):
    new = sorted(value or [])
    if new == sorted(current or []):
        raise dash.exceptions.PreventUpdate
    return new


# ── Animation callbacks ───────────────────────────────────────────────────────

# Show/hide window-size row and update its label
@app.callback(
    Output("anim-window-row",   "style"),
    Output("anim-window-label", "children"),
    Input("anim-mode",        "value"),
    Input("anim-window-size", "value"),
)
def toggle_window_row(mode, window_size):
    visible = {"display": "block"} if mode == "window" else {"display": "none"}
    label   = f"Window  ({window_size or 10} yrs)"
    return visible, label


# Play / Pause / Stop + interval tick → update store-anim + button label
@app.callback(
    Output("store-anim",         "data"),
    Output("anim-interval",      "disabled"),
    Output("anim-interval",      "interval"),
    Output("anim-play-btn",      "children"),
    Output("anim-year-display",  "children"),
    Input("anim-play-btn",   "n_clicks"),
    Input("anim-stop-btn",   "n_clicks"),
    Input("anim-interval",   "n_intervals"),
    State("store-anim",      "data"),
    State("store-year",      "data"),
    State("anim-mode",       "value"),
    State("anim-window-size","value"),
    State("anim-speed",      "value"),
    prevent_initial_call=True,
)
def control_animation(play_n, stop_n, tick, anim, year_range, mode, window_size, speed):
    triggered = ctx.triggered_id
    anim      = dict(anim or {})
    base_s, base_e = sorted(year_range or [YEAR_MIN, YEAR_MAX])

    speed_ms  = int(speed or 800)
    mode      = mode or "cumulative"
    win_size  = int(window_size or 10)

    if triggered == "anim-stop-btn":
        anim.update(active=False, year=base_s, mode=mode, window_size=win_size)
        return anim, True, speed_ms, "▶ Play", "–"

    if triggered == "anim-play-btn":
        if anim.get("active"):
            # Pause
            anim.update(active=False, mode=mode, window_size=win_size)
            yr_str = str(anim.get("year", base_s))
            return anim, True, speed_ms, "▶ Play", yr_str
        else:
            # Start / Resume
            start_yr = int(anim.get("year", base_s))
            # If already at the end, restart
            if start_yr >= base_e:
                start_yr = base_s
            anim.update(active=True, year=start_yr, mode=mode, window_size=win_size)
            return anim, False, speed_ms, "⏸ Pause", str(start_yr)

    # Interval tick — advance year by 1
    if triggered == "anim-interval":
        if not anim.get("active"):
            raise dash.exceptions.PreventUpdate
        current_yr = int(anim.get("year", base_s))
        next_yr    = current_yr + 1
        if next_yr > base_e:
            # Reached end — stop
            anim.update(active=False, year=base_e, mode=mode, window_size=win_size)
            return anim, True, speed_ms, "▶ Play", str(base_e)
        anim.update(year=next_yr, mode=mode, window_size=win_size)
        return anim, False, speed_ms, "⏸ Pause", str(next_yr)

    raise dash.exceptions.PreventUpdate


@app.callback(
    Output("expflowmap-chart",                "figure"),
    Output("expflow-topn-label",              "children"),
    Output("expflow-minflow-label",           "children"),
    Output("expflow-strength-label",          "children"),
    Output("expflow-semantic-label",          "children"),
    Output("expflow-timebins-label",          "children"),
    Output("store-expflowmap-country-nodes",  "data"),
    Input("page-render-trigger",              "data"),
    Input("store-year",                       "data"),
    Input("store-selection",                  "data"),
    Input("expflow-line-type",                "value"),
    Input("expflow-bundling",                 "value"),
    Input("expflow-bundling-strength",        "value"),
    Input("expflow-arrows",                   "checked"),
    Input("expflow-semantic-pct",             "value"),
    Input("expflow-top-n",                    "value"),
    Input("expflow-min-flow",                 "value"),
    Input("expflow-color-mode",               "value"),
    Input("expflow-faculty-filter",           "value"),
    Input("expflow-auto-zoom",                "checked"),
    Input("expflow-time-bins",                "value"),
    Input("expflowmap-chart",                 "relayoutData"),
    Input("store-expflowmap-expanded",        "data"),
    State("url", "pathname"),
)
def update_expflowmap(page, year_range, selected, line_type, bundling,
                      bundling_strength, arrows, semantic_pct,
                      top_n, min_flow, color_mode, faculty_filter,
                      auto_zoom, n_time_bins, relayout_data, expanded, pathname):
    if (pathname or "/").rstrip("/") != "/expflowmap":
        raise dash.exceptions.PreventUpdate

    eff_semantic = semantic_pct or 0
    if auto_zoom:
        vp_pct = _viewport_semantic_pct(relayout_data)
        if vp_pct is not None:
            eff_semantic = vp_pct

    bstrength = (bundling_strength or 50) / 100
    n_bins    = int(n_time_bins or 5)

    fig, country_nodes = build_expflowmap(
        year_range or [YEAR_MIN, YEAR_MAX],
        selected or [],
        line_type or "bezier",
        bundling or "off",
        bstrength,
        arrows if arrows is not None else True,
        eff_semantic,
        top_n or 20,
        min_flow or 1,
        color_mode or "uniform",
        faculty_filter or [],
        expanded_countries=expanded or [],
        n_time_bins=n_bins,
    )

    bundling_lbl = {"fdeb": "FDEB strength", "hier": "Hub blend",
                    "temporal": "Temporal strength"}.get(bundling or "off", "Bundling strength")
    zoom_lbl = " (auto)" if auto_zoom else ""
    return (
        fig,
        f"Top-N locations ({top_n or 20})",
        f"Min flow (>={min_flow or 1})",
        f"{bundling_lbl} ({bundling_strength or 50}%)",
        f"Semantic filter{zoom_lbl} (hide <{eff_semantic}% of max)",
        f"Time bins ({n_bins})",
        country_nodes,
    )


@app.callback(
    Output("store-expflowmap-expanded", "data"),
    Input("expflowmap-chart",           "clickData"),
    State("store-expflowmap-country-nodes", "data"),
    State("store-expflowmap-expanded",      "data"),
    prevent_initial_call=True,
)
def expflowmap_click(click_data, country_nodes, expanded):
    if not click_data:
        raise dash.exceptions.PreventUpdate
    pt    = click_data["points"][0]
    label = pt.get("text") or pt.get("hovertext", "")
    if not label or label not in (country_nodes or []):
        raise dash.exceptions.PreventUpdate
    expanded = list(expanded or [])
    if label in expanded:
        expanded.remove(label)
    else:
        expanded.append(label)
    return expanded


@app.callback(
    Output("expflow-expand-select", "data"),
    Output("expflow-expand-select", "value"),
    Input("store-expflowmap-country-nodes", "data"),
    State("store-expflowmap-expanded",      "data"),
    prevent_initial_call=True,
)
def sync_expflow_expand_options(country_nodes, expanded):
    options = [{"value": n, "label": n} for n in sorted(country_nodes or [])]
    valid   = {o["value"] for o in options}
    value   = [v for v in (expanded or []) if v in valid]
    return options, value


@app.callback(
    Output("store-expflowmap-expanded", "data", allow_duplicate=True),
    Input("expflow-expand-select", "value"),
    State("store-expflowmap-expanded",  "data"),
    prevent_initial_call=True,
)
def expflow_expand_select_changed(value, current):
    if sorted(value or []) == sorted(current or []):
        raise dash.exceptions.PreventUpdate
    return value or []


if __name__ == "__main__":
    app.run(debug=True, port=8051, dev_tools_ui=False)
