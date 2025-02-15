# coding=utf-8
"""Unit tests for models.py."""
from unittest import mock

from flask import get_flashed_messages
from granary import as2
from oauth_dropins.webutil.testutil import requests_response

from app import app
import common
from models import Follower, Object, User
from . import testutil

from .test_activitypub import ACTOR

class UserTest(testutil.TestCase):

    def setUp(self):
        super(UserTest, self).setUp()
        self.user = User.get_or_create('y.z')

        self.full_redir = requests_response(
            status=302,
            redirected_url='http://localhost/.well-known/webfinger?resource=acct:y.z@y.z')

    def test_get_or_create(self):
        assert self.user.mod
        assert self.user.public_exponent
        assert self.user.private_exponent

        same = User.get_or_create('y.z')
        self.assertEqual(same, self.user)

    def test_get_or_create_use_instead(self):
        user = User.get_or_create('a.b')
        user.use_instead = self.user.key
        user.put()

        self.assertEqual('y.z', User.get_or_create('a.b').key.id())

    def test_href(self):
        href = self.user.href()
        self.assertTrue(href.startswith('data:application/magic-public-key,RSA.'), href)
        self.assertIn(self.user.mod, href)
        self.assertIn(self.user.public_exponent, href)

    def test_public_pem(self):
        pem = self.user.public_pem()
        self.assertTrue(pem.decode().startswith('-----BEGIN PUBLIC KEY-----\n'), pem)
        self.assertTrue(pem.decode().endswith('-----END PUBLIC KEY-----'), pem)

    def test_private_pem(self):
        pem = self.user.private_pem()
        self.assertTrue(pem.decode().startswith('-----BEGIN RSA PRIVATE KEY-----\n'), pem)
        self.assertTrue(pem.decode().endswith('-----END RSA PRIVATE KEY-----'), pem)

    def test_address(self):
        self.assertEqual('@y.z@y.z', self.user.address())

        self.user.actor_as2 = {'type': 'Person'}
        self.assertEqual('@y.z@y.z', self.user.address())

        self.user.actor_as2 = {'url': 'http://foo'}
        self.assertEqual('@y.z@y.z', self.user.address())

        self.user.actor_as2 = {'url': ['http://foo', 'acct:bar@foo', 'acct:baz@y.z']}
        self.assertEqual('@baz@y.z', self.user.address())

    def _test_verify(self, redirects, hcard, actor, redirects_error=None):
        with app.test_request_context('/'):
            got = self.user.verify()
            self.assertEqual(self.user.key, got.key)

        with self.subTest(redirects=redirects, hcard=hcard, actor=actor,
                          redirects_error=redirects_error):
            self.assert_equals(redirects, bool(self.user.has_redirects))
            self.assert_equals(hcard, bool(self.user.has_hcard))
            if actor is None:
                self.assertIsNone(self.user.actor_as2)
            else:
                got = {k: v for k, v in self.user.actor_as2.items()
                       if k in actor}
                self.assert_equals(actor, got)
            self.assert_equals(redirects_error, self.user.redirects_error)

    @mock.patch('requests.get')
    def test_verify_neither(self, mock_get):
        empty = requests_response('')
        mock_get.side_effect = [empty, empty]
        self._test_verify(False, False, None)

    @mock.patch('requests.get')
    def test_verify_redirect_strips_query_params(self, mock_get):
        half_redir = requests_response(
            status=302, redirected_url='http://localhost/.well-known/webfinger')
        no_hcard = requests_response('<html><body></body></html>')
        mock_get.side_effect = [half_redir, no_hcard]
        self._test_verify(False, False, None, """\
Current vs expected:<pre>- http://localhost/.well-known/webfinger
+ https://fed.brid.gy/.well-known/webfinger?resource=acct:y.z@y.z</pre>""")

    @mock.patch('requests.get')
    def test_verify_multiple_redirects(self, mock_get):
        two_redirs = requests_response(
            status=302, redirected_url=[
                'https://www.y.z/.well-known/webfinger?resource=acct:y.z@y.z',
                'http://localhost/.well-known/webfinger?resource=acct:y.z@y.z',
            ])
        no_hcard = requests_response('<html><body></body></html>')
        mock_get.side_effect = [two_redirs, no_hcard]
        self._test_verify(True, False, None)

    @mock.patch('requests.get')
    def test_verify_redirect_404(self, mock_get):
        redir_404 = requests_response(status=404, redirected_url='http://this/404s')
        no_hcard = requests_response('<html><body></body></html>')
        mock_get.side_effect = [redir_404, no_hcard]
        self._test_verify(False, False, None, """\
<pre>https://y.z/.well-known/webfinger?resource=acct:y.z@y.z
  redirected to:
http://this/404s
  returned HTTP 404</pre>""")

    @mock.patch('requests.get')
    def test_verify_no_hcard(self, mock_get):
        mock_get.side_effect = [
            self.full_redir,
            requests_response("""
<body>
<div class="h-entry">
  <p class="e-content">foo bar</p>
</div>
</body>
"""),
        ]
        self._test_verify(True, False, None)

    @mock.patch('requests.get')
    def test_verify_non_representative_hcard(self, mock_get):
        bad_hcard = requests_response(
            '<html><body><a class="h-card u-url" href="https://a.b/">acct:me@y.z</a></body></html>',
            url='https://y.z/',
        )
        mock_get.side_effect = [self.full_redir, bad_hcard]
        self._test_verify(True, False, None)

    @mock.patch('requests.get')
    def test_verify_both_work(self, mock_get):
        hcard = requests_response("""
<html><body class="h-card">
  <a class="u-url p-name" href="/">me</a>
  <a class="u-url" href="acct:myself@y.z">Masto</a>
</body></html>""",
            url='https://y.z/',
        )
        mock_get.side_effect = [self.full_redir, hcard]
        self._test_verify(True, True, {
            'type': 'Person',
            'name': 'me',
            'url': ['http://localhost/r/https://y.z/', 'acct:myself@y.z'],
            'preferredUsername': 'y.z',
        })

    @mock.patch('requests.get')
    def test_verify_www_redirect(self, mock_get):
        www_user = User.get_or_create('www.y.z')

        empty = requests_response('')
        mock_get.side_effect = [
            requests_response(status=302, redirected_url='https://www.y.z/'),
            empty, empty,
        ]

        with app.test_request_context('/'):
            got = www_user.verify()
            self.assertEqual('y.z', got.key.id())

        root_user = User.get_by_id('y.z')
        self.assertEqual(root_user.key, www_user.key.get().use_instead)
        self.assertEqual(root_user.key, User.get_or_create('www.y.z').key)

    @mock.patch('requests.get')
    def test_verify_actor_rel_me_links(self, mock_get):
        mock_get.side_effect = [
            self.full_redir,
            requests_response("""
<body>
<div class="h-card">
<a class="u-url" rel="me" href="/about-me">Mrs. ☕ Foo</a>
<a class="u-url" rel="me" href="/">should be ignored</a>
<a class="u-url" rel="me" href="http://one" title="one title">
  one text
</a>
<a class="u-url" rel="me" href="https://two" title=" two title "> </a>
</div>
</body>
""", url='https://y.z/'),
        ]
        self._test_verify(True, True, {
            'attachment': [{
            'type': 'PropertyValue',
            'name': 'Mrs. ☕ Foo',
            'value': '<a rel="me" href="https://y.z/about-me">y.z/about-me</a>',
        }, {
            'type': 'PropertyValue',
            'name': 'Web site',
            'value': '<a rel="me" href="https://y.z/">y.z</a>',
        }, {
            'type': 'PropertyValue',
            'name': 'one text',
            'value': '<a rel="me" href="http://one">one</a>',
        }, {
            'type': 'PropertyValue',
            'name': 'two title',
            'value': '<a rel="me" href="https://two">two</a>',
        }]})

    @mock.patch('requests.get')
    def test_verify_override_preferredUsername(self, mock_get):
        mock_get.side_effect = [
            self.full_redir,
            requests_response("""
<body>
<a class="h-card u-url" rel="me" href="/about-me">
  <span class="p-nickname">Nick</span>
</a>
</body>
""", url='https://y.z/'),
        ]
        self._test_verify(True, True, {
            # stays y.z despite user's username. since Mastodon queries Webfinger
            # for preferredUsername@fed.brid.gy
            # https://github.com/snarfed/bridgy-fed/issues/77#issuecomment-949955109
            'preferredUsername': 'y.z',
        })

    def test_homepage(self):
        self.assertEqual('https://y.z/', self.user.homepage)

    def test_is_homepage(self):
        for url in 'y.z', '//y.z', 'http://y.z', 'https://y.z':
            self.assertTrue(self.user.is_homepage(url), url)

        for url in None, '', 'y', 'z', 'z.z', 'ftp://y.z', 'http://y', '://y.z':
            self.assertFalse(self.user.is_homepage(url), url)


