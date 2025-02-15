"""Common test utility code.
"""
import copy
import datetime
import unittest
from unittest.mock import ANY, call

from granary import as2
from granary.tests.test_as1 import (
    COMMENT,
    MENTION,
    NOTE,
)
from oauth_dropins.webutil import testutil, util
from oauth_dropins.webutil.appengine_config import ndb_client
from oauth_dropins.webutil.testutil import requests_response
import requests

from app import app, cache
import activitypub, common
from models import Object, Target


class TestCase(unittest.TestCase, testutil.Asserts):
    maxDiff = None

    def setUp(self):
        super().setUp()
        app.testing = True
        cache.clear()
        activitypub.seen_ids.clear()
        common.get_object.cache.clear()

        self.client = app.test_client()
        self.client.__enter__()

        # clear datastore
        requests.post(f'http://{ndb_client.host}/reset')
        self.ndb_context = ndb_client.context()
        self.ndb_context.__enter__()

        util.now = lambda **kwargs: testutil.NOW

    def tearDown(self):
        self.ndb_context.__exit__(None, None, None)
        self.client.__exit__(None, None, None)
        super().tearDown()

    @staticmethod
    def add_objects():
        with app.test_request_context('/'):
            # post
            Object(id='a', domains=['foo.com'], labels=['feed', 'notification'],
                   as2=as2.from_as1(NOTE)).put()
            # different domain
            Object(id='b', domains=['bar.org'], labels=['feed', 'notification'],
                   as2=as2.from_as1(MENTION)).put()
            # reply
            Object(id='d', domains=['foo.com'], labels=['feed', 'notification'],
                   as2=as2.from_as1(COMMENT)).put()
            # not feed/notif
            Object(id='e', domains=['foo.com'],
                   as2=as2.from_as1(NOTE)).put()
            # deleted
            Object(id='f', domains=['foo.com'], labels=['feed', 'notification', 'user'],
                   as2=as2.from_as1(NOTE), deleted=True).put()

    def req(self, url, **kwargs):
        """Returns a mock requests call."""
        kwargs.setdefault('headers', {}).update({
            'User-Agent': util.user_agent,
        })
        kwargs.setdefault('timeout', util.HTTP_TIMEOUT)
        kwargs.setdefault('stream', True)
        return call(url, **kwargs)

    def as2_req(self, url, **kwargs):
        headers = {
            'Date': 'Sun, 02 Jan 2022 03:04:05 GMT',
            'Host': util.domain_from_link(url, minimize=False),
            'Content-Type': 'application/activity+json',
            'Digest': ANY,
            **common.CONNEG_HEADERS_AS2_HTML,
            **kwargs.pop('headers', {}),
        }
        return self.req(url, data=None, auth=ANY, headers=headers,
                        allow_redirects=False, **kwargs)

    def as2_resp(self, obj):
        return requests_response(obj, content_type=as2.CONTENT_TYPE)

    def assert_req(self, mock, url, **kwargs):
        """Checks a mock requests call."""
        kwargs.setdefault('headers', {}).setdefault(
            'User-Agent', 'Bridgy Fed (https://fed.brid.gy/)')
        kwargs.setdefault('stream', True)
        kwargs.setdefault('timeout', util.HTTP_TIMEOUT)
        mock.assert_any_call(url, **kwargs)

    def assert_object(self, id, **props):
        got = Object.get_by_id(id)
        assert got, id

        # right now we only do ActivityPub
        # TODO: revisit as soon as we launch the next protocol, eg bluesky
        for field in 'delivered', 'undelivered', 'failed':
            props[field] = [Target(uri=uri, protocol='activitypub')
                            for uri in props.get(field, [])]

        mf2 = props.get('mf2')
        if mf2 and 'items' in mf2:
            props['mf2'] = mf2['items'][0]

        for computed in 'type', 'object_ids':
            val = props.pop(computed, None)
            if val is not None:
                self.assertEqual(val, getattr(got, computed))

        if expected_as1 := props.pop('as1', None):
            self.assert_equals(common.redirect_unwrap(expected_as1), got.as1)

        self.assert_entities_equal(Object(id=id, **props), got,
                                   ignore=['created', 'updated'])
