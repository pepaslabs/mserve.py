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
def send_file(handler, fpath, is_head):
    def send_whole_file(handler, fd, content_type, file_size):
        content_length = file_size
        handler.send_response(200)
        handler.send_header('Content-Length', "%s" % content_length)
        handler.send_header('Content-Type', content_length)
        handler.send_header('Accept-Ranges', 'bytes')
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
        content_type = get_content_type(fpath)
        file_size = os.path.getsize(fpath)
        fd = open(fpath, 'rb')
        if range_header is None:
            send_whole_file(handler, fd, content_type, file_size)
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
    return mimetypes.guess_type(fpath)[0] or 'application/octet-stream'

# This is a video file?
def is_video(fpath):
    exts = [
        '.avi', '.flv', '.wmv',
        '.mpg', '.mpeg', '.mp2', '.mp4', '.m4v',
        '.ogg', '.ogm', '.ogv',
        '.webm',
        '.mkv',
        '.mov',
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
    g_tmdb_token = os.env['TMDB_TOKEN']

#
# mserve disk storage layer
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
                with open(json_fpath) as fd:
                    metadata = json.load(fd)
                    title = metadata.get('title', slug)
                    triples.append([title, slug, metadata])
            except Exception as e:
                sys.stderr.write("parse_mserve_json: exception: %s\n" % e)
    triples.sort()
    return triples

# Load the mserve.json if present.
def parse_mserve_json(url_path):
    json_fpath = make_file_path(g_media_dir, url_path, "mserve.json")
    if os.path.isfile(json_fpath):
        try:
            with open(json_fpath) as fd:
                return json.load(fd)
        except Exception as e:
            sys.stderr.write("parse_mserve_json: exception: %s\n" % e)
    return None

# Parse the season and episode number from a filename.
def parse_filename(fname):
    episode_pattern = re.compile(".*?-s(\d)+e(\d+)")
    m = episode_pattern.match(fname)
    if m and len(m.groups()) == 2:
        season_num = int(m.group(1))
        episode_num = int(m.group(2))
        return [season_num, episode_num, fname]
    else:
        return [None, None, fname]

# Scan a show for seasons, episodes and filenames.
# E.g. [[1,1,'tng-s1e1.mp4'], [1,2,'tng-s1e2.mp4']]
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
    return episode_triples + video_triples

#
# /.../:file/player endpoint
#

def player_endpoint(handler):
    full_url_path, query_dict = parse_GET_path(handler.path)
    show_url_path = '/'.join(full_url_path.split('/')[:-2])
    fname = full_url_path.split('/')[-2]
    metadata = parse_mserve_json(show_url_path)
    if metadata is None:
        send_404(handler)
        return
    (season, episode, _) = parse_filename(fname)
    fpath = make_file_path(show_url_path, fname)
    content_type = get_content_type(fpath)
    title = metadata.get('title', fname)
    body = render_player(show_url_path, title, season, episode, fname, content_type)
    send_html(handler, 200, body)

add_regex_route(
    'GET',
    '/.../:file/player',
    re.compile('^(\/[a-zA-Z0-9-_]+)*\/[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_\.]+\/player$'),
    player_endpoint
)

def render_player(show_url_path, title, season, episode, fname, content_type):
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
    html += '<video id="my-video" class="video-js" controls preload="auto" width="640" height="480" data-setup="{}">\n'
    html += '<source src="%s" type="%s" />\n' % (file_url, content_type)
    html += "</video>\n"
    html += '<script src="https://vjs.zencdn.net/8.3.0/video.min.js"></script>\n'
    html += "</body>\n"
    html += "</html>\n"
    return html

#
# /.../:file endpoint
#

def file_endpoint(handler):
    url_path, query_dict = parse_GET_path(handler.path)
    fpath = make_file_path(g_media_dir, url_path)
    print("fpath: %s" % fpath)
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
    metadata = parse_mserve_json(url_path)
    if metadata is None:
        send_404(handler)
        return
    if metadata["type"] == "directory":
        body = render_directory(handler, url_path)
        send_html(handler, 200, body)
    elif metadata["type"] == "series" or metadata["type"] == "movie":
        body = render_show(handler, url_path, metadata)
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
        html += "<ul>\n"
        for triple in triples:
            title, slug, metadata = triple
            url = make_url_path(url_path, slug)
            html += '<li><a href="%s">%s</a></li>\n' % (url, title)
        html += "</ul>\n"
    html += "</body>\n"
    html += "</html>\n"
    return html

def render_show(handler, url_path, metadata):
    def render_links(fname):
        file_url = make_url_path(url_path, fname)
        player_url = make_url_path(url_path, fname, 'player')
        inet_url = "http://%s:%s%s" % (handler.server.server_name, handler.server.server_port, file_url)
        vlc_callback_url = "vlc-x-callback://x-callback-url/stream?url=%s" % inet_url
        vlc_file_url = "vlc-file://%s:%s%s" % (handler.server.server_name, handler.server.server_port, file_url)
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
    html = "<!DOCTYPE html>\n<html>\n"
    html += '<head>\n<meta charset="UTF-8">\n'
    html += '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    html += "</head>\n"
    html += "<body>\n"
    html += "<h1>%s</h1>\n" % render_url_path_links(url_path)
    if 'title' in metadata:
        html += '<h2>%s</h2>\n' % metadata['title']
    video_triples = scan_for_videos(url_path)
    if len(video_triples):
        html += "<ul>\n"
        current_season = None
        for video_triple in video_triples:
            season_num, episode_num, fname = video_triple
            if season_num != current_season:
                html += "</ul>\n"
                if season_num is None:
                    html += '<h3>Other videos</h3>\n'
                else:
                    html += '<h3>Season %s</h3>\n' % season_num
                html += "<ul>\n"
            current_season = season_num
            if episode_num is not None:
                item_name = "Episode %s" % episode_num
            else:
                item_name = fname
            links_html = render_links(fname)
            html += '<li>%s %s</li>\n' % (item_name, links_html)
        html += "</ul>\n"
    html += render_footer()
    html += "</body>\n"
    html += "</html>\n"
    return html

#
# main
#

if __name__ == "__main__":
    port = 8000
    address_pair = ('', 8000)
    server = http.server.ThreadingHTTPServer(address_pair, Handler)
    sys.stderr.write("Listening on port %s\n" % port)
    server.serve_forever()
