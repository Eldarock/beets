# -*- coding: utf-8 -*-
# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Glue between metadata sources and the matching logic."""
from __future__ import division, absolute_import, print_function

from collections import namedtuple
from functools import total_ordering
import re

from beets import logging
from beets import plugins
from beets import config
from beets.util import as_string
from beets.autotag import mb
from jellyfish import levenshtein_distance
from unidecode import unidecode
import six

log = logging.getLogger('beets')

# The name of the type for patterns in re changed in Python 3.7.
try:
    Pattern = re._pattern_type
except AttributeError:
    Pattern = re.Pattern


# Classes used to represent candidate options.
class AttrDict(dict):
    """A dictionary that supports attribute ("dot") access, so `d.field`
    is equivalent to `d['field']`.
    """

    def __getattr__(self, attr):
        if attr in self:
            return self.get(attr)
        else:
            raise AttributeError

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __hash__(self):
        return id(self)


class AlbumInfo(AttrDict):
    """Describes a canonical release that may be used to match a release
    in the library. Consists of these data members:

    - ``album``: the release title
    - ``album_id``: MusicBrainz ID; UUID fragment only
    - ``artist``: name of the release's primary artist
    - ``artist_id``
    - ``tracks``: list of TrackInfo objects making up the release

    ``mediums`` along with the fields up through ``tracks`` are required.
    The others are optional and may be None.
    """
    def __init__(self, tracks, album=None, album_id=None, artist=None,
                 artist_id=None, asin=None, albumtype=None, va=False,
                 year=None, month=None, day=None, label=None, mediums=None,
                 artist_sort=None, releasegroup_id=None, catalognum=None,
                 script=None, language=None, country=None, style=None,
                 genre=None, albumstatus=None, media=None, albumdisambig=None,
                 releasegroupdisambig=None, artist_credit=None,
                 original_year=None, original_month=None,
                 original_day=None, data_source=None, data_url=None,
                 discogs_albumid=None, discogs_labelid=None,
                 discogs_artistid=None, game=None, **kwargs):
        self.album = album
        self.album_id = album_id
        self.artist = artist
        self.artist_id = artist_id
        self.tracks = tracks
        self.asin = asin
        self.albumtype = albumtype
        self.va = va
        self.year = year
        self.month = month
        self.day = day
        self.label = label
        self.mediums = mediums
        self.artist_sort = artist_sort
        self.releasegroup_id = releasegroup_id
        self.catalognum = catalognum
        self.script = script
        self.language = language
        self.country = country
        self.style = style
        self.genre = genre
        self.albumstatus = albumstatus
        self.media = media
        self.albumdisambig = albumdisambig
        self.releasegroupdisambig = releasegroupdisambig
        self.artist_credit = artist_credit
        self.original_year = original_year
        self.original_month = original_month
        self.original_day = original_day
        self.data_source = data_source
        self.data_url = data_url
        self.discogs_albumid = discogs_albumid
        self.discogs_labelid = discogs_labelid
        self.discogs_artistid = discogs_artistid
        self.game = game
        self.update(kwargs)

    # Work around a bug in python-musicbrainz-ngs that causes some
    # strings to be bytes rather than Unicode.
    # https://github.com/alastair/python-musicbrainz-ngs/issues/85
    def decode(self, codec='utf-8'):
        """Ensure that all string attributes on this object, and the
        constituent `TrackInfo` objects, are decoded to Unicode.
        """
        for fld in ['album', 'artist', 'albumtype', 'label', 'artist_sort',
                    'catalognum', 'script', 'language', 'country', 'style',
                    'genre', 'albumstatus', 'albumdisambig',
                    'releasegroupdisambig', 'artist_credit',
                    'media', 'discogs_albumid', 'discogs_labelid',
                    'discogs_artistid']:
            value = getattr(self, fld)
            if isinstance(value, bytes):
                setattr(self, fld, value.decode(codec, 'ignore'))

        for track in self.tracks:
            track.decode(codec)

    def copy(self):
        dupe = AlbumInfo([])
        dupe.update(self)
        dupe.tracks = [track.copy() for track in self.tracks]
        return dupe


