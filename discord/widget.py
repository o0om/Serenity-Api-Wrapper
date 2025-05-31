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

from typing import List, Optional, TYPE_CHECKING, Union, Dict, Set
from datetime import datetime
import time
from dataclasses import dataclass, field

from .utils import snowflake_time, _get_as_snowflake, resolve_invite
from .user import BaseUser
from .activity import BaseActivity, Spotify, create_activity
from .invite import Invite
from .enums import Status, try_enum
from .errors import HTTPException, Forbidden, NotFound

if TYPE_CHECKING:
    from .state import ConnectionState
    from .types.widget import (
        WidgetMember as WidgetMemberPayload,
        Widget as WidgetPayload,
    )

__all__ = (
    'WidgetChannel',
    'WidgetMember',
    'Widget',
    'WidgetStats',
)


@dataclass
class WidgetStats:
    """Statistics for a widget.
    
    Attributes
    ----------
    member_count: int
        Number of members in the widget.
    channel_count: int
        Number of channels in the widget.
    last_update: float
        Timestamp of the last widget update.
    """
    member_count: int = 0
    channel_count: int = 0
    last_update: float = field(default_factory=time.time)
    
    def update(self, member_count: int, channel_count: int) -> None:
        """Updates the widget statistics.
        
        Parameters
        ----------
        member_count: int
            The new member count.
        channel_count: int
            The new channel count.
        """
        self.member_count = member_count
        self.channel_count = channel_count
        self.last_update = time.time()


class WidgetChannel:
    """Represents a "partial" widget channel.

    .. container:: operations

        .. describe:: x == y

            Checks if two partial channels are the same.

        .. describe:: x != y

            Checks if two partial channels are not the same.

        .. describe:: hash(x)

            Return the partial channel's hash.

        .. describe:: str(x)

            Returns the partial channel's name.

    Attributes
    -----------
    id: :class:`int`
        The channel's ID.
    name: :class:`str`
        The channel's name.
    position: :class:`int`
        The channel's position
    """

    __slots__ = ('id', 'name', 'position', '_created_at')

    def __init__(self, id: int, name: str, position: int) -> None:
        self.id: int = id
        self.name: str = name
        self.position: int = position
        self._created_at: Optional[datetime] = None

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f'<WidgetChannel id={self.id} name={self.name!r} position={self.position!r}>'

    @property
    def mention(self) -> str:
        """:class:`str`: The string that allows you to mention the channel."""
        return f'<#{self.id}>'

    @property
    def created_at(self) -> datetime:
        """:class:`datetime.datetime`: Returns the channel's creation time in UTC."""
        if self._created_at is None:
            self._created_at = snowflake_time(self.id)
        return self._created_at


class WidgetMember(BaseUser):
    """Represents a "partial" member of the widget's guild.

    .. container:: operations

        .. describe:: x == y

            Checks if two widget members are the same.

        .. describe:: x != y

            Checks if two widget members are not the same.

        .. describe:: hash(x)

            Return the widget member's hash.

        .. describe:: str(x)

            Returns the widget member's handle (e.g. ``name`` or ``name#discriminator``).

    Attributes
    -----------
    id: :class:`int`
        The member's ID.
    name: :class:`str`
        The member's username.
    discriminator: :class:`str`
        The member's discriminator. This is a legacy concept that is no longer used.
    global_name: Optional[:class:`str`]
        The member's global nickname, taking precedence over the username in display.

        .. versionadded:: 2.3
    bot: :class:`bool`
        Whether the member is a bot.
    status: :class:`Status`
        The member's status.
    nick: Optional[:class:`str`]
        The member's guild-specific nickname. Takes precedence over the global name.
    avatar: Optional[:class:`str`]
        The member's avatar hash.
    activity: Optional[Union[:class:`BaseActivity`, :class:`Spotify`]]
        The member's activity.
    deafened: Optional[:class:`bool`]
        Whether the member is currently deafened.
    muted: Optional[:class:`bool`]
        Whether the member is currently muted.
    suppress: Optional[:class:`bool`]
        Whether the member is currently being suppressed.
    connected_channel: Optional[:class:`WidgetChannel`]
        Which channel the member is connected to.
    """

    __slots__ = (
        'status',
        'nick',
        'avatar',
        'activity',
        'deafened',
        'suppress',
        'muted',
        'connected_channel',
        '_last_update',
    )

    if TYPE_CHECKING:
        activity: Optional[Union[BaseActivity, Spotify]]

    def __init__(
        self,
        *,
        state: ConnectionState,
        data: WidgetMemberPayload,
        connected_channel: Optional[WidgetChannel] = None,
    ) -> None:
        super().__init__(state=state, data=data)
        self.nick: Optional[str] = data.get('nick')
        self.status: Status = try_enum(Status, data.get('status'))
        self.deafened: Optional[bool] = data.get('deaf', False) or data.get('self_deaf', False)
        self.muted: Optional[bool] = data.get('mute', False) or data.get('self_mute', False)
        self.suppress: Optional[bool] = data.get('suppress', False)
        self._last_update: float = time.time()

        try:
            game = data['game']
        except KeyError:
            activity = None
        else:
            activity = create_activity(game, state)

        self.activity: Optional[Union[BaseActivity, Spotify]] = activity
        self.connected_channel: Optional[WidgetChannel] = connected_channel

    def __repr__(self) -> str:
        return f"<WidgetMember name={self.name!r} global_name={self.global_name!r}" f" bot={self.bot} nick={self.nick!r}>"

    @property
    def display_name(self) -> str:
        """:class:`str`: Returns the member's display name."""
        return self.nick or self.name

    def update(self, data: WidgetMemberPayload) -> None:
        """Updates the member's data.
        
        Parameters
        ----------
        data: :class:`WidgetMemberPayload`
            The new member data.
        """
        self.nick = data.get('nick')
        self.status = try_enum(Status, data.get('status'))
        self.deafened = data.get('deaf', False) or data.get('self_deaf', False)
        self.muted = data.get('mute', False) or data.get('self_mute', False)
        self.suppress = data.get('suppress', False)
        self._last_update = time.time()

        try:
            game = data['game']
        except KeyError:
            self.activity = None
        else:
            self.activity = create_activity(game, self._state)


