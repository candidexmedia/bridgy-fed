"""Datastore model classes."""
import base64
import difflib
import logging
import urllib.parse

import requests
from werkzeug.exceptions import BadRequest, NotFound

from Crypto import Random
from Crypto.PublicKey import RSA
from Crypto.Util import number
from flask import request
from google.cloud import ndb
from granary import as1, as2, bluesky, microformats2
from oauth_dropins.webutil.appengine_info import DEBUG
from oauth_dropins.webutil.models import ComputedJsonProperty, JsonProperty, StringIdModel
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import json_dumps, json_loads

import common

# https://github.com/snarfed/bridgy-fed/issues/314
WWW_DOMAINS = frozenset((
    'www.jvt.me',
))
PROTOCOLS = ('activitypub', 'bluesky', 'ostatus', 'webmention', 'ui')
# 2048 bits makes tests slow, so use 1024 for them
KEY_BITS = 1024 if DEBUG else 2048

logger = logging.getLogger(__name__)


def base64_to_long(x):
    """Converts x from URL safe base64 encoding to a long integer.

    Originally from django_salmon.magicsigs.
    """
    return number.bytes_to_long(base64.urlsafe_b64decode(x))


def long_to_base64(x):
    """Converts x from a long integer to base64 URL safe encoding.

    Originally from django_salmon.magicsigs.
    """
    return base64.urlsafe_b64encode(number.long_to_bytes(x))


class User(StringIdModel):
    """Stores a Bridgy Fed user.

    The key name is the domain. The key pair is used for ActivityPub HTTP Signatures.

    https://tools.ietf.org/html/draft-cavage-http-signatures-07

    The key pair's modulus and exponent properties are all encoded as base64url
    (ie URL-safe base64) strings as described in RFC 4648 and section 5.1 of the
    Magic Signatures spec.
    """
    mod = ndb.StringProperty(required=True)
    public_exponent = ndb.StringProperty(required=True)
    private_exponent = ndb.StringProperty(required=True)
    has_redirects = ndb.BooleanProperty()
    redirects_error = ndb.TextProperty()
    has_hcard = ndb.BooleanProperty()
    actor_as2 = JsonProperty()
    use_instead = ndb.KeyProperty()

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    @property
    def homepage(self):
        return f'https://{self.key.id()}/'

    @classmethod
    def _get_kind(cls):
        return 'MagicKey'

    def _post_put_hook(self, future):
        logger.info(f'Wrote User {self.key.id()}')

    @classmethod
    def get_by_id(cls, id):
        """Override Model.get_by_id to follow the use_instead property."""
        user = cls._get_by_id(id)
        if user and user.use_instead:
            return user.use_instead.get()

        return user

    @staticmethod
    @ndb.transactional()
    def get_or_create(domain, **kwargs):
        """Loads and returns a User. Creates it if necessary."""
        user = User.get_by_id(domain)

        if not user:
            # originally from django_salmon.magicsigs
            # this uses urandom(), and does nontrivial math, so it can take a
            # while depending on the amount of randomness available.
            rng = Random.new().read
            key = RSA.generate(KEY_BITS, rng)
            user = User(id=domain,
                        mod=long_to_base64(key.n),
                        public_exponent=long_to_base64(key.e),
                        private_exponent=long_to_base64(key.d),
                        **kwargs)
            user.put()

        return user

    def href(self):
        return f'data:application/magic-public-key,RSA.{self.mod}.{self.public_exponent}'

    def public_pem(self):
        """Returns: bytes"""
        rsa = RSA.construct((base64_to_long(str(self.mod)),
                             base64_to_long(str(self.public_exponent))))
        return rsa.exportKey(format='PEM')

    def private_pem(self):
        """Returns: bytes"""
        rsa = RSA.construct((base64_to_long(str(self.mod)),
                             base64_to_long(str(self.public_exponent)),
                             base64_to_long(str(self.private_exponent))))
        return rsa.exportKey(format='PEM')

    def to_as1(self):
        """Returns this user as an AS1 actor dict, if possible."""
        if self.actor_as2:
            return as2.to_as1(self.actor_as2)

    def username(self):
        """Returns the user's preferred username.

        Uses stored representative h-card, falls back to domain if that's not
        available.

        Returns: str
        """
        domain = self.key.id()

        if self.actor_as2:
            for url in [u.get('value') if isinstance(u, dict) else u
                        for u in util.get_list(self.actor_as2, 'url')]:
                if url and url.startswith('acct:'):
                    urluser, urldomain = util.parse_acct_uri(url)
                    if urldomain == domain:
                        logger.info(f'Found custom username: {urluser}')
                        return urluser

        logger.info(f'Defaulting username to domain {domain}')
        return domain

    def address(self):
        """Returns this user's ActivityPub address, eg '@me@foo.com'."""
        return f'@{self.username()}@{self.key.id()}'

    def is_homepage(self, url):
        """Returns True if the given URL points to this user's home page."""
        if not url:
            return False

        url = url.strip().rstrip('/')
        if url == self.key.id():
            return True

        parsed = urllib.parse.urlparse(url)
        return (parsed.netloc == self.key.id()
                and parsed.scheme in ('', 'http', 'https')
                and not parsed.path and not parsed.query
                and not parsed.params and not parsed.fragment)

    def user_page_link(self):
        """Returns a pretty user page link with the user's name and profile picture."""
        domain = self.key.id()
        actor = self.actor_as2 or {}
        name = (actor.get('name') or
                # prettify if domain, noop if username
                util.domain_from_link(self.username()))
        img = util.get_url(actor, 'icon') or ''

        return f'<a href="/user/{domain}"><img src="{img}" class="profile"> {name}</a>'

    def verify(self):
        """Fetches site a couple ways to check for redirects and h-card.

        Returns: User that was verified. May be different than self! eg if self's
          domain started with www and we switch to the root domain.
        """
        domain = self.key.id()
        logger.info(f'Verifying {domain}')

        if domain.startswith('www.') and domain not in WWW_DOMAINS:
            # if root domain redirects to www, use root domain instead
            # https://github.com/snarfed/bridgy-fed/issues/314
            root = domain.removeprefix("www.")
            root_site = f'https://{root}/'
            try:
                resp = util.requests_get(root_site, gateway=False)
                if resp.ok and self.is_homepage(resp.url):
                    logger.info(f'{root_site} redirects to {resp.url} ; using {root} instead')
                    root_user = User.get_or_create(root)
                    self.use_instead = root_user.key
                    self.put()
                    return root_user.verify()
            except requests.RequestException:
                pass

        # check webfinger redirect
        path = f'/.well-known/webfinger?resource=acct:{domain}@{domain}'
        self.has_redirects = False
        self.redirects_error = None
        try:
            url = urllib.parse.urljoin(self.homepage, path)
            resp = util.requests_get(url, gateway=False)
            domain_urls = ([f'https://{domain}/' for domain in common.DOMAINS] +
                           [common.host_url()])
            expected = [urllib.parse.urljoin(url, path) for url in domain_urls]
            if resp.ok:
                if resp.url in expected:
                    self.has_redirects = True
                elif resp.url:
                    diff = '\n'.join(difflib.Differ().compare([resp.url], [expected[0]]))
                    self.redirects_error = f'Current vs expected:<pre>{diff}</pre>'
            else:
                lines = [url, f'  returned HTTP {resp.status_code}']
                if resp.url != url:
                    lines[1:1] = ['  redirected to:', resp.url]
                self.redirects_error = '<pre>' + '\n'.join(lines) + '</pre>'
        except requests.RequestException:
            pass

        # check home page
        try:
            _, _, self.actor_as2 = common.actor(self)
            self.has_hcard = True
        except (BadRequest, NotFound):
            self.actor_as2 = None
            self.has_hcard = False

        return self