class TrackInfo(AttrDict):
    """Describes a canonical track present on a release. Appears as part
    of an AlbumInfo's ``tracks`` list. Consists of these data members:

    - ``title``: name of the track
    - ``track_id``: MusicBrainz ID; UUID fragment only

    Only ``title`` and ``track_id`` are required. The rest of the fields
    may be None. The indices ``index``, ``medium``, and ``medium_index``
    are all 1-based.
    """
    def __init__(self, title=None, track_id=None, release_track_id=None,
                 artist=None, artist_id=None, length=None, index=None,
                 medium=None, medium_index=None, medium_total=None,
                 artist_sort=None, disctitle=None, artist_credit=None,
                 data_source=None, data_url=None, media=None, lyricist=None,
                 composer=None, composer_sort=None, arranger=None,
                 track_alt=None, work=None, mb_workid=None,
                 work_disambig=None, bpm=None, initial_key=None, genre=None,
                 **kwargs):
        self.title = title
        self.track_id = track_id
        self.release_track_id = release_track_id
        self.artist = artist
        self.artist_id = artist_id
        self.length = length
        self.index = index
        self.media = media
        self.medium = medium
        self.medium_index = medium_index
        self.medium_total = medium_total
        self.artist_sort = artist_sort
        self.disctitle = disctitle
        self.artist_credit = artist_credit
        self.data_source = data_source
        self.data_url = data_url
        self.lyricist = lyricist
        self.composer = composer
        self.composer_sort = composer_sort
        self.arranger = arranger
        self.track_alt = track_alt
        self.work = work
        self.mb_workid = mb_workid
        self.work_disambig = work_disambig
        self.bpm = bpm
        self.initial_key = initial_key
        self.genre = genre
        self.update(kwargs)

    # As above, work around a bug in python-musicbrainz-ngs.
    def decode(self, codec='utf-8'):
        """Ensure that all string attributes on this object are decoded
        to Unicode.
        """
        for fld in ['title', 'artist', 'medium', 'artist_sort', 'disctitle',
                    'artist_credit', 'media']:
            value = getattr(self, fld)
            if isinstance(value, bytes):
                setattr(self, fld, value.decode(codec, 'ignore'))

    def copy(self):
        dupe = TrackInfo()
        dupe.update(self)
        return dupe


# Candidate distance scoring.

# Parameters for string distance function.
# Words that can be moved to the end of a string using a comma.
SD_END_WORDS = ['the', 'a', 'an']
# Reduced weights for certain portions of the string.
SD_PATTERNS = [
    (r'^the ', 0.1),
    (r'[\[\(]?(ep|single)[\]\)]?', 0.0),
    (r'[\[\(]?(featuring|feat|ft)[\. :].+', 0.1),
    (r'\(.*?\)', 0.3),
    (r'\[.*?\]', 0.3),
    (r'(, )?(pt\.|part) .+', 0.2),
]
# Replacements to use before testing distance.
SD_REPLACE = [
    (r'&', 'and'),
]


def _string_dist_basic(str1, str2):
    """Basic edit distance between two strings, ignoring
    non-alphanumeric characters and case. Comparisons are based on a
    transliteration/lowering to ASCII characters. Normalized by string
    length.
    """
    assert isinstance(str1, six.text_type)
    assert isinstance(str2, six.text_type)
    str1 = as_string(unidecode(str1))
    str2 = as_string(unidecode(str2))
    str1 = re.sub(r'[^a-z0-9]', '', str1.lower())
    str2 = re.sub(r'[^a-z0-9]', '', str2.lower())
    if not str1 and not str2:
        return 0.0
    return levenshtein_distance(str1, str2) / float(max(len(str1), len(str2)))


def string_dist(str1, str2):
    """Gives an "intuitive" edit distance between two strings. This is
    an edit distance, normalized by the string length, with a number of
    tweaks that reflect intuition about text.
    """
    if str1 is None and str2 is None:
        return 0.0
    if str1 is None or str2 is None:
        return 1.0

    str1 = str1.lower()
    str2 = str2.lower()

    # Don't penalize strings that move certain words to the end. For
    # example, "the something" should be considered equal to
    # "something, the".
    for word in SD_END_WORDS:
        if str1.endswith(', %s' % word):
            str1 = '%s %s' % (word, str1[:-len(word) - 2])
        if str2.endswith(', %s' % word):
            str2 = '%s %s' % (word, str2[:-len(word) - 2])

    # Perform a couple of basic normalizing substitutions.
    for pat, repl in SD_REPLACE:
        str1 = re.sub(pat, repl, str1)
        str2 = re.sub(pat, repl, str2)

    # Change the weight for certain string portions matched by a set
    # of regular expressions. We gradually change the strings and build
    # up penalties associated with parts of the string that were
    # deleted.
    base_dist = _string_dist_basic(str1, str2)
    penalty = 0.0
    for pat, weight in SD_PATTERNS:
        # Get strings that drop the pattern.
        case_str1 = re.sub(pat, '', str1)
        case_str2 = re.sub(pat, '', str2)

        if case_str1 != str1 or case_str2 != str2:
            # If the pattern was present (i.e., it is deleted in the
            # the current case), recalculate the distances for the
            # modified strings.
            case_dist = _string_dist_basic(case_str1, case_str2)
            case_delta = max(0.0, base_dist - case_dist)
            if case_delta == 0.0:
                continue

            # Shift our baseline strings down (to avoid rematching the
            # same part of the string) and add a scaled distance
            # amount to the penalties.
            str1 = case_str1
            str2 = case_str2
            base_dist = case_dist
            penalty += weight * case_delta

    return base_dist + penalty


