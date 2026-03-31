#!/usr/bin/env python3  

import argparse  #  for command-line arguments --json-dir and --execute
import calendar 
import glob  
import json  
import os  
import re  # regular expressions for date parsing
import subprocess 
import sys  
from datetime import date 

SEEN_LOCATIONS = set()  # Global set used to avoid generating duplicate INSERTs for the same location into the database

# convert a Python string into a SQL-safe string
def sql_quote(value):  
    if value is None:  
        return "NULL" 
    value = str(value)  
    return "'" + value.replace("'", "''") + "'"  

# Normalize text values from JSON before using them
def clean_text(value):  
    if value is None:  
        return None 
    if not isinstance(value, str):  # convert to string
        value = str(value)  
    value = value.strip()  # remove whitespace 
    if not value:  
        return None  # return None for values that should be considered unknown.
    return value 


def safe_date_iso(year, month, day):  # iso data 
    try:  # try to construct a real calendar date.
        d = date(int(year), int(month), int(day))  
        return d.isoformat()  # return iso format
    except ValueError:  # If the date is invalid (like month 13 or Feb 30)...
        return None  # return None instead of crashing

# return the last numeric day in a given month/year.
def last_day_of_month(year, month):  
    return calendar.monthrange(year, month)[1]  


def normalize_single_date(token):  # convert a single data into a range
    """
    Supported:
      06-03-1698     -> 1698-03-06, 1698-03-06
      11-1720        -> 1720-11-01, 1720-11-30
      1740           -> 1740-01-01, 1740-12-31
      1784-1785      -> 1784-01-01, 1785-12-31

    Invalid values return (None, None).
    """
    token = clean_text(token)  
    if token is None:  
        return None, None  

    token = token.replace("–", "-").replace("—", "-").strip()  # change different dash characters to a plain hyphen.

    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", token)  # match DD-MM-YYYY format exactly.
    if m:  
        day, month, year = map(int, m.groups())  
        iso = safe_date_iso(year, month, day) 
        if iso is None:  
            return None, None  
        return iso, iso 

    m = re.fullmatch(r"(\d{2})-(\d{4})", token)  # match MM-YYYY format exactly.
    if m: 
        month, year = map(int, m.groups()) 
        if not (1 <= month <= 12):  
            return None, None  
        begin = safe_date_iso(year, month, 1) 
        end = safe_date_iso(year, month, last_day_of_month(year, month))  
        return begin, end  

    m = re.fullmatch(r"(\d{4})", token)  # match a single year.
    if m:  
        year = int(m.group(1))  
        begin = safe_date_iso(year, 1, 1)  
        end = safe_date_iso(year, 12, 31)  
        return begin, end  

    m = re.fullmatch(r"(\d{4})-(\d{4})", token)  # match  year range like 1784-1785
    if m:  
        year1, year2 = map(int, m.groups())  
        if year2 < year1: 
            return None, None  
        begin = safe_date_iso(year1, 1, 1)  
        end = safe_date_iso(year2, 12, 31)  
        return begin, end  

    return None, None  


def normalize_range_date(raw):  # normalize a date that may be a single date or a slash-separated range
    raw = clean_text(raw)  
    if raw is None: 
        return None, None 

    raw = raw.replace("–", "/").replace("—", "/")  # change dashes as range separators, converting them to slash

    if "/" in raw:  
        left, right = raw.split("/", 1)  
        b1, e1 = normalize_single_date(left.strip())  
        b2, e2 = normalize_single_date(right.strip())  
        begin = b1 or b2  
        end = e2 or e1  
        return begin, end  # return the combined range

    return normalize_single_date(raw)  # if there is no separator, just normalize it as a single date token.


def person_match_where(person):  # creates a SQL WHERE clause that uniquely identifies a person row
    clauses = [  
        f"first_name = {sql_quote(clean_text(person.get('FirstName')))}",  
        f"last_name = {sql_quote(clean_text(person.get('LastName')))}",  
    ]

    affix = clean_text(person.get("Affix"))  
    alt = clean_text(  
        person.get("alternative_last_names")  
    )

    if affix is None:  
        clauses.append("affix IS NULL")  
    else:  
        clauses.append(f"affix = {sql_quote(affix)}")  

    if alt is None:  
        clauses.append("alternative_last_name IS NULL") 
    else: 
        clauses.append(f"alternative_last_name = {sql_quote(alt)}") 

    return " AND ".join(clauses)  # combine all conditions into one SQL WHERE clause.


