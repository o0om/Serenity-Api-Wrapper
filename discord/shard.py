"""
The MIT License (MIT)

Copyright (c) 2015-present Serenity

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

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Tuple, Type, Optional, List, Dict, Set, Union
from dataclasses import dataclass, field

import aiohttp
import yarl

from .state import AutoShardedConnectionState
from .client import Client
from .backoff import ExponentialBackoff
from .gateway import *
from .errors import (
    ClientException,
    HTTPException,
    GatewayNotFound,
    ConnectionClosed,
    PrivilegedIntentsRequired,
    ShardError,
)
from .enums import Status
from .utils import MISSING

if TYPE_CHECKING:
    from .gateway import DiscordWebSocket
    from .activity import BaseActivity
    from .flags import Intents

__all__ = (
    'AutoShardedClient',
    'ShardInfo',
    'ShardStats',
)

_log = logging.getLogger(__name__)


class EventType:
    """Event types for shard management."""
    close = 0
    reconnect = 1
    resume = 2
    identify = 3
    terminate = 4
    clean_close = 5


@dataclass
class ShardStats:
    """Statistics for a shard connection.
    
    Attributes
    ----------
    events_received: int
        Number of events received by the shard.
    events_sent: int
        Number of events sent by the shard.
    reconnects: int
        Number of reconnection attempts.
    latency: float
        Current latency of the shard in seconds.
    last_heartbeat: float
        Timestamp of the last heartbeat.
    last_heartbeat_ack: float
        Timestamp of the last heartbeat acknowledgment.
    """
    events_received: int = 0
    events_sent: int = 0
    reconnects: int = 0
    latency: float = 0.0
    last_heartbeat: float = 0.0
    last_heartbeat_ack: float = 0.0
    _last_update: float = field(default_factory=time.time)
    
    def update_latency(self, latency: float) -> None:
        """Updates the latency with exponential moving average.
        
        Parameters
        ----------
        latency: float
            The new latency measurement.
        """
        alpha = 0.1  # Smoothing factor
        self.latency = (alpha * latency) + ((1 - alpha) * self.latency)
        self._last_update = time.time()
    
    def update_heartbeat(self) -> None:
        """Updates the last heartbeat timestamp."""
        self.last_heartbeat = time.time()
    
    def update_heartbeat_ack(self) -> None:
        """Updates the last heartbeat acknowledgment timestamp."""
        self.last_heartbeat_ack = time.time()
        if self.last_heartbeat > 0:
            self.update_latency(self.last_heartbeat_ack - self.last_heartbeat)


class EventItem:
    """Represents a shard event.
    
    Attributes
    ----------
    type: int
        The type of event.
    shard: Optional[Shard]
        The shard associated with the event.
    error: Optional[Exception]
        Any error associated with the event.
    """
    __slots__ = ('type', 'shard', 'error')

    def __init__(self, etype: int, shard: Optional['Shard'], error: Optional[Exception]) -> None:
        self.type: int = etype
        self.shard: Optional['Shard'] = shard
        self.error: Optional[Exception] = error

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, EventItem):
            return NotImplemented
        return self.type < other.type

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EventItem):
            return NotImplemented
        return self.type == other.type

    def __hash__(self) -> int:
        return hash(self.type)


class Shard:
    """Represents a Discord shard connection.
    
    Attributes
    ----------
    ws: DiscordWebSocket
        The websocket connection for this shard.
    stats: ShardStats
        Statistics for this shard.
    """
    
    def __init__(self, ws: DiscordWebSocket, client: AutoShardedClient, queue_put: Callable[[EventItem], None]) -> None:
        self.ws: DiscordWebSocket = ws
        self._client: Client = client
        self._dispatch: Callable[..., None] = client.dispatch
        self._queue_put: Callable[[EventItem], None] = queue_put
        self._disconnect: bool = False
        self._reconnect = client._reconnect
        self._backoff: ExponentialBackoff = ExponentialBackoff()
        self._task: Optional[asyncio.Task] = None
        self.stats: ShardStats = ShardStats()
        self._handled_exceptions: Tuple[Type[Exception], ...] = (
            OSError,
            HTTPException,
            GatewayNotFound,
            ConnectionClosed,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        )
        self._last_heartbeat: float = 0.0
        self._heartbeat_timeout: float = 30.0
        self._reconnect_timeout: float = 60.0
        self._max_reconnects: int = 5
        self._reconnect_count: int = 0

    @property
    def id(self) -> int:
        """The shard ID."""
        return self.ws.shard_id  # type: ignore

    @property
    def latency(self) -> float:
        """The current latency of the shard in seconds."""
        return self.stats.latency

    @property
    def is_healthy(self) -> bool:
        """Whether the shard connection is healthy."""
        if not self.ws.open:
            return False
        if time.time() - self._last_heartbeat > self._heartbeat_timeout:
            return False
        return True

    def launch(self) -> None:
        """Launches the shard worker task."""
        self._task = self._client.loop.create_task(self.worker())

    def _cancel_task(self) -> None:
        """Cancels the worker task if it exists."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def close(self) -> None:
        """Closes the shard connection."""
        self._cancel_task()
        await self.ws.close(code=1000)

    async def disconnect(self) -> None:
        """Disconnects the shard and dispatches the disconnect event."""
        await self.close()
        self._dispatch('shard_disconnect', self.id)

    async def _handle_disconnect(self, e: Exception) -> None:
        """Handles a disconnection event.
        
        Parameters
        ----------
        e: Exception
            The exception that caused the disconnection.
        """
        self._dispatch('disconnect')
        self._dispatch('shard_disconnect', self.id)
        
        if not self._reconnect:
            self._queue_put(EventItem(EventType.close, self, e))
            return

        if self._client.is_closed():
            return

        if isinstance(e, OSError) and e.errno in (54, 10054):
            # If we get Connection reset by peer then always try to RESUME the connection.
            exc = ReconnectWebSocket(self.id, resume=True)
            self._queue_put(EventItem(EventType.resume, self, exc))
            return

        if isinstance(e, ConnectionClosed):
            if e.code == 4014:
                self._queue_put(EventItem(EventType.terminate, self, PrivilegedIntentsRequired(self.id)))
                return
            if e.code != 1000:
                self._queue_put(EventItem(EventType.close, self, e))
                return

        self._reconnect_count += 1
        if self._reconnect_count > self._max_reconnects:
            self._queue_put(EventItem(EventType.terminate, self, ShardError(f"Max reconnects ({self._max_reconnects}) exceeded")))
            return

        retry = self._backoff.delay()
        _log.error('Attempting a reconnect for shard ID %s in %.2fs (attempt %d/%d)', 
                  self.id, retry, self._reconnect_count, self._max_reconnects, exc_info=e)
        await asyncio.sleep(retry)
        self._queue_put(EventItem(EventType.reconnect, self, e))

    async def worker(self) -> None:
        """The main worker task for the shard."""
        while not self._client.is_closed():
            try:
                await self.ws.poll_event()
                self.stats.events_received += 1
            except ReconnectWebSocket as e:
                etype = EventType.resume if e.resume else EventType.identify
                self._queue_put(EventItem(etype, self, e))
                break
            except self._handled_exceptions as e:
                await self._handle_disconnect(e)
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._queue_put(EventItem(EventType.terminate, self, e))
                break

    async def reidentify(self, exc: ReconnectWebSocket) -> None:
        """Reidentifies the shard connection.
        
        Parameters
        ----------
        exc: ReconnectWebSocket
            The reconnection exception.
        """
        self._cancel_task()
        self._dispatch('disconnect')
        self._dispatch('shard_disconnect', self.id)
        _log.debug('Got a request to %s the websocket at Shard ID %s.', exc.op, self.id)
        
        try:
            coro = DiscordWebSocket.from_client(
                self._client,
                resume=exc.resume,
                gateway=None if not exc.resume else self.ws.gateway,
                shard_id=self.id,
                session=self.ws.session_id,
                sequence=self.ws.sequence,
            )
            self.ws = await asyncio.wait_for(coro, timeout=self._reconnect_timeout)
            self.stats.reconnects += 1
        except self._handled_exceptions as e:
            await self._handle_disconnect(e)
        except ReconnectWebSocket as e:
            _log.debug('Somehow got a signal to %s while trying to %s shard ID %s.', e.op, exc.op, self.id)
            op = EventType.resume if e.resume else EventType.identify
            self._queue_put(EventItem(op, self, e))
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._queue_put(EventItem(EventType.terminate, self, e))
        else:
            self.launch()

    async def reconnect(self) -> None:
        """Reconnects the shard."""
        self._cancel_task()
        try:
            coro = DiscordWebSocket.from_client(self._client, shard_id=self.id)
            self.ws = await asyncio.wait_for(coro, timeout=self._reconnect_timeout)
            self.stats.reconnects += 1
        except self._handled_exceptions as e:
            await self._handle_disconnect(e)
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._queue_put(EventItem(EventType.terminate, self, e))
        else:
            self.launch()

    def update_heartbeat(self) -> None:
        """Updates the heartbeat timestamp."""
        self._last_heartbeat = time.time()
        self.stats.update_heartbeat()

    def update_heartbeat_ack(self) -> None:
        """Updates the heartbeat acknowledgment timestamp."""
        self.stats.update_heartbeat_ack()


