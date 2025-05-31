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

import asyncio
from collections import deque
import concurrent.futures
import logging
import struct
import sys
import time
import threading
import traceback

from typing import Any, Callable, Coroutine, Deque, Dict, List, TYPE_CHECKING, NamedTuple, Optional, TypeVar, Tuple

import aiohttp
import yarl

from . import utils
from .activity import BaseActivity
from .enums import SpeakingState
from .errors import ConnectionClosed

_log = logging.getLogger(__name__)

__all__ = (
    'DiscordWebSocket',
    'KeepAliveHandler',
    'VoiceKeepAliveHandler',
    'DiscordVoiceWebSocket',
    'ReconnectWebSocket',
)

if TYPE_CHECKING:
    from typing_extensions import Self

    from .client import Client
    from .state import ConnectionState
    from .voice_state import VoiceConnectionState


class ReconnectWebSocket(Exception):
    """Signals to safely reconnect the websocket."""

    def __init__(self, shard_id: Optional[int], *, resume: bool = True) -> None:
        self.shard_id: Optional[int] = shard_id
        self.resume: bool = resume
        self.op: str = 'RESUME' if resume else 'IDENTIFY'


class WebSocketClosure(Exception):
    """An exception to make up for the fact that aiohttp doesn't signal closure."""

    pass


class EventListener(NamedTuple):
    predicate: Callable[[Dict[str, Any]], bool]
    event: str
    result: Optional[Callable[[Dict[str, Any]], Any]]
    future: asyncio.Future[Any]


class GatewayRatelimiter:
    def __init__(self, count: int = 110, per: float = 60.0) -> None:
        # The default is 110 to give room for at least 10 heartbeats per minute
        self.max: int = count
        self.remaining: int = count
        self.window: float = 0.0
        self.per: float = per
        self.lock: asyncio.Lock = asyncio.Lock()
        self.shard_id: Optional[int] = None

    def is_ratelimited(self) -> bool:
        current = time.time()
        if current > self.window + self.per:
            return False
        return self.remaining == 0

    def get_delay(self) -> float:
        current = time.time()

        if current > self.window + self.per:
            self.remaining = self.max

        if self.remaining == self.max:
            self.window = current

        if self.remaining == 0:
            return self.per - (current - self.window)

        self.remaining -= 1
        return 0.0

    async def block(self) -> None:
        async with self.lock:
            delta = self.get_delay()
            if delta:
                _log.warning('WebSocket in shard ID %s is ratelimited, waiting %.2f seconds', self.shard_id, delta)
                await asyncio.sleep(delta)


class KeepAliveHandler(threading.Thread):
    def __init__(
        self,
        *args: Any,
        ws: DiscordWebSocket,
        interval: Optional[float] = None,
        shard_id: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        daemon: bool = kwargs.pop('daemon', True)
        name: str = kwargs.pop('name', f'keep-alive-handler:shard-{shard_id}')
        super().__init__(*args, daemon=daemon, name=name, **kwargs)
        self.ws: DiscordWebSocket = ws
        self._main_thread_id: int = ws.thread_id
        self.interval: Optional[float] = interval
        self.shard_id: Optional[int] = shard_id
        self.msg: str = 'Keeping shard ID %s websocket alive with sequence %s.'
        self.block_msg: str = 'Shard ID %s heartbeat blocked for more than %s seconds.'
        self.behind_msg: str = 'Can\'t keep up, shard ID %s websocket is %.1fs behind.'
        self._stop_ev: threading.Event = threading.Event()
        self._last_ack: float = time.perf_counter()
        self._last_send: float = time.perf_counter()
        self._last_recv: float = time.perf_counter()
        self.latency: float = float('inf')
        self.heartbeat_timeout: float = ws._max_heartbeat_timeout

    def run(self) -> None:
        while not self._stop_ev.wait(self.interval):
            if self._last_recv + self.heartbeat_timeout < time.perf_counter():
                _log.warning("Shard ID %s has stopped responding to the gateway. Closing and restarting.", self.shard_id)
                coro = self.ws.close(4000)
                f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)

                try:
                    f.result()
                except Exception:
                    _log.exception('An error occurred while stopping the gateway. Ignoring.')
                finally:
                    self.stop()
                    return

            data = self.get_payload()
            _log.debug(self.msg, self.shard_id, data['d'])
            coro = self.ws.send_heartbeat(data)
            f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
            try:
                # block until sending is complete
                total = 0
                while True:
                    try:
                        f.result(10)
                        break
                    except concurrent.futures.TimeoutError:
                        total += 10
                        try:
                            frame = sys._current_frames()[self._main_thread_id]
                        except KeyError:
                            msg = self.block_msg
                        else:
                            stack = ''.join(traceback.format_stack(frame))
                            msg = f'{self.block_msg}\nLoop thread traceback (most recent call last):\n{stack}'
                        _log.warning(msg, self.shard_id, total)

            except Exception:
                self.stop()
            else:
                self._last_send = time.perf_counter()

    def get_payload(self) -> Dict[str, Any]:
        return {
            'op': self.ws.HEARTBEAT,
            'd': self.ws.sequence,
        }

    def stop(self) -> None:
        self._stop_ev.set()

    def tick(self) -> None:
        self._last_recv = time.perf_counter()

    def ack(self) -> None:
        ack_time = time.perf_counter()
        self._last_ack = ack_time
        self.latency = ack_time - self._last_send
        if self.latency > 10:
            _log.warning(self.behind_msg, self.shard_id, self.latency)


