#!/usr/bin/python3

# use regular expressions to rename files.

# example: to rename foo-season-1-episode-2.mp4 to foo-s1e2.mp4:
# ./resub.py 'season-' 's' *.mp4
# ./resub.py '-episode-' 'e' *.mp4

import sys
import os
import re

args = sys.argv[1:]

just_print = False
if "-p" == args[0]:
    just_print = True
    args = args[1:]

yes = False
if "-y" == args[0]:
    yes = True
    args = args[1:]

re1 = args[0]
re2 = args[1]
fnames = args[2:]

for fname in fnames:
    fname2 = re.sub(re1, re2, fname)
    if just_print:
        print(fname2)
    else:
        if fname2 != fname:
            if yes:
                answer = 'y'
            else:
                answer = input("Rename '%s' -> '%s'? [Yn] " % (fname, fname2))
            if answer.lower() == 'y' or answer == '':
                os.rename(fname, fname2)
