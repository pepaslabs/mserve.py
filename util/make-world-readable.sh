#!/bin/bash

# make all files and dirs within a dir world-readable.

cd "$1"
find -type d -exec chmod ugo+rx {} \;
find -type f -exec chmod ugo+r {} \;
