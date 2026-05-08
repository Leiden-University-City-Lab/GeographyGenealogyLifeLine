#!/usr/bin/env python3
"""
JSON → MonetDB importer
========================
Reads enriched professor JSON files and generates SQL for:
  - person (with faculty, gender, birth/death stored directly)
  - location
  - event (birth, death, education, career)
  - relation (spouse, parent, child, in_law, grand_parent, far_family)
  - enrichment_log (from _wikidata / _openarch metadata)

Usage:
    # Dry run — writes SQL file only:
    python import_json.py --json-dir Corrected_JSON

    # Execute directly in MonetDB:
    python import_json.py --json-dir Corrected_JSON --execute
"""

import argparse
import calendar
import glob
import json
import os
import re
import subprocess
import sys
from datetime import date

# ─── Global dedup sets ────────────────────────────────────────────────────────

SEEN_LOCATIONS = set()   # (country, city) pairs already INSERT-ed
SEEN_PERSONS   = set()   # (first_name, last_name, affix, alt) tuples already INSERT-ed


# ─── SQL helpers ─────────────────────────────────────────────────────────────

def sql_quote(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def clean_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").strip()
    return value or None


def normalise_gender(value):
    """Map any gender string to 'Man', 'Vrouw', or None."""
    v = clean_text(value)
    if v is None:
        return None
    l = v.lower()
    if l in ("man", "m", "male", "masculin"):
        return "Man"
    if l in ("vrouw", "v", "f", "female", "woman", "feminin"):
        return "Vrouw"
    # Unknown / non-binary / other — store as-is but truncate to 50 chars
    return v[:50]


def safe_list(value):
    return value if isinstance(value, list) else []


def safe_dict(value):
    return value if isinstance(value, dict) else {}


# ─── Date normalisation ───────────────────────────────────────────────────────

def safe_date_iso(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def last_day_of_month(year, month):
    return calendar.monthrange(year, month)[1]


def normalize_single_date(token):
    token = clean_text(token)
    if token is None:
        return None, None
    token = token.replace("–", "-").replace("—", "-").strip()

    # YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", token)
    if m:
        y, mo, d = map(int, m.groups())
        iso = safe_date_iso(y, mo, d)
        return (iso, iso) if iso else (None, None)

    # DD-MM-YYYY
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", token)
    if m:
        d, mo, y = map(int, m.groups())
        iso = safe_date_iso(y, mo, d)
        return (iso, iso) if iso else (None, None)

    # YYYY-MM
    m = re.fullmatch(r"(\d{4})-(\d{2})", token)
    if m:
        y, mo = map(int, m.groups())
        if not (1 <= mo <= 12):
            return None, None
        return safe_date_iso(y, mo, 1), safe_date_iso(y, mo, last_day_of_month(y, mo))

    # MM-YYYY
    m = re.fullmatch(r"(\d{2})-(\d{4})", token)
    if m:
        mo, y = map(int, m.groups())
        if not (1 <= mo <= 12):
            return None, None
        return safe_date_iso(y, mo, 1), safe_date_iso(y, mo, last_day_of_month(y, mo))

    # YYYY
    m = re.fullmatch(r"(\d{4})", token)
    if m:
        y = int(m.group(1))
        return safe_date_iso(y, 1, 1), safe_date_iso(y, 12, 31)

    # YYYY-YYYY
    m = re.fullmatch(r"(\d{4})-(\d{4})", token)
    if m:
        y1, y2 = map(int, m.groups())
        if y2 < y1:
            return None, None
        return safe_date_iso(y1, 1, 1), safe_date_iso(y2, 12, 31)

    return None, None


def normalize_range_date(raw):
    raw = clean_text(raw)
    if raw is None:
        return None, None
    raw = raw.replace("–", "/").replace("—", "/")
    if "/" in raw:
        left, right = raw.split("/", 1)
        b1, _ = normalize_single_date(left.strip())
        _, e2 = normalize_single_date(right.strip())
        return b1, e2
    return normalize_single_date(raw)


# ─── WHERE clause builders ────────────────────────────────────────────────────

def person_where(first, last, affix, alt):
    """Stable identity key for a person row."""
    def clause(col, val):
        return f"{col} IS NULL" if val is None else f"{col} = {sql_quote(val)}"
    return " AND ".join([
        clause("first_name",            clean_text(first)),
        clause("last_name",             clean_text(last)),
        clause("affix",                 clean_text(affix)),
        clause("alternative_last_name", clean_text(alt)),
    ])


def location_where(country, city):
    def clause(col, val):
        return f"{col} IS NULL" if val is None else f"{col} = {sql_quote(val)}"
    return " AND ".join([
        clause("country", clean_text(country)),
        clause("city",    clean_text(city)),
    ])


# ─── INSERT generators ────────────────────────────────────────────────────────

def add_location(lines, country, city):
    country = clean_text(country)
    city    = clean_text(city)
    if country is None and city is None:
        return
    key = (country, city)
    if key in SEEN_LOCATIONS:
        return
    SEEN_LOCATIONS.add(key)
    where = location_where(country, city)
    lines.append(
        "INSERT INTO location (country, city, latitude, longitude) "
        f"SELECT {sql_quote(country)}, {sql_quote(city)}, NULL, NULL "
        f"WHERE NOT EXISTS (SELECT 1 FROM location WHERE {where});"
    )


def add_person(lines, first, last, affix=None, alt=None,
               gender=None, faculty=None, type_of_person=None,
               birth_begin=None, birth_end=None,
               birth_city=None, birth_country=None,
               death_begin=None, death_end=None,
               death_city=None, death_country=None,
               wikidata_qid=None, source_file=None):
    first   = clean_text(first)
    last    = clean_text(last)
    affix   = clean_text(affix)
    alt     = clean_text(alt)

    # Skip truly anonymous entries
    if first is None and last is None:
        return

    key = (first, last, affix, alt)
    if key in SEEN_PERSONS:
        return
    SEEN_PERSONS.add(key)

    where = person_where(first, last, affix, alt)
    lines.append(
        "INSERT INTO person ("
        "first_name, last_name, affix, alternative_last_name, "
        "gender, faculty, type_of_person, "
        "birth_date_begin, birth_date_end, birth_city, birth_country, "
        "death_date_begin, death_date_end, death_city, death_country, "
        "wikidata_qid, source_file"
        ") SELECT "
        f"{sql_quote(first)}, {sql_quote(last)}, {sql_quote(affix)}, {sql_quote(alt)}, "
        f"{sql_quote(gender)}, {sql_quote(faculty)}, "
        f"{'NULL' if type_of_person is None else int(type_of_person)}, "
        f"{sql_quote(birth_begin)}, {sql_quote(birth_end)}, "
        f"{sql_quote(birth_city)}, {sql_quote(birth_country)}, "
        f"{sql_quote(death_begin)}, {sql_quote(death_end)}, "
        f"{sql_quote(death_city)}, {sql_quote(death_country)}, "
        f"{sql_quote(wikidata_qid)}, {sql_quote(source_file)} "
        f"WHERE NOT EXISTS (SELECT 1 FROM person WHERE {where});"
    )


def add_event(lines, person_first, person_last, person_affix, person_alt,
              event_type, description, begin_date, end_date,
              country, city, source_ref=None):
    if begin_date is None and end_date is None \
       and clean_text(city) is None and clean_text(country) is None:
        return

    person_w = person_where(person_first, person_last, person_affix, person_alt)
    add_location(lines, country, city)

    if clean_text(country) is not None or clean_text(city) is not None:
        loc_expr = f"(SELECT MIN(location_id) FROM location WHERE {location_where(country, city)})"
    else:
        loc_expr = "NULL"

    lines.append(
        "INSERT INTO event "
        "(person_id, location_id, event_type_id, begin_date, end_date, description, source_ref) "
        "SELECT "
        f"(SELECT MIN(person_id) FROM person WHERE {person_w}), "
        f"{loc_expr}, "
        f"(SELECT event_type_id FROM event_type WHERE event_type_name = {sql_quote(event_type)}), "
        f"{sql_quote(begin_date)}, {sql_quote(end_date)}, "
        f"{sql_quote(description)}, {sql_quote(source_ref)} "
        f"WHERE (SELECT MIN(person_id) FROM person WHERE {person_w}) IS NOT NULL;"
    )


def person_where_aliased(first, last, affix, alt, alias):
    """Same as person_where() but with every column prefixed by a table alias."""
    def clause(col, val):
        full_col = f"{alias}.{col}"
        return f"{full_col} IS NULL" if val is None else f"{full_col} = {sql_quote(val)}"
    return " AND ".join([
        clause("first_name",            clean_text(first)),
        clause("last_name",             clean_text(last)),
        clause("affix",                 clean_text(affix)),
        clause("alternative_last_name", clean_text(alt)),
    ])


def add_relation(lines,
                 prof_first, prof_last, prof_affix, prof_alt,
                 rel_first, rel_last, rel_affix, rel_alt,
                 relation_type, source_ref=None):
    """Insert a relation between the professor and a relative."""
    rel_first = clean_text(rel_first)
    rel_last  = clean_text(rel_last)
    if rel_first is None and rel_last is None:
        return

    prof_w        = person_where(prof_first, prof_last, prof_affix, prof_alt)
    rel_w         = person_where(rel_first,  rel_last,  rel_affix,  rel_alt)
    prof_w_alias  = person_where_aliased(prof_first, prof_last, prof_affix, prof_alt, "p1")
    rel_w_alias   = person_where_aliased(rel_first,  rel_last,  rel_affix,  rel_alt,  "p2")

    lines.append(
        "INSERT INTO relation (person_id_1, person_id_2, relation_type_id, source_ref) "
        "SELECT "
        f"(SELECT MIN(person_id) FROM person WHERE {prof_w}), "
        f"(SELECT MIN(person_id) FROM person WHERE {rel_w}), "
        f"(SELECT relation_type_id FROM relation_type WHERE relation_type_name = {sql_quote(relation_type)}), "
        f"{sql_quote(source_ref)} "
        f"WHERE (SELECT MIN(person_id) FROM person WHERE {prof_w}) IS NOT NULL "
        f"  AND (SELECT MIN(person_id) FROM person WHERE {rel_w}) IS NOT NULL "
        "  AND NOT EXISTS ("
        "  SELECT 1 FROM relation r "
        f" JOIN person p1 ON p1.person_id = r.person_id_1 AND {prof_w_alias} "
        f" JOIN person p2 ON p2.person_id = r.person_id_2 AND {rel_w_alias} "
        f" WHERE r.relation_type_id = (SELECT relation_type_id FROM relation_type WHERE relation_type_name = {sql_quote(relation_type)})"
        ");"
    )


def add_enrichment_log(lines,
                       prof_first, prof_last, prof_affix, prof_alt,
                       source, field_name, field_value,
                       needs_review=False, accepted=None):
    """Log a field added by the enrichment pipeline."""
    if clean_text(field_value) is None:
        return
    prof_w = person_where(prof_first, prof_last, prof_affix, prof_alt)
    accepted_sql = "NULL" if accepted is None else ("TRUE" if accepted else "FALSE")
    lines.append(
        "INSERT INTO enrichment_log "
        "(person_id, source, field_name, field_value, needs_review, accepted) "
        "VALUES ("
        f"(SELECT MIN(person_id) FROM person WHERE {prof_w}), "
        f"{sql_quote(source)}, {sql_quote(field_name)}, {sql_quote(str(field_value))}, "
        f"{'TRUE' if needs_review else 'FALSE'}, {accepted_sql}"
        ");"
    )


# ─── Per-file logic ───────────────────────────────────────────────────────────

def process_relative(lines, relative, relation_type,
                     prof_first, prof_last, prof_affix, prof_alt,
                     source_file):
    """Insert a relative as a person + create the relation link."""
    rel = safe_dict(relative)
    first  = clean_text(rel.get("FirstName"))
    last   = clean_text(rel.get("LastName"))
    affix  = clean_text(rel.get("Affix"))
    alt    = clean_text(rel.get("alternative_last_names") or rel.get("alternative_last_name"))
    gender = normalise_gender(rel.get("Gender"))
    source = clean_text(rel.get("source"))

    if first is None and last is None:
        return

    birth_b, birth_e = normalize_range_date(rel.get("BirthDate"))
    death_b, death_e = normalize_range_date(rel.get("DeathDate"))

    add_person(lines,
               first=first, last=last, affix=affix, alt=alt,
               gender=gender, type_of_person=2,  # 2 = relative
               birth_begin=birth_b, birth_end=birth_e,
               birth_city=clean_text(rel.get("BirthCity")),
               birth_country=clean_text(rel.get("BirthCountry")),
               death_begin=death_b, death_end=death_e,
               death_city=clean_text(rel.get("DeathCity")),
               source_file=source_file)

    add_relation(lines,
                 prof_first, prof_last, prof_affix, prof_alt,
                 first, last, affix, alt,
                 relation_type, source_ref=source)


def build_sql_for_file(path, lines):
    with open(path, "r", encoding="utf-8") as f:
        person = safe_dict(json.load(f))

    source_file = os.path.basename(path)

    first   = clean_text(person.get("FirstName"))
    last    = clean_text(person.get("LastName"))
    affix   = clean_text(person.get("Affix"))
    alt     = clean_text(person.get("alternative_last_names") or
                         person.get("alternative_last_name"))
    gender  = clean_text(person.get("Gender"))
    faculty = clean_text(person.get("faculty"))
    top     = person.get("type_of_person")

    birth_b, birth_e = normalize_range_date(person.get("BirthDate"))
    death_b, death_e = normalize_range_date(person.get("DeathDate"))

    wikidata_qid = None
    wd = safe_dict(person.get("_wikidata"))
    if wd.get("found"):
        wikidata_qid = wd.get("qid")

    # ── Professor row ─────────────────────────────────────────────────────────
    add_person(lines,
               first=first, last=last, affix=affix, alt=alt,
               gender=gender, faculty=faculty, type_of_person=top,
               birth_begin=birth_b, birth_end=birth_e,
               birth_city=clean_text(person.get("BirthCity")),
               birth_country=clean_text(person.get("BirthCountry")),
               death_begin=death_b, death_end=death_e,
               death_city=clean_text(person.get("DeathCity")),
               wikidata_qid=wikidata_qid,
               source_file=source_file)

    # ── Birth event ───────────────────────────────────────────────────────────
    add_event(lines, first, last, affix, alt,
              "birth", "Birth", birth_b, birth_e,
              person.get("BirthCountry"), person.get("BirthCity"))

    # ── Death event ───────────────────────────────────────────────────────────
    add_event(lines, first, last, affix, alt,
              "death", "Death", death_b, death_e,
              person.get("DeathCountry"), person.get("DeathCity"))

    # ── Education events ──────────────────────────────────────────────────────
    for edu in safe_list(person.get("education")):
        edu = safe_dict(edu)
        b, e = normalize_range_date(edu.get("date"))
        desc = clean_text(edu.get("subject")) or "Education"
        add_event(lines, first, last, affix, alt,
                  "education", desc, b, e,
                  None, edu.get("location"),
                  source_ref=clean_text(edu.get("source")))

    # ── Career events ─────────────────────────────────────────────────────────
    for career in safe_list(person.get("careers")):
        career = safe_dict(career)
        b, e = normalize_range_date(career.get("date"))
        desc = clean_text(career.get("job")) or "Career"
        add_event(lines, first, last, affix, alt,
                  "career", desc, b, e,
                  None, career.get("location"),
                  source_ref=clean_text(career.get("source")))

    # ── Relatives ─────────────────────────────────────────────────────────────
    rel_fields = {
        "spouses":      "spouse",
        "parents":      "parent",
        "children":     "child",
        "in_laws":      "in_law",
        "grand_parents": "grand_parent",
        "far_family":   "far_family",
    }
    for field, rel_type in rel_fields.items():
        for relative in safe_list(person.get(field)):
            process_relative(lines, relative, rel_type,
                             first, last, affix, alt,
                             source_file)

    # ── Enrichment log ────────────────────────────────────────────────────────
    # Wikidata
    if wd.get("found"):
        needs_review = bool(wd.get("needs_review"))
        accepted     = wd.get("accepted")  # True/False/None
        for field, value in safe_dict(wd.get("fields_added")).items():
            add_enrichment_log(lines, first, last, affix, alt,
                               "wikidata", field, value,
                               needs_review=needs_review, accepted=accepted)

    # Open Archives
    oa = safe_dict(person.get("_openarch"))
    if oa.get("found"):
        needs_review = bool(oa.get("needs_review"))
        for field, value in safe_dict(oa.get("fields_added")).items():
            add_enrichment_log(lines, first, last, affix, alt,
                               "openarchives", field, value,
                               needs_review=needs_review)

    # Relative enrichment changes
    rl = safe_dict(person.get("_relatives"))
    for rel_key, changes in safe_dict(rl.get("changes")).items():
        for field, value in safe_dict(changes).items():
            add_enrichment_log(lines, first, last, affix, alt,
                               "openarchives_relatives",
                               f"{rel_key}.{field}", value,
                               needs_review=True)


# ─── Runner ───────────────────────────────────────────────────────────────────

def run_mclient(sql_text, database, user):
    cmd = ["mclient", "-lsql", "-d", database, "-u", user]
    proc = subprocess.run(cmd, input=sql_text, text=True, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def main():
    parser = argparse.ArgumentParser(
        description="Import enriched professor JSONs into MonetDB."
    )
    parser.add_argument("--json-dir",    default=".",           help="Folder with JSON files")
    parser.add_argument("--database",    default="peopledb",    help="MonetDB database name")
    parser.add_argument("--user",        default="monetdb",     help="MonetDB username")
    parser.add_argument("--execute",     action="store_true",   help="Run SQL in MonetDB")
    parser.add_argument("--output-sql",  default="import.sql",  help="Output SQL file")
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(args.json_dir, "*.json")))
    if not files:
        print(f"No JSON files found in {args.json_dir}", file=sys.stderr)
        sys.exit(1)

    lines = [
        "START TRANSACTION;",
        # Seed event types
        "INSERT INTO event_type (event_type_name) SELECT 'birth'     WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'birth');",
        "INSERT INTO event_type (event_type_name) SELECT 'death'     WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'death');",
        "INSERT INTO event_type (event_type_name) SELECT 'education' WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'education');",
        "INSERT INTO event_type (event_type_name) SELECT 'career'    WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'career');",
        # Seed relation types
        "INSERT INTO relation_type (relation_type_name) SELECT 'spouse'       WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = 'spouse');",
        "INSERT INTO relation_type (relation_type_name) SELECT 'parent'       WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = 'parent');",
        "INSERT INTO relation_type (relation_type_name) SELECT 'child'        WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = 'child');",
        "INSERT INTO relation_type (relation_type_name) SELECT 'in_law'       WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = 'in_law');",
        "INSERT INTO relation_type (relation_type_name) SELECT 'grand_parent' WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = 'grand_parent');",
        "INSERT INTO relation_type (relation_type_name) SELECT 'far_family'   WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = 'far_family');",
    ]

    skipped = 0
    for path in files:
        try:
            build_sql_for_file(path, lines)
        except Exception as e:
            print(f"Warning: skipping {path}: {e}", file=sys.stderr)
            skipped += 1

    lines.append("COMMIT;")
    sql_text = "\n".join(lines) + "\n"

    with open(args.output_sql, "w", encoding="utf-8") as f:
        f.write(sql_text)

    print(f"Generated SQL for {len(files) - skipped}/{len(files)} files → {args.output_sql}")
    if skipped:
        print(f"  ({skipped} files skipped due to errors — check stderr)")

    if args.execute:
        code, out, err = run_mclient(sql_text, args.database, args.user)
        sys.stdout.write(out)
        sys.stderr.write(err)
        if code != 0:
            sys.exit(code)
        print("Import completed successfully.")


if __name__ == "__main__":
    main()
