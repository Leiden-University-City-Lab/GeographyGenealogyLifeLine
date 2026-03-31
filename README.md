# geographicPluginThesis

Vol 1, entries 1-15 have been corrected for lifepaths.
Database table schema for Person and Events has been added.


Fix: 
-duplicate person entry and events
-Prevent numbers as legitimate input for locations

Quick commands list:

1. monetdb start /path/to/dbfarm
2. monetdb create peopledb
3. monetdb release peopledb
4. mclient -u monetdb -d peopledb
5. python3 JSONImporter.py --json-dir /path/to/dir --database peopledb --execute