class Widget:
    """Represents a :class:`Guild` widget.

    .. container:: operations

        .. describe:: x == y

            Checks if two widgets are the same.

        .. describe:: x != y

            Checks if two widgets are not the same.

        .. describe:: str(x)

            Returns the widget's JSON URL.

    Attributes
    -----------
    id: :class:`int`
        The guild's ID.
    name: :class:`str`
        The guild's name.
    channels: List[:class:`WidgetChannel`]
        The accessible voice channels in the guild.
    members: List[:class:`WidgetMember`]
        The online members in the guild. Offline members
        do not appear in the widget.

        .. note::

            Due to a Discord limitation, if this data is available
            the users will be "anonymized" with linear IDs and discriminator
            information being incorrect. Likewise, the number of members
            retrieved is capped.
    presence_count: :class:`int`
        The approximate number of online members in the guild.
        Offline members are not included in this count.

        .. versionadded:: 2.0
    stats: :class:`WidgetStats`
        Statistics for this widget.

        .. versionadded:: 2.4
    """

    __slots__ = (
        '_state',
        'channels',
        '_invite',
        'id',
        'members',
        'name',
        'presence_count',
        'stats',
        '_last_update',
        '_update_interval',
        '_member_cache',
    )

    def __init__(self, *, state: ConnectionState, data: WidgetPayload) -> None:
        self._state: ConnectionState = state
        self._member_cache: Dict[int, WidgetMember] = {}
        self._last_update: float = 0.0
        self._update_interval: float = 300.0  # 5 minutes
        self.stats: WidgetStats = WidgetStats()
        self._from_data(data)

    def _from_data(self, data: WidgetPayload) -> None:
        self.id: int = int(data['id'])
        self.name: str = data['name']
        self.channels: List[WidgetChannel] = []
        self.members: List[WidgetMember] = []
        self.presence_count: int = data.get('presence_count', 0)

        for channel_data in data.get('channels', []):
            _id = int(channel_data['id'])
            channel = WidgetChannel(id=_id, name=channel_data['name'], position=channel_data['position'])
            self.channels.append(channel)

        for member_data in data.get('members', []):
            connected_channel = None
            if channel_id := _get_as_snowflake(member_data, 'channel_id'):
                connected_channel = discord.utils.get(self.channels, id=channel_id)

            member = WidgetMember(state=self._state, data=member_data, connected_channel=connected_channel)
            self.members.append(member)
            self._member_cache[member.id] = member

        # Update stats
        self.stats.update(len(self.members), len(self.channels))
        self._last_update = time.time()

    def _should_update(self) -> bool:
        """Checks if the widget should be updated."""
        return time.time() - self._last_update > self._update_interval

    def __str__(self) -> str:
        return self.json_url

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Widget):
            return NotImplemented
        return self.id == other.id

    def __repr__(self) -> str:
        return f'<Widget id={self.id} name={self.name!r} channels={len(self.channels)} members={len(self.members)}>'

    @property
    def created_at(self) -> datetime:
        """:class:`datetime.datetime`: Returns the guild's creation time in UTC."""
        return snowflake_time(self.id)

    @property
    def json_url(self) -> str:
        """:class:`str`: The JSON URL of the widget."""
        return f'https://discord.com/api/guilds/{self.id}/widget.json'

    @property
    def invite_url(self) -> Optional[str]:
        """Optional[:class:`str`]: The invite URL for the guild, if available."""
        if self._invite is None:
            return None
        return self._invite.url

    async def fetch_invite(self, *, with_counts: bool = True) -> Optional[Invite]:
        """|coro|

        Retrieves an :class:`Invite` with a widget channel. The channel
        must be a widget channel.

        Parameters
        -----------
        with_counts: :class:`bool`
            Whether to include count information in the invite. This fills the
            :attr:`Invite.approximate_member_count` and :attr:`Invite.approximate_presence_count`
            fields.

        Raises
        -------
        Forbidden
            The widget for this guild is disabled.
        HTTPException
            Retrieving the invite failed.
        NotFound
            The widget channel is not found.

        Returns
        --------
        Optional[:class:`Invite`]
            The invite for the widget channel.
        """
        if not self.channels:
            return None

        try:
            invite = await self._state.http.get_widget_invite(self.id, with_counts=with_counts)
            return Invite(state=self._state, data=invite)
        except HTTPException as e:
            if e.code == 50001:  # Missing Access
                raise Forbidden("The widget for this guild is disabled.") from e
            if e.code == 10003:  # Unknown Channel
                raise NotFound("The widget channel was not found.") from e
            raise

    async def refresh(self) -> None:
        """|coro|

        Refreshes the widget data.

        Raises
        -------
        Forbidden
            The widget for this guild is disabled.
        HTTPException
            Refreshing the widget failed.
        """
        try:
            data = await self._state.http.get_widget(self.id)
            self._from_data(data)
        except HTTPException as e:
            if e.code == 50001:  # Missing Access
                raise Forbidden("The widget for this guild is disabled.") from e
            raise