def location_match_where(country, city):  # creates a SQL WHERE clause that identifies a location row.
    clauses = []  # start an empty list of SQL conditions.
    country = clean_text(country)  
    city = clean_text(city)  

    if country is None:  
        clauses.append("country IS NULL") 
    else:  
        clauses.append(f"country = {sql_quote(country)}") 

    if city is None: 
        clauses.append("city IS NULL")  
    else:  
        clauses.append(f"city = {sql_quote(city)}")  

    return " AND ".join(clauses)  # join the conditions into a SQL WHERE clause.


def add_location_sql(sql_lines, seen_locations, country, city):  # Add an INSERT for a location if it has not been added yet.
    country = clean_text(country)  
    city = clean_text(city)  

    if country is None and city is None:  
        return  

    key = (country, city)  
    if key in seen_locations:  # If this location was already handled in this run, do not create another INSERT
        return  
    seen_locations.add(key)  # Record that this location has now been seen

    where = location_match_where(country, city)  # Build a WHERE clause to detect an existing DB row
    sql_lines.append(  
        "INSERT INTO location (country, city, latitude, longitude) "  
        f"SELECT {sql_quote(country)}, {sql_quote(city)}, NULL, NULL "  
        f"WHERE NOT EXISTS (SELECT 1 FROM location WHERE {where});" 
    )


def add_person_sql(sql_lines, person):  # Add SQL to insert a person if they do not already exist
    first_name = clean_text(person.get("FirstName"))  
    last_name = clean_text(person.get("LastName"))  
    affix = clean_text(person.get("Affix"))  
    alt_last = clean_text(  
        person.get("alternative_last_names")  
        or person.get("alternative_last_name")  
    )

    where = person_match_where(person)  # Build a WHERE clause that identifies this person
    sql_lines.append(  
        "INSERT INTO person (first_name, last_name, affix, alternative_last_name) "  
        f"SELECT {sql_quote(first_name)}, {sql_quote(last_name)}, " 
        f"{sql_quote(affix)}, {sql_quote(alt_last)} "  
        f"WHERE NOT EXISTS (SELECT 1 FROM person WHERE {where});"  
    )


def add_event_sql(sql_lines, person, event_type_name, description, begin_date, end_date, country, city):  # Add SQL for a person-related event
    if (  
        begin_date is None 
        and end_date is None  
        and clean_text(city) is None  
        and clean_text(country) is None 
    ):
        return  

    person_where = person_match_where(person)  
    add_location_sql(sql_lines, SEEN_LOCATIONS, country, city)  # Ensure the event location exists before inserting the event.

    location_expr = "NULL"  # Default the event's location to NULL
    if clean_text(country) is not None or clean_text(city) is not None:  
        loc_where = location_match_where(country, city)  
        location_expr = f"(SELECT MIN(location_id) FROM location WHERE {loc_where})" 

    sql_lines.append(  
        "INSERT INTO event (person_id, location_id, event_type_id, begin_date, end_date, description) VALUES ("  
        f"(SELECT MIN(person_id) FROM person WHERE {person_where}), " 
        f"{location_expr}, "  
        f"(SELECT event_type_id FROM event_type WHERE event_type_name = {sql_quote(event_type_name)}), " 
        f"{sql_quote(begin_date)}, {sql_quote(end_date)}, {sql_quote(description)}" 
        ");" 
    )


def safe_list(value):  # Return the value only if it is a list; otherwise return an empty list.
    if isinstance(value, list):  
        return value  
    return []  # Non-lists are replaced with an empty list to avoid iteration errors.


def safe_dict(value):  # Return the value only if it is a dict; otherwise return an empty dict.
    if isinstance(value, dict):  
        return value  
    return {}  # Non-dicts are replaced with an empty dict to avoid key access errors.


