#!/usr/bin/env python3

# create an mserve.json for a movie.

title = input("title? ")
tmdb_id = input("tmdb_id? ")
with open("mserve.json", "w") as fd:
    fd.write("{\n")
    fd.write('    "type": "movie",\n')
    fd.write('    "title": "%s",\n' % title)
    fd.write('    "tmdb_id": "movie/%s"\n' % tmdb_id)
    fd.write("}\n")
