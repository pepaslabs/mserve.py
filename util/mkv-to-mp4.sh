#!/bin/bash

set -e -o pipefail

ffmpeg -i "$1" -c copy -c:s mov_text "$(basename $1 .mkv).mp4"
