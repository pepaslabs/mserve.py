#!/usr/bin/env python3

# mserve.py: A Python media server.
# See https://github.com/cellularmitosis/mserve.py

# Copyright 2023 Jason Pepas.
# Released under the terms of the MIT license.
# See https://opensource.org/license/mit/

# Note: certain video files aren't supported via HTML5 <video> (i.e. .mkv).
# To open these files directly using e.g. VLC, use VLCFileUrl to associate
# the custom URL scheme vlc-file:// with VLC.
# See https://github.com/pepaslabs/VLCFileUrl

import sys
import os
import datetime
import re
import json
import http.server
import mimetypes
import urllib.request
import socket
import subprocess

#
# Python utils
#

# Safe array access.
# Thanks to https://stackoverflow.com/a/5125636
def list_get(l, index, default=None):
    try:
        return l[index]
    except IndexError:
        return default

# Guess the outwardly-routable IP address.
def outward_ip_address():
    # Some boxes resolve their hostname to 127.0.0.1 (or 127.0.1.1), which makes gethostbyname() useless.
    # So first we try to use cmdline utils to get the routeable IP.
    if sys.platform == 'linux':
        try:
            cmd = "ip route | grep '^default' | head -n1 | tr ' ' '\\n' | grep -A1 '^dev$' | tail -n1"
            iface = subprocess.check_output(cmd, shell=True).decode().splitlines()[0]
            cmd = "ip -f inet -json address show %s" % iface
            jsn = subprocess.check_output(cmd, shell=True).decode()
            ip = json.loads(jsn)[0]['addr_info'][0]['local']
            return ip
        except:
            return socket.gethostbyname(socket.gethostname())
    elif sys.platform == 'darwin':
        try:
            cmd = "netstat -rn -f inet | grep '^default' | head -n1"
            iface = subprocess.check_output(cmd, shell=True).decode().splitlines()[0]
            cmd = "ifconfig en0 | awk '{print $1 \" \" $2}' | grep '^inet ' | head -n1 | awk '{print $2}'"
            ip = subprocess.check_output(cmd, shell=True).decode().splitlines()[0]
            return ip
        except:
            return socket.gethostbyname(socket.gethostname())
    else:
        return socket.gethostbyname(socket.gethostname())

g_ip_address = outward_ip_address()

#
# HTTP / HTML utils
#

# Detect iPhone/iPad (or macos Safari).
# We would like to detect just iPhone/iPad, but unfortunately iPads return
# a desktop User-Agent, so we can't distinguish between iPad vs macOS/Safari.
def is_ios_or_macos_safari(handler):
    user_agent = handler.headers['User-Agent']
    if 'iPhone' in user_agent:
        return True
    elif 'Safari' in user_agent and 'Chrome' not in user_agent:
        return True
    else:
        return False

def is_macos_or_ipad(handler):
    user_agent = handler.headers['User-Agent']
    return 'Macintosh' in user_agent

def is_chrome(handler):
    user_agent = handler.headers['User-Agent']
    return 'Chrome' in user_agent

# Break an url_path down into componentized hyperlinks.
def render_url_path_links(url_path):
    if url_path == '/':
        return 'mserve'
    html = ''
    url = ''
    chunks = url_path.split('/')
    for chunk in chunks[:-1]:
        if chunk == '':
            url = '/'
            html = '<a href="/">mserve</a>'
        else:
            url = make_url_path(url, chunk)
            html += '&nbsp;/&nbsp;<a href="%s">%s</a>' % (url, chunk)
    html += '&nbsp;/&nbsp;%s' % chunks[-1]
    return html

# Format a filesize to be human-readable.
def format_filesize(nbytes):
    n = nbytes
    for suffix in [' bytes', 'KB', 'MB', 'GB', 'TB']:
        if n > 999:
            n = n / 1024.0
            continue
        elif n > 99:
            return "%0.0f%s" % (n, suffix)
        elif n > 9:
            return "%0.1f%s" % (n, suffix)
        else:
            return "%0.2f%s" % (n, suffix)
    else:
        return '%s bytes' % nbytes

