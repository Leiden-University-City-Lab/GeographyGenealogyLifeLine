import React, { useState, useCallback } from 'react';
import PropTypes from 'prop-types';

const EVENT_COLORS = {
    birth:     '#2ca02c',
    education: '#1f77b4',
    career:    '#ff7f0e',
    death:     '#d62728',
};

const TIMELINE_W = 320;
const TIMELINE_H = 36;
const MARKER_R   = 5;

function MiniTimeline({ events, birthYear, deathYear, color }) {
    const [hovered, setHovered] = useState(null);

    const years = events.map(e => e.year).filter(Boolean);
    const allYears = [birthYear, deathYear, ...years].filter(y => y != null);
    const minY = Math.min(...allYears);
    const maxY = Math.max(...allYears);
    const span = Math.max(maxY - minY, 1);

    const xOf = year => ((year - minY) / span) * (TIMELINE_W - 20) + 10;

    // group events that land within 8px of each other to reduce clutter
    const positioned = events
        .filter(e => e.year != null)
        .map(e => ({ ...e, x: xOf(e.year) }))
        .sort((a, b) => a.x - b.x);

    return (
        <div style={{ position: 'relative' }}>
            <svg
                width="100%"
                viewBox={`0 0 ${TIMELINE_W} ${TIMELINE_H}`}
                style={{ overflow: 'visible', display: 'block' }}
            >
                {/* life-span bar */}
                {birthYear != null && deathYear != null && (
                    <rect
                        x={xOf(birthYear)} y={TIMELINE_H / 2 - 2}
                        width={xOf(deathYear) - xOf(birthYear)} height={4}
                        fill={color} opacity={0.25} rx={2}
                    />
                )}

                {/* axis line */}
                <line
                    x1={10} y1={TIMELINE_H / 2}
                    x2={TIMELINE_W - 10} y2={TIMELINE_H / 2}
                    stroke="#e0e0e0" strokeWidth={1}
                />

                {/* event markers */}
                {positioned.map((ev, i) => (
                    <circle
                        key={i}
                        cx={ev.x} cy={TIMELINE_H / 2}
                        r={hovered === i ? MARKER_R + 2 : MARKER_R}
                        fill={EVENT_COLORS[ev.type] || '#888'}
                        stroke="white" strokeWidth={1.5}
                        style={{ cursor: 'pointer', transition: 'r 0.12s ease' }}
                        onMouseEnter={() => setHovered(i)}
                        onMouseLeave={() => setHovered(null)}
                    />
                ))}

                {/* year labels */}
                <text x={10} y={TIMELINE_H - 2} fontSize={8} fill="#bbb" textAnchor="middle">
                    {minY}
                </text>
                <text x={TIMELINE_W - 10} y={TIMELINE_H - 2} fontSize={8} fill="#bbb" textAnchor="middle">
                    {maxY}
                </text>
            </svg>

            {/* hover tooltip — rendered in HTML for easy layout */}
            {hovered !== null && positioned[hovered] && (
                <div style={{
                    position:    'absolute',
                    bottom:      '100%',
                    left:        `${(positioned[hovered].x / TIMELINE_W) * 100}%`,
                    transform:   'translateX(-50%)',
                    background:  'rgba(30,30,30,0.92)',
                    color:       '#fff',
                    fontSize:    '11px',
                    padding:     '5px 9px',
                    borderRadius:'5px',
                    whiteSpace:  'nowrap',
                    pointerEvents:'none',
                    zIndex:      100,
                    boxShadow:   '0 2px 8px rgba(0,0,0,0.25)',
                }}>
                    <span style={{ color: EVENT_COLORS[positioned[hovered].type] || '#aaa', fontWeight: 700 }}>
                        {positioned[hovered].type}
                    </span>
                    {' · '}{positioned[hovered].year}
                    {positioned[hovered].location && (
                        <span style={{ color: '#ccc' }}> · {positioned[hovered].location}</span>
                    )}
                    {positioned[hovered].description && (
                        <div style={{ color: '#aaa', marginTop: 2 }}>
                            {positioned[hovered].description}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

/**
 * PersonCard — a rich, interactive person summary card for the Life Paths app.
 *
 * Shows name, faculty badge, birth–death years, event count, a mini SVG
 * timeline with hover tooltips, and a click-to-select interaction.
 *
 * The `selected` prop is two-way: Dash reads it to know who is selected;
 * the component writes it back via `setProps` when the user clicks.
 */
const PersonCard = ({
    id,
    name,
    faculty,
    color,
    birth_year,
    death_year,
    events,
    selected,
    setProps,
}) => {
    const handleClick = useCallback(() => {
        setProps({ selected: !selected });
    }, [selected, setProps]);

    const nCities = [...new Set((events || []).map(e => e.location).filter(Boolean))].length;

    return (
        <div
            id={id}
            onClick={handleClick}
            style={{
                border:        `2px solid ${color}`,
                borderRadius:  '8px',
                padding:       '10px 14px',
                marginBottom:  '8px',
                background:    selected ? `${color}18` : '#fff',
                cursor:        'pointer',
                transition:    'background 0.18s ease, box-shadow 0.18s ease',
                boxShadow:     selected
                    ? `0 0 0 3px ${color}55, 0 2px 8px rgba(0,0,0,0.08)`
                    : '0 1px 3px rgba(0,0,0,0.06)',
                userSelect:    'none',
            }}
        >
            {/* Header row */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <strong style={{ color, fontSize: '13px', lineHeight: 1.3 }}>{name}</strong>
                {faculty && (
                    <span style={{
                        fontSize:     '10px',
                        background:   color,
                        color:        '#fff',
                        padding:      '2px 7px',
                        borderRadius: '10px',
                        marginLeft:   '8px',
                        whiteSpace:   'nowrap',
                        flexShrink:   0,
                    }}>
                        {faculty}
                    </span>
                )}
            </div>

            {/* Stats row */}
            <div style={{ fontSize: '11px', color: '#888', marginBottom: 8, display: 'flex', gap: '12px' }}>
                {birth_year != null && (
                    <span>
                        {birth_year}{death_year != null ? `–${death_year}` : '–?'}
                    </span>
                )}
                <span>{(events || []).length} events</span>
                {nCities > 0 && <span>{nCities} {nCities === 1 ? 'city' : 'cities'}</span>}
            </div>

            {/* Mini timeline */}
            {events && events.length > 0 && (
                <MiniTimeline
                    events={events}
                    birthYear={birth_year}
                    deathYear={death_year}
                    color={color}
                />
            )}

            {/* Selection indicator */}
            {selected && (
                <div style={{
                    marginTop:  '6px',
                    fontSize:   '10px',
                    color:      color,
                    fontWeight: 700,
                    letterSpacing: '0.05em',
                }}>
                    ✓ SELECTED — click to deselect
                </div>
            )}
        </div>
    );
};

PersonCard.defaultProps = {
    faculty:    null,
    color:      '#7f8c8d',
    birth_year: null,
    death_year: null,
    events:     [],
    selected:   false,
};

PersonCard.propTypes = {
    /** Component id (required by Dash) */
    id: PropTypes.string.isRequired,
    /** Person's display name */
    name: PropTypes.string.isRequired,
    /** Faculty/department label shown as a badge */
    faculty: PropTypes.string,
    /** Hex colour for the card border and timeline */
    color: PropTypes.string,
    /** Birth year (integer) */
    birth_year: PropTypes.number,
    /** Death year (integer) */
    death_year: PropTypes.number,
    /**
     * List of events to plot on the mini timeline.
     * Each item: { year, type, location, description }
     */
    events: PropTypes.arrayOf(PropTypes.shape({
        year:        PropTypes.number,
        type:        PropTypes.string,
        location:    PropTypes.string,
        description: PropTypes.string,
    })),
    /** Whether this card is currently selected */
    selected: PropTypes.bool,
    /** Dash-injected setter — writes { selected } back to Python */
    setProps: PropTypes.func,
};

export default PersonCard;