class LazyClassProperty(object):
    """A decorator implementing a read-only property that is *lazy* in
    the sense that the getter is only invoked once. Subsequent accesses
    through *any* instance use the cached result.
    """
    def __init__(self, getter):
        self.getter = getter
        self.computed = False

    def __get__(self, obj, owner):
        if not self.computed:
            self.value = self.getter(owner)
            self.computed = True
        return self.value


@total_ordering
@six.python_2_unicode_compatible
class Distance(object):
    """Keeps track of multiple distance penalties. Provides a single
    weighted distance for all penalties as well as a weighted distance
    for each individual penalty.
    """
    def __init__(self):
        self._penalties = {}

    @LazyClassProperty
    def _weights(cls):  # noqa: N805
        """A dictionary from keys to floating-point weights.
        """
        weights_view = config['match']['distance_weights']
        weights = {}
        for key in weights_view.keys():
            weights[key] = weights_view[key].as_number()
        return weights

    # Access the components and their aggregates.

    @property
    def distance(self):
        """Return a weighted and normalized distance across all
        penalties.
        """
        dist_max = self.max_distance
        if dist_max:
            return self.raw_distance / self.max_distance
        return 0.0

    @property
    def max_distance(self):
        """Return the maximum distance penalty (normalization factor).
        """
        dist_max = 0.0
        for key, penalty in self._penalties.items():
            dist_max += len(penalty) * self._weights[key]
        return dist_max

    @property
    def raw_distance(self):
        """Return the raw (denormalized) distance.
        """
        dist_raw = 0.0
        for key, penalty in self._penalties.items():
            dist_raw += sum(penalty) * self._weights[key]
        return dist_raw

    def items(self):
        """Return a list of (key, dist) pairs, with `dist` being the
        weighted distance, sorted from highest to lowest. Does not
        include penalties with a zero value.
        """
        list_ = []
        for key in self._penalties:
            dist = self[key]
            if dist:
                list_.append((key, dist))
        # Convert distance into a negative float we can sort items in
        # ascending order (for keys, when the penalty is equal) and
        # still get the items with the biggest distance first.
        return sorted(
            list_,
            key=lambda key_and_dist: (-key_and_dist[1], key_and_dist[0])
        )

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self.distance == other

    # Behave like a float.

    def __lt__(self, other):
        return self.distance < other

    def __float__(self):
        return self.distance

    def __sub__(self, other):
        return self.distance - other

    def __rsub__(self, other):
        return other - self.distance

    def __str__(self):
        return "{0:.2f}".format(self.distance)

    # Behave like a dict.

    def __getitem__(self, key):
        """Returns the weighted distance for a named penalty.
        """
        dist = sum(self._penalties[key]) * self._weights[key]
        dist_max = self.max_distance
        if dist_max:
            return dist / dist_max
        return 0.0

    def __iter__(self):
        return iter(self.items())

    def __len__(self):
        return len(self.items())

    def keys(self):
        return [key for key, _ in self.items()]

    def update(self, dist):
        """Adds all the distance penalties from `dist`.
        """
        if not isinstance(dist, Distance):
            raise ValueError(
                u'`dist` must be a Distance object, not {0}'.format(type(dist))
            )
        for key, penalties in dist._penalties.items():
            self._penalties.setdefault(key, []).extend(penalties)

    # Adding components.

    def _eq(self, value1, value2):
        """Returns True if `value1` is equal to `value2`. `value1` may
        be a compiled regular expression, in which case it will be
        matched against `value2`.
        """
        if isinstance(value1, Pattern):
            return bool(value1.match(value2))
        return value1 == value2

    def add(self, key, dist):
        """Adds a distance penalty. `key` must correspond with a
        configured weight setting. `dist` must be a float between 0.0
        and 1.0, and will be added to any existing distance penalties
        for the same key.
        """
        if not 0.0 <= dist <= 1.0:
            raise ValueError(
                u'`dist` must be between 0.0 and 1.0, not {0}'.format(dist)
            )
        self._penalties.setdefault(key, []).append(dist)

    def add_equality(self, key, value, options):
        """Adds a distance penalty of 1.0 if `value` doesn't match any
        of the values in `options`. If an option is a compiled regular
        expression, it will be considered equal if it matches against
        `value`.
        """
        if not isinstance(options, (list, tuple)):
            options = [options]
        for opt in options:
            if self._eq(opt, value):
                dist = 0.0
                break
        else:
            dist = 1.0
        self.add(key, dist)

    def add_expr(self, key, expr):
        """Adds a distance penalty of 1.0 if `expr` evaluates to True,
        or 0.0.
        """
        if expr:
            self.add(key, 1.0)
        else:
            self.add(key, 0.0)

    def add_number(self, key, number1, number2):
        """Adds a distance penalty of 1.0 for each number of difference
        between `number1` and `number2`, or 0.0 when there is no
        difference. Use this when there is no upper limit on the
        difference between the two numbers.
        """
        diff = abs(number1 - number2)
        if diff:
            for i in range(diff):
                self.add(key, 1.0)
        else:
            self.add(key, 0.0)

    def add_priority(self, key, value, options):
        """Adds a distance penalty that corresponds to the position at
        which `value` appears in `options`. A distance penalty of 0.0
        for the first option, or 1.0 if there is no matching option. If
        an option is a compiled regular expression, it will be
        considered equal if it matches against `value`.
        """
        if not isinstance(options, (list, tuple)):
            options = [options]
        unit = 1.0 / (len(options) or 1)
        for i, opt in enumerate(options):
            if self._eq(opt, value):
                dist = i * unit
                break
        else:
            dist = 1.0
        self.add(key, dist)

    def add_ratio(self, key, number1, number2):
        """Adds a distance penalty for `number1` as a ratio of `number2`.
        `number1` is bound at 0 and `number2`.
        """
        number = float(max(min(number1, number2), 0))
        if number2:
            dist = number / number2
        else:
            dist = 0.0
        self.add(key, dist)

    def add_string(self, key, str1, str2):
        """Adds a distance penalty based on the edit distance between
        `str1` and `str2`.
        """
        dist = string_dist(str1, str2)
        self.add(key, dist)


