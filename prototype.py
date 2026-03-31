import os
from functools import lru_cache

import dash
from dash import Dash, dcc, html, Input, Output, State, dash_table, no_update
import pandas as pd
import plotly.graph_objects as go

try:
    import pymonetdb
except ImportError as e:
    raise SystemExit(
        "pymonetdb is required. Install with: pip install pymonetdb"
    ) from e


DB_HOST = os.getenv("MONETDB_HOST", "localhost")
DB_PORT = int(os.getenv("MONETDB_PORT", "50000"))
DB_NAME = os.getenv("MONETDB_DATABASE", "peopledb")
DB_USER = os.getenv("MONETDB_USER", "monetdb")
DB_PASSWORD = os.getenv("MONETDB_PASSWORD", "monetdb")

EVENT_ORDER = {"birth": 0, "education": 1, "career": 2, "death": 3}
EVENT_COLORS = {
    "birth": "#2ca02c",
    "education": "#1f77b4",
    "career": "#ff7f0e",
    "death": "#d62728",
}


def get_connection():
    return pymonetdb.connect(
        hostname=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        username=DB_USER,
        password=DB_PASSWORD,
    )


QUERY = """
SELECT
    p.person_id,
    TRIM(
        COALESCE(p.first_name, '') || ' ' ||
        COALESCE(p.affix || ' ', '') ||
        COALESCE(p.last_name, '')
    ) AS person_name,
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


@lru_cache(maxsize=1)
def load_data() -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql(QUERY, conn)

    if df.empty:
        return df

    df["begin_date"] = pd.to_datetime(df["begin_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    df["event_date"] = df["begin_date"].fillna(df["end_date"])
    df = df.dropna(subset=["event_date", "latitude", "longitude"]).copy()

    df["year"] = df["event_date"].dt.year.astype(int)
    df["event_order"] = df["event_type_name"].map(EVENT_ORDER).fillna(99).astype(int)

    df["city_label"] = df["city"].fillna("")
    df["country_label"] = df["country"].fillna("")
    df["location_label"] = df["city_label"]

    mask = df["country_label"].ne("") & df["location_label"].ne("")
    df.loc[mask, "location_label"] = df["city_label"] + ", " + df["country_label"]
    df.loc[df["location_label"].eq(""), "location_label"] = df["country_label"]
    df.loc[df["location_label"].eq(""), "location_label"] = "Unknown location"

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


def build_marks(years):
    if len(years) <= 12:
        return {int(y): str(int(y)) for y in years}
    step = max(1, len(years) // 10)
    sampled = years[::step]
    if years[-1] not in sampled:
        sampled = list(sampled) + [years[-1]]
    return {int(y): str(int(y)) for y in sampled}


def filter_year_range(df: pd.DataFrame, year_range):
    start_year, end_year = sorted(year_range)
    return df[(df["year"] >= start_year) & (df["year"] <= end_year)].copy()


def build_location_options(df: pd.DataFrame):
    if df.empty:
        return []

    locations = (
        df[["location_label", "latitude", "longitude"]]
        .drop_duplicates()
        .sort_values(["location_label", "latitude", "longitude"])
        .reset_index(drop=True)
    )

    options = []
    for _, row in locations.iterrows():
        label = row["location_label"]
        lat = float(row["latitude"])
        lon = float(row["longitude"])
        value = f"{lat:.8f}|{lon:.8f}|{label}"
        options.append({"label": label, "value": value})

    return options


def encode_location_value(lat: float, lon: float, location_label: str) -> str:
    return f"{float(lat):.8f}|{float(lon):.8f}|{location_label}"


def decode_location_value(value):
    if not value:
        return None
    try:
        lat_str, lon_str, location_label = value.split("|", 2)
        return {
            "lat": float(lat_str),
            "lon": float(lon_str),
            "location_label": location_label,
        }
    except Exception:
        return None


DF = load_data()
LOCATION_OPTIONS = build_location_options(DF)

if not DF.empty:
    MIN_YEAR = int(DF["year"].min())
    MAX_YEAR = int(DF["year"].max())
    YEARS = list(range(MIN_YEAR, MAX_YEAR + 1))
else:
    MIN_YEAR = 0
    MAX_YEAR = 0
    YEARS = [0]

DEFAULT_RANGE = [YEARS[0], YEARS[-1]]

PERSON_OPTIONS = (
    DF[["person_id", "person_name"]]
    .drop_duplicates()
    .sort_values("person_name")
    .to_dict("records")
    if not DF.empty
    else []
)

app = Dash(__name__)
app.title = "Life Paths Prototype"

app.layout = html.Div(
    [
        dcc.Store(id="selected-people", data=[]),
        dcc.Store(id="clicked-location", data=None),
        html.Div(
            [
                html.H2("Life paths prototype", style={"marginBottom": "0.25rem"}),
                html.Div(
                    "Move through time, click paths or markers to select people, hover a location marker for an overview, and click a location marker for detailed location history.",
                    style={"color": "#555", "marginBottom": "0.75rem"},
                ),
            ],
            style={"padding": "1rem 1rem 0.25rem 1rem"},
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.Label("Events over time"),
                        dcc.Graph(
                            id="event-count-bar",
                            style={"height": "180px"},
                            config={"displayModeBar": False},
                        ),
                    ],
                    style={"marginBottom": "0.75rem"},
                ),
                html.Div(
                    [
                        html.Label("Year interval"),
                        dcc.RangeSlider(
                            id="year-slider",
                            min=YEARS[0],
                            max=YEARS[-1],
                            step=1,
                            value=DEFAULT_RANGE,
                            marks=build_marks(YEARS),
                            allowCross=False,
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                    ],
                    style={"marginBottom": "1rem"},
                ),
                html.Div(
                    [
                        html.Div(
                            [
                                html.Label("Highlight people"),
                                dcc.Dropdown(
                                    id="person-dropdown",
                                    options=[
                                        {
                                            "label": row["person_name"],
                                            "value": int(row["person_id"]),
                                        }
                                        for row in PERSON_OPTIONS
                                    ],
                                    value=[],
                                    multi=True,
                                    placeholder="Click a path or event marker, or choose names here",
                                ),
                            ],
                            style={"flex": "2", "minWidth": "320px"},
                        ),
                        html.Div(
                            [
                                html.Label("Display mode"),
                                dcc.RadioItems(
                                    id="display-mode",
                                    options=[
                                        {"label": "All paths", "value": "all"},
                                        {"label": "Selected only", "value": "selected"},
                                    ],
                                    value="all",
                                    inline=True,
                                ),
                            ],
                            style={"flex": "1", "minWidth": "220px"},
                        ),
                        html.Div(
                            [
                                html.Label("Direction cues"),
                                dcc.RadioItems(
                                    id="direction-mode",
                                    options=[
                                        {"label": "Off", "value": "off"},
                                        {"label": "Selected only", "value": "selected"},
                                        {"label": "All visible", "value": "all"},
                                    ],
                                    value="selected",
                                    inline=True,
                                ),
                            ],
                            style={"flex": "1", "minWidth": "260px"},
                        ),
                        html.Div(
                            [
                                html.Label("Background density"),
                                dcc.Slider(
                                    id="background-opacity",
                                    min=0.02,
                                    max=0.35,
                                    step=0.01,
                                    value=0.08,
                                    marks={0.05: "light", 0.15: "medium", 0.3: "dense"},
                                ),
                            ],
                            style={"flex": "1", "minWidth": "220px"},
                        ),
                    ],
                    style={
                        "display": "flex",
                        "gap": "1rem",
                        "flexWrap": "wrap",
                        "marginBottom": "1rem",
                    },
                ),
            ],
            style={"padding": "0.5rem 1rem 1rem 1rem"},
        ),
        html.Div(
            [
                html.Div(
                    [
                        dcc.Graph(
                            id="lifepath-map",
                            style={"height": "76vh"},
                            config={"displayModeBar": True},
                        ),
                    ],
                    style={"flex": "3", "minWidth": "700px"},
                ),
                html.Div(
                    [
                        html.H4("Selection"),
                        html.Div(id="selection-summary", style={"marginBottom": "0.75rem"}),
                        dash_table.DataTable(
                            id="person-table",
                            columns=[
                                {"name": "Person", "id": "person_name"},
                                {"name": "Visible events", "id": "visible_events"},
                                {"name": "First year", "id": "first_year"},
                                {"name": "Latest shown", "id": "latest_event"},
                            ],
                            data=[],
                            style_table={"overflowX": "auto"},
                            style_cell={
                                "textAlign": "left",
                                "padding": "6px",
                                "fontFamily": "sans-serif",
                            },
                            style_header={"fontWeight": "bold"},
                            page_size=12,
                        ),
                        html.Div(
                            [
                                html.H4("Selected person timeline"),
                                html.Div(
                                    id="selected-person-timeline",
                                    style={
                                        "maxHeight": "34vh",
                                        "overflowY": "auto",
                                        "paddingRight": "0.25rem",
                                    },
                                ),
                            ],
                            style={"marginTop": "1rem"},
                        ),
                        html.Div(
                            [
                                html.H4("Hovered location overview"),
                                html.Div(
                                    id="hover-location-summary",
                                    style={
                                        "maxHeight": "24vh",
                                        "overflowY": "auto",
                                        "paddingRight": "0.25rem",
                                    },
                                ),
                            ],
                            style={"marginTop": "1rem"},
                        ),
                        html.Div(
                            [
                                html.H4("Clicked location detail"),
                                html.Div(id="location-detail-header", style={"marginBottom": "0.5rem"}),
                                dcc.Dropdown(
                                    id="location-person-dropdown",
                                    options=[],
                                    value=None,
                                    placeholder="Choose a person at this location",
                                ),
                                html.Div(
                                    id="location-person-events",
                                    style={
                                        "maxHeight": "22vh",
                                        "overflowY": "auto",
                                        "paddingRight": "0.25rem",
                                        "marginTop": "0.75rem",
                                    },
                                ),
                            ],
                            style={"marginTop": "1rem"},
                        ),
                        html.Div(
                            [
                                html.H4("Place arrivals view"),
                                html.Div(
                                    "Use a clicked black marker or choose a place here.",
                                    style={"color": "#666", "marginBottom": "0.5rem"},
                                ),
                                html.Label("Place"),
                                dcc.Dropdown(
                                    id="arrival-location-dropdown",
                                    options=LOCATION_OPTIONS,
                                    value=None,
                                    placeholder="Type or select a location",
                                    searchable=True,
                                    clearable=True,
                                ),
                                html.Div(id="arrival-place-header", style={"marginTop": "0.75rem", "marginBottom": "0.5rem"}),
                                html.Label("Place-specific interval"),
                                dcc.RangeSlider(
                                    id="arrival-year-slider",
                                    min=YEARS[0],
                                    max=YEARS[-1],
                                    step=1,
                                    value=DEFAULT_RANGE,
                                    marks=build_marks(YEARS),
                                    allowCross=False,
                                    tooltip={"placement": "bottom", "always_visible": False},
                                ),
                                html.Div(id="arrival-summary", style={"marginTop": "0.75rem", "marginBottom": "0.75rem"}),
                                dcc.Graph(
                                    id="arrival-country-pie",
                                    style={"height": "300px"},
                                    config={"displayModeBar": False},
                                ),
                                dash_table.DataTable(
                                    id="arrival-table",
                                    columns=[
                                        {"name": "Person", "id": "person_name"},
                                        {"name": "Arrival date", "id": "arrival_date"},
                                        {"name": "To", "id": "to_location"},
                                        {"name": "From", "id": "from_location"},
                                        {"name": "Origin country", "id": "from_country"},
                                        {"name": "Previous date", "id": "from_date"},
                                        {"name": "Previous event", "id": "from_event_type"},
                                        {"name": "Arrival event", "id": "to_event_type"},
                                    ],
                                    data=[],
                                    style_table={"overflowX": "auto"},
                                    style_cell={
                                        "textAlign": "left",
                                        "padding": "6px",
                                        "fontFamily": "sans-serif",
                                        "whiteSpace": "normal",
                                        "height": "auto",
                                    },
                                    style_header={"fontWeight": "bold"},
                                    page_size=8,
                                ),
                            ],
                            style={"marginTop": "1rem"},
                        ),
                    ],
                    style={"flex": "1.25", "minWidth": "420px", "padding": "0.5rem 1rem 1rem 0.5rem"},
                ),
            ],
            style={
                "display": "flex",
                "gap": "0.5rem",
                "flexWrap": "wrap",
                "padding": "0 1rem 1rem 1rem",
            },
        ),
    ],
    style={"fontFamily": "Arial, sans-serif", "maxWidth": "2000px", "margin": "0 auto"},
)


def empty_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        annotations=[
            {
                "text": message,
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": 0.5,
                "showarrow": False,
                "font": {"size": 18},
            }
        ],
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
    )
    return fig


def empty_pie_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        annotations=[
            {
                "text": message,
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": 0.5,
                "showarrow": False,
                "font": {"size": 15},
            }
        ],
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        title="Origin countries",
    )
    return fig


def center_for(df: pd.DataFrame):
    if df.empty:
        return {"lat": 20, "lon": 0}
    return {"lat": float(df["latitude"].mean()), "lon": float(df["longitude"].mean())}


def build_event_count_figure(df: pd.DataFrame, year_range, selected_people=None) -> go.Figure:
    selected_people = selected_people or []
    start_year, end_year = sorted(year_range)

    base_df = df[df["person_id"].isin(selected_people)] if selected_people else df

    full_years = pd.DataFrame({"year": YEARS})
    if base_df.empty:
        counts = full_years.copy()
        counts["event_count"] = 0
    else:
        counts = (
            base_df.groupby("year", as_index=False)
            .size()
            .rename(columns={"size": "event_count"})
        )
        counts = full_years.merge(counts, on="year", how="left").fillna({"event_count": 0})

    colors = [
        "#c0392b" if start_year <= int(y) <= end_year else "#9aa5b1"
        for y in counts["year"]
    ]

    fig = go.Figure(
        data=[
            go.Bar(
                x=counts["year"],
                y=counts["event_count"],
                marker={"color": colors},
                hovertemplate="Year %{x}<br>Events %{y}<extra></extra>",
                showlegend=False,
            )
        ]
    )
    fig.update_layout(
        template="plotly_white",
        margin={"l": 35, "r": 10, "t": 10, "b": 30},
        xaxis_title=None,
        yaxis_title="Events",
        bargap=0.05,
        dragmode=False,
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(showgrid=True, rangemode="tozero")
    return fig


def build_origin_country_pie(arrivals_df: pd.DataFrame) -> go.Figure:
    if arrivals_df.empty:
        return empty_pie_figure("No arrivals to summarize.")

    pie_df = (
        arrivals_df.groupby("from_country", as_index=False)
        .size()
        .rename(columns={"size": "count"})
        .sort_values("count", ascending=False)
    )

    fig = go.Figure(
        data=[
            go.Pie(
                labels=pie_df["from_country"],
                values=pie_df["count"],
                hole=0.25,
                textinfo="label+percent",
                hovertemplate="%{label}<br>Arrivals %{value}<br>%{percent}<extra></extra>",
            )
        ]
    )
    fig.update_layout(
        template="plotly_white",
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        title="Origin countries",
    )
    return fig


def add_path_trace(fig, person_df, selected=False, opacity=0.08):
    person_df = person_df.sort_values(["event_date", "event_order"]).copy()
    if len(person_df) < 2:
        return

    color = "#c0392b" if selected else "#7f8c8d"
    width = 4 if selected else 1
    person_name = person_df["person_name"].iloc[0]
    person_id = int(person_df["person_id"].iloc[0])

    fig.add_trace(
        go.Scattermap(
            lat=person_df["latitude"],
            lon=person_df["longitude"],
            mode="lines",
            line={"width": width, "color": color},
            opacity=0.95 if selected else opacity,
            name=person_name,
            hovertemplate=f"<b>{person_name}</b><br>Path<extra></extra>",
            customdata=[[person_id, person_name, "path"]] * len(person_df),
            showlegend=False,
        )
    )


def add_direction_markers(fig, person_df, selected=False):
    person_df = person_df.sort_values(["event_date", "event_order"]).copy()
    if person_df.empty:
        return

    numbered = person_df.copy()
    numbered["sequence_label"] = [str(i) for i in range(1, len(numbered) + 1)]
    numbered["dir_hover"] = (
        "<b>" + numbered["person_name"] + "</b><br>"
        + "Step " + numbered["sequence_label"] + "<br>"
        + numbered["event_type_name"].str.title() + "<br>"
        + numbered["location_label"] + "<br>"
        + numbered["event_date"].dt.strftime("%Y-%m-%d")
    )

    fig.add_trace(
        go.Scattermap(
            lat=numbered["latitude"],
            lon=numbered["longitude"],
            mode="markers+text",
            marker={
                "size": 18 if selected else 10,
                "color": "#111111" if selected else "#666666",
                "opacity": 0.95 if selected else 0.6,
            },
            text=numbered["sequence_label"],
            textfont={"size": 10 if selected else 8, "color": "#ffffff"},
            textposition="middle center",
            customdata=numbered[["person_id", "person_name"]].assign(kind="direction").values,
            hovertext=numbered["dir_hover"],
            hovertemplate="%{hovertext}<extra></extra>",
            showlegend=False,
        )
    )

    start = person_df.head(1).copy()
    start["start_hover"] = (
        "<b>" + start["person_name"] + "</b><br>"
        + "Start<br>"
        + start["event_type_name"].str.title() + "<br>"
        + start["location_label"] + "<br>"
        + start["event_date"].dt.strftime("%Y-%m-%d")
    )

    end = person_df.tail(1).copy()
    end["end_hover"] = (
        "<b>" + end["person_name"] + "</b><br>"
        + ("Now" if selected else "End") + "<br>"
        + end["event_type_name"].str.title() + "<br>"
        + end["location_label"] + "<br>"
        + end["event_date"].dt.strftime("%Y-%m-%d")
    )

    fig.add_trace(
        go.Scattermap(
            lat=start["latitude"],
            lon=start["longitude"],
            mode="markers+text",
            marker={"size": 20 if selected else 12, "color": "#2ca02c", "opacity": 0.95},
            text=["Start"],
            textposition="bottom right",
            customdata=start[["person_id", "person_name"]].assign(kind="start").values,
            hovertext=start["start_hover"],
            hovertemplate="%{hovertext}<extra></extra>",
            showlegend=False,
        )
    )

    fig.add_trace(
        go.Scattermap(
            lat=end["latitude"],
            lon=end["longitude"],
            mode="markers+text",
            marker={"size": 22 if selected else 14, "color": "#d62728", "opacity": 0.98},
            text=["Now" if selected else "End"],
            textposition="top right",
            customdata=end[["person_id", "person_name"]].assign(kind="end").values,
            hovertext=end["end_hover"],
            hovertemplate="%{hovertext}<extra></extra>",
            showlegend=False,
        )
    )


def add_event_markers(fig, event_df, selected_ids):
    if event_df.empty:
        return

    for event_type, group in event_df.groupby("event_type_name"):
        group = group.copy()
        sizes = [12 if pid in selected_ids else 7 for pid in group["person_id"]]

        fig.add_trace(
            go.Scattermap(
                lat=group["latitude"],
                lon=group["longitude"],
                mode="markers",
                marker={
                    "size": sizes,
                    "color": EVENT_COLORS.get(event_type, "#636efa"),
                    "opacity": 0.9,
                },
                name=event_type.title(),
                customdata=group[["person_id", "person_name"]].assign(kind="event").values,
                hovertext=group["hover_text"],
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=True,
            )
        )


def aggregate_year_locations(events_df: pd.DataFrame, year_range) -> pd.DataFrame:
    if events_df.empty:
        return pd.DataFrame()

    start_year, end_year = sorted(year_range)
    current = events_df[
        (events_df["year"] >= start_year) & (events_df["year"] <= end_year)
    ].copy()

    if current.empty:
        return pd.DataFrame()

    grouped_rows = []

    for (lat, lon, location_label), group in current.groupby(
        ["latitude", "longitude", "location_label"], dropna=False
    ):
        people = group[["person_id", "person_name"]].drop_duplicates().sort_values("person_name")
        events = group.sort_values(["person_name", "event_order", "event_date"])

        visits_in_range = (
            events_df[
                (events_df["year"] >= start_year)
                & (events_df["year"] <= end_year)
                & (events_df["latitude"] == lat)
                & (events_df["longitude"] == lon)
            ]
            .groupby(["person_id", "person_name"], as_index=False)
            .size()
            .rename(columns={"size": "visits"})
            .sort_values(["visits", "person_name"], ascending=[False, True])
        )

        hover_lines = [
            f"<b>{location_label or 'Unknown location'}</b>",
            f"Interval: {start_year}–{end_year}",
            f"People in interval: {people['person_id'].nunique()}",
            f"Events in interval: {len(events)}",
            "",
            "<b>People overview</b>",
        ]

        for _, row in visits_in_range.iterrows():
            hover_lines.append(f"• {row['person_name']} — {row['visits']} visits")

        grouped_rows.append(
            {
                "latitude": lat,
                "longitude": lon,
                "location_label": location_label,
                "start_year": start_year,
                "end_year": end_year,
                "people_count": int(people["person_id"].nunique()),
                "events_count": int(len(events)),
                "marker_size": 10
                + 4 * (people["person_id"].nunique() - 1)
                + 2 * max(0, len(events) - people["person_id"].nunique()),
                "hover_text": "<br>".join(hover_lines),
            }
        )

    return pd.DataFrame(grouped_rows)


def add_year_location_markers(fig, grouped_df: pd.DataFrame):
    if grouped_df.empty:
        return

    fig.add_trace(
        go.Scattermap(
            lat=grouped_df["latitude"],
            lon=grouped_df["longitude"],
            mode="markers",
            marker={
                "size": grouped_df["marker_size"],
                "color": "#111111",
                "opacity": 0.8,
                "allowoverlap": True,
            },
            text=grouped_df["hover_text"],
            hovertemplate="%{text}<extra></extra>",
            customdata=grouped_df[
                ["latitude", "longitude", "location_label", "start_year", "end_year"]
            ].values,
            name="Events in selected interval",
            showlegend=True,
        )
    )


def build_selected_timeline(all_visible_df: pd.DataFrame, selected_people):
    if not selected_people:
        return html.Div(
            "Select one or more people to see their chronological events.",
            style={"color": "#666"},
        )

    selected_df = (
        all_visible_df[all_visible_df["person_id"].isin(selected_people)]
        .sort_values(["person_name", "event_date", "event_order", "location_label"])
        .copy()
    )

    if selected_df.empty:
        return html.Div(
            "No visible events for the selected people in this interval.",
            style={"color": "#666"},
        )

    blocks = []

    for person_name, person_df in selected_df.groupby("person_name"):
        items = []

        for _, row in person_df.iterrows():
            when = row["event_date"].strftime("%Y-%m-%d") if pd.notna(row["event_date"]) else "Unknown date"
            location = row["location_label"] if pd.notna(row["location_label"]) and row["location_label"] else "Unknown location"
            description = row["description"] if pd.notna(row["description"]) and row["description"] else row["event_type_name"].title()

            items.append(
                html.Div(
                    [
                        html.Div(when, style={"fontWeight": "bold", "minWidth": "92px"}),
                        html.Div(
                            [
                                html.Div(row["event_type_name"].title(), style={"fontWeight": "bold"}),
                                html.Div(description),
                                html.Div(location, style={"color": "#666", "fontSize": "0.92rem"}),
                            ],
                            style={"flex": "1"},
                        ),
                    ],
                    style={
                        "display": "flex",
                        "gap": "0.65rem",
                        "padding": "0.45rem 0",
                        "borderBottom": "1px solid #eee",
                        "alignItems": "flex-start",
                    },
                )
            )

        blocks.append(
            html.Div(
                [
                    html.Div(
                        person_name,
                        style={"fontWeight": "bold", "fontSize": "1rem", "marginBottom": "0.35rem"},
                    ),
                    html.Div(items),
                ],
                style={
                    "marginBottom": "1rem",
                    "padding": "0.75rem",
                    "border": "1px solid #ddd",
                    "borderRadius": "10px",
                },
            )
        )

    return blocks


def summarize_location_year(events_df: pd.DataFrame, year_range, lat: float, lon: float):
    start_year, end_year = sorted(year_range)

    current = events_df[
        (events_df["year"] >= start_year)
        & (events_df["year"] <= end_year)
        & (events_df["latitude"] == lat)
        & (events_df["longitude"] == lon)
    ].copy()

    if current.empty:
        return None

    location_label = current["location_label"].iloc[0]

    historical = current.copy()

    people_now = (
        current[["person_id", "person_name"]]
        .drop_duplicates()
        .sort_values("person_name")
    )

    visit_counts = (
        historical.groupby(["person_id", "person_name"], as_index=False)
        .size()
        .rename(columns={"size": "visits"})
        .sort_values(["visits", "person_name"], ascending=[False, True])
    )

    return {
        "location_label": location_label,
        "start_year": start_year,
        "end_year": end_year,
        "people_now": people_now,
        "visit_counts": visit_counts,
        "current_events": current,
        "historical_events": historical,
    }


def extract_country_from_location_label(location_label: str) -> str:
    if not location_label:
        return "Unknown origin"
    if ", " in location_label:
        return location_label.split(", ")[-1].strip() or "Unknown origin"
    return location_label.strip() or "Unknown origin"


def build_arrivals_for_location(df: pd.DataFrame, lat: float, lon: float, year_range):
    start_year, end_year = sorted(year_range)
    arrivals = []

    for _, person_df in df.groupby("person_id"):
        person_df = person_df.sort_values(["event_date", "event_order", "location_id"]).reset_index(drop=True)

        for i, row in person_df.iterrows():
            row_year = int(row["year"])
            if not (start_year <= row_year <= end_year):
                continue

            is_target = float(row["latitude"]) == float(lat) and float(row["longitude"]) == float(lon)
            if not is_target:
                continue

            prev_row = person_df.iloc[i - 1] if i > 0 else None

            if prev_row is None:
                from_location = "First known location"
                from_date = ""
                from_event_type = ""
                from_country = "First known location"
            else:
                prev_same_place = (
                    float(prev_row["latitude"]) == float(lat)
                    and float(prev_row["longitude"]) == float(lon)
                )
                if prev_same_place:
                    continue

                from_location = prev_row["location_label"]
                from_date = (
                    prev_row["event_date"].strftime("%Y-%m-%d")
                    if pd.notna(prev_row["event_date"])
                    else ""
                )
                from_event_type = str(prev_row["event_type_name"]).title()
                from_country = extract_country_from_location_label(from_location)

            arrivals.append(
                {
                    "person_id": int(row["person_id"]),
                    "person_name": row["person_name"],
                    "arrival_date": row["event_date"].strftime("%Y-%m-%d")
                    if pd.notna(row["event_date"])
                    else "",
                    "arrival_year": int(row["year"]),
                    "to_location": row["location_label"],
                    "to_event_type": str(row["event_type_name"]).title(),
                    "from_location": from_location,
                    "from_country": from_country,
                    "from_date": from_date,
                    "from_event_type": from_event_type,
                }
            )

    if not arrivals:
        return pd.DataFrame()

    out = pd.DataFrame(arrivals)
    out = out.sort_values(["arrival_date", "person_name"]).reset_index(drop=True)
    return out


@app.callback(
    Output("year-slider", "value"),
    Input("event-count-bar", "clickData"),
    State("year-slider", "value"),
    prevent_initial_call=True,
)
def sync_year_from_bar(click_data, current_range):
    if not click_data or not click_data.get("points"):
        return current_range

    point = click_data["points"][0]
    x = point.get("x")

    try:
        clicked_year = int(x)
    except (TypeError, ValueError):
        return current_range

    start_year, end_year = sorted(current_range)

    if clicked_year < start_year:
        start_year = clicked_year
    elif clicked_year > end_year:
        end_year = clicked_year
    else:
        start_year = clicked_year
        end_year = clicked_year

    return [start_year, end_year]


@app.callback(
    Output("selected-people", "data"),
    Output("person-dropdown", "value"),
    Input("lifepath-map", "clickData"),
    Input("person-dropdown", "value"),
    State("selected-people", "data"),
    prevent_initial_call=True,
)
def sync_selection(click_data, dropdown_value, selected_people):
    ctx = dash.callback_context
    selected_people = selected_people or []
    dropdown_value = dropdown_value or []

    if not ctx.triggered:
        return selected_people, dropdown_value

    trigger = ctx.triggered[0]["prop_id"].split(".")[0]

    if trigger == "person-dropdown":
        cleaned = sorted({int(x) for x in dropdown_value})
        return cleaned, cleaned

    if trigger == "lifepath-map" and click_data and click_data.get("points"):
        customdata = click_data["points"][0].get("customdata")
        if customdata:
            if len(customdata) == 5:
                return selected_people, dropdown_value
            try:
                person_id = int(customdata[0])
                if person_id in selected_people:
                    cleaned = [pid for pid in selected_people if pid != person_id]
                else:
                    cleaned = sorted(selected_people + [person_id])
                return cleaned, cleaned
            except (TypeError, ValueError):
                pass

    cleaned = sorted({int(x) for x in dropdown_value})
    return cleaned, cleaned


@app.callback(
    Output("clicked-location", "data"),
    Output("arrival-location-dropdown", "value"),
    Input("lifepath-map", "clickData"),
    Input("arrival-location-dropdown", "value"),
    State("year-slider", "value"),
    State("clicked-location", "data"),
    prevent_initial_call=True,
)
def store_selected_location(click_data, dropdown_value, selected_range, current_clicked_location):
    ctx = dash.callback_context
    if not ctx.triggered:
        return current_clicked_location, dropdown_value

    trigger = ctx.triggered[0]["prop_id"].split(".")[0]
    start_year, end_year = sorted(selected_range)

    if trigger == "lifepath-map":
        if not click_data or not click_data.get("points"):
            return current_clicked_location, dropdown_value

        customdata = click_data["points"][0].get("customdata")
        if not customdata or len(customdata) != 5:
            return current_clicked_location, dropdown_value

        lat, lon, location_label, marker_start_year, marker_end_year = customdata
        value = encode_location_value(float(lat), float(lon), location_label)
        return (
            {
                "lat": float(lat),
                "lon": float(lon),
                "location_label": location_label,
                "start_year": int(marker_start_year),
                "end_year": int(marker_end_year),
            },
            value,
        )

    if trigger == "arrival-location-dropdown":
        decoded = decode_location_value(dropdown_value)
        if not decoded:
            return None, None

        return (
            {
                "lat": decoded["lat"],
                "lon": decoded["lon"],
                "location_label": decoded["location_label"],
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
            dropdown_value,
        )

    return current_clicked_location, dropdown_value


@app.callback(
    Output("hover-location-summary", "children"),
    Input("lifepath-map", "hoverData"),
    State("year-slider", "value"),
    State("selected-people", "data"),
)
def update_hover_summary(hover_data, selected_range, selected_people):
    default_msg = html.Div(
        "Hover over a black location marker to see who appears there in the selected interval.",
        style={"color": "#666"},
    )

    if not hover_data or not hover_data.get("points"):
        return default_msg

    point = hover_data["points"][0]
    customdata = point.get("customdata")

    if not customdata or len(customdata) != 5:
        return default_msg

    lat, lon, location_label, start_year, end_year = customdata

    base_df = DF
    if selected_people:
        filtered = DF[DF["person_id"].isin(selected_people)]
        if not filtered.empty:
            base_df = filtered

    summary = summarize_location_year(
        base_df,
        [int(start_year), int(end_year)],
        float(lat),
        float(lon),
    )
    if not summary:
        return html.Div("No details available.", style={"color": "#666"})

    lines = [
        html.Li(f"{row['person_name']} — {row['visits']} visits")
        for _, row in summary["visit_counts"].iterrows()
    ]

    return html.Div(
        [
            html.Div(summary["location_label"], style={"fontWeight": "bold"}),
            html.Div(f"Interval: {summary['start_year']}–{summary['end_year']}"),
            html.Div(f"People there in interval: {len(summary['people_now'])}"),
            html.Div(f"Events there in interval: {len(summary['current_events'])}"),
            html.Div("People overview", style={"marginTop": "0.5rem", "fontWeight": "bold"}),
            html.Ul(lines, style={"marginTop": "0.35rem"}),
            html.Div(
                "Click this location marker to browse each person's events there in the interval.",
                style={"marginTop": "0.5rem", "color": "#666", "fontSize": "0.92rem"},
            ),
        ]
    )


@app.callback(
    Output("location-detail-header", "children"),
    Output("location-person-dropdown", "options"),
    Output("location-person-dropdown", "value"),
    Input("clicked-location", "data"),
    State("selected-people", "data"),
)
def update_location_detail_header(clicked_location, selected_people):
    if not clicked_location:
        return (
            html.Div("Click a black location marker to inspect a place.", style={"color": "#666"}),
            [],
            None,
        )

    base_df = DF
    if selected_people:
        filtered = DF[DF["person_id"].isin(selected_people)]
        if not filtered.empty:
            base_df = filtered

    summary = summarize_location_year(
        base_df,
        [clicked_location["start_year"], clicked_location["end_year"]],
        clicked_location["lat"],
        clicked_location["lon"],
    )

    if not summary:
        return html.Div("No details available.", style={"color": "#666"}), [], None

    options = [
        {"label": row["person_name"], "value": int(row["person_id"])}
        for _, row in summary["people_now"].iterrows()
    ]
    default_value = options[0]["value"] if options else None

    header = html.Div(
        [
            html.Div(summary["location_label"], style={"fontWeight": "bold"}),
            html.Div(f"Interval: {summary['start_year']}–{summary['end_year']}"),
            html.Div(f"People there in interval: {len(summary['people_now'])}"),
            html.Div(f"Events there in interval: {len(summary['current_events'])}"),
        ]
    )

    return header, options, default_value


@app.callback(
    Output("location-person-events", "children"),
    Input("clicked-location", "data"),
    Input("location-person-dropdown", "value"),
)
def update_location_person_events(clicked_location, person_id):
    if not clicked_location or person_id is None:
        return html.Div("Select a clicked location and a person.", style={"color": "#666"})

    lat = clicked_location["lat"]
    lon = clicked_location["lon"]
    start_year = clicked_location["start_year"]
    end_year = clicked_location["end_year"]

    person_events = DF[
        (DF["person_id"] == person_id)
        & (DF["year"] >= start_year)
        & (DF["year"] <= end_year)
        & (DF["latitude"] == lat)
        & (DF["longitude"] == lon)
    ].sort_values(["event_date", "event_order"])

    if person_events.empty:
        return html.Div("No events found for this person at this location.", style={"color": "#666"})

    person_name = person_events["person_name"].iloc[0]
    visit_count = len(person_events)

    rows = []
    for _, row in person_events.iterrows():
        when = row["event_date"].strftime("%Y-%m-%d") if pd.notna(row["event_date"]) else "Unknown date"
        desc = row["description"] if pd.notna(row["description"]) and row["description"] else row["event_type_name"].title()
        rows.append(
            html.Div(
                [
                    html.Div(when, style={"fontWeight": "bold", "minWidth": "92px"}),
                    html.Div(
                        [
                            html.Div(row["event_type_name"].title(), style={"fontWeight": "bold"}),
                            html.Div(desc),
                        ],
                        style={"flex": "1"},
                    ),
                ],
                style={
                    "display": "flex",
                    "gap": "0.65rem",
                    "padding": "0.45rem 0",
                    "borderBottom": "1px solid #eee",
                },
            )
        )

    return html.Div(
        [
            html.Div(f"{person_name} — events here in interval", style={"fontWeight": "bold"}),
            html.Div(f"Visits in {start_year}–{end_year}: {visit_count}", style={"marginBottom": "0.5rem", "color": "#666"}),
            html.Div(rows),
        ]
    )


@app.callback(
    Output("arrival-year-slider", "value"),
    Input("clicked-location", "data"),
    State("arrival-year-slider", "value"),
)
def sync_arrival_slider(clicked_location, current_value):
    if not clicked_location:
        return current_value or DEFAULT_RANGE
    return [clicked_location["start_year"], clicked_location["end_year"]]


@app.callback(
    Output("arrival-place-header", "children"),
    Output("arrival-summary", "children"),
    Output("arrival-country-pie", "figure"),
    Output("arrival-table", "data"),
    Input("clicked-location", "data"),
    Input("arrival-year-slider", "value"),
    State("selected-people", "data"),
)
def update_arrivals_view(clicked_location, arrival_range, selected_people):
    if not clicked_location:
        return (
            html.Div("No place selected.", style={"color": "#666"}),
            html.Div("Click a black location marker or use the place dropdown.", style={"color": "#666"}),
            empty_pie_figure("Select a place first."),
            [],
        )

    lat = clicked_location["lat"]
    lon = clicked_location["lon"]
    location_label = clicked_location["location_label"]
    start_year, end_year = sorted(arrival_range)

    base_df = DF
    if selected_people:
        filtered = DF[DF["person_id"].isin(selected_people)]
        if not filtered.empty:
            base_df = filtered

    arrivals_df = build_arrivals_for_location(base_df, lat, lon, [start_year, end_year])

    header = html.Div(
        [
            html.Div(location_label, style={"fontWeight": "bold"}),
            html.Div(f"Coordinates: {lat:.4f}, {lon:.4f}", style={"color": "#666", "fontSize": "0.92rem"}),
        ]
    )

    if arrivals_df.empty:
        summary = html.Div(
            [
                html.Div(f"Interval: {start_year}–{end_year}"),
                html.Div("No arrivals from another recorded location in this interval."),
            ]
        )
        return header, summary, empty_pie_figure("No origin-country data for this interval."), []

    unique_people = arrivals_df["person_id"].nunique()

    summary = html.Div(
        [
            html.Div(f"Interval: {start_year}–{end_year}"),
            html.Div(f"Arrivals recorded: {len(arrivals_df)}"),
            html.Div(f"People arriving: {unique_people}"),
            html.Div(
                "Each row shows a move into this place and the previous recorded place for that person.",
                style={"color": "#666", "fontSize": "0.92rem", "marginTop": "0.3rem"},
            ),
        ]
    )

    pie_fig = build_origin_country_pie(arrivals_df)

    return header, summary, pie_fig, arrivals_df[
        [
            "person_name",
            "arrival_date",
            "to_location",
            "from_location",
            "from_country",
            "from_date",
            "from_event_type",
            "to_event_type",
        ]
    ].to_dict("records")


@app.callback(
    Output("event-count-bar", "figure"),
    Output("lifepath-map", "figure"),
    Output("selection-summary", "children"),
    Output("person-table", "data"),
    Output("selected-person-timeline", "children"),
    Input("year-slider", "value"),
    Input("selected-people", "data"),
    Input("display-mode", "value"),
    Input("direction-mode", "value"),
    Input("background-opacity", "value"),
)
def update_map(selected_range, selected_people, display_mode, direction_mode, background_opacity):
    if DF.empty:
        empty_bar = build_event_count_figure(pd.DataFrame(columns=["year", "person_id"]), selected_range, selected_people)
        return (
            empty_bar,
            empty_figure("No geocoded events found in the database."),
            "No data available.",
            [],
            html.Div("No data available.", style={"color": "#666"}),
        )

    selected_people = selected_people or []
    start_year, end_year = sorted(selected_range)

    visible_df = filter_year_range(DF, [start_year, end_year])
    year_df = visible_df.copy()

    bar_fig = build_event_count_figure(DF, [start_year, end_year], selected_people)

    if visible_df.empty:
        return (
            bar_fig,
            empty_figure("No events available in this interval."),
            f"No events available in {start_year}–{end_year}.",
            [],
            html.Div("No events available for this interval.", style={"color": "#666"}),
        )

    fig = go.Figure()

    if display_mode == "all":
        for _, person_df in visible_df.groupby("person_id"):
            add_path_trace(fig, person_df, selected=False, opacity=background_opacity)
            if direction_mode == "all":
                add_direction_markers(fig, person_df, selected=False)
    elif selected_people:
        selected_visible = visible_df[visible_df["person_id"].isin(selected_people)]
        for _, person_df in selected_visible.groupby("person_id"):
            add_path_trace(fig, person_df, selected=False, opacity=0.16)
            if direction_mode == "all":
                add_direction_markers(fig, person_df, selected=False)

    if selected_people:
        highlighted = visible_df[visible_df["person_id"].isin(selected_people)]

        for _, person_df in highlighted.groupby("person_id"):
            add_path_trace(fig, person_df, selected=True, opacity=1)
            if direction_mode in {"selected", "all"}:
                add_direction_markers(fig, person_df, selected=True)

        add_event_markers(fig, highlighted, set(selected_people))
        add_year_location_markers(fig, aggregate_year_locations(highlighted, [start_year, end_year]))
    else:
        add_event_markers(fig, visible_df, set())
        add_year_location_markers(fig, aggregate_year_locations(visible_df, [start_year, end_year]))

    fig.update_layout(
        template="plotly_white",
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        legend={"orientation": "h", "y": 0.01, "x": 0.01},
        map={
            "style": "open-street-map",
            "center": center_for(visible_df),
            "zoom": 2.2,
        },
    )

    if selected_people:
        selection_df = visible_df[visible_df["person_id"].isin(selected_people)]
        summary = html.Div(
            [
                html.Div(f"Interval: {start_year}–{end_year}"),
                html.Div(f"Selected people: {len(selected_people)}"),
                html.Div(f"Visible events for selection: {len(selection_df)}"),
                html.Div(f"Events in selected interval: {len(selection_df)}"),
            ]
        )
        table_df = (
            selection_df.sort_values(["person_name", "event_date"])
            .groupby(["person_id", "person_name"], as_index=False)
            .agg(
                visible_events=("event_type_name", "count"),
                first_year=("year", "min"),
                latest_event=("location_label", "last"),
            )
            .sort_values("person_name")
        )
    else:
        summary = html.Div(
            [
                html.Div(f"Interval: {start_year}–{end_year}"),
                html.Div(f"Visible people: {visible_df['person_id'].nunique()}"),
                html.Div(f"Visible geocoded events in interval: {len(visible_df)}"),
                html.Div(f"Events in selected interval: {len(year_df)}"),
            ]
        )
        table_df = (
            visible_df.sort_values(["person_name", "event_date"])
            .groupby(["person_id", "person_name"], as_index=False)
            .agg(
                visible_events=("event_type_name", "count"),
                first_year=("year", "min"),
                latest_event=("location_label", "last"),
            )
            .sort_values(["visible_events", "person_name"], ascending=[False, True])
            .head(20)
        )

    timeline_children = build_selected_timeline(visible_df, selected_people)
    return bar_fig, fig, summary, table_df.to_dict("records"), timeline_children


if __name__ == "__main__":
    app.run(debug=True)