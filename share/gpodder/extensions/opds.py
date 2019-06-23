# -*- coding: utf-8 -*-
#
# gPodder - A media aggregator and podcast client
# Copyright (c) 2019 The gPodder Team
#
# gPodder is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# gPodder is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import copy
import io
import logging
import os
import re
from urllib import parse as urlparse
from xml import sax

import urllib3
from urllib3.util.request import make_headers

from podcastparser import file_basename_no_extension, is_html, parse_length, parse_pubdate, parse_type, parse_url

from gpodder import feedcore, model, user_agent
from gpodder.model import Feed, PodcastEpisode
from gpodder.util import parse_mimetype, remove_html_tags

logger = logging.getLogger(__name__)

__title__ = 'OPDS Feeds'
__description__ = 'Subscribe to Calibre or other ebook distribution channels'
__only_for__ = 'gtk, cli'
__authors__ = 'Eric Le Lay <elelay.fr:contact>'

"""
Tested with:
calibre-web
Calibre server
https://catalog.feedbooks.com/store/browse/fr/top.atom
https://gallica.bnf.fr/services/engine/search/opds?operation=searchRetrieve&version=1.2&exactSearch=false&query=dewey%20all%20%225%22%20and%20dc.format%20all%20%22epub%22&filter=provenance%20all%20%22bnf.fr%22

"""

ATOM = 'http://www.w3.org/2005/Atom'
OPDS = 'http://opds-spec.org/2010/catalog'

ACQUISITION = 'http://opds-spec.org/acquisition'
BUY = 'http://opds-spec.org/acquisition/buy'
IMAGE = 'http://opds-spec.org/image'
COVER_0_9 = 'http://opds-spec.org/cover'
THUMBNAIL = 'http://opds-spec.org/image/thumbnail'
THUMBNAIL_0_9 = 'http://opds-spec.org/thumbnail'

ELTS_TXT = set([
    (ATOM, 'content'),
    (ATOM, 'icon'),
    (ATOM, 'id'),
    (ATOM, 'name'),
    (ATOM, 'summary'),
    (ATOM, 'title'),
    (ATOM, 'updated'),
    (OPDS, 'price'),
])

PREFERRED_FORMATS = [
    'application/epub+zip',
    'application/pdf',
    'application/x-mobipocket-ebook',
    'application/xhtml+xml',
    'application/fb2+zip',
    'application/x-cbr',
    'application/x-cbz',
    'application/x-cbt',
    'application/vnd.amazon.ebook',
    'image/vnd.djvu'
]

FORMAT_NAMES = {
    'application/epub+zip': 'epub',
    'application/pdf': 'pdf',
    'application/x-mobipocket-ebook': 'mobi',
    'application/xhtml+xml': 'xhtml',
    'application/fb2+zip': 'fb2',
    'application/x-cbr': 'cbr',
    'application/x-cbz': 'cbz',
    'application/x-cbt': 'cbt',
    'application/vnd.amazon.ebook': 'azw',
    'image/vnd.djvu': 'djvu',
}


class FeedParseError(sax.SAXParseException, ValueError):
    """
    Exception raised when asked to parse an invalid feed

    This exception allows users of this library to catch exceptions
    without having to import the XML parsing library themselves.
    """


class NotOPDSError(sax.SAXParseException, ValueError):
    """
    Exception raised when asked to parse an invalid feed

    This exception allows users of this library to catch exceptions
    without having to import the XML parsing library themselves.
    """


