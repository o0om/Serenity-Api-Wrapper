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

import array
import asyncio
from textwrap import TextWrapper
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Dict,
    ForwardRef,
    Generic,
    Iterable,
    Iterator,
    List,
    Literal,
    NamedTuple,
    Optional,
    Protocol,
    Set,
    Sequence,
    SupportsIndex,
    Tuple,
    Type,
    TypeVar,
    Union,
    overload,
    TYPE_CHECKING,
)
import unicodedata
from base64 import b64encode, b64decode
from bisect import bisect_left
import datetime
import functools
from inspect import isawaitable as _isawaitable, signature as _signature
from operator import attrgetter
from urllib.parse import urlencode
import json
import re
import os
import sys
import types
import typing
import warnings
import logging
import zlib
import time
from functools import lru_cache

import yarl

try:
    import orjson  # type: ignore
except ModuleNotFoundError:
    HAS_ORJSON = False
else:
    HAS_ORJSON = True

try:
    import zstandard  # type: ignore
except ImportError:
    _HAS_ZSTD = False
else:
    _HAS_ZSTD = True

__all__ = (
    'oauth_url',
    'snowflake_time',
    'time_snowflake',
    'find',
    'get',
    'sleep_until',
    'utcnow',
    'remove_markdown',
    'escape_markdown',
    'escape_mentions',
    'maybe_coroutine',
    'as_chunks',
    'format_dt',
    'MISSING',
    'setup_logging',
    'cached_property',
    'CachedSlotProperty',
    'classproperty',
    'SequenceProxy',
    'SnowflakeList',
)

DISCORD_EPOCH = 1420070400000
DEFAULT_FILE_SIZE_LIMIT_BYTES = 26214400
DEFAULT_CACHE_SIZE = 128


class _MissingSentinel:
    __slots__ = ()

    def __eq__(self, other) -> bool:
        return False

    def __bool__(self) -> bool:
        return False

    def __hash__(self) -> int:
        return 0

    def __repr__(self):
        return '...'


MISSING: Any = _MissingSentinel()


class _cached_property:
    """A property that caches its value for better performance.
    
    This is a more efficient version of Python's built-in `@property` decorator
    that caches the computed value after the first access.
    
    Attributes
    ----------
    function: Callable
        The function to be cached.
    __doc__: str
        The docstring of the function.
    """
    
    def __init__(self, function: Callable[[Any], Any]) -> None:
        self.function = function
        self.__doc__ = getattr(function, '__doc__')
        self._cache: Dict[str, Any] = {}

    def __get__(self, instance: Optional[Any], owner: Type[Any]) -> Any:
        if instance is None:
            return self

        cache_key = f"{instance.__class__.__name__}.{self.function.__name__}"
        if cache_key not in self._cache:
            self._cache[cache_key] = self.function(instance)
        return self._cache[cache_key]

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError("Cannot set cached property")

    def __delete__(self, instance: Any) -> None:
        cache_key = f"{instance.__class__.__name__}.{self.function.__name__}"
        self._cache.pop(cache_key, None)


if TYPE_CHECKING:
    from functools import cached_property as cached_property
    from typing_extensions import ParamSpec, Self, TypeGuard
    from .permissions import Permissions
    from .abc import Snowflake
    from .invite import Invite
    from .template import Template

    class _DecompressionContext(Protocol):
        COMPRESSION_TYPE: str
        def decompress(self, data: bytes, /) -> str | None: ...

    P = ParamSpec('P')
    MaybeAwaitableFunc = Callable[P, 'MaybeAwaitable[T]']
    _SnowflakeListBase = array.array[int]
else:
    cached_property = _cached_property
    _SnowflakeListBase = array.array


T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
_Iter = Union[Iterable[T], AsyncIterable[T]]
Coro = Coroutine[Any, Any, T]
MaybeAwaitable = Union[T, Awaitable[T]]


