#!/bin/bash
monetdb stop peopledb
monetdb destroy peopledb
monetdb create peopledb
monetdb release peopledb
monetdb start peopledb
mclient -d peopledb < schema.sql