class ShardInfo:
    """A class that gives information and control over a specific shard.

    You can retrieve this object via :meth:`AutoShardedClient.get_shard`
    or :attr:`AutoShardedClient.shards`.

    Attributes
    ------------
    id: :class:`int`
        The shard ID for this shard.
    shard_count: Optional[:class:`int`]
        The shard count for this cluster. If this is ``None`` then the bot has not started yet.
    stats: :class:`ShardStats`
        Statistics for this shard.
    """

    __slots__ = ('_parent', 'id', 'shard_count', 'stats')

    def __init__(self, parent: Shard, shard_count: Optional[int]) -> None:
        self._parent: Shard = parent
        self.id: int = parent.id
        self.shard_count: Optional[int] = shard_count
        self.stats: ShardStats = parent.stats

    def is_closed(self) -> bool:
        """:class:`bool`: Whether the shard connection is currently closed."""
        return not self._parent.ws.open

    def is_healthy(self) -> bool:
        """:class:`bool`: Whether the shard connection is healthy."""
        return self._parent.is_healthy

    @property
    def latency(self) -> float:
        """:class:`float`: The current latency of the shard in seconds."""
        return self._parent.latency

    async def disconnect(self) -> None:
        """|coro|

        Disconnects the shard. When this is called, the shard connection will no
        longer be open.
        """
        await self._parent.disconnect()

    async def reconnect(self) -> None:
        """|coro|

        Disconnects and then reconnects the shard.
        """
        await self._parent.reconnect()

    async def connect(self) -> None:
        """|coro|

        Connects the shard. If the shard is already connected this does nothing.
        """
        if not self.is_closed():
            return
        await self.reconnect()

    def is_ws_ratelimited(self) -> bool:
        """:class:`bool`: Whether the websocket is currently rate limited.

        This can be useful to check when deciding whether you should query members
        using HTTP or through the gateway instead.
        """
        return self._parent.ws.is_ratelimited()