class CachedSlotProperty(Generic[T, T_co]):
    """A property that caches its value in a slot for better performance.
    
    This is similar to `_cached_property` but stores the cached value in a slot
    instead of a dictionary for better memory efficiency.
    
    Attributes
    ----------
    name: str
        The name of the slot to store the cached value.
    function: Callable
        The function to be cached.
    __doc__: str
        The docstring of the function.
    """
    
    def __init__(self, name: str, function: Callable[[T], T_co]) -> None:
        self.name = name
        self.function = function
        self.__doc__ = getattr(function, '__doc__')

    @overload
    def __get__(self, instance: None, owner: Type[T]) -> CachedSlotProperty[T, T_co]:
        ...

    @overload
    def __get__(self, instance: T, owner: Type[T]) -> T_co:
        ...

    def __get__(self, instance: Optional[T], owner: Type[T]) -> Any:
        if instance is None:
            return self

        try:
            return getattr(instance, self.name)
        except AttributeError:
            value = self.function(instance)
            setattr(instance, self.name, value)
            return value

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError("Cannot set cached slot property")

    def __delete__(self, instance: Any) -> None:
        try:
            delattr(instance, self.name)
        except AttributeError:
            pass


class classproperty(Generic[T_co]):
    """A property that works on classes instead of instances.
    
    This allows you to define properties that can be accessed on the class
    itself rather than instances of the class.
    
    Attributes
    ----------
    fget: Callable
        The function to be called when the property is accessed.
    """
    
    def __init__(self, fget: Callable[[Any], T_co]) -> None:
        self.fget = fget

    def __get__(self, instance: Optional[Any], owner: Type[Any]) -> T_co:
        return self.fget(owner)

    def __set__(self, instance: Optional[Any], value: Any) -> None:
        raise AttributeError('cannot set attribute')


def cached_slot_property(name: str) -> Callable[[Callable[[T], T_co]], CachedSlotProperty[T, T_co]]:
    """A decorator that creates a cached slot property.
    
    Parameters
    ----------
    name: str
        The name of the slot to store the cached value.
        
    Returns
    -------
    Callable
        A decorator that creates a cached slot property.
    """
    def decorator(func: Callable[[T], T_co]) -> CachedSlotProperty[T, T_co]:
        return CachedSlotProperty(name, func)
    return decorator


class SequenceProxy(Sequence[T_co]):
    """A proxy of a sequence that only creates a copy when necessary.
    
    This class provides a memory-efficient way to work with sequences by
    only creating a copy when the sequence needs to be modified.
    
    Attributes
    ----------
    __proxied: Collection[T_co]
        The underlying collection being proxied.
    __sorted: bool
        Whether the collection should be sorted.
    """
    
    def __init__(self, proxied: Collection[T_co], *, sorted: bool = False):
        self.__proxied: Collection[T_co] = proxied
        self.__sorted: bool = sorted

    @cached_property
    def __copied(self) -> List[T_co]:
        if self.__sorted:
            self.__proxied = sorted(self.__proxied)  # type: ignore
        else:
            self.__proxied = list(self.__proxied)
        return self.__proxied

    def __repr__(self) -> str:
        return f"SequenceProxy({self.__proxied!r})"

    @overload
    def __getitem__(self, idx: SupportsIndex) -> T_co:
        ...

    @overload
    def __getitem__(self, idx: slice) -> List[T_co]:
        ...

    def __getitem__(self, idx: Union[SupportsIndex, slice]) -> Union[T_co, List[T_co]]:
        return self.__copied[idx]

    def __len__(self) -> int:
        return len(self.__proxied)

    def __contains__(self, item: Any) -> bool:
        return item in self.__proxied

    def __iter__(self) -> Iterator[T_co]:
        return iter(self.__proxied)

    def __reversed__(self) -> Iterator[T_co]:
        return reversed(self.__copied)

    def index(self, value: Any, *args: Any, **kwargs: Any) -> int:
        return self.__copied.index(value, *args, **kwargs)

    def count(self, value: Any) -> int:
        return self.__proxied.count(value)


@lru_cache(maxsize=DEFAULT_CACHE_SIZE)
def snowflake_time(id: int, /) -> datetime.datetime:
    """Returns the creation time of a snowflake.
    
    Parameters
    ----------
    id: int
        The snowflake ID.
        
    Returns
    -------
    datetime.datetime
        The creation time of the snowflake.
    """
    timestamp = ((id >> 22) + DISCORD_EPOCH) / 1000
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)


@lru_cache(maxsize=DEFAULT_CACHE_SIZE)
def time_snowflake(dt: datetime.datetime, /, *, high: bool = False) -> int:
    """Returns a snowflake for the given datetime.
    
    Parameters
    ----------
    dt: datetime.datetime
        The datetime to convert to a snowflake.
    high: bool
        Whether to use the high bit for the snowflake.
        
    Returns
    -------
    int
        The snowflake ID.
    """
    discord_millis = int(dt.timestamp() * 1000 - DISCORD_EPOCH)
    return (discord_millis << 22) + (2**22 - 1 if high else 0)