class Target(ndb.Model):
    """Delivery destinations. ActivityPub inboxes, webmention targets, etc.

    Used in StructuredPropertys inside Object; not stored directly in the
    datastore.

    ndb implements this by hoisting each property here into a corresponding
    property on the parent entity, prefixed by the StructuredProperty name
    below, eg delivered.uri, delivered.protocol, etc.

    For repeated StructuredPropertys, the hoisted properties are all
    repeated on the parent entity, and reconstructed into
    StructuredPropertys based on their order.

    https://googleapis.dev/python/python-ndb/latest/model.html#google.cloud.ndb.model.StructuredProperty
    """
    uri = ndb.StringProperty(required=True)
    protocol = ndb.StringProperty(choices=PROTOCOLS, required=True)


class Object(StringIdModel):
    """An activity or other object, eg actor.

    Key name is the id. We synthesize ids if necessary.
    """
    STATUSES = ('new', 'in progress', 'complete', 'failed', 'ignored')
    LABELS = ('activity', 'feed', 'notification', 'user')

    # domains of the Bridgy Fed users this activity is to or from
    domains = ndb.StringProperty(repeated=True)
    status = ndb.StringProperty(choices=STATUSES)
    source_protocol = ndb.StringProperty(choices=PROTOCOLS)
    labels = ndb.StringProperty(repeated=True, choices=LABELS)

    # TODO: switch back to ndb.JsonProperty if/when they fix it for the web console
    # https://github.com/googleapis/python-ndb/issues/874
    as2 = JsonProperty()  # only one of the rest will be populated...
    bsky = JsonProperty() # Bluesky / AT Protocol
    mf2 = JsonProperty()  # HTML microformats2

    @ComputedJsonProperty
    def as1(self):
        # TODO: switch back to assert
        # assert (self.as2 is not None) ^ (self.bsky is not None) ^ (self.mf2 is not None), \
        #     f'{self.as2} {self.bsky} {self.mf2}'
        if not (self.as2 is not None) ^ (self.bsky is not None) ^ (self.mf2 is not None):
            logging.warning(f'{self.key} has multiple! {self.as2 is not None} {self.bsky is not None} {self.mf2 is not None}')

        if self.as2 is not None:
            return as2.to_as1(common.redirect_unwrap(self.as2))
        elif self.bsky is not None:
            return bluesky.to_as1(self.bsky)
        elif self.mf2 is not None:
            return microformats2.json_to_object(self.mf2)

    @ndb.ComputedProperty
    def type(self):  # AS1 objectType, or verb if it's an activity
        if self.as1:
            return as1.object_type(self.as1)

    def _object_ids(self):  # id(s) of inner objects
        if self.as1:
            return as1.get_ids(self.as1, 'object')
    object_ids = ndb.ComputedProperty(_object_ids, repeated=True)

    deleted = ndb.BooleanProperty()

    delivered = ndb.StructuredProperty(Target, repeated=True)
    undelivered = ndb.StructuredProperty(Target, repeated=True)
    failed = ndb.StructuredProperty(Target, repeated=True)

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    def _post_put_hook(self, future):
        """Update :func:`common.get_object` cache."""
        # TODO: assert that as1 id is same as key id? in pre put hook?
        logger.info(f'Wrote Object {self.key.id()} {self.type} {self.status or ""} {self.labels} for {len(self.domains)} users')
        if self.type != 'activity' and '#' not in self.key.id():
            key = common.get_object.cache_key(self.key.id())
            common.get_object.cache[key] = self

    def proxy_url(self):
        """Returns the Bridgy Fed proxy URL to render this post as HTML."""
        return common.host_url('render?' +
                               urllib.parse.urlencode({'id': self.key.id()}))

    def actor_link(self, user=None):
        """Returns a pretty actor link with their name and profile picture.

        Args:
          cur_user: :class:`User`, optional, user for the current request
        """
        if (self.source_protocol in ('webmention', 'ui') and user and
            user.key.id() in self.domains):
            # outbound; show a nice link to the user
            return user.user_page_link()

        actor = (util.get_first(self.as1, 'actor')
                 or util.get_first(self.as1, 'author')
                 or {})
        if isinstance(actor, str):
            return common.pretty_link(actor, user=user)

        url = util.get_first(actor, 'url') or ''
        name = actor.get('displayName') or actor.get('username') or ''
        image = util.get_url(actor, 'image') or ''
        if not image:
            return common.pretty_link(url, text=name, user=user)

        return f"""\
        <a href="{url}" title="{name}">
          <img class="profile" src="{image}" />
          {util.ellipsize(name, chars=40)}
        </a>"""


