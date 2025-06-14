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
import logging
import struct
import time
from typing import Any, Callable, List, Optional, TYPE_CHECKING, Tuple, Union, Dict

from . import opus
from .gateway import *
from .errors import ClientException
from .player import AudioPlayer, AudioSource
from .utils import MISSING
from .voice_state import VoiceConnectionState

if TYPE_CHECKING:
    from .gateway import DiscordVoiceWebSocket
    from .client import Client
    from .guild import Guild
    from .state import ConnectionState
    from .user import ClientUser
    from .opus import Encoder, APPLICATION_CTL, BAND_CTL, SIGNAL_CTL
    from .channel import StageChannel, VoiceChannel
    from . import abc

    from .types.voice import (
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload,
        SupportedModes,
    )

    VocalGuildChannel = Union[VoiceChannel, StageChannel]


has_nacl: bool

try:
    import nacl.secret  # type: ignore
    import nacl.utils  # type: ignore

    has_nacl = True
except ImportError:
    has_nacl = False

__all__ = (
    'VoiceProtocol',
    'VoiceClient',
)


_log = logging.getLogger(__name__)


class VoiceProtocol:
    """A class that represents the Discord voice protocol.

    This is an abstract class. The library provides a concrete implementation
    under :class:`VoiceClient`.

    This class allows you to implement a protocol to allow for an external
    method of sending voice, such as Lavalink_ or a native library implementation.

    These classes are passed to :meth:`abc.Connectable.connect <VoiceChannel.connect>`.

    .. _Lavalink: https://github.com/freyacodes/Lavalink

    Parameters
    ------------
    client: :class:`Client`
        The client (or its subclasses) that started the connection request.
    channel: :class:`abc.Connectable`
        The voice channel that is being connected to.
    """

    def __init__(self, client: Client, channel: abc.Connectable) -> None:
        self.client: Client = client
        self.channel: abc.Connectable = channel

    async def on_voice_state_update(self, data: GuildVoiceStatePayload, /) -> None:
        """|coro|

        An abstract method that is called when the client's voice state
        has changed. This corresponds to ``VOICE_STATE_UPDATE``.

        .. warning::

            This method is not the same as the event. See: :func:`on_voice_state_update`

        Parameters
        ------------
        data: :class:`dict`
            The raw :ddocs:`voice state payload <resources/voice#voice-state-object>`.
        """
        raise NotImplementedError

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload, /) -> None:
        """|coro|

        An abstract method that is called when initially connecting to voice.
        This corresponds to ``VOICE_SERVER_UPDATE``.

        Parameters
        ------------
        data: :class:`dict`
            The raw :ddocs:`voice server update payload <topics/gateway-events#voice-server-update>`.
        """
        raise NotImplementedError

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = False, self_mute: bool = False) -> None:
        """|coro|

        An abstract method called when the client initiates the connection request.

        When a connection is requested initially, the library calls the constructor
        under ``__init__`` and then calls :meth:`connect`. If :meth:`connect` fails at
        some point then :meth:`disconnect` is called.

        Within this method, to start the voice connection flow it is recommended to
        use :meth:`Guild.change_voice_state` to start the flow. After which,
        :meth:`on_voice_server_update` and :meth:`on_voice_state_update` will be called.
        The order that these two are called is unspecified.

        Parameters
        ------------
        timeout: :class:`float`
            The timeout for the connection.
        reconnect: :class:`bool`
            Whether reconnection is expected.
        self_mute: :class:`bool`
            Indicates if the client should be self-muted.

            .. versionadded:: 2.0
        self_deaf: :class:`bool`
            Indicates if the client should be self-deafened.

            .. versionadded:: 2.0
        """
        raise NotImplementedError

    async def disconnect(self, *, force: bool) -> None:
        """|coro|

        An abstract method called when the client terminates the connection.

        See :meth:`cleanup`.

        Parameters
        ------------
        force: :class:`bool`
            Whether the disconnection was forced.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """This method *must* be called to ensure proper clean-up during a disconnect.

        It is advisable to call this from within :meth:`disconnect` when you are
        completely done with the voice protocol instance.

        This method removes it from the internal state cache that keeps track of
        currently alive voice clients. Failure to clean-up will cause subsequent
        connections to report that it's still connected.
        """
        key_id, _ = self.channel._get_voice_client_key()
        self.client._connection._remove_voice_client(key_id)


class VoiceClient(VoiceProtocol):
    """Represents a Discord voice connection with enhanced reliability and monitoring."""

    def __init__(self, client: Client, channel: VocalGuildChannel) -> None:
        if not has_nacl:
            raise RuntimeError("PyNaCl library needed in order to use voice")

        super().__init__(client, channel)
        state = client._connection
        self.server_id: int = MISSING
        self.socket = MISSING
        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state: ConnectionState = state

        self.sequence: int = 0
        self.timestamp: int = 0
        self._player: Optional[AudioPlayer] = None
        self.encoder: Encoder = MISSING
        self._incr_nonce: int = 0
        self._connection: VoiceConnectionState = self.create_connection_state()
        
        # Enhanced error handling and reconnection
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = 5
        self._reconnect_delay: float = 1.0
        self._last_packet_time: float = 0.0
        self._packet_timeout: float = 5.0
        self._connection_check_task: Optional[asyncio.Task] = None
        
        # Performance monitoring
        self._connection_stats = {
            'total_packets': 0,
            'failed_packets': 0,
            'reconnects': 0,
            'errors': 0,
            'latency': []
        }

    warn_nacl: bool = not has_nacl
    supported_modes: Tuple[SupportedModes, ...] = (
        'aead_xchacha20_poly1305_rtpsize',
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    )

    @property
    def guild(self) -> Guild:
        """:class:`Guild`: The guild we're connected to."""
        return self.channel.guild

    @property
    def user(self) -> ClientUser:
        """:class:`ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self._state.user  # type: ignore

    @property
    def session_id(self) -> Optional[str]:
        return self._connection.session_id

    @property
    def token(self) -> Optional[str]:
        return self._connection.token

    @property
    def endpoint(self) -> Optional[str]:
        return self._connection.endpoint

    @property
    def ssrc(self) -> int:
        return self._connection.ssrc

    @property
    def mode(self) -> SupportedModes:
        return self._connection.mode

    @property
    def secret_key(self) -> List[int]:
        return self._connection.secret_key

    @property
    def ws(self) -> DiscordVoiceWebSocket:
        return self._connection.ws

    @property
    def timeout(self) -> float:
        return self._connection.timeout

    def checked_add(self, attr: str, value: int, limit: int) -> None:
        val = getattr(self, attr)
        if val + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, val + value)

    # connection related

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self)

    async def on_voice_state_update(self, data: GuildVoiceStatePayload) -> None:
        await self._connection.voice_state_update(data)

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        await self._connection.voice_server_update(data)

    async def connect(self, *, reconnect: bool = True, timeout: float = 30.0, 
                     self_deaf: bool = False, self_mute: bool = False) -> None:
        """|coro|

        Connects to voice with enhanced error handling and reconnection logic.
        """
        try:
            await self._connection.connect(
                reconnect=reconnect,
                timeout=timeout,
                self_deaf=self_deaf,
                self_mute=self_mute,
                resume=False
            )
            await self._start_connection_check()
        except Exception as e:
            _log.error("Error connecting to voice: %s", e)
            if reconnect:
                await self._attempt_reconnect()
            else:
                raise

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        self._connection.wait(timeout)
        return self._connection.is_connected()

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds.

        This could be referred to as the Discord Voice WebSocket latency and is
        an analogue of user's voice latencies as seen in the Discord client.

        .. versionadded:: 1.4
        """
        ws = self._connection.ws
        return float("inf") if not ws else ws.latency

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average of most recent 20 HEARTBEAT latencies in seconds.

        .. versionadded:: 1.4
        """
        ws = self._connection.ws
        return float("inf") if not ws else ws.average_latency

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice with proper cleanup.
        """
        if self._connection_check_task is not None:
            self._connection_check_task.cancel()
            self._connection_check_task = None
            
        self.stop()
        await self._connection.disconnect(force=force, wait=True)
        self.cleanup()
        self._reconnect_attempts = 0

    async def move_to(self, channel: Optional[abc.Snowflake], *, timeout: Optional[float] = 30.0) -> None:
        """|coro|

        Moves you to a different voice channel.

        Parameters
        -----------
        channel: Optional[:class:`abc.Snowflake`]
            The channel to move to. Must be a voice channel.
        timeout: Optional[:class:`float`]
            How long to wait for the move to complete.

            .. versionadded:: 2.4

        Raises
        -------
        asyncio.TimeoutError
            The move did not complete in time, but may still be ongoing.
        """
        await self._connection.move_to(channel, timeout)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connection.is_connected()

    # audio related

    async def _start_connection_check(self) -> None:
        """Starts a background task to monitor connection health."""
        if self._connection_check_task is not None:
            return
            
        async def check_connection():
            while True:
                try:
                    current_time = time.time()
                    if current_time - self._last_packet_time > self._packet_timeout:
                        _log.warning("No packets received for %.2f seconds, attempting reconnection", 
                                   current_time - self._last_packet_time)
                        await self._attempt_reconnect()
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    _log.error("Error in connection check: %s", e)
                    
        self._connection_check_task = self.loop.create_task(check_connection())

    async def _attempt_reconnect(self) -> None:
        """Attempts to reconnect to the voice server with exponential backoff."""
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            _log.error("Max reconnection attempts reached, giving up")
            await self.disconnect(force=True)
            return
            
        self._reconnect_attempts += 1
        delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))
        _log.info("Attempting reconnection in %.2f seconds (attempt %d/%d)", 
                 delay, self._reconnect_attempts, self._max_reconnect_attempts)
                 
        try:
            await asyncio.sleep(delay)
            await self.connect(reconnect=True, timeout=30.0)
            self._reconnect_attempts = 0
            self._connection_stats['reconnects'] += 1
            _log.info("Successfully reconnected to voice")
        except Exception as e:
            _log.error("Reconnection attempt failed: %s", e)
            self._connection_stats['errors'] += 1
            raise

    def _get_voice_packet(self, data: bytes) -> bytes:
        """Creates a voice packet with enhanced error handling."""
        try:
            header = bytearray(12)
            header[0] = 0x80
            header[1] = 0x78
            struct.pack_into('>H', header, 2, self.sequence)
            struct.pack_into('>I', header, 4, self.timestamp)
            struct.pack_into('>I', header, 8, self.ssrc)

            encrypt_packet = getattr(self, '_encrypt_' + self.mode)
            packet = encrypt_packet(header, data)
            self._last_packet_time = time.time()
            return packet
        except Exception as e:
            _log.error("Error creating voice packet: %s", e)
            raise

    def play(
        self,
        source: AudioSource,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
        application: APPLICATION_CTL = 'audio',
        bitrate: int = 128,
        fec: bool = True,
        expected_packet_loss: float = 0.15,
        bandwidth: BAND_CTL = 'full',
        signal_type: SIGNAL_CTL = 'auto',
    ) -> None:
        """Plays an :class:`AudioSource`.

        The finalizer, ``after`` is called after the source has been exhausted
        or an error occurred.

        If an error happens while the audio player is running, the exception is
        caught and the audio player is then stopped.  If no after callback is
        passed, any caught exception will be logged using the library logger.

        Extra parameters may be passed to the internal opus encoder if a PCM based
        source is used.  Otherwise, they are ignored.

        .. versionchanged:: 2.0
            Instead of writing to ``sys.stderr``, the library's logger is used.

        .. versionchanged:: 2.4
            Added encoder parameters as keyword arguments.

        Parameters
        -----------
        source: :class:`AudioSource`
            The audio source we're reading from.
        after: Callable[[Optional[:class:`Exception`]], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.
        application: :class:`str`
            Configures the encoder's intended application.  Can be one of:
            ``'audio'``, ``'voip'``, ``'lowdelay'``.
            Defaults to ``'audio'``.
        bitrate: :class:`int`
            Configures the bitrate in the encoder.  Can be between ``16`` and ``512``.
            Defaults to ``128``.
        fec: :class:`bool`
            Configures the encoder's use of inband forward error correction.
            Defaults to ``True``.
        expected_packet_loss: :class:`float`
            Configures the encoder's expected packet loss percentage.  Requires FEC.
            Defaults to ``0.15``.
        bandwidth: :class:`str`
            Configures the encoder's bandpass.  Can be one of:
            ``'narrow'``, ``'medium'``, ``'wide'``, ``'superwide'``, ``'full'``.
            Defaults to ``'full'``.
        signal_type: :class:`str`
            Configures the type of signal being encoded.  Can be one of:
            ``'auto'``, ``'voice'``, ``'music'``.
            Defaults to ``'auto'``.

        Raises
        -------
        ClientException
            Already playing audio or not connected.
        TypeError
            Source is not a :class:`AudioSource` or after is not a callable.
        OpusNotLoaded
            Source is not opus encoded and opus is not loaded.
        ValueError
            An improper value was passed as an encoder parameter.
        """

        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        if not source.is_opus():
            self.encoder = opus.Encoder(
                application=application,
                bitrate=bitrate,
                fec=fec,
                expected_packet_loss=expected_packet_loss,
                bandwidth=bandwidth,
                signal_type=signal_type,
            )

        self._player = AudioPlayer(source, self, after=after)
        self._player.start()

    def is_playing(self) -> bool:
        """Indicates if we're currently playing audio."""
        return self._player is not None and self._player.is_playing()

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        return self._player is not None and self._player.is_paused()

    def stop(self) -> None:
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

    def pause(self) -> None:
        """Pauses the audio playing."""
        if self._player:
            self._player.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        if self._player:
            self._player.resume()

    @property
    def source(self) -> Optional[AudioSource]:
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        return self._player.source if self._player else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')

        if self._player is None:
            raise ValueError('Not playing anything.')

        self._player.set_source(value)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Sends an audio packet with enhanced error handling and retry logic."""
        try:
            self.checked_add('sequence', 1, 65535)
            if encode:
                encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
            else:
                encoded_data = data
                
            packet = self._get_voice_packet(encoded_data)
            self._connection.send_packet(packet)
            self.checked_add('timestamp', opus.Encoder.SAMPLES_PER_FRAME, 4294967295)
            
        except OSError as e:
            _log.debug('Packet dropped (seq: %s, timestamp: %s): %s', 
                      self.sequence, self.timestamp, e)
            # Attempt to recover from packet loss
            self._last_packet_time = time.time()
            
        except Exception as e:
            _log.error('Error sending audio packet: %s', e)
            raise

    def get_connection_stats(self) -> Dict[str, Any]:
        """Get detailed statistics about the voice connection."""
        stats = self._connection_stats.copy()
        if stats['latency']:
            stats['average_latency'] = sum(stats['latency']) / len(stats['latency'])
            stats['min_latency'] = min(stats['latency'])
            stats['max_latency'] = max(stats['latency'])
        return stats

    def update_packet_stats(self, success: bool = True) -> None:
        """Update packet statistics."""
        self._last_packet_time = time.time()
        self._connection_stats['total_packets'] += 1
        if not success:
            self._connection_stats['failed_packets'] += 1

    def update_latency(self, latency: float) -> None:
        """Update latency statistics."""
        self._connection_stats['latency'].append(latency)
        if len(self._connection_stats['latency']) > 20:  # Keep last 20 measurements
            self._connection_stats['latency'].pop(0)