# Given '/foo?bar=42, return ['/foo', {'bar':42}]
def parse_GET_path(path_query):
    if '?' not in path_query:
        path_part = path_query
        query_dict = {}
    else:
        path_part, query_part = path_query.split('?')
        # Thanks to https://wsgi.tutorial.codepoint.net/parsing-the-request-get
        # Note: parse_qs will return an array for each item, because the user might
        # have set a value more than once in the query string.  We'll go with the
        # last value of each array.
        query_dict = {}
        for k, v in urllib.parse.parse_qs(query_part).items():
            query_dict[k] = v[-1]
    # drop any superfluous trailing slashes
    while path_part[-1] == '/' and len(path_part) > 1:
        path_part = path_part[:-1]
    return [path_part, query_dict]

# Parse a simple Range header.
#   'Range: 0-' -> (0, None)
#   'Range: 1-2' -> (1, 2)
#   'Range: -3' -> (0, 3)
#   otherwise -> None
def parse_range_header(handler):
    # only single ranges are supported.
    header = handler.headers.get('Range', None)
    if header is None:
        return None
    if not header.startswith('bytes='):
        raise Exception("Unsupported Range header format")
    try:
        (start, end) = header.split('=')[1].split('-')
        if start == '':
            start = 0
        else:
            start = int(start)
        if end == '':
            end = None
        else:
            end = int(end)
        return (start, end)
    except:
        raise Exception("Unsupported Range header format")

# Make newlines conform to HTTP spec.
def rnlines(body):
    return body.replace('\n', '\r\n')

# Send text/plain.
def send_text(handler, code, body):
    handler.send_response(code)
    data = rnlines(body).encode()
    handler.send_header('Content-Type', 'text/plain; charset=UTF-8')
    handler.send_header('Content-Length', len(data))
    handler.end_headers()
    handler.wfile.write(data)

# Send 'Bad request'
def send_400(handler, message):
    send_text(handler, 400, "Bad request: %s" % message)

# Send 'Not found'.
def send_404(handler):
    send_text(handler, 404, "Not found")

# Send 'Requested range not satisfiable'.
def send_416(handler):
    send_text(handler, 416, "Requested range not satisfiable")

# Send 'Internal server error'.
def send_500(handler, message):
    send_text(handler, 500, "Internal server error: %s" % message)

# Send text/html.
def send_html(handler, code, body):
    handler.send_response(code)
    data = rnlines(body).encode()
    handler.send_header('Content-Type', 'text/html; charset=UTF-8')
    handler.send_header('Content-Length', len(data))
    handler.end_headers()
    handler.wfile.write(data)

# Send the contents of a file.
def send_file(handler, fpath, is_head=False, data=None, content_type=None, immutable=False):
    def send_whole_file(handler, fd, content_type, file_size, data=None, immutable=False):
        content_length = file_size
        handler.send_response(200)
        handler.send_header('Content-Length', "%s" % content_length)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Accept-Ranges', 'bytes')
        if immutable:
            handler.send_header('Cache-Control', 'public, max-age=31536000, immutable')
            handler.send_header('Age', '0')
        handler.end_headers()
        if is_head:
            return
        chunk_size = 64 * 1024
        while True:
            chunk = fd.read(chunk_size)
            if not chunk:
                break
            handler.wfile.write(chunk)

    def send_partial_file(handler, fd, content_type, file_size, range_header):
        # See https://developer.mozilla.org/en-US/docs/Web/HTTP/Range_requests
        content_length = file_size
        (start, end) = range_header
        if file_size == 0 or (end is not None and end >= file_size):
            send_416(handler)
            return
        if start != 0:
            fd.seek(start)
            content_length -= start
        if end is None:
            end = file_size - 1
        else:
            content_length = end - start + 1
        handler.send_response(206)
        handler.send_header('Content-Range', 'bytes %s-%s/%s' % (start, end, file_size))
        handler.send_header('Content-Length', "%s" % content_length)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Accept-Ranges', 'bytes')
        handler.end_headers()
        if is_head:
            return
        remaining = content_length
        while remaining > 0:
            chunk_size = 64 * 1024
            if chunk_size > remaining:
                chunk_size = remaining
            chunk = fd.read(chunk_size)
            remaining -= len(chunk)
            if not chunk:
                break
            handler.wfile.write(chunk)

    if not os.path.exists(fpath):
        send_404(handler)
        return
    try:
        range_header = parse_range_header(handler)
    except:
        send_400("Unsupported Range header format")
        return
    try:
        if content_type is None:
            content_type = get_content_type(fpath)
        file_size = os.path.getsize(fpath)
        fd = open(fpath, 'rb')
        if range_header is None:
            send_whole_file(handler, fd, content_type, file_size, data=data, immutable=immutable)
        else:
            send_partial_file(handler, fd, content_type, file_size, range_header)
        fd.close()
    except BrokenPipeError:
        pass
    except ConnectionResetError:
        pass
    except Exception as e:
        send_500(handler, "%s" % e)
        raise e

