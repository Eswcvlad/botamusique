#!/usr/bin/python3

from functools import wraps
from flask import Flask, render_template, request, redirect, send_file, Response, jsonify, abort
import variables as var
import util
import math
import os
import os.path
import shutil
from werkzeug.utils import secure_filename
import errno
import media
from media.item import dicts_to_items
from media.cache import get_cached_wrapper_from_scrap, get_cached_wrapper_by_id, get_cached_wrappers_by_tags, \
    get_cached_wrapper
from database import MusicDatabase, Condition
import logging
import time


class ReverseProxied(object):
    """Wrap the application in this middleware and configure the
    front-end server to add these headers, to let you quietly bind
    this to a URL other than / and to an HTTP scheme that is
    different than what is used locally.

    In nginx:
    location /myprefix {
        proxy_pass http://192.168.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme $scheme;
        proxy_set_header X-Script-Name /myprefix;
        }

    :param app: the WSGI application
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        script_name = environ.get('HTTP_X_SCRIPT_NAME', '')
        if script_name:
            environ['SCRIPT_NAME'] = script_name
            path_info = environ['PATH_INFO']
            if path_info.startswith(script_name):
                environ['PATH_INFO'] = path_info[len(script_name):]

        scheme = environ.get('HTTP_X_SCHEME', '')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        real_ip = environ.get('HTTP_X_REAL_IP', '')
        if real_ip:
            environ['REMOTE_ADDR'] = real_ip
        return self.app(environ, start_response)


web = Flask(__name__)
log = logging.getLogger("bot")
user = 'Remote Control'


def init_proxy():
    global web
    if var.is_proxified:
        web.wsgi_app = ReverseProxied(web.wsgi_app)

# https://stackoverflow.com/questions/29725217/password-protect-one-webpage-in-flask-app


def check_auth(username, password):
    """This function is called to check if a username /
    password combination is valid.
    """
    return username == var.config.get("webinterface", "user") and password == var.config.get("webinterface", "password")


def authenticate():
    """Sends a 401 response that enables basic auth"""
    global log
    return Response('Could not verify your access level for that URL.\n'
                    'You have to login with proper credentials', 401,
                    {'WWW-Authenticate': 'Basic realm="Login Required"'})


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        global log
        auth = request.authorization
        if var.config.getboolean("webinterface", "require_auth") and (not auth or not check_auth(auth.username, auth.password)):
            if auth:
                log.info("web: Failed login attempt, user: %s" % auth.username)
            return authenticate()
        return f(*args, **kwargs)
    return decorated


def tag_color(tag):
    num = hash(tag) % 8
    if num == 0:
        return "primary"
    elif num == 1:
        return "secondary"
    elif num == 2:
        return "success"
    elif num == 3:
        return "danger"
    elif num == 4:
        return "warning"
    elif num == 5:
        return "info"
    elif num == 6:
        return "light"
    elif num == 7:
        return "dark"


def build_tags_color_lookup():
    color_lookup = {}
    for tag in var.music_db.query_all_tags():
        color_lookup[tag] = tag_color(tag)

    return color_lookup


def build_path_tags_lookup():
    path_tags_lookup = {}
    ids = list(var.cache.file_id_lookup.values())
    if len(ids) > 0:
        condition = Condition().and_equal("type", "file")
        id_tags_lookup = var.music_db.query_tags(condition)

        for path, id in var.cache.file_id_lookup.items():
            path_tags_lookup[path] = id_tags_lookup[id]

    return path_tags_lookup


def recur_dir(dirobj):
    for name, dir in dirobj.get_subdirs().items():
        print(dirobj.fullpath + "/" + name)
        recur_dir(dir)


@web.route("/", methods=['GET'])
@requires_auth
def index():
    while var.cache.dir_lock.locked():
        time.sleep(0.1)

    tags_color_lookup = build_tags_color_lookup()
    path_tags_lookup = build_path_tags_lookup()

    return render_template('index.html',
                           all_files=var.cache.files,
                           tags_lookup=path_tags_lookup,
                           tags_color_lookup=tags_color_lookup,
                           music_library=var.cache.dir,
                           os=os,
                           playlist=var.playlist,
                           user=var.user,
                           paused=var.bot.is_pause,
                           )


@web.route("/playlist", methods=['GET'])
@requires_auth
def playlist():
    if len(var.playlist) == 0:
        return jsonify({'items': [render_template('playlist.html',
                                                  m=False,
                                                  index=-1
                                                  )]
                        })

    tags_color_lookup = build_tags_color_lookup()
    items = []

    for index, item_wrapper in enumerate(var.playlist):
        items.append(render_template('playlist.html',
                                     index=index,
                                     tags_color_lookup=tags_color_lookup,
                                     m=item_wrapper.item(),
                                     playlist=var.playlist
                                     )
                     )

    return jsonify({'items': items})


def status():
    if len(var.playlist) > 0:
        return jsonify({'ver': var.playlist.version,
                        'empty': False,
                        'play': not var.bot.is_pause,
                        'mode': var.playlist.mode})
    else:
        return jsonify({'ver': var.playlist.version,
                        'empty': True,
                        'play': False,
                        'mode': var.playlist.mode})


@web.route("/post", methods=['POST'])
@requires_auth
def post():
    global log

    if request.method == 'POST':
        if request.form:
            log.debug("web: Post request from %s: %s" % (request.remote_addr, str(request.form)))

        if 'add_item_at_once' in request.form:
            music_wrapper = get_cached_wrapper_by_id(var.bot, request.form['add_item_at_once'], user)
            if music_wrapper:
                var.playlist.insert(var.playlist.current_index + 1, music_wrapper)
                log.info('web: add to playlist(next): ' + music_wrapper.format_debug_string())
                var.bot.interrupt()
            else:
                abort(404)

        if 'add_item_bottom' in request.form:
            music_wrapper = get_cached_wrapper_by_id(var.bot, request.form['add_item_bottom'], user)

            if music_wrapper:
                var.playlist.append(music_wrapper)
                log.info('web: add to playlist(bottom): ' + music_wrapper.format_debug_string())
            else:
                abort(404)

        elif 'add_item_next' in request.form:
            music_wrapper = get_cached_wrapper_by_id(var.bot, request.form['add_item_next'], user)
            if music_wrapper:
                var.playlist.insert(var.playlist.current_index + 1, music_wrapper)
                log.info('web: add to playlist(next): ' + music_wrapper.format_debug_string())
            else:
                abort(404)

        elif 'add_url' in request.form:
            music_wrapper = get_cached_wrapper_from_scrap(var.bot, type='url', url=request.form['add_url'], user=user)
            var.playlist.append(music_wrapper)

            log.info("web: add to playlist: " + music_wrapper.format_debug_string())
            if len(var.playlist) == 2:
                # If I am the second item on the playlist. (I am the next one!)
                var.bot.async_download_next()

        elif 'add_radio' in request.form:
            url = request.form['add_radio']
            music_wrapper = get_cached_wrapper_from_scrap(var.bot, type='radio', url=url, user=user)
            var.playlist.append(music_wrapper)

            log.info("cmd: add to playlist: " + music_wrapper.format_debug_string())

        elif 'delete_music' in request.form:
            music_wrapper = var.playlist[int(request.form['delete_music'])]
            log.info("web: delete from playlist: " + music_wrapper.format_debug_string())

            if len(var.playlist) >= int(request.form['delete_music']):
                index = int(request.form['delete_music'])

                if index == var.playlist.current_index:
                    var.playlist.remove(index)

                    if index < len(var.playlist):
                        if not var.bot.is_pause:
                            var.bot.interrupt()
                            var.playlist.current_index -= 1
                            # then the bot will move to next item

                    else:  # if item deleted is the last item of the queue
                        var.playlist.current_index -= 1
                        if not var.bot.is_pause:
                            var.bot.interrupt()
                else:
                    var.playlist.remove(index)

        elif 'play_music' in request.form:
            music_wrapper = var.playlist[int(request.form['play_music'])]
            log.info("web: jump to: " + music_wrapper.format_debug_string())

            if len(var.playlist) >= int(request.form['play_music']):
                var.playlist.point_to(int(request.form['play_music']) - 1)
                if not var.bot.is_pause:
                    var.bot.interrupt()
                else:
                    var.bot.is_pause = False
                time.sleep(0.1)

        elif 'delete_music_file' in request.form and ".." not in request.form['delete_music_file']:
            path = var.music_folder + request.form['delete_music_file']
            if os.path.isfile(path):
                log.info("web: delete file " + path)
                os.remove(path)

        elif 'delete_folder' in request.form and ".." not in request.form['delete_folder']:
            path = var.music_folder + request.form['delete_folder']
            if os.path.isdir(path):
                log.info("web: delete folder " + path)
                shutil.rmtree(path)
                time.sleep(0.1)

        elif 'add_tag' in request.form:
            music_wrappers = get_cached_wrappers_by_tags(var.bot, [request.form['add_tag']], user)
            for music_wrapper in music_wrappers:
                log.info("cmd: add to playlist: " + music_wrapper.format_debug_string())
            var.playlist.extend(music_wrappers)

        elif 'action' in request.form:
            action = request.form['action']
            if action == "randomize":
                if var.playlist.mode != "random":
                    var.playlist = media.playlist.get_playlist("random", var.playlist)
                else:
                    var.playlist.randomize()
                var.bot.interrupt()
                var.db.set('playlist', 'playback_mode', "random")
                log.info("web: playback mode changed to random.")
            if action == "one-shot":
                var.playlist = media.playlist.get_playlist("one-shot", var.playlist)
                var.db.set('playlist', 'playback_mode', "one-shot")
                log.info("web: playback mode changed to one-shot.")
            if action == "repeat":
                var.playlist = media.playlist.get_playlist("repeat", var.playlist)
                var.db.set('playlist', 'playback_mode', "repeat")
                log.info("web: playback mode changed to repeat.")
            if action == "autoplay":
                var.playlist = media.playlist.get_playlist("autoplay", var.playlist)
                var.db.set('playlist', 'playback_mode', "autoplay")
                log.info("web: playback mode changed to autoplay.")
            if action == "rescan":
                var.cache.build_dir_cache(var.bot)
                log.info("web: Local file cache refreshed.")
            elif action == "stop":
                var.bot.stop()
            elif action == "pause":
                var.bot.pause()
            elif action == "resume":
                var.bot.resume()
            elif action == "clear":
                var.bot.clear()
            elif action == "volume_up":
                if var.bot.volume_set + 0.03 < 1.0:
                    var.bot.volume_set = var.bot.volume_set + 0.03
                else:
                    var.bot.volume_set = 1.0
                var.db.set('bot', 'volume', str(var.bot.volume_set))
                log.info("web: volume up to %d" % (var.bot.volume_set * 100))
            elif action == "volume_down":
                if var.bot.volume_set - 0.03 > 0:
                    var.bot.volume_set = var.bot.volume_set - 0.03
                else:
                    var.bot.volume_set = 0
                var.db.set('bot', 'volume', str(var.bot.volume_set))
                log.info("web: volume up to %d" % (var.bot.volume_set * 100))

    return status()

def build_library_query_condition(form):
    try:
        condition = Condition()

        if form['type'] == 'file':
            folder = form['dir']
            if not folder.endswith('/') and folder:
                folder += '/'
            sub_cond = Condition()
            for file in var.cache.files:
                if file.startswith(folder):
                    sub_cond.or_equal("id", var.cache.file_id_lookup[file])
            condition.and_sub_condition(sub_cond)
        elif form['type'] == 'url':
            condition.and_equal("type", "url")
        elif form['type'] == 'radio':
            condition.and_equal("type", "radio")

        tags = form['tags'].split(",")
        for tag in tags:
            condition.and_like("tags", f"%{tag},%", case_sensitive=False)

        _keywords = form['keywords'].split(" ")
        keywords = []
        for kw in _keywords:
            if kw:
                keywords.append(kw)

        for keyword in keywords:
            condition.and_like("title", f"%{keyword}%", case_sensitive=False)

        return condition
    except KeyError:
        abort(400)

@web.route("/library", methods=['POST'])
@requires_auth
def library():
    global log
    ITEM_PER_PAGE = 10

    if request.form:
        log.debug("web: Post request from %s: %s" % (request.remote_addr, str(request.form)))

        condition = build_library_query_condition(request.form)

        total_count = var.music_db.query_music_count(condition)
        page_count =  math.ceil(total_count / ITEM_PER_PAGE)

        current_page = int(request.form['page']) if 'page' in request.form else 1
        if current_page <= page_count:
            condition.offset((current_page - 1) * ITEM_PER_PAGE)
        else:
            abort(404)

        condition.limit(ITEM_PER_PAGE)
        items = dicts_to_items(var.bot, var.music_db.query_music(condition))

        if 'action' in request.form and request.form['action'] == 'add':
            for item in items:
                music_wrapper = get_cached_wrapper(item, user)
                var.playlist.append(music_wrapper)

                log.info("cmd: add to playlist: " + music_wrapper.format_debug_string())

            return redirect("./", code=302)
        else:
            results = []
            for item in items:
                result = {}
                result['id'] = item.id
                result['title'] = item.title
                result['type'] = item.display_type()
                result['tags'] = [(tag, tag_color(tag)) for tag in item.tags]
                if item.thumbnail:
                    result['thumb'] = f"data:image/PNG;base64,{item.thumbnail}"
                else:
                    result['thumb'] = "static/image/unknown-album.png"

                if item.type == 'file':
                    result['path'] = item.path
                    result['artist'] = item.artist
                else:
                    result['path'] = item.url
                    result['artist'] = "??"

                results.append(result)

            return jsonify({
                'items': results,
                'total_pages': page_count,
                'active_page': current_page
            })
    else:
        abort(400)


@web.route('/upload', methods=["POST"])
def upload():
    global log

    files = request.files.getlist("file[]")
    if not files:
        return redirect("./", code=400)

    # filename = secure_filename(file.filename).strip()
    for file in files:
        filename = file.filename
        if filename == '':
            return redirect("./", code=400)

        targetdir = request.form['targetdir'].strip()
        if targetdir == '':
            targetdir = 'uploads/'
        elif '../' in targetdir:
            return redirect("./", code=400)

        log.info('web: Uploading file from %s:' % request.remote_addr)
        log.info('web: - filename: ' + filename)
        log.info('web: - targetdir: ' + targetdir)
        log.info('web: - mimetype: ' + file.mimetype)

        if "audio" in file.mimetype:
            storagepath = os.path.abspath(os.path.join(var.music_folder, targetdir))
            print('storagepath:', storagepath)
            if not storagepath.startswith(os.path.abspath(var.music_folder)):
                return redirect("./", code=400)

            try:
                os.makedirs(storagepath)
            except OSError as ee:
                if ee.errno != errno.EEXIST:
                    return redirect("./", code=500)

            filepath = os.path.join(storagepath, filename)
            log.info(' - filepath: ' + filepath)
            if os.path.exists(filepath):
                continue

            file.save(filepath)
        else:
            continue

    var.cache.build_dir_cache(var.bot)
    log.info("web: Local file cache refreshed.")

    return redirect("./", code=302)


@web.route('/download', methods=["GET"])
def download():
    global log

    print('id' in request.args)
    if 'id' in request.args and request.args['id']:
        item = dicts_to_items(var.bot,
                               var.music_db.query_music(
                                   Condition().and_equal('id', request.args['id'])))[0]

        requested_file = item.uri()
        log.info('web: Download of file %s requested from %s:' % (requested_file, request.remote_addr))

        try:
            return send_file(requested_file, as_attachment=True)
        except Exception as e:
            log.exception(e)
            abort(404)

    else:
        condition = build_library_query_condition(request.args)
        items = dicts_to_items(var.bot, var.music_db.query_music(condition))

        zipfile = util.zipdir([item.uri() for item in items])

        try:
            return send_file(zipfile, as_attachment=True)
        except Exception as e:
            log.exception(e)
            abort(404)

    return abort(400)


if __name__ == '__main__':
    web.run(port=8181, host="127.0.0.1")
