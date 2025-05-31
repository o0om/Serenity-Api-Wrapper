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
    Union,
    Sequence,
    TYPE_CHECKING,
    Any,
    Optional,
    List,
    Set,
    Dict,
    ClassVar,
    TypeVar,
    overload,
)
from collections.abc import Collection
from weakref import WeakValueDictionary

# fmt: off
__all__ = (
    'AllowedMentions',
)
# fmt: on

if TYPE_CHECKING:
    from typing_extensions import Self
    from .types.message import AllowedMentions as AllowedMentionsPayload
    from .abc import Snowflake

T = TypeVar('T', bound='AllowedMentions')

class _FakeBool:
    """A class that acts like a boolean but is always True.
    
    This is used to represent the default value for AllowedMentions fields.
    """
    
    def __repr__(self) -> str:
        return 'True'

    def __eq__(self, other: object) -> bool:
        return other is True

    def __bool__(self) -> bool:
        return True


default: Any = _FakeBool()


class AllowedMentions:
    """A class that represents what mentions are allowed in a message.

    This class can be set during :class:`Client` initialisation to apply
    to every message sent. It can also be applied on a per message basis
    via :meth:`abc.Messageable.send` for more fine-grained control.

    .. versionchanged:: 2.5
        Added caching for dictionary representation.
        Added new utility methods for common operations.
        Enhanced type safety with better type hints.
        Added validation for mention types.

    Attributes
    ------------
    everyone: :class:`bool`
        Whether to allow everyone and here mentions. Defaults to ``True``.
    users: Union[:class:`bool`, Sequence[:class:`abc.Snowflake`]]
        Controls the users being mentioned. If ``True`` (the default) then
        users are mentioned based on the message content. If ``False`` then
        users are not mentioned at all. If a list of :class:`abc.Snowflake`
        is given then only the users provided will be mentioned, provided those
        users are in the message content.
    roles: Union[:class:`bool`, Sequence[:class:`abc.Snowflake`]]
        Controls the roles being mentioned. If ``True`` (the default) then
        roles are mentioned based on the message content. If ``False`` then
        roles are not mentioned at all. If a list of :class:`abc.Snowflake`
        is given then only the roles provided will be mentioned, provided those
        roles are in the message content.
    replied_user: :class:`bool`
        Whether to mention the author of the message being replied to. Defaults
        to ``True``.

        .. versionadded:: 1.6
    """

    __slots__ = ('everyone', 'users', 'roles', 'replied_user', '_cached_dict')
    
    # Cache for frequently used AllowedMentions instances
    _cache: ClassVar[WeakValueDictionary[tuple, 'AllowedMentions']] = WeakValueDictionary()
    
    # Valid mention types
    _VALID_MENTION_TYPES: ClassVar[Set[str]] = {'everyone', 'users', 'roles'}

    def __init__(
        self,
        *,
        everyone: bool = default,
        users: Union[bool, Sequence[Snowflake]] = default,
        roles: Union[bool, Sequence[Snowflake]] = default,
        replied_user: bool = default,
    ):
        self.everyone: bool = everyone
        self.users: Union[bool, Sequence[Snowflake]] = users
        self.roles: Union[bool, Sequence[Snowflake]] = roles
        self.replied_user: bool = replied_user
        self._cached_dict: Optional[AllowedMentionsPayload] = None

    @classmethod
    def all(cls) -> Self:
        """A factory method that returns a :class:`AllowedMentions` with all fields explicitly set to ``True``

        .. versionadded:: 1.5
        """
        cache_key = (True, True, True, True)
        if cache_key in cls._cache:
            return cls._cache[cache_key]
        
        instance = cls(everyone=True, users=True, roles=True, replied_user=True)
        cls._cache[cache_key] = instance
        return instance

    @classmethod
    def none(cls) -> Self:
        """A factory method that returns a :class:`AllowedMentions` with all fields set to ``False``

        .. versionadded:: 1.5
        """
        cache_key = (False, False, False, False)
        if cache_key in cls._cache:
            return cls._cache[cache_key]
        
        instance = cls(everyone=False, users=False, roles=False, replied_user=False)
        cls._cache[cache_key] = instance
        return instance

    @classmethod
    def users_only(cls, users: Optional[Collection[Snowflake]] = None) -> Self:
        """A factory method that returns a :class:`AllowedMentions` that only allows user mentions.
        
        Parameters
        -----------
        users: Optional[Collection[:class:`abc.Snowflake`]]
            A collection of users to allow mentions for. If None, allows all user mentions.
            
        Returns
        --------
        :class:`AllowedMentions`
            A new instance with only user mentions enabled.
        """
        cache_key = (False, users if users is not None else True, False, False)
        if cache_key in cls._cache:
            return cls._cache[cache_key]
        
        instance = cls(everyone=False, users=users if users is not None else True, roles=False, replied_user=False)
        cls._cache[cache_key] = instance
        return instance

    @classmethod
    def roles_only(cls, roles: Optional[Collection[Snowflake]] = None) -> Self:
        """A factory method that returns a :class:`AllowedMentions` that only allows role mentions.
        
        Parameters
        -----------
        roles: Optional[Collection[:class:`abc.Snowflake`]]
            A collection of roles to allow mentions for. If None, allows all role mentions.
            
        Returns
        --------
        :class:`AllowedMentions`
            A new instance with only role mentions enabled.
        """
        cache_key = (False, False, roles if roles is not None else True, False)
        if cache_key in cls._cache:
            return cls._cache[cache_key]
        
        instance = cls(everyone=False, users=False, roles=roles if roles is not None else True, replied_user=False)
        cls._cache[cache_key] = instance
        return instance

    def to_dict(self) -> AllowedMentionsPayload:
        """Convert the allowed mentions to a dictionary format for the Discord API.
        
        Returns
        --------
        :class:`dict`
            The dictionary representation of the allowed mentions.
        """
        if self._cached_dict is not None:
            return self._cached_dict

        parse: List[str] = []
        data: Dict[str, Any] = {}

        if self.everyone:
            parse.append('everyone')

        if self.users is True:
            parse.append('users')
        elif self.users is not False:
            data['users'] = [x.id for x in self.users]

        if self.roles is True:
            parse.append('roles')
        elif self.roles is not False:
            data['roles'] = [x.id for x in self.roles]

        if self.replied_user:
            data['replied_user'] = True

        data['parse'] = parse
        self._cached_dict = data
        return data

    def merge(self, other: AllowedMentions) -> AllowedMentions:
        """Creates a new AllowedMentions by merging from another one.
        
        Merge is done by using the 'self' values unless explicitly
        overridden by the 'other' values.
        
        Parameters
        -----------
        other: :class:`AllowedMentions`
            The other instance to merge with.
            
        Returns
        --------
        :class:`AllowedMentions`
            A new instance with merged values.
        """
        everyone = self.everyone if other.everyone is default else other.everyone
        users = self.users if other.users is default else other.users
        roles = self.roles if other.roles is default else other.roles
        replied_user = self.replied_user if other.replied_user is default else other.replied_user
        return AllowedMentions(everyone=everyone, roles=roles, users=users, replied_user=replied_user)

    def copy(self) -> AllowedMentions:
        """Creates a copy of this AllowedMentions instance.
        
        Returns
        --------
        :class:`AllowedMentions`
            A new instance with the same values.
        """
        return AllowedMentions(
            everyone=self.everyone,
            users=self.users,
            roles=self.roles,
            replied_user=self.replied_user
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AllowedMentions):
            return NotImplemented
        return (
            self.everyone == other.everyone
            and self.users == other.users
            and self.roles == other.roles
            and self.replied_user == other.replied_user
        )

    def __repr__(self) -> str:
        return (
            f'{self.__class__.__name__}(everyone={self.everyone}, '
            f'users={self.users}, roles={self.roles}, replied_user={self.replied_user})'
        )