#
# Routing
#

g_static_routes = {}
g_regex_routes = []

# Find the function for a route.
def route(handler):
    url_path = handler.path.split('?')[0]
    method = handler.command
    fn = None
    fn_dict = g_static_routes.get(url_path, None)
    if fn_dict:
        fn = fn_dict.get(method, None)
    if fn:
        sys.stderr.write("Using static route %s\n" % url_path)
        return fn
    for (method_i, label, regex, fn) in g_regex_routes:
        if method_i != method:
            continue
        m = regex.match(url_path)
        if m:
            sys.stderr.write("Using regex route %s for %s\n" % (label, url_path))
            return fn
    return None

def add_static_route(http_method, url_path, fn):
    if url_path not in g_static_routes:
        g_static_routes[url_path] = {}
    g_static_routes[url_path][http_method] = fn

def add_regex_route(http_method, label, regex, fn):
    g_regex_routes.append([http_method, label, regex, fn])

#
# HTTP server.
#

# The core of the webapp.
def handle_request(handler):
    then = datetime.datetime.now()
    fn = route(handler)
    if fn is None:
        send_404(handler)
    else:
        try:
            fn(handler)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            send_500(handler, "%s" % e)
            raise e
    now = datetime.datetime.now()
    elapsed = now - then
    sys.stderr.write("  Elapsed: %0.3fms\n" % (elapsed.total_seconds() * 1000))

# OOP plumbing.
class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        handle_request(self)

    def do_GET(self):
        handle_request(self)

    def do_POST(self):
        handle_request(self)

    def do_PUT(self):
        handle_request(self)

    def do_PATCH(self):
        handle_request(self)

    def do_DELETE(self):
        handle_request(self)

#
# File utils
#

# Smash all of the components together to make a /foo/bar/baz path.
def make_url_path(*args):
    return '/'.join(args).replace('//','/').rstrip('/')

# Smash all of the components together to make a filesystem path.
def make_file_path(*args):
    return os.path.abspath(os.path.expanduser(make_url_path(*args)))

# Guess the content type.
def get_content_type(fpath):
    ext = os.path.splitext(fpath)[1].lower()
    if ext == '.jpg' or ext == '.jpeg':
        return 'image/jpeg'
    elif ext == '.png':
        return 'image/png'
    else:
        return mimetypes.guess_type(fpath)[0] or 'application/octet-stream'

# This is a video file?
def is_video(fpath):
    exts = [
        '.avi', '.flv', '.wmv',
        '.mpg', '.mpeg', '.mp2', '.mp4', '.m4v',
        '.ogg', '.ogm', '.ogv',
        '.webm',
        '.mkv',
        '.mov', '.qt'
    ]
    return os.path.splitext(fpath)[-1].lower() in exts

# This is a video file supported by HTML <video>?
def is_html5_video(fpath):
    exts = [
        '.mp4', '.m4v',
        '.ogg', '.ogm', '.ogv',
        '.webm'
    ]
    return os.path.splitext(fpath)[-1].lower() in exts

# Turn a string into a "slug".
def slugify(name):
    slug = ""
    for ch in name.lower():
        if ch in "'":
            continue  # drop these chars
        elif re.match(r'^[a-zA-Z0-9-.\/]+$', ch):
            slug += ch  # allow these chars
        else:
            slug += '-'  # turn anything else into a dash
    slug = re.sub('--+', '-', slug)
    return slug

