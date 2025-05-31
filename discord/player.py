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

import threading
import subprocess
import warnings
import audioop
import asyncio
import logging
import shlex
import time
import json
import sys
import re
import io
from typing import Any, Callable, Generic, IO, Optional, TYPE_CHECKING, Tuple, TypeVar, Union, List, Dict
from dataclasses import dataclass, field

from .enums import SpeakingState
from .errors import ClientException
from .opus import Encoder as OpusEncoder, OPUS_SILENCE
from .oggparse import OggStream
from .utils import MISSING

if TYPE_CHECKING:
    from typing_extensions import Self
    from .voice_client import VoiceClient

AT = TypeVar('AT', bound='AudioSource')

_log = logging.getLogger(__name__)

__all__ = (
    'AudioSource',
    'PCMAudio',
    'FFmpegAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
    'PCMVolumeTransformer',
    'AudioPlayer',
    'AudioStats',
)

CREATE_NO_WINDOW: int

if sys.platform != 'win32':
    CREATE_NO_WINDOW = 0
else:
    CREATE_NO_WINDOW = 0x08000000


@dataclass
class AudioStats:
    """Statistics for audio playback.
    
    Attributes
    ----------
    total_bytes: int
        Total bytes processed.
    read_errors: int
        Number of read errors encountered.
    process_errors: int
        Number of process errors encountered.
    startup_time: float
        Time taken to start the audio process.
    uptime: float
        Total uptime of the audio process.
    latency: float
        Current audio latency.
    buffer_size: int
        Current buffer size.
    """
    total_bytes: int = 0
    read_errors: int = 0
    process_errors: int = 0
    startup_time: float = 0.0
    uptime: float = 0.0
    latency: float = 0.0
    buffer_size: int = 0
    last_update: float = field(default_factory=time.time)
    
    def update(self, **kwargs: Any) -> None:
        """Updates the audio statistics.
        
        Parameters
        ----------
        **kwargs: Any
            The statistics to update.
        """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.last_update = time.time()


class AudioSource:
    """Represents an audio stream.

    The audio stream can be Opus encoded or not, however if the audio stream
    is not Opus encoded then the audio format must be 16-bit 48KHz stereo PCM.

    .. warning::

        The audio source reads are done in a separate thread.
    """

    def read(self) -> bytes:
        """Reads 20ms worth of audio.

        Subclasses must implement this.

        If the audio is complete, then returning an empty
        :term:`py:bytes-like object` to signal this is the way to do so.

        If :meth:`~AudioSource.is_opus` method returns ``True``, then it must return
        20ms worth of Opus encoded audio. Otherwise, it must be 20ms
        worth of 16-bit 48KHz stereo PCM, which is about 3,840 bytes
        per frame (20ms worth of audio).

        Returns
        --------
        :class:`bytes`
            A bytes like object that represents the PCM or Opus data.
        """
        raise NotImplementedError

    def is_opus(self) -> bool:
        """Checks if the audio source is already encoded in Opus."""
        return False

    def cleanup(self) -> None:
        """Called when clean-up is needed to be done.

        Useful for clearing buffer data or processes after
        it is done playing audio.
        """
        pass

    def __del__(self) -> None:
        self.cleanup()


class PCMAudio(AudioSource):
    """Represents raw 16-bit 48KHz stereo PCM audio source.

    Attributes
    -----------
    stream: :term:`py:file object`
        A file-like object that reads byte data representing raw PCM.
    """

    def __init__(self, stream: io.BufferedIOBase) -> None:
        self.stream: io.BufferedIOBase = stream
        self._stats = AudioStats()

    def read(self) -> bytes:
        ret = self.stream.read(OpusEncoder.FRAME_SIZE)
        if len(ret) != OpusEncoder.FRAME_SIZE:
            return b''
        self._stats.total_bytes += len(ret)
        return ret

    def get_stats(self) -> AudioStats:
        """Gets the current audio statistics.
        
        Returns
        -------
        :class:`AudioStats`
            The current audio statistics.
        """
        return self._stats