class OPDSHandler(sax.handler.ContentHandler):
    """ ContentHandler building the podcast and episodes contents """
    def __init__(self, url):
        self.url = url
        self.base = url
        self.text = None
        self.episodes = []
        self.data = {
            'title': file_basename_no_extension(url),
            'episodes': self.episodes,
            '_is_acquisition_feed': False,
            'url': url
        }
        self.path_stack = []
        self.ns_mapping = {}
        self.xhtml = None
        self.xhtml_output = None
        self.xhtml_stack_len = None

    def set_base(self, base):
        self.base = base

    def set_podcast_attr(self, key, value):
        self.data[key] = value

    def set_episode_attr(self, key, value):
        self.episodes[-1][key] = value

    def get_episode_attr(self, key, default=None):
        return self.episodes[-1].get(key, default)

    def add_episode(self):
        self.episodes.append({
            # title
            'description': '',
            # url
            'published': 0,
            # guid
            'link': '',
            'total_time': 0,
            'payment_url': None,
            'enclosures': [],
            '_guid_is_permalink': False,
        })

    def make_nice_description(self, entry):
        descr = """<style type="text/css">
        body > img { float: left; max-width: 20vw; }
        body > p, body > div.content, body > ul { margin-left: 25vw; }
        body > ul { list-style: none; margin-top: 1em; padding-left: 0;}
        </style>
        """
        img = entry.get('_image', entry.get('_thumbnail'))
        if img:
            descr += '<img src="{}">'.format(img)
        if '&' in entry.get('author', ''):
            descr += "<p>by {}</p>".format(entry['author'])
        descr += "<p>{}</p>".format(entry.get('summary', ''))
        if entry.get('content'):
            descr += '<div class="content">{}</div>'.format(entry['content'])
        if entry['enclosures'] or 'buy' in entry:
            descr += '<ul>'
            for e in entry['enclosures']:
                format = FORMAT_NAMES.get(e['mime_type'], e['mime_type'])
                descr += '<li><a href="{}">Download in {}</a></li>'.format(e['url'], format)
            if 'buy' in entry:
                if 'price' in entry:
                    text = "Buy item for {}".format(entry['price'])
                else:
                    text = "Buy item"
                descr += '<li><a href="{}">{}</a></li>'.format(entry['buy'], text)
            descr += '</ul>'

        entry['description_html'] = descr
        entry['description'] = remove_html_tags(entry.get('summary', entry.get('content', '')))[:120]

    def validate_episode(self):
        entry = self.episodes[-1]

        self.make_nice_description(entry)

        if 'guid' not in entry:
            if entry.get('link'):
                # Link element can serve as GUID
                entry['guid'] = entry['link']
            else:
                if len(set(enclosure['url'] for enclosure in entry['enclosures'])) != 1:
                    # Multi-enclosure feeds MUST have a GUID or the same URL for all enclosures
                    self.episodes.pop()
                    return

                # Maemo bug 12073
                entry['guid'] = entry['enclosures'][0]['url']

        if 'title' not in entry:
            self.episodes.pop()
            return

        if not entry.get('link') and entry.get('_guid_is_permalink'):
            entry['link'] = entry['guid']

        # add author to title
        if entry.get('author'):
            if ' & ' in entry['author']:
                author = "{} et al.".format(entry['author'].split('&')[0])
            else:
                author = entry['author']
            entry['title'] = '{} - {}'.format(author, entry['title'])

        # set episode's attachment
        enclosure = None
        for t in PREFERRED_FORMATS:
            if not enclosure:
                for e in entry['enclosures']:
                    if e['mime_type'] == t:
                        enclosure = e
        if not enclosure and entry['enclosures']:
            enclosure = entry['enclosures'][0]
        if enclosure:
            entry.update(enclosure)

        # cleanup custom attributes
        for k in list(entry.keys()):
            if k not in PodcastEpisode.__slots__:
                del entry[k]

    def add_enclosure(self, url, file_size, mime_type):
        self.episodes[-1]['enclosures'].append({
            'url': url,
            'file_size': file_size,
            'mime_type': mime_type,
        })

    def validate_podcast(self):
        if not self.data['_is_acquisition_feed']:
            logger.debug('no link rel=self with opds type')
            raise NotOPDSError(
                msg='Unsupported feed type',
                exception=None,
                locator=self._locator)
        logger.debug('feed %s if an OPDS feed!', self.url)
        del self.data['_is_acquisition_feed']
        # not sorting episodes by published
        # logger.debug("Feed contents: %s", self.data)

    def in_episode(self):
        return len(self.path_stack) == 3 \
            and self.path_stack[:-1] == [(ATOM, 'feed'), (ATOM, 'entry')]

    def in_podcast(self):
        return len(self.path_stack) == 2

    def startElementNS(self, name, qname, attrs):
        """ ContentHandler method """
        if not self.path_stack and name != (ATOM, 'feed'):
            raise NotOPDSError(
                msg='Unsupported feed type: {}:{}'.format(*name),
                exception=None,
                locator=self._locator,
            )
        self.path_stack.append(name)
        if self.xhtml:
            return self.xhtml.startElementNS(name, qname, attrs)
        if name in ELTS_TXT:
            self.text = []
        else:
            self.text = None
        if name == (ATOM, 'entry'):
            self.add_episode()
        elif name == (ATOM, 'link'):
            url = self.get_attr(attrs, 'href')
            rel = self.get_attr(attrs, 'rel')
            if url:
                url = parse_url(urlparse.urljoin(self.base, url.lstrip()))
            if self.in_podcast():
                if rel in ('self', 'start', 'up'):
                    type_, sub, params = parse_mimetype(self.get_attr(attrs, 'type'))
                    if type_ == 'application' and sub == 'atom+xml' and params.get('profile') == 'opds-catalog':
                        self.set_podcast_attr('_is_acquisition_feed', True)
                # RFC 5005 (http://podlove.org/paged-feeds/)
                elif rel == 'first':
                    self.set_podcast_attr('paged_feed_first', url)
                elif rel == 'next':
                    # RFC 5005 (http://podlove.org/paged-feeds/)
                    self.set_podcast_attr('paged_feed_next', url)
            elif self.in_episode():
                if rel == ACQUISITION:
                    file_size = parse_length(self.get_attr(attrs, 'length'))
                    mime_type = parse_type(self.get_attr(attrs, 'type'))
                    self.add_enclosure(url, file_size, mime_type)
                if rel == BUY:
                    self.set_episode_attr('buy', url)
                elif rel in (IMAGE, COVER_0_9):
                    self.set_episode_attr('_image', url)
                elif rel in (THUMBNAIL, THUMBNAIL_0_9):
                    self.set_episode_attr('_thumbnail', url)
        elif name == (ATOM, 'content'):
            if self.get_attr(attrs, 'type') == 'xhtml':
                self.install_xhtml()
        elif name == (OPDS, 'price') and self.path_stack[-2] == (ATOM, 'link'):
            if self.get_attr(attrs, 'currencycode'):
                self.set_episode_attr('price_currency', self.get_attr(attrs, 'currencycode'))

    def characters(self, chars):
        """ ContentHandler method """
        if self.xhtml:
            return self.xhtml.characters(chars)
        if self.text is not None:
            self.text.append(chars)

    def ignorableWhitespace(self, content):
        """ ContentHandler method """
        if self.xhtml:
            return self.xhtml.ignorableWhitespace(content)

    def startPrefixMapping(self, prefix, uri):
        """ ContentHandler method """
        if self.xhtml:
            return self.xhtml.startPrefixMapping(prefix, uri)
        self.ns_mapping[prefix] = uri

    def endPrefixMapping(self, prefix):
        """ ContentHandler method """
        if self.xhtml:
            return self.xhtml.endPrefixMapping(prefix)
        del self.ns_mapping[prefix]

    def endElementNS(self, name, qname):
        """ ContentHandler method """
        def by_published(entry):
            return entry.get('published')
        if self.xhtml:
            if len(self.path_stack) == self.xhtml_stack_len:
                self.text = [self.xhtml_output.getvalue().decode('utf-8')]
                self.xhtml = None
                self.xhtml_output = None
            else:
                self.path_stack.pop()
                return self.xhtml.endElementNS(name, qname)

        content = ''.join(self.text) if self.text is not None else ''
        self.text = None
        if name == (ATOM, 'feed'):
            self.validate_podcast()
        elif name == (ATOM, 'entry'):
            self.validate_episode()
        elif name == (ATOM, 'updated'):
            if self.in_episode():
                self.set_episode_attr('published', parse_pubdate(content))
        elif name == (ATOM, 'id'):
            if self.in_podcast():
                self.set_podcast_attr('guid', content)
            elif self.in_episode():
                self.set_episode_attr('guid', content)
        elif name == (ATOM, 'title'):
            if self.in_podcast() and content:
                self.set_podcast_attr('title', content)
            elif self.in_episode():
                self.set_episode_attr('title', content)
        elif name == (ATOM, 'name') and self.path_stack[-2] == (ATOM, 'author'):
            if self.path_stack[-3] == (ATOM, 'entry'):
                self.set_episode_attr('author', content)
        elif name == (ATOM, 'summary'):
            if self.in_episode():
                self.set_episode_attr('summary', content)
        elif name == (ATOM, 'content'):
            if self.in_episode():
                self.set_episode_attr('content', content)
        elif name == (ATOM, 'icon') and self.in_podcast() and content:
            self.set_podcast_attr('image', urlparse.urljoin(self.url, content))
        elif name == (OPDS, 'price'):
            if self.get_episode_attr('price_currency'):
                self.set_episode_attr('price', '{} {}'.format(content, self.get_episode_attr('price_currency')))
                self.set_episode_attr('price_currency', None)
            else:
                self.set_episode_attr('price', content)
        self.path_stack.pop()

    def install_xhtml(self):
        """ ContentHandler method """
        self.xhtml_output = io.BytesIO()
        self.xhtml = sax.saxutils.XMLGenerator(out=self.xhtml_output, encoding='utf-8')
        self.xhtml_stack_len = len(self.path_stack)
        for prefix, ns in self.ns_mapping.items():
            self.xhtml.startPrefixMapping(prefix, ns)

    @staticmethod
    def get_attr(attrs, qname):
        """ utility method to get attribute value or None if absent """
        if qname in attrs.getQNames():
            return attrs.getValueByQName(qname)
        else:
            return None