# Rename a file (or dir) using a slugified name.
def slugify_file(fname):
    fname = fname.rstrip('/')
    slug_fname = slugify(fname)
    if fname == slug_fname:
        return
    answer = input("Rename '%s' to '%s'? [Yn]: " % (fname, slug_fname))
    if answer.lower() == 'y' or answer == '':
        os.rename(fname, slug_fname)

#
# mserve configuration via environment
#

# The directory in which to look for media files.
g_media_dir = os.environ['HOME'] + '/Movies'
if 'MSERVE_MEDIA_DIR' in os.environ:
    g_media_dir = os.environ['MSERVE_MEDIA_DIR']

# The token used to access the themoviedb.org API.
g_tmdb_token = None
if 'TMDB_TOKEN' in os.environ:
    g_tmdb_token = os.environ['TMDB_TOKEN']

#
# mserve media storage layer
#

# Find show / directory slugs at the given subpath.
# A slug is indicated by the presence of an mserve.json file.
# returns triples of [title, slug, metadata]
def scan_dir(url_path):
    triples = []
    json_fpath = make_file_path(g_media_dir, url_path, "mserve.json")
    if not os.path.isfile(json_fpath):
        return []
    dpath = make_file_path(g_media_dir, url_path)
    for slug in os.listdir(dpath):
        json_fpath = make_file_path(g_media_dir, url_path, slug, "mserve.json")
        if os.path.isfile(json_fpath):
            try:
                with open(json_fpath, 'rb') as fd:
                    metadata = json.load(fd)
                    title = metadata.get('title', slug)
                    triples.append([title, slug, metadata])
            except Exception as e:
                sys.stderr.write("❌ scan_dir: exception: %s\n" % e)
    triples.sort()
    return triples

# Load the mserve.json if present.
def load_mserve_json(url_path):
    fpath = make_file_path(g_media_dir, url_path, "mserve.json")
    return load_json(fpath)

# Load the json file if present.
def load_json(fpath):
    if os.path.isfile(fpath):
        try:
            with open(fpath, 'rb') as fd:
                return json.load(fd)
        except Exception as e:
            sys.stderr.write("❌ load_json: exception: %s\n" % e)
    return None

# Load the binary file if present.
def load_file(fpath):
    if os.path.isfile(fpath):
        try:
            with open(fpath, 'rb') as fd:
                return fd.read()
        except Exception as e:
            sys.stderr.write("❌ load_file: exception: %s\n" % e)
    return None

# Parse the season and episode number from a filename.
def parse_filename(fname):
    episode_pattern = re.compile(".*?[sS](\d+)[eE](\d+)")
    m = episode_pattern.match(fname)
    if m and len(m.groups()) == 2:
        season_num = int(m.group(1))
        episode_num = int(m.group(2))
        return [season_num, episode_num, fname]
    else:
        episode_pattern = re.compile(".*?(\d+)x(\d+)")
        m = episode_pattern.match(fname)
        if m and len(m.groups()) == 2:
            season_num = int(m.group(1))
            episode_num = int(m.group(2))
            return [season_num, episode_num, fname]
        else:
            episode_pattern = re.compile("^(\d+)")
            m = episode_pattern.match(fname)
            if m and len(m.groups()) == 1:
                season_num = 1
                episode_num = int(m.group(1))
                return [season_num, episode_num, fname]
            else:
                return [None, None, fname]

# Scan a show for seasons, episodes and filenames.
# Returns an array of "season groups".
# E.g. [(1,[(1,'tng-s1e1.mp4'),(2,'tng-s1e2.mp4')]), (2,[(1,'tng-s2e1.mp4'),(2,'tng-s2e2.mp4')])]
def scan_for_videos(url_path):
    video_triples = []
    json_fpath = make_file_path(g_media_dir, url_path, "mserve.json")
    if not os.path.isfile(json_fpath):
        return []
    dpath = make_file_path(g_media_dir, url_path)
    episode_triples = []
    video_triple = []
    for f in os.listdir(dpath):
        if not os.path.isfile(make_file_path(g_media_dir, url_path, f)):
            continue
        if not is_video(f):
            continue
        triple = parse_filename(f)
        if triple[0] is None:
            video_triples.append(triple)
        else:
            episode_triples.append(triple)
    episode_triples.sort()
    video_triples.sort()
    season_groups = []
    current_season = None
    season_group = []
    for t in episode_triples + video_triples:
        s, e, f = t
        if s != current_season:
            if len(season_group):
                season_groups.append((current_season,season_group))
                season_group = []
            current_season = s
        season_group.append((e,f))
    if len(season_group):
        season_groups.append((current_season,season_group))
    return season_groups

