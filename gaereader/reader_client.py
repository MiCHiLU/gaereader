# -*- coding: utf-8 -*-

"""
Google Reader client.
"""

# Inspired by (including copying some code snippets):
# http://blog.gpowered.net/2007/08/google-reader-api-functions.html
#
# Key information I used initially:
# http://code.google.com/p/pyrfeed/wiki/GoogleReaderAPI
#
# More docs, not yet fully consumed:
# http://undoc.in/googlereader.html#search-items-ids

from cStringIO import StringIO
import urllib
import urllib2
import re
import json
import time
from datetime import datetime
from lxml import etree, objectify
from google.appengine.api import urlfetch
from google.appengine.ext import ndb

import logging
log = logging.getLogger("reader")

TRIM_LOG_MESSAGES_AT = 100

TOKEN_VALID_TIME = 60
#DUMP_REQUESTS = True
#DUMP_REQUESTS = False
#DUMP_REPLIES = False
#DUMP_REPLIES = True

class GoogleLoginFailed(Exception):
    """
    Exception raised on login failure and other authorization problems.
    """
    pass
class GoogleOperationFailed(Exception):
    """
    Exception raised when Google rejects some operation.
    """
    pass

# User-agent/client-name
SOURCE = 'mekk.reader_client'

GOOGLE_URL = 'http://www.google.com'
READER_URL = GOOGLE_URL + '/reader'
LOGIN_URL = 'https://www.google.com/accounts/ClientLogin'
TOKEN_URL = READER_URL + '/api/0/token'
TAG_LIST_URL = READER_URL + '/api/0/tag/list'
PREFERENCE_LIST_URL = READER_URL + '/api/0/preference/list'
UNREAD_COUNT_URL = READER_URL + '/api/0/unread-count'
SUBSCRIPTION_LIST_URL = READER_URL + '/api/0/subscription/list'
SUBSCRIPTION_EDIT_URL = READER_URL + '/api/0/subscription/edit'
SUBSCRIPTION_QUICKADD_URL = READER_URL + '/api/0/subscription/quickadd'
TAG_EDIT_URL = READER_URL + '/api/0/edit-tag'
TAG_DISABLE_URL = READER_URL + '/api/0/disable-tag'
SEARCH_ITEMS_IDS_URL = READER_URL + '/api/0/search/items/ids'
STREAM_ITEMS_CONTENTS_URL = READER_URL + '/api/0/stream/items/contents'
STREAM_CONTENTS_URL = READER_URL + '/api/0/stream/contents/%s'
STREAM_CONTENTS_FEED_URL = READER_URL + '/api/0/stream/contents/feed/%s'
IN_STATE_URL = READER_URL + '/atom/user/-/state/com.google/%s'
GET_FEED_URL = READER_URL + '/atom/feed/'
READING_TAG_URL = READER_URL + '/atom/%s'

RE_FEED_ID_PREFIX = re.compile(r"^feed/")