class Follower(StringIdModel):
    """A follower of a Bridgy Fed user.

    Key name is 'TO FROM', where each part is either a domain or an AP id, eg:
    'snarfed.org https://mastodon.social/@swentel'.

    Both parts are duplicated in the src and dest properties.
    """
    STATUSES = ('active', 'inactive')

    src = ndb.StringProperty()
    dest = ndb.StringProperty()
    # Most recent AP (AS2) JSON Follow activity. If inbound, must have a
    # composite actor object with an inbox, publicInbox, or sharedInbox.
    last_follow = JsonProperty()
    status = ndb.StringProperty(choices=STATUSES, default='active')

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    def _post_put_hook(self, future):
        logger.info(f'Wrote Follower {self.key.id()} {self.status}')

    @classmethod
    def _id(cls, dest, src):
        assert src
        assert dest
        return f'{dest} {src}'

    @classmethod
    def get_or_create(cls, dest, src, **kwargs):
        follower = cls.get_or_insert(cls._id(dest, src), src=src, dest=dest, **kwargs)
        follower.dest = dest
        follower.src = src
        for prop, val in kwargs.items():
            setattr(follower, prop, val)
        follower.put()
        return follower

    def to_as1(self):
        """Returns this follower as an AS1 actor dict, if possible."""
        return as2.to_as1(self.to_as2())

    def to_as2(self):
        """Returns this follower as an AS2 actor dict, if possible."""
        if self.last_follow:
            return self.last_follow.get('actor' if util.is_web(self.src) else 'object')