#
# themoviedb.org layer.
#

# Return the JSON for the given URL, using / populating cache if available.
# Returns empty dictionary in case of failure.
def get_json_from_url(url, cache_fpath, headers):
    dpath = os.path.dirname(cache_fpath)
    os.makedirs(dpath, exist_ok=True)
    cached_json = load_json(cache_fpath)
    if cached_json:
        return cached_json
    try:
        sys.stderr.write("Fetching %s\n" % url)
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        for pair in headers:
            req.add_header(pair[0], pair[1])
        with urllib.request.urlopen(req) as fd:
            data = fd.read()
        with open(cache_fpath, 'wb') as fd:
            fd.write(data)
        return json.loads(data.decode('utf-8'))
    except Exception as e:
        sys.stderr.write("❌ get_json_from_url: exception: %s\n" % e)
        return {}

# Return the JSON for the given show, using cache if available.
# tmdb_id should be e.g. "tv/1087" or "movie/199".
# Returns empty dictionary in case of failure.
def get_tmdb_show_details(tmdb_id):
    if tmdb_id is None:
        return {}
    tmdb_type = tmdb_id.split("/")[0]
    tmdb_num = tmdb_id.split("/")[1]
    dpath = make_file_path("~/.mserve/tmdb_cache/%s" % tmdb_type)
    fpath = make_file_path(dpath, "%s.json" % tmdb_num)
    url = "https://api.themoviedb.org/3/%s" % tmdb_id
    headers = [
        ['Authorization', 'Bearer %s' % g_tmdb_token]
    ]
    return get_json_from_url(url, fpath, headers)

# Return the JSON for the given season, using cache if available.
# tmdb_id should be e.g. "tv/1087".
# Returns empty dictionary in case of failure.
def get_tmdb_season_details(tmdb_id, season_num):
    if tmdb_id is None or season_num is None:
        return {}
    tmdb_type = tmdb_id.split("/")[0]
    tmdb_num = tmdb_id.split("/")[1]
    dpath = make_file_path("~/.mserve/tmdb_cache/%s" % tmdb_type)
    fpath = make_file_path(dpath, "%s.season%s.json" % (tmdb_num, season_num))
    url = "https://api.themoviedb.org/3/%s/season/%s" % (tmdb_id, season_num)
    headers = [
        ['Authorization', 'Bearer %s' % g_tmdb_token]
    ]
    return get_json_from_url(url, fpath, headers)

# Return the data for the given URL, using / populating cache if available.
# Returns None in case of failure.
def get_file_from_url(url, cache_fpath, headers=[]):
    dpath = os.path.dirname(cache_fpath)
    os.makedirs(dpath, exist_ok=True)
    cached_data = load_file(cache_fpath)
    if cached_data:
        return cached_data
    try:
        sys.stderr.write("Fetching %s\n" % url)
        req = urllib.request.Request(url)
        for pair in headers:
            req.add_header(pair[0], pair[1])
        with urllib.request.urlopen(req) as fd:
            data = fd.read()
        with open(cache_fpath, 'wb') as fd:
            fd.write(data)
        return data
    except Exception as e:
        sys.stderr.write("❌ get_file_from_url: exception: %s\n" % e)
        return None

#
# /.../:file/player endpoint
#

def player_endpoint(handler):
    full_url_path, query_dict = parse_GET_path(handler.path)
    show_url_path = '/'.join(full_url_path.split('/')[:-2])
    fname = full_url_path.split('/')[-2]
    metadata = load_mserve_json(show_url_path)
    if metadata is None:
        send_404(handler)
        return
    (season, episode, _) = parse_filename(fname)
    fpath = make_file_path(g_media_dir, show_url_path, fname)
    content_type = get_content_type(fpath)
    file_size = os.path.getsize(fpath)
    title = metadata.get('title', fname)
    body = render_player(show_url_path, title, season, episode, fname, content_type, file_size)
    send_html(handler, 200, body)

