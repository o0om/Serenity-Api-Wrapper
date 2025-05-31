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

from typing import (
    Any,
    ClassVar,
    Dict,
    Optional,
    Protocol,
    Type,
    TypeVar,
    Union,
    runtime_checkable,
)

from .utils import MISSING

# fmt: off
__all__ = (
    'EqualityComparable',
    'Hashable',
    'Comparable',
)
# fmt: on

T = TypeVar('T', bound='EqualityComparable')

@runtime_checkable
class Comparable(Protocol):
    """A protocol for objects that can be compared.
    
    This protocol defines the interface for objects that can be compared
    using the standard comparison operators.
    
    .. versionadded:: 2.5
    """
    
    def __lt__(self, other: Any) -> bool:
        """Less than comparison."""
        ...
    
    def __le__(self, other: Any) -> bool:
        """Less than or equal comparison."""
        ...
    
    def __gt__(self, other: Any) -> bool:
        """Greater than comparison."""
        ...
    
    def __ge__(self, other: Any) -> bool:
        """Greater than or equal comparison."""
        ...

class EqualityComparable:
    """A mixin that implements equality comparison for objects.
    
    This mixin provides a default implementation of equality comparison
    based on the object's ID. Classes that inherit from this mixin should
    have an `id` attribute.
    
    .. versionchanged:: 2.5
        Added type hints and improved documentation.
        Added support for comparing with None.
        Added utility methods for comparison.
    
    Attributes
    -----------
    id: :class:`int`
        The ID of the object.
    """
    
    __slots__ = ()
    
    id: int
    
    def __eq__(self, other: object) -> bool:
        """Checks if two objects are equal.
        
        Parameters
        -----------
        other: :class:`object`
            The object to compare with.
            
        Returns
        --------
        :class:`bool`
            Whether the objects are equal.
        """
        if isinstance(other, self.__class__):
            return other.id == self.id
        return NotImplemented
    
    def __ne__(self, other: object) -> bool:
        """Checks if two objects are not equal.
        
        Parameters
        -----------
        other: :class:`object`
            The object to compare with.
            
        Returns
        --------
        :class:`bool`
            Whether the objects are not equal.
        """
        if isinstance(other, self.__class__):
            return other.id != self.id
        return NotImplemented
    
    def is_same(self, other: Optional[Union['EqualityComparable', int]]) -> bool:
        """Checks if this object is the same as another object or ID.
        
        Parameters
        -----------
        other: Optional[Union[:class:`EqualityComparable`, :class:`int`]]
            The object or ID to compare with.
            
        Returns
        --------
        :class:`bool`
            Whether the objects are the same.
        """
        if other is None:
            return False
        if isinstance(other, int):
            return self.id == other
        return self.id == other.id
    
    def copy(self: T) -> T:
        """Creates a copy of this object.
        
        Returns
        --------
        :class:`EqualityComparable`
            The copied object.
        """
        return self.__class__(id=self.id)


class Hashable(EqualityComparable):
    """A mixin that implements hashability for objects.
    
    This mixin provides a default implementation of hashability based on
    the object's ID. Classes that inherit from this mixin should have an
    `id` attribute.
    
    .. versionchanged:: 2.5
        Added type hints and improved documentation.
        Added utility methods for hashing.
        Added caching for hash values.
    
    Attributes
    -----------
    id: :class:`int`
        The ID of the object.
    """
    
    __slots__ = ('_hash',)
    
    _hash: Optional[int]
    
    def __init__(self) -> None:
        self._hash = None
    
    def __hash__(self) -> int:
        """Returns the hash of the object.
        
        Returns
        --------
        :class:`int`
            The hash of the object.
        """
        if self._hash is None:
            self._hash = self.id >> 22
        return self._hash
    
    def clear_hash(self) -> None:
        """Clears the cached hash value.
        
        This method should be called when the object's ID changes.
        """
        self._hash = None