class ObjectTest(testutil.TestCase):

    def test_proxy_url(self):
        with app.test_request_context('/'):
            obj = Object(id='abc', as2={})
            self.assertEqual('http://localhost/render?id=abc', obj.proxy_url())

    def test_actor_link(self):
        for expected, as2 in (
                ('<a href=""></a>', {}),
                ('<a href="http://foo">foo</a>', {'actor': 'http://foo'}),
                ('<a href="">Alice</a>', {'actor': {'name': 'Alice'}}),
                ('<a href="http://foo">Alice</a>', {'actor': {
                    'name': 'Alice',
                    'url': 'http://foo',
                }}),
                ("""\
        <a href="" title="Alice">
          <img class="profile" src="http://pic" />
          Alice
        </a>""", {'actor': {
            'name': 'Alice',
            'icon': {'type': 'Image', 'url': 'http://pic'},
        }}),
        ):
            with app.test_request_context('/'):
                obj = Object(id='x', as2=as2)
                self.assertEqual(expected, obj.actor_link())

    def test_actor_link_user(self):
        user = User(id='foo.com', actor_as2={"name": "Alice"})
        obj = Object(id='x', source_protocol='ui', domains=['foo.com'])
        self.assertEqual(
            '<a href="/user/foo.com"><img src="" class="profile"> Alice</a>',
            obj.actor_link(user))

    def test_put_updates_get_object_cache(self):
        obj = Object(id='x', as2={})
        obj.put()
        key = common.get_object.cache_key('x')
        self.assert_entities_equal(obj, common.get_object.cache[key])

    def test_put_fragment_id_doesnt_update_get_object_cache(self):
        obj = Object(id='x#y', as2={})
        obj.put()

        self.assertNotIn(common.get_object.cache_key('x#y'), common.get_object.cache)
        self.assertNotIn(common.get_object.cache_key('x'), common.get_object.cache)

    def test_computed_properties_without_as1(self):
        Object(id='a').put()


class FollowerTest(testutil.TestCase):

    def setUp(self):
        super().setUp()
        self.inbound = Follower(dest='foo.com', src='http://bar/@baz',
                                last_follow={'actor': ACTOR})
        self.outbound = Follower(dest='http://bar/@baz', src='foo.com',
                                 last_follow={'object': ACTOR})

    def test_to_as1(self):
        self.assertEqual({}, Follower().to_as1())

        as1_actor = as2.to_as1(ACTOR)
        self.assertEqual(as1_actor, self.inbound.to_as1())
        self.assertEqual(as1_actor, self.outbound.to_as1())

    def test_to_as2(self):
        self.assertIsNone(Follower().to_as2())
        self.assertEqual(ACTOR, self.inbound.to_as2())
        self.assertEqual(ACTOR, self.outbound.to_as2())