add_regex_route(
    'GET',
    '/.../:file/player',
    re.compile('^(\/[a-zA-Z0-9-_]+)*\/[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_\.]+\/player$'),
    player_endpoint
)

def render_player(show_url_path, title, season, episode, fname, content_type, file_size):
    html = "<!DOCTYPE html>\n<html>\n"
    html += '<head>\n<meta charset="UTF-8">\n'
    html += '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    html += '<link href="https://vjs.zencdn.net/8.3.0/video-js.css" rel="stylesheet" />'
    html += "<style>\n"
    html += "@media (prefers-color-scheme: dark) { body { background-color: #111; color: white; }}\n"
    html += "</style>\n"
    html += "</head>\n"
    html += "<body>\n"
    file_url = make_url_path(show_url_path, fname)
    player_url = make_url_path(file_url, 'player')
    html += "<h1>%s</h1>\n" % render_url_path_links(player_url)
    html += "<h2>%s</h2>\n" % title
    if season is not None and episode is not None:
        html += "<h3>Season %s, Episode %s</h3>\n" % (season, episode)
    html += '<ul><li>%s (%s)</li></ul>\n' % (fname, format_filesize(file_size))
    html += '<video id="my-video" class="video-js" controls preload="auto" width="640" height="480" data-setup="{}">\n'
    html += '<source src="%s" type="%s" />\n' % (file_url, content_type)
    html += "</video>\n"
    html += '<script src="https://vjs.zencdn.net/8.3.0/video.min.js"></script>\n'
    html += "</body>\n"
    html += "</html>\n"
    return html

#
# /tmdb-images/:size_class/:tmdb_image endpoint
#

def image_endpoint(handler):
    url_path, query_dict = parse_GET_path(handler.path)
    tmdb_image_fname = url_path.split('/')[-1]
    size_class = url_path.split('/')[-2]
    tmdb_image_url = "https://image.tmdb.org/t/p/%s/%s" % (size_class, tmdb_image_fname)
    proxied_image_url = "/tmdb-images/%s/%s" % (size_class, tmdb_image_fname)
    image_cache_fpath = make_file_path("~/.mserve/tmdb_cache/%s/%s" % (size_class, tmdb_image_fname))
    data = get_file_from_url(tmdb_image_url, image_cache_fpath)
    send_file(handler, image_cache_fpath, data=data, immutable=True)

add_regex_route(
    'GET',
    '/tmdb-images/:size_class/:tmdb_image_fname',
    re.compile('^\/tmdb-images\/w[0-9]+\/[a-zA-Z0-9]+\.(jpg|jpeg|png|webp)$'),
    image_endpoint
)

#
# /.../:file endpoint
#

def file_endpoint(handler):
    url_path, query_dict = parse_GET_path(handler.path)
    fpath = make_file_path(g_media_dir, url_path)
    is_head = (handler.command == 'HEAD')
    send_file(handler, fpath, is_head)

add_regex_route(
    'GET',
    '/.../:file',
    re.compile('^(\/[a-zA-Z0-9-_]+)*\/[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_\.]+$'),
    file_endpoint
)
add_regex_route(
    'HEAD',
    '/.../:file',
    re.compile('^(\/[a-zA-Z0-9-_]+)*\/[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_\.]+$'),
    file_endpoint
)

#
# directory endpoint
#

def directory_endpoint(handler):
    url_path, query_dict = parse_GET_path(handler.path)
    metadata = load_mserve_json(url_path)
    if metadata is None:
        send_404(handler)
        return
    if metadata["type"] == "directory":
        body = render_directory(handler, url_path)
        send_html(handler, 200, body)
    elif metadata["type"] == "series" or metadata["type"] == "movie":
        tmdb_json = {}
        tmdb_id = metadata.get('tmdb_id')
        if tmdb_id:
            tmdb_json = get_tmdb_show_details(tmdb_id)
        body = render_show(handler, url_path, metadata, tmdb_id, tmdb_json)
        send_html(handler, 200, body)
    else:
        send_500("Bad mserve.json")