def build_sql_for_file(path, sql_lines):  # Read one JSON file and append corresponding SQL statements.
    with open(path, "r", encoding="utf-8") as f:  
        person = json.load(f)  # Parse the JSON file into a Python object.

    person = safe_dict(person)  # Ensure the top-level JSON object is a dictionary
    add_person_sql(sql_lines, person)  # Generate SQL to insert the person row

    birth_begin, birth_end = normalize_range_date(person.get("BirthDate"))  
    add_event_sql(  # Generate a birth event.
        sql_lines,  
        person,  
        "birth", 
        "Birth",  
        birth_begin,  
        birth_end,  
        person.get("BirthCountry"), 
        person.get("BirthCity"), 
    )

    death_begin, death_end = normalize_range_date(person.get("DeathDate"))  
    add_event_sql(  # Generate a death event.
        sql_lines,  
        person,  
        "death",  
        "Death",  
        death_begin,  
        death_end,  
        person.get("DeathCountry"),  
        person.get("DeathCity"),  
    )

    for edu in safe_list(person.get("education")):  # Loop over each education record
        edu = safe_dict(edu)  # Ensure the education entry is a dictionary
        begin_date, end_date = normalize_range_date(edu.get("date"))  
        description = clean_text(edu.get("subject")) or "Education"  # Use the subject as description, or a default label
        add_event_sql(  # Generate an education event.
            sql_lines, 
            person,  
            "education", 
            description,  
            begin_date, 
            end_date, 
            None,  
            edu.get("location"),  
        )

    for career in safe_list(person.get("careers")):  # Loop over each career record 
        career = safe_dict(career)  # Ensure the career entry is a dictionary
        begin_date, end_date = normalize_range_date(career.get("date"))  
        description = clean_text(career.get("job")) or "Career"  
        add_event_sql(  # Generate a career event
            sql_lines,  
            person,  
            "career",  
            description,  
            begin_date,  
            end_date,  
            None,  
            career.get("location"),  # Use the location field as city/place.
        )


def run_mclient(sql_text, database, user):  # Execute SQL text directly in MonetDB using mclient
    cmd = ["mclient", "-lsql", "-d", database, "-u", user]  # build the shell command as a list of arguments.
    proc = subprocess.run(cmd, input=sql_text, text=True, capture_output=True)  # run mclient and send SQL to stdin
    return proc.returncode, proc.stdout, proc.stderr 


def main(): 
    parser = argparse.ArgumentParser(  
        description="Import people + selected events from JSON into MonetDB." 
    )
    parser.add_argument("--json-dir", default=".")  
    parser.add_argument("--database", default="peopledb")  
    parser.add_argument("--user", default="monetdb")  
    parser.add_argument("--execute", action="store_true")  
    parser.add_argument("--output-sql", default="import_people_events.sql")  
    args = parser.parse_args()  # Parse the actual command-line arguments into an args object.

    files = sorted(glob.glob(os.path.join(args.json_dir, "*.json")))  # Find all JSON files in the chosen directory and sort them
    if not files: 
        print(f"No JSON files found in {args.json_dir}", file=sys.stderr) 
        sys.exit(1)  

    sql_lines = [  # Start building the SQL script as a list of lines.
        "START TRANSACTION;",  # Begin a transaction so all changes can be committed together
        "INSERT INTO event_type (event_type_name) SELECT 'birth' WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'birth');",  # Ensure 'birth' exists in event_type.
        "INSERT INTO event_type (event_type_name) SELECT 'death' WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'death');",  # Ensure 'death' exists in event_type.
        "INSERT INTO event_type (event_type_name) SELECT 'education' WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'education');",  # Ensure 'education' exists in event_type.
        "INSERT INTO event_type (event_type_name) SELECT 'career' WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = 'career');",  # Ensure 'career' exists in event_type.
    ]

    for path in files:  # Process each discovered JSON file one by one
        build_sql_for_file(path, sql_lines)  

    sql_lines.append("COMMIT;")  # commit the transaction
    sql_text = "\n".join(sql_lines) + "\n"  

    with open(args.output_sql, "w", encoding="utf-8") as f:  
        f.write(sql_text)  

    print(f"Generated SQL for {len(files)} JSON files -> {args.output_sql}") 

    if args.execute: 
        code, out, err = run_mclient(sql_text, args.database, args.user)  
        sys.stdout.write(out)  
        sys.stderr.write(err)  
        if code != 0:  
            sys.exit(code)  
        print("Import completed successfully.")  


if __name__ == "__main__":  
    main()  