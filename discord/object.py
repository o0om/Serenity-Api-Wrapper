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

from .mixins import Hashable
from .utils import snowflake_time, MISSING

from typing import (
    SupportsInt,
    TYPE_CHECKING,
    Type,
    Union,
    Dict,
    Any,
    Optional,
    ClassVar,
    Tuple,
)
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    import datetime
    from . import abc

    SupportsIntCast = Union[SupportsInt, str, bytes, bytearray]

# fmt: off
__all__ = (
    'Object',
)
# fmt: on

# Cache for frequently used objects
_OBJECT_CACHE: WeakValueDictionary[Tuple[int, Type[abc.Snowflake]], 'Object'] = WeakValueDictionary()

class Object(Hashable):
    """Represents a generic Discord object.

    The purpose of this class is to allow you to create 'miniature'
    versions of data classes if you want to pass in just an ID. Most functions
    that take in a specific data class with an ID can also take in this class
    as a substitute instead. Note that even though this is the case, not all
    objects (if any) actually inherit from this class.

    There are also some cases where some websocket events are received
    in :issue:`strange order <21>` and when such events happened you would
    receive this class rather than the actual data class. These cases are
    extremely rare.

    .. versionchanged:: 2.5
        Added caching for frequently used objects.
        Added new utility methods for common object operations.
        Enhanced type safety with better type hints.

    .. container:: operations

        .. describe:: x == y

            Checks if two objects are equal.

        .. describe:: x != y

            Checks if two objects are not equal.

        .. describe:: hash(x)

            Returns the object's hash.

    Attributes
    -----------
    id: :class:`int`
        The ID of the object.
    type: Type[:class:`abc.Snowflake`]
        The discord.py model type of the object, if not specified, defaults to this class.

        .. note::

            In instances where there are multiple applicable types, use a shared base class.
            for example, both :class:`Member` and :class:`User` are subclasses of :class:`abc.User`.

        .. versionadded:: 2.0
    """

    _MIN_SNOWFLAKE: ClassVar[int] = 0
    _MAX_SNOWFLAKE: ClassVar[int] = (1 << 64) - 1

    def __new__(cls, id: SupportsIntCast, *, type: Type[abc.Snowflake] = MISSING) -> 'Object':
        try:
            id = int(id)
        except ValueError:
            raise TypeError(f'id parameter must be convertible to int not {id.__class__.__name__}') from None

        # Validate snowflake range
        if not cls._MIN_SNOWFLAKE <= id <= cls._MAX_SNOWFLAKE:
            raise ValueError(f'id must be between {cls._MIN_SNOWFLAKE} and {cls._MAX_SNOWFLAKE}')

        # Check cache
        cache_key = (id, type or cls)
        if cache_key in _OBJECT_CACHE:
            return _OBJECT_CACHE[cache_key]

        # Create new instance
        instance = super().__new__(cls)
        instance.id = id
        instance.type = type or cls

        # Cache the instance
        _OBJECT_CACHE[cache_key] = instance

        return instance

    def __init__(self, id: SupportsIntCast, *, type: Type[abc.Snowflake] = MISSING) -> None:
        # Initialization is handled in __new__
        pass

    def __repr__(self) -> str:
        return f'<Object id={self.id!r} type={self.type!r}>'

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (self.type, self.__class__)):
            return self.id == other.id
        return NotImplemented

    __hash__ = Hashable.__hash__

    @property
    def created_at(self) -> datetime.datetime:
        """:class:`datetime.datetime`: Returns the snowflake's creation time in UTC."""
        return snowflake_time(self.id)

    def is_valid(self) -> bool:
        """Checks if the object's ID is valid.
        
        Returns
        -------
        :class:`bool`
            Whether the object's ID is valid.
        """
        return self._MIN_SNOWFLAKE <= self.id <= self._MAX_SNOWFLAKE

    def to_dict(self) -> Dict[str, Any]:
        """Converts the object to a dictionary.
        
        Returns
        -------
        Dict[:class:`str`, Any]
            The dictionary representation of the object.
        """
        return {
            'id': self.id,
            'type': self.type.__name__,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Object':
        """Creates an object from a dictionary.
        
        Parameters
        ----------
        data: Dict[:class:`str`, Any]
            The dictionary to create the object from.
            
        Returns
        -------
        :class:`Object`
            The created object.
        """
        return cls(
            id=data['id'],
            type=data.get('type', cls),
        )

    def copy(self) -> 'Object':
        """Creates a copy of the object.
        
        Returns
        -------
        :class:`Object`
            The copied object.
        """
        return self.__class__(id=self.id, type=self.type)

    def __str__(self) -> str:
        return str(self.id)


# A singleton object representing the oldest possible snowflake
OLDEST_OBJECT = Object(id=0)