def _find(predicate: Callable[[T], Any], iterable: Iterable[T], /) -> Optional[T]:
    """Returns the first element in the iterable that satisfies the predicate.
    
    Parameters
    ----------
    predicate: Callable
        The predicate to check each element against.
    iterable: Iterable[T]
        The iterable to search through.
        
    Returns
    -------
    Optional[T]
        The first element that satisfies the predicate, or None if no such element exists.
    """
    for element in iterable:
        if predicate(element):
            return element
    return None


async def _afind(predicate: Callable[[T], Any], iterable: AsyncIterable[T], /) -> Optional[T]:
    """Returns the first element in the async iterable that satisfies the predicate.
    
    Parameters
    ----------
    predicate: Callable
        The predicate to check each element against.
    iterable: AsyncIterable[T]
        The async iterable to search through.
        
    Returns
    -------
    Optional[T]
        The first element that satisfies the predicate, or None if no such element exists.
    """
    async for element in iterable:
        if predicate(element):
            return element
    return None


@overload
def find(predicate: Callable[[T], Any], iterable: AsyncIterable[T], /) -> Coro[Optional[T]]:
    ...


@overload
def find(predicate: Callable[[T], Any], iterable: Iterable[T], /) -> Optional[T]:
    ...


def find(predicate: Callable[[T], Any], iterable: _Iter[T], /) -> Union[Optional[T], Coro[Optional[T]]]:
    """Returns the first element in the iterable that satisfies the predicate.
    
    This function works with both synchronous and asynchronous iterables.
    
    Parameters
    ----------
    predicate: Callable
        The predicate to check each element against.
    iterable: Union[Iterable[T], AsyncIterable[T]]
        The iterable to search through.
        
    Returns
    -------
    Union[Optional[T], Coro[Optional[T]]]
        The first element that satisfies the predicate, or None if no such element exists.
        If the iterable is async, returns a coroutine.
    """
    if isinstance(iterable, AsyncIterable):
        return _afind(predicate, iterable)
    return _find(predicate, iterable)


def _get(iterable: Iterable[T], /, **attrs: Any) -> Optional[T]:
    """Returns the first element in the iterable that matches all the given attributes.
    
    Parameters
    ----------
    iterable: Iterable[T]
        The iterable to search through.
    **attrs: Any
        The attributes to match against.
        
    Returns
    -------
    Optional[T]
        The first element that matches all the given attributes, or None if no such element exists.
    """
    converted = [(attr, str(value)) for attr, value in attrs.items()]
    for elem in iterable:
        if all(getattr(elem, attr, None) == value for attr, value in converted):
            return elem
    return None


async def _aget(iterable: AsyncIterable[T], /, **attrs: Any) -> Optional[T]:
    """Returns the first element in the async iterable that matches all the given attributes.
    
    Parameters
    ----------
    iterable: AsyncIterable[T]
        The async iterable to search through.
    **attrs: Any
        The attributes to match against.
        
    Returns
    -------
    Optional[T]
        The first element that matches all the given attributes, or None if no such element exists.
    """
    converted = [(attr, str(value)) for attr, value in attrs.items()]
    async for elem in iterable:
        if all(getattr(elem, attr, None) == value for attr, value in converted):
            return elem
    return None


@overload
def get(iterable: AsyncIterable[T], /, **attrs: Any) -> Coro[Optional[T]]:
    ...


@overload
def get(iterable: Iterable[T], /, **attrs: Any) -> Optional[T]:
    ...


def get(iterable: _Iter[T], /, **attrs: Any) -> Union[Optional[T], Coro[Optional[T]]]:
    """Returns the first element in the iterable that matches all the given attributes.
    
    This function works with both synchronous and asynchronous iterables.
    
    Parameters
    ----------
    iterable: Union[Iterable[T], AsyncIterable[T]]
        The iterable to search through.
    **attrs: Any
        The attributes to match against.
        
    Returns
    -------
    Union[Optional[T], Coro[Optional[T]]]
        The first element that matches all the given attributes, or None if no such element exists.
        If the iterable is async, returns a coroutine.
    """
    if isinstance(iterable, AsyncIterable):
        return _aget(iterable, **attrs)
    return _get(iterable, **attrs)