class GoogleReaderClient(object):

    """
    Selected GoogleReader functions. Connects to specified Google Account
    and retrieves/modifies GoogleReader subscriptions.

    The get_*_atom functions retrieve different articles as Atom feeds.
    Apart from specific parameters, all those functions handle the following
    args:

    - count         - how many elements to get (by default Google default, i.e. 20)
    - continue_from - value of gr:continuation in "previous" call, to implement paging
    - older_first=True - start from older items, not from newest

    Remaining functions allow one to manage subscription feeds.
    """
    
    @ndb.synctasklet
    def __init__(self, login, password):
        self.session_id = yield self._get_session_id(login, password)
        self.cached_token = None
        self.cached_token_time = 0
        self.my_id = '-'
        self.cached_feed_item_ids = dict()

    ############################################################
    # Small utilities, used mainly internally

    @ndb.tasklet
    def tag_id(self, tag):
        """
        Converts tag name (say "Life: Politics" into 
        tag id (say "user/joe/label/Life: Politics").
        
        If parameter is already in this form, leaves it as-is
        """
        if not tag.startswith('user/'):
            result = yield self.get_my_id()
            tag = 'user/%s/label/%s' % (result, tag)
        raise ndb.Return(tag)

    @ndb.tasklet
    def get_my_id(self):
        """
        Returns true user identifier to be used in API calls, calculating
        it if necessary. Caches the result
        """
        if self.my_id == '-':
            tl = yield self.get_tag_list()
            for vl in tl['tags']:
                m = re.match('user/(\d+)/', vl['id'])
                if m:
                    self.my_id = m.group(1)
                    break
        raise ndb.Return(self.my_id)

    @ndb.tasklet
    def feed_item_id(self, feed):
        """
        Returns identifier of the first item of given tag feed.
        Used during sub/unsubscription (for some reason it is needed)
        """
        feed = RE_FEED_ID_PREFIX.sub("", feed)
        i = self.cached_feed_item_ids.get(feed)
        if not i:
            r = yield self.get_feed_atom(feed, count = 2, format = 'obj')
            i = str(r.entry.id)
            self.cached_feed_item_ids[feed] = i
        raise ndb.Return(i)

    ############################################################
    # Public API - atom feeds (articles)

    @ndb.tasklet
    def get_feed_atom(self, url, **kwargs):
        """
        Atom feed for any feed. Works also for unsubscribed feeds.

        Handled named parameters:

        format: how should the reply be returned. Can be:
            'xml' (raw xml text),
            'etree' (lxml.etree object), or
            'obj' (lxml.objectify object).
          If not specified, 'obj' is default.

        count: how many articles to get (default 20),

        older_first: if specified and set to True, means returning older articles
              first

        continue_from: start from given article instead of the first one
              (handle paging). Parameter given here should be taken from
               <gr:continuation> value from the reply obtained earlier.
        """
        url = urllib.quote_plus(RE_FEED_ID_PREFIX.sub("", url))
        result = yield self._get_atom(GET_FEED_URL + url,
                              **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_reading_list_atom(self, **kwargs):
        """
        Atom feed of unread items

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self.get_instate_atom('reading-list', **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_read_atom(self, **kwargs):
        """
        Atom feed of (recent) read items

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self.get_instate_atom('read', **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_tagged_atom(self, tag, **kwargs):
        """
        Atom feed of (unread?) items for given tag

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self.tag_id(tag)
        tagged_url = READING_TAG_URL % result
        result = yield self._get_atom(tagged_url, **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_starred_atom(self, **kwargs):
        """
        Atom feed of starred items

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self.get_instate_atom('starred', **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_fresh_atom(self, **kwargs):
        """
        Atom feed of fresh (newly added) items

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self.get_instate_atom('fresh', **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_broadcast_atom(self, **kwargs):
        """
        Atom feed of public (shared) items

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self.get_instate_atom('broadcast', **kwargs)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_instate_atom(self, state, **kwargs):
        """
        Atom feed of items in any state. Known states:

        read, kept-unread, fresh, starred, broadcast (public items),
        reading-list (all), tracking-body-link-used, tracking-emailed,
        tracking-item-link-used, tracking-kept-unread

        get_fresh_atom is equivalent to get_instate_atom('fresh') and so on.

        Handles the same named parameters as get_feed_atom
        (format, count, older_first, continue_from).
        """
        result = yield self._get_atom(IN_STATE_URL % state, **kwargs)
        raise ndb.Return(result)

    ############################################################
    # Public API - item

    @ndb.tasklet
    def search_for_articles(self, query, count=1000, tag=None):
        """
        Searches for articles using given text query.
        Returns plain list like:

           [ u'7212740130471148824',
             u'-8654279325215116158',
             u'8121555931508499120',
           ]

        Those are short article(entry) identifiers
        (note that reader also sometimes uses long form
         'tag:google.com,2005:reader/item/5d0cfa30041d4348',
         every reader api which requires article id
         should handle both forms fine)
        """
        output = "json"
        query = {
                "q": query.encode('utf-8'),
                "num": count,
                "output": output,
                "ck": int(time.mktime(datetime.now().timetuple())),
                "client": SOURCE,
                }
        if tag is not None:
          tag_id = yield self.tag_id(tag)
          query["s"] = tag_id.encode('utf-8')
        url = SEARCH_ITEMS_IDS_URL + "?"\
              + urllib.urlencode(query)
        result = yield self._make_call(url)
        reply = json.loads(result)
        raise ndb.Return([ item['id'] for item in reply['results'] ])

    @ndb.tasklet
    def article_contents(self, ids):
        """
        Return article (entry) contents of specified articles. ids is
        a list of identifiers (for example extracted from feed, or
        returned from search_for_articles).

        Returned structure is a complicated recursive dictionary
        of which ['items'] list may be of biggest interest. Dump it for
        details.
        """
        url = STREAM_ITEMS_CONTENTS_URL + "?" \
              + urllib.urlencode({"ck": int(time.mktime(datetime.now().timetuple())),
                                  "client": SOURCE})
        post_params = [("i", id_) for id_ in ids]
        #post_params.extend([("it", "0")] * len(post_params))
        result = yield self._get_token()
        post_params.append(("T", result))
        result = yield self._make_call(url, post_params)
        raise ndb.Return(json.loads(result))

    @ndb.tasklet
    def contents(self, tag, count=20, older_first=False):
        tag_id = yield self.tag_id(tag)
        url = STREAM_CONTENTS_URL % urllib.quote_plus(tag_id.encode("utf-8")) + "?" \
              + urllib.urlencode({
                "ck": int(time.mktime(datetime.now().timetuple())),
                "n": count,
                "r": (older_first and "o" or "d"),
                "client": SOURCE})
        result = yield self._make_call(url)
        raise ndb.Return(json.loads(result))

    @ndb.tasklet
    def feed_contents(self, feed_url, count=20, older_first=False):
        """
        Returns list of articles belonging to given feed.
        """
        url = STREAM_CONTENTS_FEED_URL % urllib.quote_plus(feed_url) + "?" \
              + urllib.urlencode({
                "ck": int(time.mktime(datetime.now().timetuple())),
                "n": count,
                "r": (older_first and "o" or "d"),
                "client": SOURCE})
        result = yield self._make_call(url)
        raise ndb.Return(json.loads(result))


    ############################################################
    # Public API - subscription info

    @ndb.tasklet
    def get_subscription_list(self, format = 'obj'):
        """
        Returns info about all subscribed feeds.

        If format = 'xml' returns bare XML text

        If format = 'json', returns bare JSON text

        If format = 'obj', returns parsed JSON (python dictionary)
        """
        result = yield self._get_list(SUBSCRIPTION_LIST_URL, format)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_tag_list(self, format = 'obj'):
        result = yield self._get_list(TAG_LIST_URL, format)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_preference_list(self, format = 'obj'):
        result = yield self._get_list(PREFERENCE_LIST_URL, format)
        raise ndb.Return(result)

    @ndb.tasklet
    def get_unread_count(self, format = 'obj'):
        result = yield self._get_list(UNREAD_COUNT_URL, format)
        raise ndb.Return(result)

    ############################################################
    # Public API - subscription modifications

    @ndb.tasklet
    def subscribe_quickadd(self, site_url):
        """
        Subscribe to given site url.

        Note: site_url is a normal address of website, Google Reader
        will try to autodetect feed address.

        Method returns a dictionary describing the call results.
        In succesfull case it may look like:

            {
             u'numResults': 1,
             u'query': u'http://sport.pl',
             u'streamId': u'feed/http://rss.gazeta.pl/pub/rss/sport.xml'
            }

        In failed case (feed not found) it may look like:

            {
             u'numResults': 0,
             u'query': u'http://sport.interia.pl',
            }

        """
        url = SUBSCRIPTION_QUICKADD_URL + "?" \
              + urllib.urlencode({"ck": int(time.mktime(datetime.now().timetuple())),
                                  "client": SOURCE})
        result = yield self._get_token()
        post_params = {
            "quickadd": site_url,
            "T": result,
            }
        result = yield self._make_call(url, post_params)
        raise ndb.Return(json.loads(result))

    @ndb.tasklet
    def subscribe_feed(self, feed_url, title = None):
        """
        Subscribe to given feed. Optionally set title.

        Note: feed should specify RSS/Atom url. See subscribe_quickadd for
        alternate method.
        """
        result = yield self._change_feed(feed_url, 'subscribe', title = title)
        raise ndb.Return(result)

    @ndb.tasklet
    def unsubscribe_feed(self, feed_url):
        """
        Unsubscribe from the given feed.
        """
        result = yield self._change_feed(feed_url, 'unsubscribe')
        raise ndb.Return(result)

    @ndb.tasklet
    def change_feed_title(self, feed_url, title):
        """
        Changes the feed title
        """
        result = yield self._change_feed(feed_url, 'edit', title = title)
        raise ndb.Return(result)

    @ndb.tasklet
    def add_feed_tag(self, feed_url, title, tag):
        """
        Adds feed to new tag (folder).
        Tag can be specified either as full id copied from the tag list
        (say "user/04686467480557924617/label/\u017bycie: Polityka")
        or as the sole name ("Życie: Polityka")
        
        It seems that tag may be new (not-yet-existant tags do work)
        """
        result = yield self._change_tag(feed_url, title, add_tag = tag)
        raise ndb.Return(result)

    @ndb.tasklet
    def remove_feed_tag(self, feed_url, title, tag):
        """
        Removes feed from given tag (folder).
        Tag can be specified either as full id copied from the tag list
        (say "user/04686467480557924617/label/\u017bycie: Polityka")
        or as the sole name ("Życie: Polityka")
        """
        result = yield self._change_tag(feed_url, title, remove_tag = tag)
        raise ndb.Return(result)

    @ndb.tasklet
    def disable_tag(self, tag):
        """
        Removes tag as a whole
        """
        url = TAG_DISABLE_URL + '?client=%s' % SOURCE
        result = yield self.tag_id(tag)
        post_data = {
            's' : result,
            'ac' : 'disable-tags',
            }
        reply = yield self._make_call(url, post_data)
        if reply != "OK":
            raise GoogleOperationFailed
        return

    ############################################################
    # Helper functions

    @ndb.tasklet
    def _get_session_id(self, login, password):
        """
        Logging in (and obtaining the session id)
        """
        header = {'User-agent' : SOURCE}
        post_params = {
            'Email': login,
            'Passwd': password,
            'service': 'reader',
            'source': SOURCE,
            'continue': GOOGLE_URL, 
            }
        post_data = urllib.urlencode(post_params) 
        request = urllib2.Request(LOGIN_URL, post_data, header)

        if log.isEnabledFor("info"):
            pdcopy = post_params.copy()
            pdcopy['Passwd'] = '*******'
            log.info("Calling %s with parameters:\n    %s" % (
                        request.get_full_url(), str(pdcopy)))

        result = yield ndb.get_context().urlfetch(LOGIN_URL, payload=request.data, method=urlfetch.POST, headers=request.headers)
        if result.status_code == 403:
            raise GoogleLoginFailed("%s (%s)" % (result, result.content))
        elif result.status_code != 200:
            raise urllib2.HTTPError(result.final_url, result.status_code, None, result.headers, StringIO(result.content))

        log.debug("Result: %s" % result.content[:TRIM_LOG_MESSAGES_AT])

        sid = re.search('Auth=(\S*)', result.content).group(1)
        if not sid:
            raise GoogleLoginFailed
        raise ndb.Return(sid)

    @ndb.tasklet
    def _get_token(self):
        """
        Obtain the call protection token
        """
        # Token jest jakiś czas ważny...
        t = time.time()
        if t - self.cached_token_time > TOKEN_VALID_TIME:
            self.cached_token = yield self._make_call(TOKEN_URL)
            self.cached_token_time = t
        raise ndb.Return(self.cached_token)

    @ndb.tasklet
    def _get_atom(self, url, count = None, 
                  older_first = False, continue_from = None, format = 'obj'):
        """
        Actually get ATOM feed. url is base url (one of the state or label urls).
        count is the articles count (default 20), older_first set to True means older
        first, continue_from can be set to gr:continuation value from the feed to
        grab more items

        format can be 'xml' (raw xml text), 'etree' (lxml.etree) or 'obj'
        (lxml.objectify - default)
        """
        args = {}
        if count is not None:
            args['n'] = "%d" % count
        if older_first:
            args['r'] = 'o'
        if continue_from:
            args['c'] = continue_from
        if args:
            url = url.encode('utf-8') + '?' + urllib.urlencode(args)
        r = yield self._make_call(url)
        try:
            if format == "obj":
                raise ndb.Return(objectify.fromstring(r))
            elif format == "etree":
                raise ndb.Return(etree.XML(r))
            else:
                raise ndb.Return(r)
        except ndb.Return:
            raise
        except Exception, e:
            logging.error(r)
            raise GoogleOperationFailed(e)

    @ndb.tasklet
    def _change_feed(self, feed_url, operation,
                     title = None, add_tag = None, remove_tag = None):
        """
        Subscribe or unsubscribe
        """
        prefix = "feed/"
        if not feed_url.startswith(prefix):
          feed_url = prefix + feed_url
        url = SUBSCRIPTION_EDIT_URL + '?client=%s' % SOURCE
        result = yield self._get_token()
        post_data = { 
            'ac' : operation,
            's' : feed_url,
            'T' : result,
            }
        if title:
            post_data['t'] = title
        if add_tag:
            post_data['a'] = yield self.tag_id(add_tag)
        if remove_tag:
            post_data['r'] = yield self.tag_id(remove_tag)
        reply = yield self._make_call(url, post_data)
        if reply != "OK":
            raise GoogleOperationFailed
        return

    @ndb.tasklet
    def _change_tag(self, feed_url, title, add_tag = None, remove_tag = None):
        """
        Subscribe or unsubscribe
        """
        prefix = "feed/"
        if not feed_url.startswith(prefix):
          feed_url = prefix + feed_url
        #url = TAG_EDIT_URL + '?client=%s' % SOURCE
        url = SUBSCRIPTION_EDIT_URL + '?client=%s' % SOURCE
        result = yield self._get_token()
        post_data = { 
            'ac' : 'edit',
            's' : feed_url,
            't' : title,
            'T' : result,
            }
        if add_tag:
            post_data['a'] = yield self.tag_id(add_tag)
        if remove_tag:
            post_data['r'] = yield self.tag_id(remove_tag)
        reply = yield self._make_call(url, post_data)
        if reply != "OK":
            raise GoogleOperationFailed

        # # It is likely refresh, don't work so ...
#         url = TAG_EDIT_URL + '?client=%s' % SOURCE
#         post_data = {
#             'a' : 'user/%s/state/com.google/read' % self.get_my_id(),
#             'async' : 'true',
#             'i' : self.feed_item_id(feed_url),
#             's' : "feed/" + feed_url,
#             }
#         reply = self._make_call(url, post_data)
#         if reply != "OK":
#             raise GoogleOperationFailed

        return

    @ndb.tasklet
    def _get_list(self, url, format):
        if format == 'obj':
            result = yield self._make_call(url + '?output=json')
            raise ndb.Return(json.loads(result))
        else:
            result = yield self._make_call(url + '?output=' + format)
            raise ndb.Return(result)
        

    @ndb.tasklet
    def _make_call(self, url, post_data=None):
        """
        Actually executes a call to given url, adding authorization headers
        and parameters.
        
        post_data can be either a dictionary, or list of (key, value)
        pairs. In both cases value should be unicode (and will be encoded
        to utf-8 inside this method).
        """
        header = {'User-agent' : SOURCE}
        header['Authorization'] = 'GoogleLogin auth=%s' % self.session_id
        if post_data is not None:
            if type(post_data) is list:
                true_data = [
                    (key, value.encode('utf-8')) 
                    for key, value in post_data ]
            else:
                true_data = [
                    (key, value.encode('utf-8')) 
                    for key, value in post_data.iteritems() ]
            true_data = urllib.urlencode(true_data)
            method = urlfetch.POST
        else:
            true_data = None
            method = urlfetch.GET
        request = urllib2.Request(url.encode('utf-8'), true_data, header)

        if log.isEnabledFor("info"):
            if post_data:
                log.info("Calling %s with parameters:\n    %s" % (
                        request.get_full_url(), str(post_data)[:TRIM_LOG_MESSAGES_AT]))
            else:
                log.info("Calling %s" % request.get_full_url())

        result = yield ndb.get_context().urlfetch(url.encode('utf-8'), payload=request.data, method=method, headers=request.headers)

        log.debug("Result: %s" % result.content[:TRIM_LOG_MESSAGES_AT])

        raise ndb.Return(result.content)

