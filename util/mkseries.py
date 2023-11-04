#!/usr/bin/env python3

# create an mserve.json for a tv series.

title = input("title? ")
tmdb_id = input("tmdb_id? ")
with open("mserve.json", "w") as fd:
    fd.write("{\n")
    fd.write('    "type": "series",\n')
    fd.write('    "title": "%s",\n' % title)
    fd.write('    "tmdb_id": "tv/%s"\n' % tmdb_id)
    fd.write("}\n")