# Structures that compose all the information for a candidate match.

AlbumMatch = namedtuple('AlbumMatch', ['distance', 'info', 'mapping',
                                       'extra_items', 'extra_tracks'])

TrackMatch = namedtuple('TrackMatch', ['distance', 'info'])


# Aggregation of sources.

def album_for_mbid(release_id):
    """Get an AlbumInfo object for a MusicBrainz release ID. Return None
    if the ID is not found.
    """
    try:
        album = mb.album_for_id(release_id)
        if album:
            plugins.send(u'albuminfo_received', info=album)
        return album
    except mb.MusicBrainzAPIError as exc:
        exc.log(log)


def track_for_mbid(recording_id):
    """Get a TrackInfo object for a MusicBrainz recording ID. Return None
    if the ID is not found.
    """
    try:
        track = mb.track_for_id(recording_id)
        if track:
            plugins.send(u'trackinfo_received', info=track)
        return track
    except mb.MusicBrainzAPIError as exc:
        exc.log(log)


def albums_for_id(album_id):
    """Get a list of albums for an ID."""
    a = album_for_mbid(album_id)
    if a:
        yield a
    for a in plugins.album_for_id(album_id):
        if a:
            plugins.send(u'albuminfo_received', info=a)
            yield a


def tracks_for_id(track_id):
    """Get a list of tracks for an ID."""
    t = track_for_mbid(track_id)
    if t:
        yield t
    for t in plugins.track_for_id(track_id):
        if t:
            plugins.send(u'trackinfo_received', info=t)
            yield t


@plugins.notify_info_yielded(u'albuminfo_received')
def album_candidates(items, artist, album, va_likely, extra_tags):
    """Search for album matches. ``items`` is a list of Item objects
    that make up the album. ``artist`` and ``album`` are the respective
    names (strings), which may be derived from the item list or may be
    entered by the user. ``va_likely`` is a boolean indicating whether
    the album is likely to be a "various artists" release. ``extra_tags``
    is an optional dictionary of additional tags used to further
    constrain the search.
    """

    # Base candidates if we have album and artist to match.
    if artist and album:
        try:
            for candidate in mb.match_album(artist, album, len(items),
                                            extra_tags):
                yield candidate
        except mb.MusicBrainzAPIError as exc:
            exc.log(log)

    # Also add VA matches from MusicBrainz where appropriate.
    if va_likely and album:
        try:
            for candidate in mb.match_album(None, album, len(items),
                                            extra_tags):
                yield candidate
        except mb.MusicBrainzAPIError as exc:
            exc.log(log)

    # Candidates from plugins.
    for candidate in plugins.candidates(items, artist, album, va_likely,
                                        extra_tags):
        yield candidate


@plugins.notify_info_yielded(u'trackinfo_received')
def item_candidates(item, artist, title):
    """Search for item matches. ``item`` is the Item to be matched.
    ``artist`` and ``title`` are strings and either reflect the item or
    are specified by the user.
    """

    # MusicBrainz candidates.
    if artist and title:
        try:
            for candidate in mb.match_track(artist, title):
                yield candidate
        except mb.MusicBrainzAPIError as exc:
            exc.log(log)

    # Plugin candidates.
    for candidate in plugins.item_candidates(item, artist, title):
        yield candidate