add_regex_route(
    'GET',
    '/...',
    re.compile('^((\/[a-zA-Z0-9-_]+)+|\/)$'),
    directory_endpoint
)

def render_directory(handler, url_path):
    html = "<!DOCTYPE html>\n<html>\n"
    html += '<head>\n<meta charset="UTF-8">\n'
    html += '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    html += "</head>\n"
    html += "<body>\n"
    html += "<h1>%s</h1>\n" % render_url_path_links(url_path)
    triples = scan_dir(url_path)
    if len(triples):
        for triple in triples:
            title, slug, metadata = triple
            url = make_url_path(url_path, slug)
            tmdb_json = {}
            tmdb_id = metadata.get('tmdb_id')
            tmdb_json = get_tmdb_show_details(tmdb_id)
            if 'poster_path' in tmdb_json:
                proxied_image_url = "/tmdb-images/w92%s" % tmdb_json.get('poster_path')
            else:
                proxied_image_url = None
            if 'title' in tmdb_json or 'name' in tmdb_json:
                title_text = tmdb_json.get('title', tmdb_json.get('name'))
                release_date = tmdb_json.get('release_date', tmdb_json.get('first_air_date'))
                if len(release_date):
                    title_text += ' (%s)' % release_date.split('-')[0]
            elif 'title' in metadata:
                title_text = metadata['title']
            else:
                title_text = url.split('/')[-1]
            if proxied_image_url:
                html += "<div>\n"
                html += '<a href="%s"><img src="%s" style="max-width:100%%"></a>\n' % (url, proxied_image_url)
                html += '<a href="%s">%s</a>\n' % (url, title_text)
                html += "</div>\n"
            else:
                html += '<ul><li><a href="%s">%s</a></li></ul>\n' % (url, title_text)
    html += "</body>\n"
    html += "</html>\n"
    return html