def _unique(iterable: Iterable[T]) -> List[T]:
    """Returns a list of unique elements from the iterable.
    
    Parameters
    ----------
    iterable: Iterable[T]
        The iterable to get unique elements from.
        
    Returns
    -------
    List[T]
        A list of unique elements.
    """
    return list(dict.fromkeys(iterable))


def _get_as_snowflake(data: Any, key: str) -> Optional[int]:
    """Gets a snowflake ID from the data dictionary.
    
    Parameters
    ----------
    data: Any
        The data dictionary to get the snowflake from.
    key: str
        The key to get the snowflake from.
        
    Returns
    -------
    Optional[int]
        The snowflake ID, or None if not found.
    """
    try:
        value = data[key]
    except (KeyError, TypeError):
        return None
    else:
        return value and int(value)


def _get_mime_type_for_image(data: bytes) -> Optional[str]:
    """Gets the MIME type for an image.
    
    Parameters
    ----------
    data: bytes
        The image data.
        
    Returns
    -------
    Optional[str]
        The MIME type of the image, or None if not recognized.
    """
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    elif data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'image/gif'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _get_mime_type_for_audio(data: bytes) -> Optional[str]:
    """Gets the MIME type for an audio file.
    
    Parameters
    ----------
    data: bytes
        The audio data.
        
    Returns
    -------
    Optional[str]
        The MIME type of the audio, or None if not recognized.
    """
    if data.startswith(b'OggS'):
        return 'audio/ogg'
    elif data.startswith(b'ID3') or data.startswith(b'\xff\xfb'):
        return 'audio/mpeg'
    return None


def _bytes_to_base64_data(data: bytes, *, audio: bool = False) -> str:
    """Converts bytes to base64 data.
    
    Parameters
    ----------
    data: bytes
        The data to convert.
    audio: bool
        Whether the data is audio.
        
    Returns
    -------
    str
        The base64 encoded data.
    """
    fmt = 'data:audio/{};base64,{}' if audio else 'data:image/{};base64,{}'
    mime = _get_mime_type_for_audio(data) if audio else _get_mime_type_for_image(data)
    if mime is None:
        mime = 'application/octet-stream'
    b64 = b64encode(data).decode('ascii')
    return fmt.format(mime, b64)


def _base64_to_bytes(data: str) -> bytes:
    """Converts base64 data to bytes.
    
    Parameters
    ----------
    data: str
        The base64 data to convert.
        
    Returns
    -------
    bytes
        The decoded bytes.
    """
    return b64decode(data.split(',')[1])


def _is_submodule(parent: str, child: str) -> bool:
    """Checks if a module is a submodule of another module.
    
    Parameters
    ----------
    parent: str
        The parent module name.
    child: str
        The child module name.
        
    Returns
    -------
    bool
        Whether the child is a submodule of the parent.
    """
    return parent == child or child.startswith(f'{parent}.')


def _to_json(obj: Any) -> str:
    """Converts an object to JSON.
    
    Parameters
    ----------
    obj: Any
        The object to convert.
        
    Returns
    -------
    str
        The JSON string.
    """
    if HAS_ORJSON:
        return orjson.dumps(obj).decode('utf-8')
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=True)


def _parse_ratelimit_header(request: Any, *, use_clock: bool = False) -> float:
    """Parses the rate limit header from a request.
    
    Parameters
    ----------
    request: Any
        The request to parse the header from.
    use_clock: bool
        Whether to use the clock for timing.
        
    Returns
    -------
    float
        The rate limit reset time.
    """
    reset_after = request.headers.get('X-Ratelimit-Reset-After')
    if use_clock or not reset_after:
        utc = datetime.timezone.utc
        now = datetime.datetime.now(utc)
        reset = datetime.datetime.fromtimestamp(float(request.headers['X-Ratelimit-Reset']), utc)
        return (reset - now).total_seconds()
    return float(reset_after)


