#!/usr/bin/env python3  

import argparse  
import csv  
import io  
import json  
import os  
import subprocess  
import sys  
import time  

from geopy.geocoders import Nominatim  # Use OpenStreetMap's Nominatim service for geocoding place names, could be replaced at some point
from geopy.extra.rate_limiter import RateLimiter  # prevent overloading the geocoding service


def run_mclient_sql(database, user, sql):  # Run a SQL statement through mclient and return its stdout
    cmd = ["mclient", "-lsql", "-f", "csv", "-d", database]  
    proc = subprocess.run(cmd, input=sql, text=True, capture_output=True)  
    if proc.returncode != 0:  
        raise RuntimeError(proc.stderr.strip() or "mclient failed")  
    return proc.stdout  


def read_select_rows(database, user, sql):  # Execute a SELECT query and parse the CSV output into rows
    out = run_mclient_sql(database, user, sql)  
    reader = csv.reader(io.StringIO(out))  
    rows = [row for row in reader if row]  # Collect all non-empty rows into a list
    return rows  


def exec_update(database, user, sql):  # Execute a non-SELECT SQL statement like UPDATE
    run_mclient_sql(database, user, sql) 


def sql_quote(value):  # Convert a Python value into a SQL-safe value
    if value is None:  
        return "NULL"  
    return "'" + str(value).replace("'", "''") + "'"  


def load_cache(path):  # Load the geocoding cache from disk if it exists
    if os.path.exists(path):  
        with open(path, "r", encoding="utf-8") as f: 
            return json.load(f)  
    return {}  


def save_cache(path, cache):  # Save the current geocoding cache to disk.
    with open(path, "w", encoding="utf-8") as f:  
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)  


def normalize_field(value):  # Normalize database string fields so blank/NULL-like values become None
    if value is None:  
        return None  
    value = value.strip()  
    if value == "" or value.upper() == "NULL":  #
        return None  
    return value  


def main():  
    parser = argparse.ArgumentParser(description="Fill latitude/longitude in MonetDB location table")  # Create the command-line parser
    parser.add_argument("--database", default="peopledb")  
    parser.add_argument("--user", default="monetdb")  
    parser.add_argument("--cache", default="geocode_cache.json")  
    parser.add_argument("--delay", type=float, default=1.2)  
    parser.add_argument("--limit", type=int, default=None)  
    args = parser.parse_args()  

    cache = load_cache(args.cache)  # Load any previously saved geocoding results

    rows = read_select_rows(  # Query the database for locations missing coordinates
        args.database,  
        args.user,  
        """
        SELECT location_id, country, city
        FROM location
        WHERE latitude IS NULL OR longitude IS NULL
        ORDER BY location_id;
        """  
    )

    if args.limit is not None:  
        rows = rows[:args.limit]  

    if not rows:  
        print("No rows found that need geocoding.")  
        return  

    geolocator = Nominatim(user_agent="monetdb-location-enricher")  # Create a Nominatim geocoder client with a custom user agent
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=args.delay)  # enforce delay between requests

    updated = 0  
    skipped = 0 
    failed = 0  

    for row in rows:  # Process each database row 
        if len(row) < 3:  
            skipped += 1  
            continue  

        location_id = row[0].strip()  
        country = normalize_field(row[1])  
        city = normalize_field(row[2])  

        if not city and not country:  
            print(f"Skipping location_id={location_id}: no city/country") 
            skipped += 1  
            continue 

        query = ", ".join(part for part in [city, country] if part)  # Build a geocoding query like "Amsterdam, Netherlands"

        if query in cache:  # If this exact query has already been geocoded before
            result = cache[query]  # ...reuse the cached result instead of calling the API again
        else:  # Otherwise, perform a new geocoding lookup.
            try: 
                loc = geocode(query)  # Send the place query to Nominatim
                if loc is None: 
                    result = {"lat": None, "lon": None}  
                else: 
                    result = {"lat": loc.latitude, "lon": loc.longitude} 
                cache[query] = result  
                save_cache(args.cache, cache) 
            except Exception as e: 
                print(f"Failed geocoding {query}: {e}", file=sys.stderr) 
                failed += 1  
                continue  

        lat = result.get("lat")  
        lon = result.get("lon") 

        if lat is None or lon is None:  
            print(f"No match for: {query}") 
            skipped += 1 
            continue 

        update_sql = f""" 
        UPDATE location
        SET latitude = {lat}, longitude = {lon}
        WHERE location_id = {location_id};
        """  

        try:  # Try to update the database row
            exec_update(args.database, args.user, update_sql)  
            print(f"Updated {location_id}: {query} -> ({lat}, {lon})")  
            updated += 1 
        except Exception as e: 
            print(f"Failed update for location_id={location_id}: {e}", file=sys.stderr) 
            failed += 1 

        time.sleep(args.delay)  # Sleep after each processed row to avoid sending requests too quickly overall

    print(f"Done. Updated={updated}, skipped={skipped}, failed={failed}") 


if __name__ == "__main__":  
    main()  