def render_show(handler, url_path, metadata, tmdb_id, tmdb_json):
    def render_links(fname):
        file_url = make_url_path(url_path, fname)
        player_url = make_url_path(url_path, fname, 'player')
        port = handler.server.server_port
        inet_url = "http://%s:%s%s" % (g_ip_address, port, file_url)
        vlc_callback_url = "vlc-x-callback://x-callback-url/stream?url=%s" % inet_url
        vlc_file_url = "vlc-file://%s:%s%s" % (g_ip_address, port, file_url)
        links = []
        if is_html5_video(fname):
            link = '<a href="%s">player</a>' % player_url
            links.append(link)
        if is_ios_or_macos_safari(handler):
            link = '<a href="%s">vlc</a>' % vlc_callback_url
            links.append(link)
        if is_macos_or_ipad(handler):
            link = '<a href="%s">vlc-file</a>' % vlc_file_url
            links.append(link)
        link = '<a href="%s">file</a>' % file_url
        links.append(link)
        html = '[ %s ]' % ' | '.join(links)
        return html

    def render_footer():
        html = ""
        html += "<br><br><br>\n"
        html += "<hr>\n"
        html += 'To play <tt>vlc-file://</tt> URLs, install <a href="https://github.com/pepaslabs/VLCFileUrl">VLCFileUrl</a>.\n'
        return html

    def render_show_title():
        html = ""
        if 'title' in tmdb_json or 'name' in tmdb_json:
            title_line = tmdb_json.get('title', tmdb_json.get('name'))
            release_date = tmdb_json.get('release_date', tmdb_json.get('first_air_date'))
            if len(release_date):
                title_line += ' (%s)' % release_date.split('-')[0]
            html += '<h1>%s</h1>\n' % title_line
            tagline = tmdb_json.get('tagline','')
            if len(tagline):
                html += '<p><i>%s</i></p>\n' % tagline
            if 'poster_path' in tmdb_json:
                proxied_image_url = "/tmdb-images/w500%s" % tmdb_json.get('poster_path')
                html += '<img src="%s" style="max-width:100%%">\n' % proxied_image_url
            html += '<p>%s</p>\n' % tmdb_json['overview']
        elif 'title' in metadata:
            html += '<h1>%s</h1>\n' % metadata['title']
        return html

    def render_season_links(season_groups):
        links = []
        for season_group in season_groups:
            season_num, _ = season_group
            if season_num:
                link = '<a href="#season-%s">Season %s</a>' % (season_num, season_num)
            else:
                link = '<a href="#misc">Misc.</a>'
            links.append(link)
        html = '[ %s ]' % ' | '.join(links)
        return html

    def render_season(season_group):
        def render_episode(episodes_jsons, episode_num, fname):
            html = ""
            fpath = make_file_path(g_media_dir, url_path, fname)
            file_size = os.path.getsize(fpath)
            episode_index = episode_num - 1
            if episode_index < len(episodes_jsons) and episodes_jsons[episode_index].get('episode_number',-1) == episode_num:
                episode_json = episodes_jsons[episode_index]
                html += "<h3>Episode %s: %s</h3>\n" % (episode_num, episode_json.get('name'))
                still_path = episode_json.get('still_path')
                if still_path:
                    proxied_image_url = "/tmdb-images/w342%s" % still_path
                    html += '<img src="%s" style="max-width:100%%">\n' % proxied_image_url
                html += '<ul>\n'
                html += '<li>%s</li>\n' % episode_json.get('overview', '')
                html += '<li>%s (%s)</li>\n' % (fname, format_filesize(file_size))
                html += '<li>%s</li>\n' % render_links(fname)
                html += '</ul>\n'
            else:
                html += "<h3>Episode %s</h3>\n" % episode_num
                html += '<ul>\n'
                html += '<li>%s (%s)</li>\n' % (fname, format_filesize(file_size))
                html += '<li>%s</li>\n' % render_links(fname)
                html += '</ul>\n'
            return html

        html = ""
        season_num, episode_pairs = season_group
        season_json = get_tmdb_season_details(tmdb_id, season_num)
        episodes_jsons = season_json.get('episodes', [])
        if season_num:
            air_date = season_json.get('air_date')
            heading = '<h2 id="season-%s">Season %s' % (season_num, season_num)
            if air_date:
                heading += ' (%s)' % air_date.split('-')[0]
            heading += '</h2>\n'
            html += heading
            for episode_num, fname in episode_pairs:
                html += render_episode(episodes_jsons, episode_num, fname)
        else:
            if has_seasons:
                html += '<h2 id="misc">Misc.</h2>\n'
            html += '<ul>\n'
            for _, fname in episode_pairs:
                fpath = make_file_path(g_media_dir, url_path, fname)
                file_size = os.path.getsize(fpath)
                html += '<li>'
                html += '%s (%s)\n' % (fname, format_filesize(file_size))
                html += '<ul>\n'
                html += '<li>%s</li>\n' % render_links(fname)
                html += '</ul>\n'
                html += '</li>'
            html += '</ul>\n'
        return html

    season_groups = scan_for_videos(url_path)
    has_seasons = list_get(list_get(season_groups, 0), 0) != None
    html = "<!DOCTYPE html>\n<html>\n"
    html += '<head>\n<meta charset="UTF-8">\n'
    html += '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    html += "</head>\n"
    html += "<body>\n"
    html += "<h1>%s</h1>\n" % render_url_path_links(url_path)
    html += render_show_title()
    if len(season_groups) > 1:
        html += render_season_links(season_groups) + '<br>\n'
    for season_group in season_groups:
        html += render_season(season_group)
    html += render_footer()
    html += "</body>\n"
    html += "</html>\n"
    return html

#
# main
#

if __name__ == "__main__":
    if sys.argv[0].split('/')[-1] == 'slugify.py':
        # if we were invoked as 'slugify.py' symlink, act as a renaming utility.
        for arg in sys.argv[1:]:
            slugify_file(arg)
    else:
        # otherwise start the server.
        port = 8000
        address_pair = ('', 8000)
        server = http.server.ThreadingHTTPServer(address_pair, Handler)
        sys.stderr.write("Routable IP address detected as %s\n" % g_ip_address)
        sys.stderr.write("Listening on port %s\n" % port)
        server.serve_forever()
