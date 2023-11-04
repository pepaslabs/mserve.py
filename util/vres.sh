#!/bin/bash

# print the vertical resolution of a video file.

set -e -o pipefail

ffprobe -print_format json -show_format -show_streams "$1" 2>/dev/null \
    | jq '.streams[0].coded_height'