class OPDSCustomChannel(Feed):
    """
    custom channel implementation for PodcastChannel._consume_custom_feed()
    """
    def __init__(self, fetcher, data, headers, max_episodes):
        self.fetcher = fetcher
        self.data = data
        self.episodes = data['episodes']
        del data['episodes']
        if max_episodes > 0 and max_episodes < len(self.episodes):
            logger.debug("truncating episodes (%i out of %i)", max_episodes, len(self.episodes))
            self.episodes = self.episodes[:max_episodes]
        self.headers = headers
        self.max_episodes = max_episodes

    def get_title(self):
        return self.data['title']

    def get_cover_url(self):
        return self.data.get('image')

    def get_link(self):
        return self.data['url']

    def get_description(self):
        return 'OPDS Feed for {}'.format(self.data['url'])

    def get_http_etag(self):
        return self.headers.get('etag')

    def get_http_last_modified(self):
        return self.headers.get('last-modified')

    def get_new_episodes(self, channel, existing_guids):
        logger.debug("get_new_episodes(%i)", len(self.episodes))
        seen_guids = set(e['guid'] for e in self.episodes)
        episodes = []

        for e in self.episodes:
            episode = channel.episode_factory(e)
            existing_episode = existing_guids.get(episode.guid)
            if existing_episode:
                existing_episode.update_from(episode)
                existing_episode.save()
            else:
                episode.save()
                episodes.append(episode)

        return episodes, seen_guids

    def get_next_page(self, channel, max_episodes):
        if 'paged_feed_next' in self.data:
            url = self.data['paged_feed_next']
            logger.debug("get_next_page: opds feed has next %s", url)
            return self.fetcher.fetch(channel, url, max_episodes, True)
        return None