class FFmpegAudio(AudioSource):
    """Represents an FFmpeg (or AVConv) based AudioSource with enhanced reliability."""

    BLOCKSIZE: int = io.DEFAULT_BUFFER_SIZE
    MAX_RETRIES: int = 3
    PROCESS_TIMEOUT: float = 30.0
    BUFFER_SIZE: int = 5

    def __init__(
        self,
        source: Union[str, io.BufferedIOBase],
        *,
        executable: str = 'ffmpeg',
        args: Any,
        **subprocess_kwargs: Any,
    ):
        piping_stdin = subprocess_kwargs.get('stdin') == subprocess.PIPE
        if piping_stdin and isinstance(source, str):
            raise TypeError("parameter conflict: 'source' parameter cannot be a string when piping to stdin")

        stderr: Optional[IO[bytes]] = subprocess_kwargs.pop('stderr', None)

        if stderr == subprocess.PIPE:
            warnings.warn("Passing subprocess.PIPE does nothing", DeprecationWarning, stacklevel=3)
            stderr = None

        piping_stderr = False
        if stderr is not None:
            try:
                stderr.fileno()
            except Exception:
                piping_stderr = True

        args = [executable, *args]
        kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE if piping_stderr else stderr}
        kwargs.update(subprocess_kwargs)

        # Enhanced error tracking and monitoring
        self._error_count: int = 0
        self._last_error_time: float = 0.0
        self._max_errors: int = 5
        self._process_start_time: float = 0.0
        
        # Performance monitoring
        self._stats = AudioStats()

        # Ensure attribute is assigned even in the case of errors
        self._process: subprocess.Popen = MISSING
        self._process = self._spawn_process(args, **kwargs)
        self._stdout: IO[bytes] = self._process.stdout  # type: ignore # process stdout is explicitly set
        self._stdin: Optional[IO[bytes]] = None
        self._stderr: Optional[IO[bytes]] = None
        self._pipe_writer_thread: Optional[threading.Thread] = None
        self._pipe_reader_thread: Optional[threading.Thread] = None
        self._buffer: List[bytes] = []
        self._buffer_lock: threading.Lock = threading.Lock()
        self._buffer_event: threading.Event = threading.Event()
        self._buffer_full: threading.Event = threading.Event()

        if piping_stdin:
            n = f'popen-stdin-writer:pid-{self._process.pid}'
            self._stdin = self._process.stdin
            self._pipe_writer_thread = threading.Thread(target=self._pipe_writer, args=(source,), daemon=True, name=n)
            self._pipe_writer_thread.start()

        if piping_stderr:
            n = f'popen-stderr-reader:pid-{self._process.pid}'
            self._stderr = self._process.stderr
            self._pipe_reader_thread = threading.Thread(target=self._pipe_reader, args=(stderr,), daemon=True, name=n)
            self._pipe_reader_thread.start()

        self._process_start_time = time.time()
        self._stats.startup_time = time.time() - self._process_start_time

    def _spawn_process(self, args: Any, **subprocess_kwargs: Any) -> subprocess.Popen:
        """Spawns the FFmpeg process with enhanced error handling."""
        _log.debug('Spawning ffmpeg process with command: %s', args)
        process = None
        try:
            process = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW, **subprocess_kwargs)
        except FileNotFoundError:
            executable = args.partition(' ')[0] if isinstance(args, str) else args[0]
            raise ClientException(executable + ' was not found.') from None
        except subprocess.SubprocessError as exc:
            raise ClientException(f'Popen failed: {exc.__class__.__name__}: {exc}') from exc
        else:
            return process

    def _kill_process(self) -> None:
        """Kills the FFmpeg process with enhanced cleanup."""
        proc = getattr(self, '_process', MISSING)
        if proc is MISSING:
            return

        _log.debug('Preparing to terminate ffmpeg process %s.', proc.pid)

        try:
            proc.kill()
        except Exception:
            _log.exception('Ignoring error attempting to kill ffmpeg process %s', proc.pid)

        if proc.poll() is None:
            _log.info('ffmpeg process %s has not terminated. Waiting to terminate...', proc.pid)
            try:
                proc.communicate(timeout=self.PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                _log.info('ffmpeg process %s has not terminated. Force killing...', proc.pid)
                proc.kill()
                proc.communicate()

        self._stats.uptime = time.time() - self._process_start_time

    def _handle_error(self, error: Exception) -> None:
        """Handles errors with enhanced tracking."""
        self._error_count += 1
        self._last_error_time = time.time()
        self._stats.process_errors += 1
        
        if self._error_count >= self._max_errors:
            raise ClientException(f'Too many errors occurred: {error}') from error
        
        _log.warning('Error in FFmpeg process: %s', error)

    def _pipe_writer(self, source: io.BufferedIOBase) -> None:
        """Writes data to the FFmpeg process with enhanced error handling."""
        while True:
            try:
                data = source.read(self.BLOCKSIZE)
                if not data:
                    break
                self._stdin.write(data)  # type: ignore
                self._stats.total_bytes += len(data)
            except Exception as exc:
                self._handle_error(exc)
                break
            finally:
                self._stdin.close()  # type: ignore

    def _pipe_reader(self, dest: IO[bytes]) -> None:
        """Reads data from the FFmpeg process with enhanced error handling."""
        while True:
            try:
                data = self._stderr.read(self.BLOCKSIZE)  # type: ignore
                if not data:
                    break
                dest.write(data)
            except Exception as exc:
                self._handle_error(exc)
                break

    def cleanup(self) -> None:
        """Cleans up resources with enhanced error handling."""
        self._kill_process()
        self._stats.uptime = time.time() - self._process_start_time

    def get_stats(self) -> AudioStats:
        """Gets the current audio statistics.
        
        Returns
        -------
        :class:`AudioStats`
            The current audio statistics.
        """
        return self._stats


class FFmpegPCMAudio(FFmpegAudio):
    """Represents an FFmpeg (or AVConv) based AudioSource that produces 16-bit 48KHz stereo PCM audio."""

    def __init__(
        self,
        source: Union[str, io.BufferedIOBase],
        *,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
    ) -> None:
        args = []
        subprocess_kwargs = {'stdin': subprocess.PIPE if pipe else None, 'stderr': stderr}

        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))

        args.append('-i')
        args.append('-' if pipe else source)
        args.extend(('-f', 's16le', '-ar', '48000', '-ac', '2', '-loglevel', 'warning'))

        if isinstance(options, str):
            args.extend(shlex.split(options))

        args.append('pipe:1')

        super().__init__(source, executable=executable, args=args, **subprocess_kwargs)

    def read(self) -> bytes:
        ret = self._stdout.read(OpusEncoder.FRAME_SIZE)
        if len(ret) != OpusEncoder.FRAME_SIZE:
            return b''
        self._stats.total_bytes += len(ret)
        return ret

    def is_opus(self) -> bool:
        return False


