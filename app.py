"""Main Flask application."""
import json
import logging
from pathlib import Path

from flask import Flask
from flask_caching import Cache
import flask_gae_static
from lexrpc.server import Server
from lexrpc.flask_server import init_flask
from oauth_dropins.webutil import (
    appengine_info,
    appengine_config,
    flask_util,
    util,
)

import common

logger = logging.getLogger(__name__)
logging.getLogger('lexrpc').setLevel(logging.INFO)
logging.getLogger('negotiator').setLevel(logging.WARNING)

app_dir = Path(__file__).parent


app = Flask(__name__, static_folder=None)
app.template_folder = './templates'
app.json.compact = False
app.config.from_pyfile(app_dir / 'config.py')
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
if appengine_info.LOCAL:
    flask_gae_static.init_app(app)

# don't redirect API requests with blank path elements
app.url_map.redirect_defaults = True

app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client)

cache = Cache(app)

util.set_user_agent('Bridgy Fed (https://fed.brid.gy/)')

# XRPC server
lexicons = []
for filename in (app_dir / 'lexicons/app/bsky').glob('**/*.json'):
    with open(filename) as f:
        lexicons.append(json.load(f))

xrpc_server = Server(lexicons, validate=False)
init_flask(xrpc_server, app)

# import all modules to register their Flask handlers
import activitypub, add_webmention, follow, pages, redirect, render, superfeedr, webfinger, webmention, xrpc_actor, xrpc_feed, xrpc_graph
