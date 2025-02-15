"""Simple conneg endpoint that serves AS2 or redirects to to the original post.

Serves /r/https://foo.com/bar URL paths, where https://foo.com/bar is an
original post. Needed for Mastodon interop, they require that AS2 object ids and
urls are on the same domain that serves them. Background:

https://github.com/snarfed/bridgy-fed/issues/16#issuecomment-424799599
https://github.com/tootsuite/mastodon/pull/6219#issuecomment-429142747

The conneg makes these /r/ URLs searchable in Mastodon:
https://github.com/snarfed/bridgy-fed/issues/352
"""
import logging
import re
import urllib.parse

from flask import redirect, request
from granary import as2
from negotiator import ContentNegotiator, AcceptParameters, ContentType
from oauth_dropins.webutil import flask_util, util
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.util import json_dumps, json_loads

from app import app, cache
from common import (
    CACHE_TIME,
    CONTENT_TYPE_HTML,
    postprocess_as2,
)
from models import Object, User

logger = logging.getLogger(__name__)

_negotiator = ContentNegotiator(acceptable=[
    AcceptParameters(ContentType(CONTENT_TYPE_HTML)),
    AcceptParameters(ContentType(as2.CONTENT_TYPE)),
    AcceptParameters(ContentType(as2.CONTENT_TYPE_LD)),
])


@app.get(r'/r/<path:to>')
@flask_util.cached(cache, CACHE_TIME, headers=['Accept'])
def redir(to):
    """301 redirect to the embedded fully qualified URL.

    e.g. redirects /r/https://foo.com/bar?baz to https://foo.com/bar?baz
    """
    if request.args:
        to += '?' + urllib.parse.urlencode(request.args)
    # some browsers collapse repeated /s in the path down to a single slash.
    # if that happened to this URL, expand it back to two /s.
    to = re.sub(r'^(https?:/)([^/])', r'\1/\2', to)

    if not util.is_web(to):
        error(f'Expected fully qualified URL; got {to}')

    # check that we've seen this domain before so we're not an open redirect
    domains = set((util.domain_from_link(to, minimize=True),
                   util.domain_from_link(to, minimize=False),
                   urllib.parse.urlparse(to).hostname))
    for domain in domains:
        if domain:
            user = User.get_by_id(domain)
            if user:
                logger.info(f'Found User for domain {domain}')
                break
    else:
        return f'No user found for any of {domains}', 404

    # check conneg, serve AS2 if requested
    accept = request.headers.get('Accept')
    if accept:
        try:
            negotiated = _negotiator.negotiate(accept)
        except ValueError:
            # work around https://github.com/CottageLabs/negotiator/issues/6
            negotiated = None
        if negotiated:
            type = str(negotiated.content_type)
            if type in (as2.CONTENT_TYPE, as2.CONTENT_TYPE_LD):
                # load from the datastore
                obj = Object.get_by_id(to)
                if not obj or obj.deleted:
                    return f'Object not found: {to}', 404
                ret = postprocess_as2(as2.from_as1(obj.as1),
                                      user, create=False)
                logger.info(f'Returning: {json_dumps(ret, indent=2)}')
                return ret, {
                    'Content-Type': type,
                    'Access-Control-Allow-Origin': '*',
                }

    # redirect
    logger.info(f'redirecting to {to}')
    return redirect(to, code=301)