class AutoShardedClient(Client):
    """A client similar to :class:`Client` except it handles the complications
    of sharding for the user into a more manageable and transparent single
    process bot.

    When using this client, you will be able to use it as-if it was a regular
    :class:`Client` with a single shard when implementation wise internally it
    is split up into multiple shards. This allows you to not have to deal with
    IPC or other complicated infrastructure.

    It is recommended to use this client only if you have surpassed at least
    1000 guilds.

    If no :attr:`.shard_count` is provided, then the library will use the
    Bot Gateway endpoint call to figure out how many shards to use.

    If a ``shard_ids`` parameter is given, then those shard IDs will be used
    to launch the internal shards. Note that :attr:`.shard_count` must be provided
    if this is used. By default, when omitted, the client will launch shards from
    0 to ``shard_count - 1``.

    .. container:: operations

        .. describe:: async with x

            Asynchronously initialises the client and automatically cleans up.

            .. versionadded:: 2.0

    Attributes
    ------------
    shard_ids: Optional[List[:class:`int`]]
        An optional list of shard_ids to launch the shards with.
    shard_connect_timeout: Optional[:class:`float`]
        The maximum number of seconds to wait before timing out when launching a shard.
        Defaults to 180 seconds.

        .. versionadded:: 2.4
    """

    if TYPE_CHECKING:
        _connection: AutoShardedConnectionState

    def __init__(self, *args: Any, intents: Intents, **kwargs: Any) -> None:
        kwargs.pop('shard_id', None)
        self.shard_ids: Optional[List[int]] = kwargs.pop('shard_ids', None)
        self.shard_connect_timeout: Optional[float] = kwargs.pop('shard_connect_timeout', 180.0)

        super().__init__(*args, intents=intents, **kwargs)

        if self.shard_ids is not None:
            if self.shard_count is None:
                raise ClientException('When passing manual shard_ids, you must provide a shard_count.')
            elif not isinstance(self.shard_ids, (list, tuple)):
                raise ClientException('shard_ids parameter must be a list or a tuple.')

        # instead of a single websocket, we have multiple
        # the key is the shard_id
        self.__shards = {}
        self._connection._get_websocket = self._get_websocket
        self._connection._get_client = lambda: self

    def _get_websocket(self, guild_id: Optional[int] = None, *, shard_id: Optional[int] = None) -> DiscordWebSocket:
        if shard_id is None:
            # guild_id won't be None if shard_id is None and shard_count won't be None here
            shard_id = (guild_id >> 22) % self.shard_count  # type: ignore
        return self.__shards[shard_id].ws

    def _get_state(self, **options: Any) -> AutoShardedConnectionState:
        return AutoShardedConnectionState(
            dispatch=self.dispatch,
            handlers=self._handlers,
            hooks=self._hooks,
            http=self.http,
            **options,
        )

    @property
    def latency(self) -> float:
        """:class:`float`: Measures latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds.

        This operates similarly to :meth:`Client.latency` except it uses the average
        latency of every shard's latency. To get a list of shard latency, check the
        :attr:`latencies` property. Returns ``nan`` if there are no shards ready.
        """
        if not self.__shards:
            return float('nan')
        return sum(latency for _, latency in self.latencies) / len(self.__shards)

    @property
    def latencies(self) -> List[Tuple[int, float]]:
        """List[Tuple[:class:`int`, :class:`float`]]: A list of latencies between a HEARTBEAT and a HEARTBEAT_ACK in seconds.

        This returns a list of tuples with elements ``(shard_id, latency)``.
        """
        return [(shard_id, shard.ws.latency) for shard_id, shard in self.__shards.items()]

    def get_shard(self, shard_id: int, /) -> Optional[ShardInfo]:
        """
        Gets the shard information at a given shard ID or ``None`` if not found.

        .. versionchanged:: 2.0

            ``shard_id`` parameter is now positional-only.

        Returns
        --------
        Optional[:class:`ShardInfo`]
            Information about the shard with given ID. ``None`` if not found.
        """
        try:
            parent = self.__shards[shard_id]
        except KeyError:
            return None
        else:
            return ShardInfo(parent, self.shard_count)

    @property
    def shards(self) -> Dict[int, ShardInfo]:
        """Mapping[int, :class:`ShardInfo`]: Returns a mapping of shard IDs to their respective info object."""
        return {shard_id: ShardInfo(parent, self.shard_count) for shard_id, parent in self.__shards.items()}

    async def launch_shard(self, gateway: yarl.URL, shard_id: int, *, initial: bool = False) -> None:
        try:
            coro = DiscordWebSocket.from_client(self, initial=initial, gateway=gateway, shard_id=shard_id)
            ws = await asyncio.wait_for(coro, timeout=self.shard_connect_timeout)
        except Exception:
            _log.exception('Failed to connect for shard_id: %s. Retrying...', shard_id)
            await asyncio.sleep(5.0)
            return await self.launch_shard(gateway, shard_id)

        # keep reading the shard while others connect
        self.__shards[shard_id] = ret = Shard(ws, self, self.__queue.put_nowait)
        ret.launch()

    async def launch_shards(self) -> None:
        if self.is_closed():
            return

        if self.shard_count is None:
            self.shard_count: int
            self.shard_count, gateway_url = await self.http.get_bot_gateway()
            gateway = yarl.URL(gateway_url)
        else:
            gateway = DiscordWebSocket.DEFAULT_GATEWAY

        self._connection.shard_count = self.shard_count

        shard_ids = self.shard_ids or range(self.shard_count)
        self._connection.shard_ids = shard_ids

        for shard_id in shard_ids:
            initial = shard_id == shard_ids[0]
            await self.launch_shard(gateway, shard_id, initial=initial)

    async def _async_setup_hook(self) -> None:
        await super()._async_setup_hook()
        self.__queue = asyncio.PriorityQueue()

    async def connect(self, *, reconnect: bool = True) -> None:
        self._reconnect = reconnect
        await self.launch_shards()

        while not self.is_closed():
            item = await self.__queue.get()
            if item.type == EventType.close:
                await self.close()
                if isinstance(item.error, ConnectionClosed):
                    if item.error.code != 1000:
                        raise item.error
                    if item.error.code == 4014:
                        raise PrivilegedIntentsRequired(item.shard.id) from None
                return
            elif item.type in (EventType.identify, EventType.resume):
                await item.shard.reidentify(item.error)
            elif item.type == EventType.reconnect:
                await item.shard.reconnect()
            elif item.type == EventType.terminate:
                await self.close()
                raise item.error
            elif item.type == EventType.clean_close:
                return

    async def close(self) -> None:
        """|coro|

        Closes the connection to Discord.
        """
        if self._closing_task:
            return await self._closing_task

        async def _close():
            await self._connection.close()

            to_close = [asyncio.ensure_future(shard.close(), loop=self.loop) for shard in self.__shards.values()]
            if to_close:
                await asyncio.wait(to_close)

            await self.http.close()
            self.__queue.put_nowait(EventItem(EventType.clean_close, None, None))

        self._closing_task = asyncio.create_task(_close())
        await self._closing_task

    async def change_presence(
        self,
        *,
        activity: Optional[BaseActivity] = None,
        status: Optional[Status] = None,
        shard_id: Optional[int] = None,
    ) -> None:
        """|coro|

        Changes the client's presence.

        Example: ::

            game = discord.Game("with the API")
            await client.change_presence(status=discord.Status.idle, activity=game)

        .. versionchanged:: 2.0
            Removed the ``afk`` keyword-only parameter.

        .. versionchanged:: 2.0
            This function will now raise :exc:`TypeError` instead of
            ``InvalidArgument``.

        Parameters
        ----------
        activity: Optional[:class:`BaseActivity`]
            The activity being done. ``None`` if no currently active activity is done.
        status: Optional[:class:`Status`]
            Indicates what status to change to. If ``None``, then
            :attr:`Status.online` is used.
        shard_id: Optional[:class:`int`]
            The shard_id to change the presence to. If not specified
            or ``None``, then it will change the presence of every
            shard the bot can see.

        Raises
        ------
        TypeError
            If the ``activity`` parameter is not of proper type.
        """

        if status is None:
            status_value = 'online'
            status_enum = Status.online
        elif status is Status.offline:
            status_value = 'invisible'
            status_enum = Status.offline
        else:
            status_enum = status
            status_value = str(status)

        if shard_id is None:
            for shard in self.__shards.values():
                await shard.ws.change_presence(activity=activity, status=status_value)

            guilds = self._connection.guilds
        else:
            shard = self.__shards[shard_id]
            await shard.ws.change_presence(activity=activity, status=status_value)
            guilds = [g for g in self._connection.guilds if g.shard_id == shard_id]

        activities = () if activity is None else (activity,)
        for guild in guilds:
            me = guild.me
            if me is None:
                continue

            # Member.activities is typehinted as Tuple[ActivityType, ...], we may be setting it as Tuple[BaseActivity, ...]
            me.activities = activities  # type: ignore
            me.status = status_enum

    def is_ws_ratelimited(self) -> bool:
        """:class:`bool`: Whether the websocket is currently rate limited.

        This can be useful to know when deciding whether you should query members
        using HTTP or via the gateway.

        This implementation checks if any of the shards are rate limited.
        For more granular control, consider :meth:`ShardInfo.is_ws_ratelimited`.

        .. versionadded:: 1.6
        """
        return any(shard.ws.is_ratelimited() for shard in self.__shards.values())
