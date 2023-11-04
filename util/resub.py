#!/usr/bin/python3

# use regular expressions to rename files.

# example: to rename foo-season-1-episode-2.mp4 to foo-s1e2.mp4:
# ./resub.py 'season-' 's' *.mp4
# ./resub.py '-episode-' 'e' *.mp4

import sys
import os
import re

re1 = sys.argv[1]
re2 = sys.argv[2]
fnames = sys.argv[3:]
for fname in fnames:
    fname2 = re.sub(re1, re2, fname)
    if fname2 != fname:
        answer = input("Rename '%s' -> '%s'? [Yn] " % (fname, fname2))
        if answer.lower() == 'y' or answer == '':
            os.rename(fname, fname2)