class OPDSFetcher:
    def __init__(self):
        self.http = urllib3.PoolManager()

    def fetch_channel(self, channel, max_episodes=0):
        if channel.get_ext_data('opds').v1.not_opds is True:
            logger.debug("channel is marked as not opds, returning None")
            return None
        url = channel.url
        return self.fetch(channel, url, max_episodes)

    def fetch(self, channel, url, max_episodes, is_more=False):
        if channel.auth_username:
            auth = '{}:{}'.format(channel.auth_username, channel.auth_password)
        else:
            auth = None
        headers = make_headers(basic_auth=auth, user_agent=user_agent)
        if not is_more:
            if channel.http_last_modified is not None:
                headers['If-Modified-Since'] = channel.http_last_modified
            if channel.http_etag is not None:
                headers['If-None-Match'] = channel.http_etag
        try:
            r = self.http.request('GET', url, headers=headers)
            if r.status == 200:
                return self.parse(channel, r, max_episodes)
            elif r.status == 304:
                logger.debug('%s channel not modified', channel.url)
                return feedcore.Result(feedcore.NOT_MODIFIED, None)
            logger.debug('fetched %s: %s', channel.url, r.status)
        except urllib3.exceptions.HTTPError:
            logger.exception("Exception fetching url %s", url)
            return None

    def parse(self, channel, response, max_episodes):
        handler = OPDSHandler(response.geturl())
        try:
            parser = sax.make_parser()
            parser.setFeature(sax.handler.feature_namespaces, True)
            parser.setContentHandler(handler)
            source = sax.saxutils.prepare_input_source(io.BytesIO(response.data), response.geturl())
            parser.parse(source)
            res = OPDSCustomChannel(self, handler.data, response.headers, max_episodes)
            # if (max_episodes <= 0 or len(handler.episodes) < max_episodes) and 'paged_feed_next' in handler.data:
            #     max_episodes -= len(handler.episodes)
            #     url = handler.data['paged_feed_next']
            #     res.add_episodes(self.fetch(channel, url, max_episodes, is_more=True))
            return feedcore.Result(feedcore.UPDATED_FEED, res)
        except NotOPDSError:
            logger.debug("%s is not an OPDS feed", handler.url)
            channel.get_ext_data('opds').not_opds = True
            return None
        except sax.SAXParseException:
            logger.exception("error parsing %s", handler.url)
            return None


class gPodderExtension:
    def __init__(self, container):
        self.container = container
        self.fetcher = OPDSFetcher()

    def on_load(self):
        logger.info('Registering OPDS.')
        model.register_custom_handler(self.fetcher)

    def on_unload(self):
        logger.info('Unregistering OPDS.')
        model.unregister_custom_handler(self.fetcher)