class VoiceKeepAliveHandler(KeepAliveHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        name: str = kwargs.pop('name', f'voice-keep-alive-handler:{id(self):#x}')
        super().__init__(*args, name=name, **kwargs)
        self.recent_ack_latencies: Deque[float] = deque(maxlen=20)
        self.msg: str = 'Keeping shard ID %s voice websocket alive with timestamp %s.'
        self.block_msg: str = 'Shard ID %s voice heartbeat blocked for more than %s seconds'
        self.behind_msg: str = 'High socket latency, shard ID %s heartbeat is %.1fs behind'

    def get_payload(self) -> Dict[str, Any]:
        return {
            'op': self.ws.HEARTBEAT,
            'd': int(time.time() * 1000),
        }

    def ack(self) -> None:
        ack_time = time.perf_counter()
        self._last_ack = ack_time
        self._last_recv = ack_time
        self.latency: float = ack_time - self._last_send
        self.recent_ack_latencies.append(self.latency)


class DiscordClientWebSocketResponse(aiohttp.ClientWebSocketResponse):
    async def close(self, *, code: int = 4000, message: bytes = b'') -> bool:
        return await super().close(code=code, message=message)


DWS = TypeVar('DWS', bound='DiscordWebSocket')


class DiscordWebSocket:
    """Implements a WebSocket for Discord's gateway v10 with enhanced reliability and monitoring."""

    if TYPE_CHECKING:
        token: Optional[str]
        _connection: ConnectionState
        _discord_parsers: Dict[str, Callable[..., Any]]
        call_hooks: Callable[..., Any]
        _initial_identify: bool
        shard_id: Optional[int]
        shard_count: Optional[int]
        gateway: yarl.URL
        _max_heartbeat_timeout: float

    # fmt: off
    DEFAULT_GATEWAY    = yarl.URL('wss://gateway.discord.gg/')
    DISPATCH                    = 0
    HEARTBEAT                   = 1
    IDENTIFY                    = 2
    PRESENCE                    = 3
    VOICE_STATE                 = 4
    VOICE_PING                  = 5
    RESUME                      = 6
    RECONNECT                   = 7
    REQUEST_MEMBERS             = 8
    INVALIDATE_SESSION          = 9
    HELLO                       = 10
    HEARTBEAT_ACK               = 11
    GUILD_SYNC                  = 12
    # fmt: on

    def __init__(self, socket: aiohttp.ClientWebSocketResponse, *, loop: asyncio.AbstractEventLoop) -> None:
        self.socket: aiohttp.ClientWebSocketResponse = socket
        self.loop: asyncio.AbstractEventLoop = loop

        # an empty dispatcher to prevent crashes
        self._dispatch: Callable[..., Any] = lambda *args: None
        # generic event listeners
        self._dispatch_listeners: List[EventListener] = []
        # the keep alive
        self._keep_alive: Optional[KeepAliveHandler] = None
        self.thread_id: int = threading.get_ident()

        # ws related stuff
        self.session_id: Optional[str] = None
        self.sequence: Optional[int] = None
        self._decompressor: utils._DecompressionContext = utils._ActiveDecompressionContext()
        self._close_code: Optional[int] = None
        self._rate_limiter: GatewayRatelimiter = GatewayRatelimiter()

        # Enhanced connection management
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = 5
        self._reconnect_delay: float = 1.0
        self._last_reconnect: float = 0.0
        self._connection_errors: int = 0
        self._max_connection_errors: int = 10
        self._error_reset_time: float = 300.0  # 5 minutes
        self._last_error_time: float = 0.0
        self._connection_health: bool = True
        self._connection_stats = {
            'total_messages': 0,
            'failed_messages': 0,
            'reconnects': 0,
            'errors': 0,
            'latency': []
        }

        # Message queue for rate limiting
        self._message_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._message_processor: Optional[asyncio.Task] = None

    @property
    def open(self) -> bool:
        return not self.socket.closed

    def is_ratelimited(self) -> bool:
        return self._rate_limiter.is_ratelimited()

    def debug_log_receive(self, data: Dict[str, Any], /) -> None:
        self._dispatch('socket_raw_receive', data)

    def log_receive(self, _: Dict[str, Any], /) -> None:
        pass

    @classmethod
    async def from_client(
        cls,
        client: Client,
        *,
        initial: bool = False,
        gateway: Optional[yarl.URL] = None,
        shard_id: Optional[int] = None,
        session: Optional[str] = None,
        sequence: Optional[int] = None,
        resume: bool = False,
        encoding: str = 'json',
        compress: bool = True,
    ) -> Self:
        """Creates a main websocket for Discord from a :class:`Client`.

        This is for internal use only.
        """
        # Circular import
        from .http import INTERNAL_API_VERSION

        gateway = gateway or cls.DEFAULT_GATEWAY

        if not compress:
            url = gateway.with_query(v=INTERNAL_API_VERSION, encoding=encoding)
        else:
            url = gateway.with_query(
                v=INTERNAL_API_VERSION, encoding=encoding, compress=utils._ActiveDecompressionContext.COMPRESSION_TYPE
            )

        socket = await client.http.ws_connect(str(url))
        ws = cls(socket, loop=client.loop)

        # dynamically add attributes needed
        ws.token = client.http.token
        ws._connection = client._connection
        ws._discord_parsers = client._connection.parsers
        ws._dispatch = client.dispatch
        ws.gateway = gateway
        ws.call_hooks = client._connection.call_hooks
        ws._initial_identify = initial
        ws.shard_id = shard_id
        ws._rate_limiter.shard_id = shard_id
        ws.shard_count = client._connection.shard_count
        ws.session_id = session
        ws.sequence = sequence
        ws._max_heartbeat_timeout = client._connection.heartbeat_timeout

        if client._enable_debug_events:
            ws.send = ws.debug_send
            ws.log_receive = ws.debug_log_receive

        client._connection._update_references(ws)

        _log.debug('Created websocket connected to %s', gateway)

        # poll event for OP Hello
        await ws.poll_event()

        if not resume:
            await ws.identify()
            return ws

        await ws.resume()
        return ws

    def wait_for(
        self,
        event: str,
        predicate: Callable[[Dict[str, Any]], bool],
        result: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> asyncio.Future[Any]:
        """Waits for a DISPATCH'd event that meets the predicate.

        Parameters
        -----------
        event: :class:`str`
            The event name in all upper case to wait for.
        predicate
            A function that takes a data parameter to check for event
            properties. The data parameter is the 'd' key in the JSON message.
        result
            A function that takes the same data parameter and executes to send
            the result to the future. If ``None``, returns the data.

        Returns
        --------
        asyncio.Future
            A future to wait for.
        """

        future = self.loop.create_future()
        entry = EventListener(event=event, predicate=predicate, result=result, future=future)
        self._dispatch_listeners.append(entry)
        return future

    async def identify(self) -> None:
        """Sends the IDENTIFY packet."""
        payload = {
            'op': self.IDENTIFY,
            'd': {
                'token': self.token,
                'properties': {
                    'os': 'Discord iOS',
                    'browser': 'Discord iOS',
                    'device': 'Discord iOS',
                },
                'compress': True,
                'large_threshold': 250,
            },
        }

        if self.shard_id is not None and self.shard_count is not None:
            payload['d']['shard'] = [self.shard_id, self.shard_count]

        state = self._connection
        if state._activity is not None or state._status is not None:
            payload['d']['presence'] = {
                'status': state._status,
                'game': state._activity,
                'since': 0,
                'afk': False,
            }

        if state._intents is not None:
            payload['d']['intents'] = state._intents.value

        await self.call_hooks('before_identify', self.shard_id, initial=self._initial_identify)
        await self.send_as_json(payload)
        _log.debug('Shard ID %s has sent the IDENTIFY payload.', self.shard_id)

    async def resume(self) -> None:
        """Sends the RESUME packet."""
        payload = {
            'op': self.RESUME,
            'd': {
                'seq': self.sequence,
                'session_id': self.session_id,
                'token': self.token,
            },
        }

        await self.send_as_json(payload)
        _log.debug('Shard ID %s has sent the RESUME payload.', self.shard_id)

    async def received_message(self, msg: Any, /) -> None:
        if type(msg) is bytes:
            msg = self._decompressor.decompress(msg)

            # Received a partial gateway message
            if msg is None:
                return

        self.log_receive(msg)
        msg = utils._from_json(msg)

        _log.debug('For Shard ID %s: WebSocket Event: %s', self.shard_id, msg)
        event = msg.get('t')
        if event:
            self._dispatch('socket_event_type', event)

        op = msg.get('op')
        data = msg.get('d')
        seq = msg.get('s')
        if seq is not None:
            self.sequence = seq

        if self._keep_alive:
            self._keep_alive.tick()

        if op != self.DISPATCH:
            if op == self.RECONNECT:
                # "reconnect" can only be handled by the Client
                # so we terminate our connection and raise an
                # internal exception signalling to reconnect.
                _log.debug('Received RECONNECT opcode.')
                await self.close()
                raise ReconnectWebSocket(self.shard_id)

            if op == self.HEARTBEAT_ACK:
                if self._keep_alive:
                    self._keep_alive.ack()
                return

            if op == self.HEARTBEAT:
                if self._keep_alive:
                    beat = self._keep_alive.get_payload()
                    await self.send_as_json(beat)
                return

            if op == self.HELLO:
                interval = data['heartbeat_interval'] / 1000.0
                self._keep_alive = KeepAliveHandler(ws=self, interval=interval, shard_id=self.shard_id)
                # send a heartbeat immediately
                await self.send_as_json(self._keep_alive.get_payload())
                self._keep_alive.start()
                return

            if op == self.INVALIDATE_SESSION:
                if data is True:
                    await self.close()
                    raise ReconnectWebSocket(self.shard_id)

                self.sequence = None
                self.session_id = None
                self.gateway = self.DEFAULT_GATEWAY
                _log.info('Shard ID %s session has been invalidated.', self.shard_id)
                await self.close(code=1000)
                raise ReconnectWebSocket(self.shard_id, resume=False)

            _log.warning('Unknown OP code %s.', op)
            return

        if event == 'READY':
            self.sequence = msg['s']
            self.session_id = data['session_id']
            self.gateway = yarl.URL(data['resume_gateway_url'])
            _log.info('Shard ID %s has connected to Gateway (Session ID: %s).', self.shard_id, self.session_id)

        elif event == 'RESUMED':
            # pass back the shard ID to the resumed handler
            data['__shard_id__'] = self.shard_id
            _log.info('Shard ID %s has successfully RESUMED session %s.', self.shard_id, self.session_id)

        try:
            func = self._discord_parsers[event]
        except KeyError:
            _log.debug('Unknown event %s.', event)
        else:
            func(data)

        # remove the dispatched listeners
        removed = []
        for index, entry in enumerate(self._dispatch_listeners):
            if entry.event != event:
                continue

            future = entry.future
            if future.cancelled():
                removed.append(index)
                continue

            try:
                valid = entry.predicate(data)
            except Exception as exc:
                future.set_exception(exc)
                removed.append(index)
            else:
                if valid:
                    ret = data if entry.result is None else entry.result(data)
                    future.set_result(ret)
                    removed.append(index)

        for index in reversed(removed):
            del self._dispatch_listeners[index]

    @property
    def latency(self) -> float:
        """:class:`float`: Measures latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        heartbeat = self._keep_alive
        return float('inf') if heartbeat is None else heartbeat.latency

    def _can_handle_close(self) -> bool:
        code = self._close_code or self.socket.close_code
        return code not in (1000, 4004, 4010, 4011, 4012, 4013, 4014)

    async def poll_event(self) -> None:
        """Polls for a DISPATCH event and handles the general gateway loop with enhanced error handling."""
        try:
            msg = await self.socket.receive(timeout=self._max_heartbeat_timeout)
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self.received_message(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await self.received_message(msg.data)
            elif msg.type is aiohttp.WSMsgType.ERROR:
                _log.error('WebSocket error: %s', msg.data)
                await self._handle_connection_error(WebSocketClosure())
                raise WebSocketClosure
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE):
                _log.debug('Received %s', msg)
                await self._handle_connection_error(WebSocketClosure())
                raise WebSocketClosure

            # Update connection stats
            self._connection_stats['total_messages'] += 1

        except (asyncio.TimeoutError, WebSocketClosure) as e:
            self._connection_stats['failed_messages'] += 1
            self._connection_stats['errors'] += 1

            # Ensure the keep alive handler is closed
            if self._keep_alive:
                self._keep_alive.stop()
                self._keep_alive = None

            if isinstance(e, asyncio.TimeoutError):
                _log.debug('Timed out receiving packet. Attempting a reconnect.')
                await self._handle_connection_error(e)
                raise ReconnectWebSocket(self.shard_id) from None

            code = self._close_code or self.socket.close_code
            if self._can_handle_close():
                _log.debug('Websocket closed with %s, attempting a reconnect.', code)
                await self._handle_connection_error(e)
                raise ReconnectWebSocket(self.shard_id) from None
            else:
                _log.debug('Websocket closed with %s, cannot reconnect.', code)
                raise ConnectionClosed(self.socket, shard_id=self.shard_id, code=code) from None

    async def debug_send(self, data: str, /) -> None:
        await self._rate_limiter.block()
        self._dispatch('socket_raw_send', data)
        await self.socket.send_str(data)

    async def send(self, data: str, /) -> None:
        await self._rate_limiter.block()
        await self.socket.send_str(data)

    async def send_as_json(self, data: Any) -> None:
        """Send data as JSON with enhanced error handling and rate limiting."""
        try:
            await self._message_queue.put(data)
            if self._message_processor is None or self._message_processor.done():
                self._message_processor = self.loop.create_task(self._process_message_queue())
        except Exception as e:
            _log.error('Error queueing WebSocket message: %s', e)
            await self._handle_connection_error(e)
            raise

    async def send_heartbeat(self, data: Any) -> None:
        # This bypasses the rate limit handling code since it has a higher priority
        try:
            await self.socket.send_str(utils._to_json(data))
        except RuntimeError as exc:
            if not self._can_handle_close():
                raise ConnectionClosed(self.socket, shard_id=self.shard_id) from exc

    async def change_presence(
        self,
        *,
        activity: Optional[BaseActivity] = None,
        status: Optional[str] = None,
        since: float = 0.0,
    ) -> None:
        if activity is not None:
            if not isinstance(activity, BaseActivity):
                raise TypeError('activity must derive from BaseActivity.')
            activities = [activity.to_dict()]
        else:
            activities = []

        if status == 'idle':
            since = int(time.time() * 1000)

        payload = {
            'op': self.PRESENCE,
            'd': {
                'activities': activities,
                'afk': False,
                'since': since,
                'status': status,
            },
        }

        sent = utils._to_json(payload)
        _log.debug('Sending "%s" to change status', sent)
        await self.send(sent)

    async def request_chunks(
        self,
        guild_id: int,
        query: Optional[str] = None,
        *,
        limit: int,
        user_ids: Optional[List[int]] = None,
        presences: bool = False,
        nonce: Optional[str] = None,
    ) -> None:
        payload = {
            'op': self.REQUEST_MEMBERS,
            'd': {
                'guild_id': guild_id,
                'presences': presences,
                'limit': limit,
            },
        }

        if nonce:
            payload['d']['nonce'] = nonce

        if user_ids:
            payload['d']['user_ids'] = user_ids

        if query is not None:
            payload['d']['query'] = query

        await self.send_as_json(payload)

    async def voice_state(
        self,
        guild_id: int,
        channel_id: Optional[int],
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> None:
        payload = {
            'op': self.VOICE_STATE,
            'd': {
                'guild_id': guild_id,
                'channel_id': channel_id,
                'self_mute': self_mute,
                'self_deaf': self_deaf,
            },
        }

        _log.debug('Updating our voice state to %s.', payload)
        await self.send_as_json(payload)

    async def close(self, code: int = 1000, reason: Optional[str] = None) -> None:
        """Closes the connection with proper cleanup."""
        if self._keep_alive:
            self._keep_alive.stop()
            self._keep_alive = None

        if self._message_processor:
            self._message_processor.cancel()
            try:
                await self._message_processor
            except asyncio.CancelledError:
                pass

        try:
            await self.socket.close(code=code, message=reason)
        except Exception as e:
            _log.error('Error closing WebSocket: %s', e)
            raise

    async def _handle_connection_error(self, error: Exception) -> None:
        """Handle connection errors with exponential backoff."""
        current_time = time.time()
        self._connection_errors += 1

        # Reset error count after error_reset_time
        if current_time - self._last_error_time > self._error_reset_time:
            self._connection_errors = 0
            self._last_error_time = current_time

        if self._connection_errors >= self._max_connection_errors:
            _log.error('Too many connection errors, marking connection as unhealthy')
            self._connection_health = False
            raise ConnectionClosed(self.socket, shard_id=self.shard_id, code=1006)

        delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))
        _log.warning('Connection error occurred, retrying in %.2f seconds (attempt %d/%d)',
                    delay, self._reconnect_attempts, self._max_reconnect_attempts)
        await asyncio.sleep(delay)

    async def _check_connection_health(self) -> None:
        """Monitor connection health and attempt recovery if needed."""
        if not self._connection_health:
            if self._reconnect_attempts >= self._max_reconnect_attempts:
                _log.error('Max reconnection attempts reached, closing connection')
                await self.close(1000, "Max reconnection attempts reached")
                return

            try:
                await self._attempt_reconnect()
            except Exception as e:
                _log.error('Failed to recover connection: %s', e)
                self._connection_health = False

    async def _attempt_reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        self._reconnect_attempts += 1
        delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))
        _log.info('Attempting to reconnect in %.2f seconds (attempt %d/%d)',
                 delay, self._reconnect_attempts, self._max_reconnect_attempts)

        try:
            await asyncio.sleep(delay)
            await self.resume()
            self._reconnect_attempts = 0
            self._connection_health = True
            self._connection_stats['reconnects'] += 1
            _log.info('Successfully reconnected')
        except Exception as e:
            _log.error('Reconnection attempt failed: %s', e)
            raise

    async def _process_message_queue(self) -> None:
        """Process messages from the queue with rate limiting."""
        while True:
            try:
                message = await self._message_queue.get()
                await self._rate_limiter.block()
                await self.socket.send_str(utils._to_json(message))
                self._message_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.error('Error processing message from queue: %s', e)
                self._connection_stats['errors'] += 1

    def get_connection_stats(self) -> Dict[str, Any]:
        """Get detailed statistics about the WebSocket connection."""
        stats = self._connection_stats.copy()
        if stats['latency']:
            stats['average_latency'] = sum(stats['latency']) / len(stats['latency'])
            stats['min_latency'] = min(stats['latency'])
            stats['max_latency'] = max(stats['latency'])
        return stats