class FFmpegOpusAudio(FFmpegAudio):
    """Represents an FFmpeg (or AVConv) based AudioSource that produces Opus encoded audio."""

    def __init__(
        self,
        source: Union[str, io.BufferedIOBase],
        *,
        bitrate: Optional[int] = None,
        codec: Optional[str] = None,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
    ) -> None:
        args = []
        subprocess_kwargs = {'stdin': subprocess.PIPE if pipe else None, 'stderr': stderr}

        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))

        args.append('-i')
        args.append('-' if pipe else source)
        args.extend(('-f', 'opus', '-ar', '48000', '-ac', '2', '-loglevel', 'warning'))

        if bitrate is not None:
            args.extend(('-b:a', str(bitrate)))

        if codec is not None:
            args.extend(('-c:a', codec))

        if isinstance(options, str):
            args.extend(shlex.split(options))

        args.append('pipe:1')

        super().__init__(source, executable=executable, args=args, **subprocess_kwargs)

    @classmethod
    async def from_probe(
        cls,
        source: str,
        *,
        method: Optional[Union[str, Callable[[str, str], Tuple[Optional[str], Optional[int]]]]] = None,
        **kwargs: Any,
    ) -> Self:
        """|coro|

        Creates an FFmpegOpusAudio instance with enhanced error handling and performance monitoring.

        Parameters
        -----------
        source: :class:`str`
            The source to create the audio from.
        method: Optional[Union[:class:`str`, Callable]]
            The method to use for probing the source.
        **kwargs: Any
            Additional keyword arguments to pass to the constructor.

        Returns
        --------
        :class:`FFmpegOpusAudio`
            The created audio instance.

        Raises
        -------
        ClientException
            If the source could not be probed.
        """
        executable = kwargs.get('executable', 'ffmpeg')
        codec, bitrate = await cls.probe(source, method=method, executable=executable)
        return cls(source, bitrate=bitrate, codec=codec, **kwargs)

    @classmethod
    async def probe(
        cls,
        source: str,
        *,
        method: Optional[Union[str, Callable[[str, str], Tuple[Optional[str], Optional[int]]]]] = None,
        executable: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[int]]:
        """|coro|

        Probes the source for codec and bitrate information with enhanced error handling.

        Parameters
        -----------
        source: :class:`str`
            The source to probe.
        method: Optional[Union[:class:`str`, Callable]]
            The method to use for probing.
        executable: Optional[:class:`str`]
            The FFmpeg executable to use.

        Returns
        --------
        Tuple[Optional[:class:`str`], Optional[:class:`int`]]
            The codec and bitrate of the source.

        Raises
        -------
        ClientException
            If the source could not be probed.
        """
        executable = executable or 'ffmpeg'
        method = method or 'native'

        if isinstance(method, str):
            probefunc = getattr(cls, f'_probe_codec_{method}', None)
            if probefunc is None:
                raise ClientException(f'Invalid probe method {method!r}')

            if asyncio.iscoroutinefunction(probefunc):
                codec, bitrate = await probefunc(source, executable)
            else:
                codec, bitrate = probefunc(source, executable)
        else:
            codec, bitrate = method(source, executable)

        return codec, bitrate

    @staticmethod
    def _probe_codec_native(source: str, executable: str = 'ffmpeg') -> Tuple[Optional[str], Optional[int]]:
        """Probes the source using FFmpeg's native probe with enhanced error handling."""
        try:
            command = [executable, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', source]
            process = subprocess.Popen(command, creationflags=CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = process.communicate(timeout=20)

            if process.returncode != 0:
                _log.warning('Failed to probe source %s with error: %s', source, err.decode())
                return None, None

            data = json.loads(out)
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'audio':
                    codec = stream.get('codec_name')
                    bitrate = int(stream.get('bit_rate', 0))
                    return codec, bitrate

            return None, None
        except Exception as exc:
            _log.warning('Failed to probe source %s: %s', source, exc)
            return None, None

    @staticmethod
    def _probe_codec_fallback(source: str, executable: str = 'ffmpeg') -> Tuple[Optional[str], Optional[int]]:
        """Probes the source using a fallback method with enhanced error handling."""
        try:
            command = [executable, '-v', 'error', '-i', source, '-f', 'null', '-']
            process = subprocess.Popen(command, creationflags=CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = process.communicate(timeout=20)

            if process.returncode != 0:
                _log.warning('Failed to probe source %s with error: %s', source, err.decode())
                return None, None

            # Try to find codec and bitrate in the output
            codec_match = re.search(r'Stream.*Audio: (\w+)', err.decode())
            bitrate_match = re.search(r'bitrate: (\d+)', err.decode())

            codec = codec_match.group(1) if codec_match else None
            bitrate = int(bitrate_match.group(1)) if bitrate_match else None

            return codec, bitrate
        except Exception as exc:
            _log.warning('Failed to probe source %s: %s', source, exc)
            return None, None

    def read(self) -> bytes:
        ret = self._stdout.read(OpusEncoder.FRAME_SIZE)
        if len(ret) != OpusEncoder.FRAME_SIZE:
            return b''
        self._stats.total_bytes += len(ret)
        return ret

    def is_opus(self) -> bool:
        return True


class PCMVolumeTransformer(AudioSource, Generic[AT]):
    """Transforms a previous :class:`AudioSource` to have volume controls.

    This does not work with audio sources that have :meth:`AudioSource.is_opus` set to ``True``.

    Parameters
    ------------
    original: :class:`AudioSource`
        The original AudioSource. Must not be Opus encoded.
    volume: :class:`float`
        The initial volume to set it to.
        See :attr:`volume` for more info.

    Raises
    -------
    TypeError
        Not an audio source.
    ClientException
        The audio source is opus encoded.
    """

    def __init__(self, original: AT, volume: float = 1.0):
        if not isinstance(original, AudioSource):
            raise TypeError(f'expected AudioSource not {original.__class__.__name__}.')

        if original.is_opus():
            raise ClientException('Volume cannot be applied to an opus encoded source.')

        self.original: AT = original
        self.volume: float = volume
        self._stats = AudioStats()

    @property
    def volume(self) -> float:
        """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))

    def cleanup(self) -> None:
        self.original.cleanup()

    def read(self) -> bytes:
        ret = self.original.read()
        if not ret:
            return b''
        
        # Apply volume transformation
        ret = audioop.mul(ret, 2, min(self._volume, 2.0))
        self._stats.total_bytes += len(ret)
        return ret

    def get_stats(self) -> AudioStats:
        """Gets the current audio statistics.
        
        Returns
        -------
        :class:`AudioStats`
            The current audio statistics.
        """
        return self._stats


class AudioPlayer(threading.Thread):
    """Represents an audio player with enhanced buffering and error handling."""

    DELAY: float = OpusEncoder.FRAME_LENGTH / 1000.0
    BUFFER_SIZE: int = 5  # Number of frames to buffer
    MAX_RETRIES: int = 3  # Maximum number of retries for failed reads
    BUFFER_TIMEOUT: float = 5.0  # Maximum time to wait for buffer to fill
    ERROR_RESET_TIME: float = 60.0  # Time to reset error count

    def __init__(
        self,
        source: AudioSource,
        client: VoiceClient,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
    ) -> None:
        super().__init__(daemon=True, name=f'audio-player-{id(self)}')
        self.source: AudioSource = source
        self.client: VoiceClient = client
        self.after: Optional[Callable[[Optional[Exception]], Any]] = after

        self._end: threading.Event = threading.Event()
        self._resumed: threading.Event = threading.Event()
        self._resumed.set()  # we are not paused
        self._current_error: Optional[Exception] = None
        self._connected: threading.Event = threading.Event()

        self._buffer: List[bytes] = []
        self._buffer_lock: threading.Lock = threading.Lock()
        self._buffer_event: threading.Event = threading.Event()
        self._buffer_full: threading.Event = threading.Event()

        self._error_count: int = 0
        self._last_error_time: float = 0.0
        self._stats = AudioStats()

        if after is not None and not callable(after):
            raise TypeError('Expected a function or a coroutine for the "after" parameter.')

    def _buffer_worker(self) -> None:
        """Worker thread for buffering audio data with enhanced error handling."""
        while not self._end.is_set():
            # Wait for buffer to have space
            if len(self._buffer) >= self.BUFFER_SIZE:
                self._buffer_full.set()
                self._buffer_event.wait()
                self._buffer_event.clear()
                continue

            # Read data with retry
            data = self._read_with_retry()
            if not data:
                break

            # Add to buffer
            with self._buffer_lock:
                self._buffer.append(data)
                self._stats.buffer_size = len(self._buffer)
                self._stats.total_bytes += len(data)

            # Signal that we have data
            self._buffer_event.set()

    def _read_with_retry(self) -> bytes:
        """Reads audio data with retry logic and error handling."""
        for _ in range(self.MAX_RETRIES):
            try:
                return self.source.read()
            except Exception as exc:
                self._handle_error(exc)
                if self._should_stop():
                    return b''
                time.sleep(0.1)
        return b''

    def _handle_error(self, error: Exception) -> None:
        """Handles errors with enhanced tracking."""
        self._error_count += 1
        self._last_error_time = time.time()
        self._stats.read_errors += 1
        self._current_error = error
        
        if self._error_count >= self.MAX_RETRIES:
            _log.error('Too many errors occurred: %s', error)
            self._end.set()

    def _should_stop(self) -> bool:
        """Checks if the player should stop."""
        if time.time() - self._last_error_time > self.ERROR_RESET_TIME:
            self._error_count = 0
        return self._end.is_set() or self._error_count >= self.MAX_RETRIES

    def _do_run(self) -> None:
        """Main run loop with enhanced error handling and performance monitoring."""
        self._buffer_worker()
        
        while not self._end.is_set():
            # Wait for buffer to have data
            if not self._buffer:
                self._buffer_event.wait()
                if self._end.is_set():
                    break
                continue

            # Get data from buffer
            with self._buffer_lock:
                data = self._buffer.pop(0)
                self._stats.buffer_size = len(self._buffer)

            # Wait for resume if paused
            self._resumed.wait()

            # Send data
            try:
                self.client.send_audio_packet(data, encode=not self.source.is_opus())
                self._stats.total_bytes += len(data)
            except Exception as exc:
                self._handle_error(exc)
                if self._should_stop():
                    break

            # Wait for next frame
            time.sleep(self.DELAY)

    def get_stats(self) -> AudioStats:
        """Gets the current audio statistics.
        
        Returns
        -------
        :class:`AudioStats`
            The current audio statistics.
        """
        return self._stats

    def update_latency(self, latency: float) -> None:
        """Updates the current audio latency.
        
        Parameters
        ----------
        latency: :class:`float`
            The new latency value.
        """
        self._stats.latency = latency

    def run(self) -> None:
        """Main thread entry point with enhanced error handling."""
        try:
            self._do_run()
        except Exception as exc:
            self._handle_error(exc)
        finally:
            self._call_after()
            self.cleanup()

    def _call_after(self) -> None:
        """Calls the after callback with enhanced error handling."""
        if self.after is not None:
            try:
                self.after(self._current_error)
            except Exception as exc:
                _log.exception('Calling the after function failed.')
                self._handle_error(exc)

    def stop(self) -> None:
        """Stops playing audio with enhanced cleanup."""
        self._end.set()
        self._buffer_event.set()
        self._resumed.set()
        self.cleanup()

    def pause(self, *, update_speaking: bool = True) -> None:
        """Pauses the audio player with enhanced state management."""
        self._resumed.clear()
        if update_speaking:
            self._speak(SpeakingState.none)

    def resume(self, *, update_speaking: bool = True) -> None:
        """Resumes the audio player with enhanced state management."""
        self._resumed.set()
        if update_speaking:
            self._speak(SpeakingState.voice)

    def is_playing(self) -> bool:
        """Checks if the audio player is currently playing."""
        return self._resumed.is_set() and not self._end.is_set()

    def is_paused(self) -> bool:
        """Checks if the audio player is currently paused."""
        return not self._resumed.is_set() and not self._end.is_set()

    def set_source(self, source: AudioSource) -> None:
        """Sets a new audio source with enhanced error handling.
        
        Parameters
        ----------
        source: :class:`AudioSource`
            The new audio source to use.
        """
        if not isinstance(source, AudioSource):
            raise TypeError(f'expected AudioSource not {source.__class__.__name__}.')
        
        self.source = source
        self._stats = AudioStats()

    def _speak(self, speaking: SpeakingState) -> None:
        """Updates the speaking state with enhanced error handling."""
        try:
            self.client.speak(speaking)
        except Exception as exc:
            self._handle_error(exc)

    def send_silence(self, count: int = 5) -> None:
        """Sends silence packets with enhanced error handling.
        
        Parameters
        ----------
        count: :class:`int`
            The number of silence packets to send.
        """
        for _ in range(count):
            try:
                self.client.send_audio_packet(OPUS_SILENCE, encode=False)
            except Exception as exc:
                self._handle_error(exc)
                if self._should_stop():
                    break
