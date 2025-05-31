"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import struct
import zlib
from typing import TYPE_CHECKING, ClassVar, IO, Generator, Tuple, Optional, Dict, Any, List
from weakref import WeakValueDictionary

from .errors import DiscordException

__all__ = (
    'OggError',
    'OggPage',
    'OggStream',
    'OggStreamInfo',
)


class OggError(DiscordException):
    """An exception that is thrown for Ogg stream parsing errors.
    
    Attributes
    ----------
    message: :class:`str`
        The error message.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# https://tools.ietf.org/html/rfc3533
# https://tools.ietf.org/html/rfc7845

# Cache for frequently used Ogg pages
_PAGE_CACHE: WeakValueDictionary[Tuple[int, int, int], 'OggPage'] = WeakValueDictionary()

class OggPage:
    """Represents an Ogg page in an Ogg stream.
    
    .. versionchanged:: 2.5
        Added caching for frequently used pages.
        Added new utility methods for common Ogg operations.
        Enhanced type safety with better type hints.
    
    Attributes
    ----------
    flag: :class:`int`
        The page flag.
    gran_pos: :class:`int`
        The granule position.
    serial: :class:`int`
        The stream serial number.
    pagenum: :class:`int`
        The page sequence number.
    crc: :class:`int`
        The CRC checksum.
    segnum: :class:`int`
        The number of segments.
    segtable: :class:`bytes`
        The segment table.
    data: :class:`bytes`
        The page data.
    """

    _header: ClassVar[struct.Struct] = struct.Struct('<xBQIIIB')
    _MAGIC: ClassVar[bytes] = b'OggS'
    _FLAGS: ClassVar[Dict[str, int]] = {
        'continued': 0x01,
        'first': 0x02,
        'last': 0x04,
    }

    if TYPE_CHECKING:
        flag: int
        gran_pos: int
        serial: int
        pagenum: int
        crc: int
        segnum: int
        segtable: bytes
        data: bytes

    def __init__(self, stream: IO[bytes]) -> None:
        try:
            # Verify magic number
            magic = stream.read(4)
            if magic != self._MAGIC:
                raise OggError(f'Invalid Ogg magic number: {magic!r}')

            header = stream.read(struct.calcsize(self._header.format))
            if len(header) != struct.calcsize(self._header.format):
                raise OggError('Incomplete Ogg page header')

            self.flag, self.gran_pos, self.serial, self.pagenum, self.crc, self.segnum = self._header.unpack(header)

            # Read segment table
            self.segtable = stream.read(self.segnum)
            if len(self.segtable) != self.segnum:
                raise OggError('Incomplete segment table')

            # Calculate and read page data
            bodylen = sum(struct.unpack('B' * self.segnum, self.segtable))
            self.data = stream.read(bodylen)
            if len(self.data) != bodylen:
                raise OggError('Incomplete page data')

            # Verify CRC
            if not self._verify_crc():
                raise OggError('CRC mismatch')

        except Exception as e:
            if isinstance(e, OggError):
                raise
            raise OggError(f'Error parsing Ogg page: {str(e)}') from None

    def __new__(cls, stream: IO[bytes]) -> 'OggPage':
        # Read magic and header for cache key
        magic = stream.read(4)
        if magic != cls._MAGIC:
            raise OggError(f'Invalid Ogg magic number: {magic!r}')

        header = stream.read(struct.calcsize(cls._header.format))
        if len(header) != struct.calcsize(cls._header.format):
            raise OggError('Incomplete Ogg page header')

        flag, gran_pos, serial, pagenum, crc, segnum = cls._header.unpack(header)
        
        # Check cache
        cache_key = (serial, pagenum, crc)
        if cache_key in _PAGE_CACHE:
            # Reset stream position
            stream.seek(-(4 + struct.calcsize(cls._header.format)), 1)
            return _PAGE_CACHE[cache_key]

        # Create new instance
        instance = super().__new__(cls)
        instance.flag = flag
        instance.gran_pos = gran_pos
        instance.serial = serial
        instance.pagenum = pagenum
        instance.crc = crc
        instance.segnum = segnum

        # Cache the instance
        _PAGE_CACHE[cache_key] = instance

        return instance

    def _verify_crc(self) -> bool:
        """Verifies the CRC checksum of the page.
        
        Returns
        -------
        :class:`bool`
            Whether the CRC is valid.
        """
        # Calculate CRC
        crc = 0
        for i in range(self.segnum):
            crc = zlib.crc32(bytes([self.segtable[i]]), crc)
        crc = zlib.crc32(self.data, crc)
        
        return crc == self.crc

    def iter_packets(self) -> Generator[Tuple[bytes, bool], None, None]:
        """Iterates over the packets in the page.
        
        Yields
        ------
        Tuple[:class:`bytes`, :class:`bool`]
            The packet data and whether it is complete.
        """
        packetlen = offset = 0
        partial = True

        for seg in self.segtable:
            if seg == 255:
                packetlen += 255
                partial = True
            else:
                packetlen += seg
                yield self.data[offset : offset + packetlen], True
                offset += packetlen
                packetlen = 0
                partial = False

        if partial:
            yield self.data[offset:], False

    def is_continued(self) -> bool:
        """Checks if this page continues a packet from the previous page.
        
        Returns
        -------
        :class:`bool`
            Whether this page continues a packet.
        """
        return bool(self.flag & self._FLAGS['continued'])

    def is_first(self) -> bool:
        """Checks if this is the first page of a stream.
        
        Returns
        -------
        :class:`bool`
            Whether this is the first page.
        """
        return bool(self.flag & self._FLAGS['first'])

    def is_last(self) -> bool:
        """Checks if this is the last page of a stream.
        
        Returns
        -------
        :class:`bool`
            Whether this is the last page.
        """
        return bool(self.flag & self._FLAGS['last'])

    def get_packet_count(self) -> int:
        """Gets the number of complete packets in this page.
        
        Returns
        -------
        :class:`int`
            The number of complete packets.
        """
        return sum(1 for seg in self.segtable if seg < 255)

    def get_total_size(self) -> int:
        """Gets the total size of the page in bytes.
        
        Returns
        -------
        :class:`int`
            The total size of the page.
        """
        return 27 + self.segnum + len(self.data)


class OggStreamInfo:
    """Represents information about an Ogg stream.
    
    Attributes
    ----------
    serial: :class:`int`
        The stream serial number.
    page_count: :class:`int`
        The number of pages in the stream.
    packet_count: :class:`int`
        The number of packets in the stream.
    total_size: :class:`int`
        The total size of the stream in bytes.
    """

    def __init__(self, serial: int):
        self.serial = serial
        self.page_count = 0
        self.packet_count = 0
        self.total_size = 0


class OggStream:
    """Represents an Ogg stream.
    
    .. versionchanged:: 2.5
        Added stream information tracking.
        Added new utility methods for common Ogg operations.
        Enhanced type safety with better type hints.
    
    Attributes
    ----------
    stream: :term:`py:file object`
        The underlying file-like object.
    info: :class:`OggStreamInfo`
        Information about the stream.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream: IO[bytes] = stream
        self.info: Optional[OggStreamInfo] = None

    def _next_page(self) -> Optional[OggPage]:
        """Gets the next page in the stream.
        
        Returns
        -------
        Optional[:class:`OggPage`]
            The next page, or None if at the end of the stream.
        """
        head = self.stream.read(4)
        if head == b'OggS':
            page = OggPage(self.stream)
            
            # Update stream info
            if self.info is None:
                self.info = OggStreamInfo(page.serial)
            elif self.info.serial != page.serial:
                raise OggError(f'Stream serial number mismatch: {self.info.serial} != {page.serial}')
            
            self.info.page_count += 1
            self.info.packet_count += page.get_packet_count()
            self.info.total_size += page.get_total_size()
            
            return page
        elif not head:
            return None
        else:
            raise OggError(f'Invalid header magic {head}')

    def _iter_pages(self) -> Generator[OggPage, None, None]:
        """Iterates over all pages in the stream.
        
        Yields
        ------
        :class:`OggPage`
            The next page in the stream.
        """
        page = self._next_page()
        while page:
            yield page
            page = self._next_page()

    def iter_packets(self) -> Generator[bytes, None, None]:
        """Iterates over all packets in the stream.
        
        Yields
        ------
        :class:`bytes`
            The next packet in the stream.
        """
        partial = b''
        for page in self._iter_pages():
            for data, complete in page.iter_packets():
                partial += data
                if complete:
                    yield partial
                    partial = b''

    def get_info(self) -> OggStreamInfo:
        """Gets information about the stream.
        
        Returns
        -------
        :class:`OggStreamInfo`
            Information about the stream.
        """
        if self.info is None:
            # Read the first page to initialize info
            self._next_page()
        return self.info

    def seek(self, offset: int) -> None:
        """Seeks to a position in the stream.
        
        Parameters
        ----------
        offset: :class:`int`
            The offset to seek to.
        """
        self.stream.seek(offset)

    def tell(self) -> int:
        """Gets the current position in the stream.
        
        Returns
        -------
        :class:`int`
            The current position.
        """
        return self.stream.tell()

    def close(self) -> None:
        """Closes the stream."""
        self.stream.close()