async def maybe_coroutine(f: MaybeAwaitableFunc[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Executes a function that might be a coroutine.
    
    Parameters
    ----------
    f: Callable
        The function to execute.
    *args: Any
        The positional arguments to pass to the function.
    **kwargs: Any
        The keyword arguments to pass to the function.
        
    Returns
    -------
    T
        The result of the function.
    """
    value = f(*args, **kwargs)
    if _isawaitable(value):
        return await value
    return value


async def async_all(
    gen: Iterable[Union[T, Awaitable[T]]],
    *,
    check: Callable[[Union[T, Awaitable[T]]], TypeGuard[Awaitable[T]]] = _isawaitable,
) -> bool:
    """Checks if all elements in an iterable satisfy a condition.
    
    Parameters
    ----------
    gen: Iterable[Union[T, Awaitable[T]]]
        The iterable to check.
    check: Callable
        The function to check each element with.
        
    Returns
    -------
    bool
        Whether all elements satisfy the condition.
    """
    for elem in gen:
        if check(elem):
            if not await elem:
                return False
        elif not elem:
            return False
    return True


async def sane_wait_for(futures: Iterable[Awaitable[T]], *, timeout: Optional[float]) -> Set[asyncio.Task[T]]:
    """Waits for a set of futures to complete.
    
    Parameters
    ----------
    futures: Iterable[Awaitable[T]]
        The futures to wait for.
    timeout: Optional[float]
        The timeout in seconds.
        
    Returns
    -------
    Set[asyncio.Task[T]]
        The completed tasks.
    """
    tasks = {asyncio.create_task(future) for future in futures}
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    return done


def get_slots(cls: Type[Any]) -> Iterator[str]:
    """Gets the slots of a class.
    
    Parameters
    ----------
    cls: Type[Any]
        The class to get slots from.
        
    Returns
    -------
    Iterator[str]
        The slots of the class.
    """
    for c in cls.__mro__:
        if '__slots__' in c.__dict__:
            yield from c.__dict__['__slots__']


def compute_timedelta(dt: datetime.datetime) -> float:
    """Computes the time delta between now and a datetime.
    
    Parameters
    ----------
    dt: datetime.datetime
        The datetime to compute the delta from.
        
    Returns
    -------
    float
        The time delta in seconds.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (dt - now).total_seconds()


@overload
async def sleep_until(when: datetime.datetime, result: T) -> T:
    ...


@overload
async def sleep_until(when: datetime.datetime) -> None:
    ...


async def sleep_until(when: datetime.datetime, result: Optional[T] = None) -> Optional[T]:
    """Sleeps until a specific datetime.
    
    Parameters
    ----------
    when: datetime.datetime
        The datetime to sleep until.
    result: Optional[T]
        The result to return after sleeping.
        
    Returns
    -------
    Optional[T]
        The result, if provided.
    """
    delta = compute_timedelta(when)
    if delta > 0:
        await asyncio.sleep(delta)
    return result


def utcnow() -> datetime.datetime:
    """Returns the current UTC datetime.
    
    Returns
    -------
    datetime.datetime
        The current UTC datetime.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def valid_icon_size(size: int) -> bool:
    """Checks if an icon size is valid.
    
    Parameters
    ----------
    size: int
        The size to check.
        
    Returns
    -------
    bool
        Whether the size is valid.
    """
    return 16 <= size <= 4096 and not (size & (size - 1))


class SnowflakeList(_SnowflakeListBase):
    """Internal data storage class to efficiently store a list of snowflakes.
    
    This should have the following characteristics:
    - Low memory usage
    - O(n) iteration (obviously)
    - O(n log n) initial creation if data is unsorted
    - O(log n) search and indexing
    - O(n) insertion
    """
    
    __slots__ = ()

    def __new__(cls, data: Iterable[int], *, is_sorted: bool = False) -> Self:
        self = super().__new__(cls, 'Q')
        if not is_sorted:
            data = sorted(data)
        self.extend(data)
        return self

    def add(self, element: int) -> None:
        """Adds an element to the list.
        
        Parameters
        ----------
        element: int
            The element to add.
        """
        self.append(element)

    def get(self, element: int) -> Optional[int]:
        """Gets an element from the list.
        
        Parameters
        ----------
        element: int
            The element to get.
            
        Returns
        -------
        Optional[int]
            The element, or None if not found.
        """
        return element if element in self else None

    def has(self, element: int) -> bool:
        """Checks if an element is in the list.
        
        Parameters
        ----------
        element: int
            The element to check.
            
        Returns
        -------
        bool
            Whether the element is in the list.
        """
        return element in self
