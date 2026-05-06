#!/bin/bash
# ============================================================
#  Full database reset + rebuild pipeline
#  Usage: bash reset.sh [--json-dir PATH] [--db NAME]
# ============================================================

set -e  # exit immediately on any error

# ── Defaults (override with flags) ───────────────────────────────────────────
JSON_DIR="Corrected_JSON"
DB_NAME="peopledb"
DB_USER="monetdb"

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json-dir) JSON_DIR="$2"; shift 2 ;;
        --db)       DB_NAME="$2";  shift 2 ;;
        --user)     DB_USER="$2";  shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

echo ""
echo "=========================================="
echo "  Database reset & rebuild"
echo "  DB:       $DB_NAME"
echo "  JSON dir: $JSON_DIR"
echo "=========================================="
echo ""

# ── Step 1: Reset MonetDB ─────────────────────────────────────────────────────
echo "[1/5] Resetting MonetDB database..."

monetdbd start dbfarm 2>/dev/null || true   # start farm if not already running
monetdb stop    "$DB_NAME" 2>/dev/null || true
monetdb destroy "$DB_NAME" 2>/dev/null || true
monetdb create  "$DB_NAME"
monetdb release "$DB_NAME"
monetdb start   "$DB_NAME"

echo "      ✓ Database '$DB_NAME' recreated"

# ── Step 2: Apply schema ──────────────────────────────────────────────────────
echo "[2/5] Applying schema..."

mclient -u "$DB_USER" -d "$DB_NAME" < schema.sql

echo "      ✓ Schema applied"

# ── Step 3: Import JSON files ─────────────────────────────────────────────────
echo "[3/5] Importing JSON files from '$JSON_DIR'..."

python3 JSONImporter.py \
    --json-dir   "$JSON_DIR" \
    --database   "$DB_NAME" \
    --user       "$DB_USER" \
    --output-sql import.sql \
    --execute

echo "      ✓ JSON import complete"

# ── Step 4: Run geolocation ───────────────────────────────────────────────────
echo "[4/5] Running geolocation enrichment..."

python3 geolocation.py \
    --database "$DB_NAME" \
    --user     "$DB_USER"

echo "      ✓ Geolocation complete"

# ── Step 5: Done ──────────────────────────────────────────────────────────────
echo "[5/5] All done."
echo ""
echo "  To start the visualisation app:"
echo "    python3 app.py"
echo ""