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

from typing import Any, Dict, Optional, TYPE_CHECKING, Union, ClassVar, Set, Tuple
import re
from weakref import WeakValueDictionary

from .asset import Asset, AssetMixin
from . import utils

# fmt: off
__all__ = (
    'PartialEmoji',
)
# fmt: on

if TYPE_CHECKING:
    from typing_extensions import Self

    from .state import ConnectionState
    from datetime import datetime
    from .types.emoji import Emoji as EmojiPayload, PartialEmoji as PartialEmojiPayload
    from .types.activity import ActivityEmoji

# Cache for frequently used emoji combinations
_EMOJI_CACHE: WeakValueDictionary[Tuple[bool, str, Optional[int]], 'PartialEmoji'] = WeakValueDictionary()

class _EmojiTag:
    __slots__ = ()

    id: int

    def _to_partial(self) -> PartialEmoji:
        raise NotImplementedError


class PartialEmoji(_EmojiTag, AssetMixin):
    """Represents a "partial" emoji.

    This model will be given in two scenarios:

    - "Raw" data events such as :func:`on_raw_reaction_add`
    - Custom emoji that the bot cannot see from e.g. :attr:`Message.reactions`

    .. versionchanged:: 2.5
        Added caching for frequently used emoji combinations.
        Added new utility methods for common emoji operations.
        Enhanced type safety with better type hints.

    .. container:: operations

        .. describe:: x == y

            Checks if two emoji are the same.

        .. describe:: x != y

            Checks if two emoji are not the same.

        .. describe:: hash(x)

            Return the emoji's hash.

        .. describe:: str(x)

            Returns the emoji rendered for discord.

    Attributes
    -----------
    name: Optional[:class:`str`]
        The custom emoji name, if applicable, or the unicode codepoint
        of the non-custom emoji. This can be ``None`` if the emoji
        got deleted (e.g. removing a reaction with a deleted emoji).
    animated: :class:`bool`
        Whether the emoji is animated or not.
    id: Optional[:class:`int`]
        The ID of the custom emoji, if applicable.
    """

    __slots__ = ('animated', 'name', 'id', '_state')

    _CUSTOM_EMOJI_RE: ClassVar[re.Pattern] = re.compile(r'<?(?:(?P<animated>a)?:)?(?P<name>[A-Za-z0-9\_]+):(?P<id>[0-9]{13,20})>?')
    _VALID_EMOJI_NAME_RE: ClassVar[re.Pattern] = re.compile(r'^[A-Za-z0-9\_]+$')

    if TYPE_CHECKING:
        id: Optional[int]

    def __init__(self, *, name: str, animated: bool = False, id: Optional[int] = None):
        self.animated: bool = animated
        self.name: str = name
        self.id: Optional[int] = id
        self._state: Optional[ConnectionState] = None

    def __new__(cls, *, name: str, animated: bool = False, id: Optional[int] = None) -> 'PartialEmoji':
        # Check cache for frequently used emoji combinations
        cache_key = (animated, name, id)
        if cache_key in _EMOJI_CACHE:
            return _EMOJI_CACHE[cache_key]
        
        instance = super().__new__(cls)
        instance.animated = animated
        instance.name = name
        instance.id = id
        instance._state = None
        
        # Cache the instance
        _EMOJI_CACHE[cache_key] = instance
        
        return instance

    @classmethod
    def from_dict(cls, data: Union[PartialEmojiPayload, ActivityEmoji, Dict[str, Any]]) -> Self:
        """Creates a :class:`PartialEmoji` from a dictionary.

        Parameters
        -----------
        data: Union[:class:`PartialEmojiPayload`, :class:`ActivityEmoji`, Dict[:class:`str`, Any]]
            The dictionary to create the emoji from.

        Returns
        --------
        :class:`PartialEmoji`
            The created partial emoji.
        """
        return cls(
            animated=data.get('animated', False),
            id=utils._get_as_snowflake(data, 'id'),
            name=data.get('name') or '',
        )

    @classmethod
    def from_str(cls, value: str) -> Self:
        """Converts a Discord string representation of an emoji to a :class:`PartialEmoji`.

        The formats accepted are:

        - ``a:name:id``
        - ``<a:name:id>``
        - ``name:id``
        - ``<:name:id>``

        If the format does not match then it is assumed to be a unicode emoji.

        .. versionadded:: 2.0

        Parameters
        ------------
        value: :class:`str`
            The string representation of an emoji.

        Returns
        --------
        :class:`PartialEmoji`
            The partial emoji from this string.

        Raises
        -------
        ValueError
            If the emoji name is invalid.
        """
        match = cls._CUSTOM_EMOJI_RE.match(value)
        if match is not None:
            groups = match.groupdict()
            animated = bool(groups['animated'])
            emoji_id = int(groups['id'])
            name = groups['name']
            
            # Validate emoji name
            if not cls._VALID_EMOJI_NAME_RE.match(name):
                raise ValueError(f'Invalid emoji name: {name}')
                
            return cls(name=name, animated=animated, id=emoji_id)

        return cls(name=value, id=None, animated=False)

    def to_dict(self) -> EmojiPayload:
        """Converts the emoji to a dictionary.

        Returns
        --------
        :class:`EmojiPayload`
            The dictionary representation of the emoji.
        """
        payload: EmojiPayload = {
            'id': self.id,
            'name': self.name,
        }

        if self.animated:
            payload['animated'] = self.animated

        return payload

    def _to_partial(self) -> PartialEmoji:
        return self

    def _to_forum_tag_payload(self) -> Dict[str, Any]:
        if self.id is not None:
            return {'emoji_id': self.id, 'emoji_name': None}
        return {'emoji_id': None, 'emoji_name': self.name}

    @classmethod
    def with_state(
        cls,
        state: ConnectionState,
        *,
        name: str,
        animated: bool = False,
        id: Optional[int] = None,
    ) -> Self:
        """Creates a :class:`PartialEmoji` with a state.

        Parameters
        -----------
        state: :class:`ConnectionState`
            The state to use.
        name: :class:`str`
            The name of the emoji.
        animated: :class:`bool`
            Whether the emoji is animated.
        id: Optional[:class:`int`]
            The ID of the emoji.

        Returns
        --------
        :class:`PartialEmoji`
            The created partial emoji.
        """
        self = cls(name=name, animated=animated, id=id)
        self._state = state
        return self

    def __str__(self) -> str:
        # Coerce empty names to _ so it renders in the client regardless of having no name
        name = self.name or '_'
        if self.id is None:
            return name
        if self.animated:
            return f'<a:{name}:{self.id}>'
        return f'<:{name}:{self.id}>'

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} animated={self.animated} name={self.name!r} id={self.id}>'

    def __eq__(self, other: object) -> bool:
        if self.is_unicode_emoji():
            return isinstance(other, PartialEmoji) and self.name == other.name

        if isinstance(other, _EmojiTag):
            return self.id == other.id
        return False

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((self.id, self.name))

    def is_custom_emoji(self) -> bool:
        """:class:`bool`: Checks if this is a custom non-Unicode emoji."""
        return self.id is not None

    def is_unicode_emoji(self) -> bool:
        """:class:`bool`: Checks if this is a Unicode emoji."""
        return self.id is None

    def _as_reaction(self) -> str:
        if self.id is None:
            return self.name
        return f'{self.name}:{self.id}'

    @property
    def created_at(self) -> Optional[datetime]:
        """Optional[:class:`datetime.datetime`]: Returns the emoji's creation time in UTC, or None if Unicode emoji.

        .. versionadded:: 1.6
        """
        if self.id is None:
            return None

        return utils.snowflake_time(self.id)

    @property
    def url(self) -> str:
        """:class:`str`: Returns the URL of the emoji, if it is custom.

        If this isn't a custom emoji then an empty string is returned
        """
        if self.is_unicode_emoji():
            return ''

        fmt = 'gif' if self.animated else 'png'
        return f'{Asset.BASE}/emojis/{self.id}.{fmt}'

    async def read(self) -> bytes:
        """|coro|

        Retrieves the content of this asset as a :class:`bytes` object.

        Raises
        ------
        DiscordException
            There was no internal connection state.
        HTTPException
            Downloading the asset failed.
        NotFound
            The asset was deleted.
        ValueError
            The PartialEmoji is not a custom emoji.

        Returns
        -------
        :class:`bytes`
            The content of the asset.
        """
        if self.is_unicode_emoji():
            raise ValueError('PartialEmoji is not a custom emoji')

        return await super().read()

    def validate(self) -> bool:
        """Validates the emoji.

        Returns
        --------
        :class:`bool`
            Whether the emoji is valid.
        """
        if self.is_unicode_emoji():
            return bool(self.name)
        return bool(self.name and self.id and self._VALID_EMOJI_NAME_RE.match(self.name))

    def to_unicode(self) -> str:
        """Converts the emoji to a Unicode representation.

        Returns
        --------
        :class:`str`
            The Unicode representation of the emoji.
        """
        if self.is_unicode_emoji():
            return self.name
        return str(self)

    def to_custom_emoji(self) -> Optional['PartialEmoji']:
        """Converts the emoji to a custom emoji if possible.

        Returns
        --------
        Optional[:class:`PartialEmoji`]
            The custom emoji representation, or None if not possible.
        """
        if self.is_custom_emoji():
            return self
        return None
