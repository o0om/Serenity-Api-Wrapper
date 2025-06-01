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

from typing import TYPE_CHECKING, Optional, Union, Dict, Any, List, Set, ClassVar, Tuple, Protocol, runtime_checkable, TypeVar, Generic, Callable, Awaitable
import datetime
import asyncio
import time
import json
import hashlib
from enum import Enum, auto
from dataclasses import dataclass, field
from collections import defaultdict, deque, OrderedDict
import weakref
import aiohttp
import numpy as np
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from functools import lru_cache, wraps
import pickle
import zlib
import base64

from . import utils
from .mixins import Hashable
from .partial_emoji import PartialEmoji, _EmojiTag
from .user import User
from .utils import MISSING
from .asset import Asset, AssetMixin
from .errors import ValidationError, HTTPException

if TYPE_CHECKING:
    from .types.soundboard import (
        BaseSoundboardSound as BaseSoundboardSoundPayload,
        SoundboardDefaultSound as SoundboardDefaultSoundPayload,
        SoundboardSound as SoundboardSoundPayload,
    )
    from .state import ConnectionState
    from .guild import Guild
    from .message import EmojiInputType

__all__ = (
    'BaseSoundboardSound', 'SoundboardDefaultSound', 'SoundboardSound', 
    'SoundCategory', 'SoundboardValidationError', 'SoundEffect', 'SoundAnalytics',
    'SoundboardRateLimitError', 'SoundboardCache', 'AISoundProcessor', 'DistributedCache',
    'SoundboardMetrics', 'SoundboardOptimizer', 'SoundboardPredictor', 'SoundboardRecommender'
)

T = TypeVar('T')
logger = logging.getLogger(__name__)

class SoundboardMetrics:
    """Advanced metrics and monitoring for soundboard operations.
    
    .. versionadded:: 2.8
    """
    def __init__(self):
        self._metrics: Dict[str, Any] = defaultdict(lambda: {
            'count': 0,
            'total_time': 0.0,
            'errors': 0,
            'last_error': None,
            'success_rate': 1.0,
            'latency': deque(maxlen=1000),
            'concurrent_operations': 0
        })
        self._lock = threading.Lock()
        self._start_time = time.time()
        
    def record_operation(self, operation: str, duration: float, success: bool = True, error: Optional[Exception] = None):
        """Records an operation's metrics.
        
        Parameters
        -----------
        operation: :class:`str`
            The name of the operation.
        duration: :class:`float`
            The duration of the operation.
        success: :class:`bool`
            Whether the operation was successful.
        error: Optional[:class:`Exception`]
            The error that occurred, if any.
        """
        with self._lock:
            metrics = self._metrics[operation]
            metrics['count'] += 1
            metrics['total_time'] += duration
            metrics['latency'].append(duration)
            
            if not success:
                metrics['errors'] += 1
                metrics['last_error'] = str(error)
                metrics['success_rate'] = 1 - (metrics['errors'] / metrics['count'])
                
    def get_metrics(self, operation: Optional[str] = None) -> Dict[str, Any]:
        """Gets metrics for an operation or all operations.
        
        Parameters
        -----------
        operation: Optional[:class:`str`]
            The operation to get metrics for, or None for all operations.
            
        Returns
        -------
        Dict[str, Any]
            The metrics data.
        """
        with self._lock:
            if operation:
                return dict(self._metrics[operation])
            return {op: dict(metrics) for op, metrics in self._metrics.items()}
            
    def get_uptime(self) -> float:
        """Gets the uptime in seconds.
        
        Returns
        -------
        :class:`float`
            The uptime in seconds.
        """
        return time.time() - self._start_time

class DistributedCache:
    """A distributed cache implementation for soundboard sounds.
    
    .. versionadded:: 2.8
    """
    def __init__(self, nodes: List[str], ttl: int = 3600):
        self.nodes = nodes
        self.ttl = ttl
        self._local_cache = {}
        self._lock = asyncio.Lock()
        self._session = None
        self._metrics = SoundboardMetrics()
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Gets or creates an aiohttp session.
        
        Returns
        -------
        :class:`aiohttp.ClientSession`
            The session.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
        
    async def get(self, key: str) -> Optional[Any]:
        """Gets a value from the distributed cache.
        
        Parameters
        -----------
        key: :class:`str`
            The key to get.
            
        Returns
        -------
        Optional[Any]
            The cached value, or None if not found.
        """
        start_time = time.time()
        try:
            # Try local cache first
            if key in self._local_cache:
                value, timestamp = self._local_cache[key]
                if time.time() - timestamp <= self.ttl:
                    self._metrics.record_operation('cache_get_local', time.time() - start_time)
                    return value
                    
            # Try distributed cache
            session = await self._get_session()
            for node in self.nodes:
                try:
                    async with session.get(f"{node}/cache/{key}") as response:
                        if response.status == 200:
                            data = await response.json()
                            value = pickle.loads(zlib.decompress(base64.b64decode(data['value'])))
                            self._local_cache[key] = (value, time.time())
                            self._metrics.record_operation('cache_get_distributed', time.time() - start_time)
                            return value
                except Exception as e:
                    logger.warning(f"Failed to get from node {node}: {e}")
                    continue
                    
            self._metrics.record_operation('cache_get_miss', time.time() - start_time)
            return None
        except Exception as e:
            self._metrics.record_operation('cache_get_error', time.time() - start_time, False, e)
            return None
            
    async def set(self, key: str, value: Any) -> bool:
        """Sets a value in the distributed cache.
        
        Parameters
        -----------
        key: :class:`str`
            The key to set.
        value: Any
            The value to cache.
            
        Returns
        -------
        :class:`bool`
            Whether the operation was successful.
        """
        start_time = time.time()
        try:
            # Set in local cache
            self._local_cache[key] = (value, time.time())
            
            # Set in distributed cache
            session = await self._get_session()
            serialized = base64.b64encode(zlib.compress(pickle.dumps(value))).decode()
            
            success = False
            for node in self.nodes:
                try:
                    async with session.post(f"{node}/cache/{key}", json={'value': serialized, 'ttl': self.ttl}) as response:
                        if response.status == 200:
                            success = True
                            break
                except Exception as e:
                    logger.warning(f"Failed to set in node {node}: {e}")
                    continue
                    
            self._metrics.record_operation('cache_set', time.time() - start_time, success)
            return success
        except Exception as e:
            self._metrics.record_operation('cache_set_error', time.time() - start_time, False, e)
            return False
            
    async def close(self):
        """Closes the cache and cleans up resources."""
        if self._session and not self._session.closed:
            await self._session.close()

class AISoundProcessor:
    """AI-powered sound processing and analysis.
    
    .. versionadded:: 2.8
    """
    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path
        self._model = None
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._metrics = SoundboardMetrics()
        
    async def load_model(self):
        """Loads the AI model for sound processing."""
        if self._model is not None:
            return
            
        def _load():
            # This is a placeholder for actual model loading
            # In a real implementation, this would load a trained model
            return None
            
        self._model = await asyncio.get_event_loop().run_in_executor(self._executor, _load)
        
    async def process_sound(self, sound_data: bytes) -> bytes:
        """Processes sound data using AI.
        
        Parameters
        -----------
        sound_data: :class:`bytes`
            The sound data to process.
            
        Returns
        -------
        :class:`bytes`
            The processed sound data.
        """
        start_time = time.time()
        try:
            if self._model is None:
                await self.load_model()
                
            def _process():
                # This is a placeholder for actual AI processing
                # In a real implementation, this would use the loaded model
                return sound_data
                
            result = await asyncio.get_event_loop().run_in_executor(self._executor, _process)
            self._metrics.record_operation('ai_process', time.time() - start_time)
            return result
        except Exception as e:
            self._metrics.record_operation('ai_process_error', time.time() - start_time, False, e)
            return sound_data
            
    async def analyze_sound(self, sound_data: bytes) -> Dict[str, Any]:
        """Analyzes sound data using AI.
        
        Parameters
        -----------
        sound_data: :class:`bytes`
            The sound data to analyze.
            
        Returns
        -------
        Dict[str, Any]
            Analysis results.
        """
        start_time = time.time()
        try:
            if self._model is None:
                await self.load_model()
                
            def _analyze():
                # This is a placeholder for actual AI analysis
                # In a real implementation, this would use the loaded model
                return {
                    'duration': 0.0,
                    'format': 'unknown',
                    'quality': 'unknown',
                    'features': []
                }
                
            result = await asyncio.get_event_loop().run_in_executor(self._executor, _analyze)
            self._metrics.record_operation('ai_analyze', time.time() - start_time)
            return result
        except Exception as e:
            self._metrics.record_operation('ai_analyze_error', time.time() - start_time, False, e)
            return {}

class SoundboardPredictor:
    """Predicts sound usage patterns and optimizes caching.
    
    .. versionadded:: 2.8
    """
    def __init__(self, history_size: int = 1000):
        self.history_size = history_size
        self._play_history = deque(maxlen=history_size)
        self._user_history = defaultdict(lambda: deque(maxlen=history_size))
        self._time_history = defaultdict(int)
        self._metrics = SoundboardMetrics()
        
    def record_play(self, sound_id: int, user_id: int, timestamp: float):
        """Records a sound play for prediction.
        
        Parameters
        -----------
        sound_id: :class:`int`
            The ID of the sound.
        user_id: :class:`int`
            The ID of the user.
        timestamp: :class:`float`
            The timestamp of the play.
        """
        self._play_history.append((sound_id, user_id, timestamp))
        self._user_history[user_id].append((sound_id, timestamp))
        self._time_history[int(timestamp / 3600)] += 1
        
    def predict_next_plays(self, user_id: int, limit: int = 5) -> List[int]:
        """Predicts the next sounds a user will play.
        
        Parameters
        -----------
        user_id: :class:`int`
            The ID of the user.
        limit: :class:`int`
            The maximum number of predictions to return.
            
        Returns
        -------
        List[:class:`int`]
            The predicted sound IDs.
        """
        start_time = time.time()
        try:
            # Simple prediction based on user history
            user_sounds = defaultdict(int)
            for sound_id, _ in self._user_history[user_id]:
                user_sounds[sound_id] += 1
                
            predictions = sorted(user_sounds.items(), key=lambda x: x[1], reverse=True)[:limit]
            self._metrics.record_operation('predict_next_plays', time.time() - start_time)
            return [sound_id for sound_id, _ in predictions]
        except Exception as e:
            self._metrics.record_operation('predict_next_plays_error', time.time() - start_time, False, e)
            return []
            
    def predict_popular_sounds(self, hours: int = 24, limit: int = 10) -> List[int]:
        """Predicts the most popular sounds in the next time period.
        
        Parameters
        -----------
        hours: :class:`int`
            The number of hours to look ahead.
        limit: :class:`int`
            The maximum number of predictions to return.
            
        Returns
        -------
        List[:class:`int`]
            The predicted sound IDs.
        """
        start_time = time.time()
        try:
            # Simple prediction based on recent history
            sound_counts = defaultdict(int)
            current_hour = int(time.time() / 3600)
            
            for sound_id, _, timestamp in self._play_history:
                hour = int(timestamp / 3600)
                if current_hour - hour < hours:
                    sound_counts[sound_id] += 1
                    
            predictions = sorted(sound_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
            self._metrics.record_operation('predict_popular_sounds', time.time() - start_time)
            return [sound_id for sound_id, _ in predictions]
        except Exception as e:
            self._metrics.record_operation('predict_popular_sounds_error', time.time() - start_time, False, e)
            return []

class SoundboardRecommender:
    """Recommends sounds based on user behavior and sound characteristics.
    
    .. versionadded:: 2.8
    """
    def __init__(self):
        self._user_sounds = defaultdict(set)
        self._sound_similarity = defaultdict(dict)
        self._metrics = SoundboardMetrics()
        
    def record_user_sound(self, user_id: int, sound_id: int):
        """Records a user's interaction with a sound.
        
        Parameters
        -----------
        user_id: :class:`int`
            The ID of the user.
        sound_id: :class:`int`
            The ID of the sound.
        """
        self._user_sounds[user_id].add(sound_id)
        
    def update_similarity(self, sound_id1: int, sound_id2: int, similarity: float):
        """Updates the similarity between two sounds.
        
        Parameters
        -----------
        sound_id1: :class:`int`
            The ID of the first sound.
        sound_id2: :class:`int`
            The ID of the second sound.
        similarity: :class:`float`
            The similarity score.
        """
        self._sound_similarity[sound_id1][sound_id2] = similarity
        self._sound_similarity[sound_id2][sound_id1] = similarity
        
    def get_recommendations(self, user_id: int, limit: int = 5) -> List[int]:
        """Gets sound recommendations for a user.
        
        Parameters
        -----------
        user_id: :class:`int`
            The ID of the user.
        limit: :class:`int`
            The maximum number of recommendations to return.
            
        Returns
        -------
        List[:class:`int`]
            The recommended sound IDs.
        """
        start_time = time.time()
        try:
            user_sounds = self._user_sounds[user_id]
            if not user_sounds:
                return []
                
            # Calculate recommendation scores
            scores = defaultdict(float)
            for sound_id in user_sounds:
                for similar_id, similarity in self._sound_similarity[sound_id].items():
                    if similar_id not in user_sounds:
                        scores[similar_id] += similarity
                        
            recommendations = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
            self._metrics.record_operation('get_recommendations', time.time() - start_time)
            return [sound_id for sound_id, _ in recommendations]
        except Exception as e:
            self._metrics.record_operation('get_recommendations_error', time.time() - start_time, False, e)
            return []

class SoundboardOptimizer:
    """Optimizes soundboard performance and resource usage.
    
    .. versionadded:: 2.8
    """
    def __init__(self):
        self._cache_stats = defaultdict(lambda: {'hits': 0, 'misses': 0})
        self._resource_usage = defaultdict(float)
        self._metrics = SoundboardMetrics()
        
    def record_cache_access(self, cache_name: str, hit: bool):
        """Records a cache access.
        
        Parameters
        -----------
        cache_name: :class:`str`
            The name of the cache.
        hit: :class:`bool`
            Whether it was a cache hit.
        """
        stats = self._cache_stats[cache_name]
        if hit:
            stats['hits'] += 1
        else:
            stats['misses'] += 1
            
    def record_resource_usage(self, resource: str, amount: float):
        """Records resource usage.
        
        Parameters
        -----------
        resource: :class:`str`
            The name of the resource.
        amount: :class:`float`
            The amount used.
        """
        self._resource_usage[resource] += amount
        
    def get_cache_stats(self) -> Dict[str, Dict[str, int]]:
        """Gets cache statistics.
        
        Returns
        -------
        Dict[str, Dict[str, int]]
            The cache statistics.
        """
        return dict(self._cache_stats)
        
    def get_resource_usage(self) -> Dict[str, float]:
        """Gets resource usage statistics.
        
        Returns
        -------
        Dict[str, float]
            The resource usage statistics.
        """
        return dict(self._resource_usage)
        
    def optimize_cache(self, cache: DistributedCache) -> None:
        """Optimizes cache settings based on usage patterns.
        
        Parameters
        -----------
        cache: :class:`DistributedCache`
            The cache to optimize.
        """
        start_time = time.time()
        try:
            stats = self._cache_stats['distributed']
            hit_rate = stats['hits'] / (stats['hits'] + stats['misses']) if (stats['hits'] + stats['misses']) > 0 else 0
            
            # Adjust TTL based on hit rate
            if hit_rate < 0.5:
                cache.ttl = min(cache.ttl * 1.5, 7200)  # Increase TTL up to 2 hours
            elif hit_rate > 0.8:
                cache.ttl = max(cache.ttl * 0.8, 1800)  # Decrease TTL down to 30 minutes
                
            self._metrics.record_operation('optimize_cache', time.time() - start_time)
        except Exception as e:
            self._metrics.record_operation('optimize_cache_error', time.time() - start_time, False, e)

@dataclass
class SoundAnalytics:
    """Represents analytics data for a sound.
    
    .. versionadded:: 2.7
    """
    play_count: int = 0
    unique_users: Set[int] = field(default_factory=set)
    play_times: List[datetime.datetime] = field(default_factory=list)
    average_volume: float = 1.0
    total_duration: float = 0.0
    last_played: Optional[datetime.datetime] = None
    
    def add_play(self, user_id: int, volume: float, duration: float) -> None:
        """Adds a play to the analytics.
        
        Parameters
        -----------
        user_id: :class:`int`
            The ID of the user who played the sound.
        volume: :class:`float`
            The volume the sound was played at.
        duration: :class:`float`
            The duration of the sound.
        """
        self.play_count += 1
        self.unique_users.add(user_id)
        now = datetime.datetime.now()
        self.play_times.append(now)
        self.last_played = now
        self.total_duration += duration
        self.average_volume = ((self.average_volume * (self.play_count - 1)) + volume) / self.play_count

    def get_play_frequency(self, hours: int = 24) -> float:
        """Gets the average number of plays per hour.
        
        Parameters
        -----------
        hours: :class:`int`
            The number of hours to look back.
            
        Returns
        -------
        :class:`float`
            The average number of plays per hour.
        """
        if not self.play_times:
            return 0.0
            
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
        recent_plays = [t for t in self.play_times if t > cutoff]
        return len(recent_plays) / hours

    def get_unique_users_count(self) -> int:
        """Gets the number of unique users who have played the sound.
        
        Returns
        -------
        :class:`int`
            The number of unique users.
        """
        return len(self.unique_users)

@dataclass
class SoundEffect:
    """Represents a sound effect that can be applied to a sound.
    
    .. versionadded:: 2.7
    """
    name: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    
    def apply(self, sound_data: bytes) -> bytes:
        """Applies the effect to the sound data.
        
        Parameters
        -----------
        sound_data: :class:`bytes`
            The sound data to apply the effect to.
            
        Returns
        -------
        :class:`bytes`
            The modified sound data.
        """
        # This is a placeholder for actual sound effect processing
        # In a real implementation, this would use a sound processing library
        return sound_data

class SoundboardCache:
    """A cache manager for soundboard sounds.
    
    .. versionadded:: 2.7
    """
    def __init__(self, max_size: int = 1000, ttl: int = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: Dict[int, Tuple[Any, float]] = {}
        self._lock = asyncio.Lock()
        
    async def get(self, key: int) -> Optional[Any]:
        """Gets a value from the cache.
        
        Parameters
        -----------
        key: :class:`int`
            The key to get.
            
        Returns
        -------
        Optional[Any]
            The cached value, or None if not found or expired.
        """
        async with self._lock:
            if key not in self._cache:
                return None
                
            value, timestamp = self._cache[key]
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                return None
                
            return value
            
    async def set(self, key: int, value: Any) -> None:
        """Sets a value in the cache.
        
        Parameters
        -----------
        key: :class:`int`
            The key to set.
        value: Any
            The value to cache.
        """
        async with self._lock:
            if len(self._cache) >= self.max_size:
                # Remove oldest entry
                oldest_key = min(self._cache.items(), key=lambda x: x[1][1])[0]
                del self._cache[oldest_key]
                
            self._cache[key] = (value, time.time())

class SoundCategory(Enum):
    """Represents a category for soundboard sounds.
    
    .. versionadded:: 2.6
    """
    MUSIC = auto()
    EFFECT = auto()
    VOICE = auto()
    AMBIENT = auto()
    CUSTOM = auto()
    
    @classmethod
    def from_str(cls, value: str) -> 'SoundCategory':
        """Converts a string to a SoundCategory.
        
        Parameters
        -----------
        value: :class:`str`
            The string to convert.
            
        Returns
        -------
        :class:`SoundCategory`
            The corresponding SoundCategory.
        """
        try:
            return cls[value.upper()]
        except KeyError:
            return cls.CUSTOM

class SoundboardValidationError(ValidationError):
    """Exception raised when soundboard validation fails."""
    pass

class SoundboardRateLimitError(HTTPException):
    """Exception raised when soundboard rate limit is exceeded."""
    pass

@runtime_checkable
class SoundProcessor(Protocol):
    """Protocol for sound processing.
    
    .. versionadded:: 2.7
    """
    def process(self, sound_data: bytes) -> bytes:
        """Processes sound data.
        
        Parameters
        -----------
        sound_data: :class:`bytes`
            The sound data to process.
            
        Returns
        -------
        :class:`bytes`
            The processed sound data.
        """
        ...

class BaseSoundboardSound(Hashable, AssetMixin):
    """Represents a generic Discord soundboard sound.

    .. versionadded:: 2.5

    .. container:: operations

        .. describe:: x == y

            Checks if two sounds are equal.

        .. describe:: x != y

            Checks if two sounds are not equal.

        .. describe:: hash(x)

            Returns the sound's hash.

    Attributes
    ------------
    id: :class:`int`
        The ID of the sound.
    volume: :class:`float`
        The volume of the sound as floating point percentage (e.g. ``1.0`` for 100%).
    """

    __slots__ = ('_state', 'id', 'volume', '_cached_url', '_cached_metadata', '_last_played', 
                 '_analytics', '_effects', '_processor', '_ai_processor', '_cache', '_predictor',
                 '_recommender', '_optimizer', '_metrics')
    
    # Class-level cache for sound metadata
    _metadata_cache: ClassVar[DistributedCache] = DistributedCache(['http://cache1.example.com', 'http://cache2.example.com'])
    _cache_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    
    # Rate limiting
    _play_cooldown: ClassVar[float] = 0.5  # seconds
    _last_play_times: ClassVar[Dict[int, float]] = {}
    
    # Sound processing
    _processors: ClassVar[Dict[str, SoundProcessor]] = {}
    _ai_processor: ClassVar[AISoundProcessor] = AISoundProcessor()
    
    # Analytics and optimization
    _predictor: ClassVar[SoundboardPredictor] = SoundboardPredictor()
    _recommender: ClassVar[SoundboardRecommender] = SoundboardRecommender()
    _optimizer: ClassVar[SoundboardOptimizer] = SoundboardOptimizer()
    _metrics: ClassVar[SoundboardMetrics] = SoundboardMetrics()

    def __init__(self, *, state: ConnectionState, data: BaseSoundboardSoundPayload):
        self._state: ConnectionState = state
        self.id: int = int(data['sound_id'])
        self._cached_url: Optional[str] = None
        self._cached_metadata: Optional[Dict[str, Any]] = None
        self._last_played: Optional[datetime.datetime] = None
        self._analytics: SoundAnalytics = SoundAnalytics()
        self._effects: List[SoundEffect] = []
        self._processor: Optional[SoundProcessor] = None
        self._update(data)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, self.__class__):
            return self.id == other.id
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def _update(self, data: BaseSoundboardSoundPayload):
        self.volume: float = data['volume']
        self._validate_volume()

    def _validate_volume(self) -> None:
        """Validates the volume value.
        
        Raises
        -------
        SoundboardValidationError
            If the volume is not between 0 and 1.
        """
        if not 0 <= self.volume <= 1:
            raise SoundboardValidationError(f'Volume must be between 0 and 1, got {self.volume}')

    @property
    def url(self) -> str:
        """:class:`str`: Returns the URL of the sound."""
        if self._cached_url is None:
            self._cached_url = f'{Asset.BASE}/soundboard-sounds/{self.id}'
        return self._cached_url

    @property
    def created_at(self) -> Optional[datetime.datetime]:
        """Optional[:class:`datetime.datetime`]: Returns the snowflake's creation time in UTC.
        Returns ``None`` if it's a default sound."""
        if self.is_default():
            return None
        return utils.snowflake_time(self.id)

    def is_default(self) -> bool:
        """:class:`bool`: Whether it's a default sound or not."""
        return self.id < utils.DISCORD_EPOCH

    @property
    def analytics(self) -> SoundAnalytics:
        """:class:`SoundAnalytics`: The analytics data for this sound."""
        return self._analytics

    def add_effect(self, effect: SoundEffect) -> None:
        """Adds a sound effect to this sound.
        
        Parameters
        -----------
        effect: :class:`SoundEffect`
            The effect to add.
        """
        self._effects.append(effect)

    def remove_effect(self, effect_name: str) -> None:
        """Removes a sound effect from this sound.
        
        Parameters
        -----------
        effect_name: :class:`str`
            The name of the effect to remove.
        """
        self._effects = [e for e in self._effects if e.name != effect_name]

    def set_processor(self, processor: SoundProcessor) -> None:
        """Sets the sound processor for this sound.
        
        Parameters
        -----------
        processor: :class:`SoundProcessor`
            The processor to use.
        """
        self._processor = processor

    @classmethod
    def register_processor(cls, name: str, processor: SoundProcessor) -> None:
        """Registers a sound processor.
        
        Parameters
        -----------
        name: :class:`str`
            The name of the processor.
        processor: :class:`SoundProcessor`
            The processor to register.
        """
        cls._processors[name] = processor

    async def process_with_ai(self, sound_data: bytes) -> bytes:
        """|coro|

        Processes the sound data using AI.
        
        Parameters
        -----------
        sound_data: :class:`bytes`
            The sound data to process.
            
        Returns
        -------
        :class:`bytes`
            The processed sound data.
        """
        return await self._ai_processor.process_sound(sound_data)

    async def analyze_with_ai(self) -> Dict[str, Any]:
        """|coro|

        Analyzes the sound using AI.
        
        Returns
        -------
        Dict[str, Any]
            Analysis results.
        """
        sound_data = await self._state.http.get_soundboard_sound_data(self.id)
        return await self._ai_processor.analyze_sound(sound_data)

    async def get_recommendations(self, limit: int = 5) -> List[int]:
        """|coro|

        Gets sound recommendations based on this sound.
        
        Parameters
        -----------
        limit: :class:`int`
            The maximum number of recommendations to return.
            
        Returns
        -------
        List[:class:`int`]
            The recommended sound IDs.
        """
        return self._recommender.get_recommendations(self.id, limit)

    async def predict_next_plays(self, user_id: int, limit: int = 5) -> List[int]:
        """|coro|

        Predicts the next sounds a user will play after this one.
        
        Parameters
        -----------
        user_id: :class:`int`
            The ID of the user.
        limit: :class:`int`
            The maximum number of predictions to return.
            
        Returns
        -------
        List[:class:`int`]
            The predicted sound IDs.
        """
        return self._predictor.predict_next_plays(user_id, limit)

    def record_play(self, user_id: int, volume: float, duration: float) -> None:
        """Records a play of this sound.
        
        Parameters
        -----------
        user_id: :class:`int`
            The ID of the user who played the sound.
        volume: :class:`float`
            The volume the sound was played at.
        duration: :class:`float`
            The duration of the sound.
        """
        self._analytics.add_play(user_id, volume, duration)
        self._predictor.record_play(self.id, user_id, time.time())
        self._recommender.record_user_sound(user_id, self.id)
        self._metrics.record_operation('sound_play', duration)

    async def get_metadata(self) -> Optional[Dict[str, Any]]:
        """|coro|

        Gets the metadata for this sound.
        This includes information like duration, format, etc.
        Results are cached to avoid repeated API calls.

        Returns
        -------
        Optional[Dict[str, Any]]
            The sound metadata, or None if not available.
        """
        if self._cached_metadata is not None:
            self._optimizer.record_cache_access('local', True)
            return self._cached_metadata

        cached = await self._metadata_cache.get(str(self.id))
        if cached is not None:
            self._cached_metadata = cached
            self._optimizer.record_cache_access('distributed', True)
            return cached

        try:
            data = await self._state.http.get_soundboard_sound_metadata(self.id)
            await self._metadata_cache.set(str(self.id), data)
            self._cached_metadata = data
            self._optimizer.record_cache_access('distributed', False)
            return data
        except:
            return None

    async def process_sound(self, sound_data: bytes) -> bytes:
        """|coro|

        Processes the sound data with any registered effects, processors, and AI.
        
        Parameters
        -----------
        sound_data: :class:`bytes`
            The sound data to process.
            
        Returns
        -------
        :class:`bytes`
            The processed sound data.
        """
        start_time = time.time()
        try:
            # Apply effects
            for effect in self._effects:
                sound_data = effect.apply(sound_data)
                
            # Apply processor if set
            if self._processor is not None:
                sound_data = self._processor.process(sound_data)
                
            # Apply AI processing
            sound_data = await self.process_with_ai(sound_data)
            
            self._metrics.record_operation('process_sound', time.time() - start_time)
            return sound_data
        except Exception as e:
            self._metrics.record_operation('process_sound_error', time.time() - start_time, False, e)
            return sound_data

class SoundboardDefaultSound(BaseSoundboardSound):
    """Represents a Discord soundboard default sound.

    .. versionadded:: 2.5

    .. container:: operations

        .. describe:: x == y

            Checks if two sounds are equal.

        .. describe:: x != y

            Checks if two sounds are not equal.

        .. describe:: hash(x)

            Returns the sound's hash.

    Attributes
    ------------
    id: :class:`int`
        The ID of the sound.
    volume: :class:`float`
        The volume of the sound as floating point percentage (e.g. ``1.0`` for 100%).
    name: :class:`str`
        The name of the sound.
    emoji: :class:`PartialEmoji`
        The emoji of the sound.
    category: :class:`SoundCategory`
        The category of the sound.
    """

    __slots__ = ('name', 'emoji', 'category', '_ai_analysis', '_quality_score', '_popularity_score')

    def __init__(self, *, state: ConnectionState, data: SoundboardDefaultSoundPayload):
        self.name: str = data['name']
        self.emoji: PartialEmoji = PartialEmoji(name=data['emoji_name'])
        self.category: SoundCategory = SoundCategory[data.get('category', 'CUSTOM')]
        self._ai_analysis: Optional[Dict[str, Any]] = None
        self._quality_score: float = 0.0
        self._popularity_score: float = 0.0
        super().__init__(state=state, data=data)

    async def analyze_quality(self) -> float:
        """|coro|

        Analyzes the quality of the default sound using AI.
        
        Returns
        -------
        :class:`float`
            A quality score between 0 and 1.
        """
        if self._quality_score > 0:
            return self._quality_score
            
        analysis = await self.analyze_with_ai()
        if not analysis:
            return 0.0
            
        # Calculate quality score based on various factors
        duration_score = min(1.0, analysis.get('duration', 0) / 30)  # Prefer sounds under 30 seconds
        format_score = 1.0 if analysis.get('format') in ['mp3', 'wav'] else 0.5
        feature_score = len(analysis.get('features', [])) / 10  # Normalize feature count
        
        self._quality_score = (duration_score + format_score + feature_score) / 3
        return self._quality_score

    async def calculate_popularity(self) -> float:
        """|coro|

        Calculates a popularity score for this default sound.
        
        Returns
        -------
        :class:`float`
            A popularity score between 0 and 1.
        """
        if self._popularity_score > 0:
            return self._popularity_score
            
        # Get analytics data
        play_count = self._analytics.play_count
        unique_users = self._analytics.get_unique_users_count()
        play_frequency = self._analytics.get_play_frequency()
        
        # Calculate popularity score
        count_score = min(1.0, play_count / 1000)  # Normalize play count
        user_score = min(1.0, unique_users / 100)  # Normalize unique users
        frequency_score = min(1.0, play_frequency / 10)  # Normalize play frequency
        
        self._popularity_score = (count_score + user_score + frequency_score) / 3
        return self._popularity_score

    async def get_play_insights(self) -> Dict[str, Any]:
        """|coro|

        Gets insights about how this default sound is played.
        
        Returns
        -------
        Dict[str, Any]
            Insights about the sound's usage.
        """
        return {
            'total_plays': self._analytics.play_count,
            'unique_users': self._analytics.get_unique_users_count(),
            'play_frequency': self._analytics.get_play_frequency(),
            'average_volume': self._analytics.average_volume,
            'total_duration': self._analytics.total_duration,
            'last_played': self._analytics.last_played,
            'popularity_score': await self.calculate_popularity(),
            'quality_score': await self.analyze_quality(),
            'category': self.category.name,
            'is_default': True
        }

    def __repr__(self) -> str:
        attrs = [
            ('id', self.id),
            ('name', self.name),
            ('volume', self.volume),
            ('emoji', self.emoji),
            ('category', self.category),
            ('quality_score', self._quality_score),
            ('popularity_score', self._popularity_score)
        ]
        inner = ' '.join('%s=%r' % t for t in attrs)
        return f"<{self.__class__.__name__} {inner}>"

class SoundboardSound(BaseSoundboardSound):
    """Represents a Discord soundboard sound.

    .. versionadded:: 2.5

    .. container:: operations

        .. describe:: x == y

            Checks if two sounds are equal.

        .. describe:: x != y

            Checks if two sounds are not equal.

        .. describe:: hash(x)

            Returns the sound's hash.

    Attributes
    ------------
    id: :class:`int`
        The ID of the sound.
    volume: :class:`float`
        The volume of the sound as floating point percentage (e.g. ``1.0`` for 100%).
    name: :class:`str`
        The name of the sound.
    emoji: Optional[:class:`PartialEmoji`]
        The emoji of the sound. ``None`` if no emoji is set.
    guild: :class:`Guild`
        The guild in which the sound is uploaded.
    available: :class:`bool`
        Whether this sound is available for use.
    category: :class:`SoundCategory`
        The category of the sound.
    tags: Set[:class:`str`]
        A set of tags associated with the sound.
    """

    __slots__ = ('_state', 'name', 'emoji', '_user', 'available', '_user_id', 'guild', 
                 '_cached_duration', 'category', 'tags', '_play_count', '_ai_analysis',
                 '_similar_sounds', '_popularity_score', '_quality_score')

    def __init__(self, *, guild: Guild, state: ConnectionState, data: SoundboardSoundPayload):
        super().__init__(state=state, data=data)
        self.guild = guild
        self._user_id = utils._get_as_snowflake(data, 'user_id')
        self._user = data.get('user')
        self._cached_duration: Optional[float] = None
        self._play_count: int = 0
        self._ai_analysis: Optional[Dict[str, Any]] = None
        self._similar_sounds: List[int] = []
        self._popularity_score: float = 0.0
        self._quality_score: float = 0.0
        self._update(data)

    def __repr__(self) -> str:
        attrs = [
            ('id', self.id),
            ('name', self.name),
            ('volume', self.volume),
            ('emoji', self.emoji),
            ('user', self.user),
            ('available', self.available),
            ('category', self.category),
            ('play_count', self._play_count),
        ]
        inner = ' '.join('%s=%r' % t for t in attrs)
        return f"<{self.__class__.__name__} {inner}>"

    def _update(self, data: SoundboardSoundPayload):
        super()._update(data)

        self.name: str = data['name']
        self._validate_name()
        
        self.emoji: Optional[PartialEmoji] = None
        emoji_id = utils._get_as_snowflake(data, 'emoji_id')
        emoji_name = data['emoji_name']
        if emoji_id is not None or emoji_name is not None:
            self.emoji = PartialEmoji(id=emoji_id, name=emoji_name)  # type: ignore # emoji_name cannot be None here

        self.available: bool = data['available']
        self.category: SoundCategory = SoundCategory[data.get('category', 'CUSTOM')]
        self.tags: Set[str] = set(data.get('tags', []))

    def _validate_name(self) -> None:
        """Validates the name value.
        
        Raises
        -------
        SoundboardValidationError
            If the name length is not between 2 and 32 characters.
        """
        if not 2 <= len(self.name) <= 32:
            raise SoundboardValidationError(f'Name must be between 2 and 32 characters, got length {len(self.name)}')

    @property
    def user(self) -> Optional[User]:
        """Optional[:class:`User`]: The user who uploaded the sound."""
        if self._user is None:
            if self._user_id is None:
                return None
            return self._state.get_user(self._user_id)
        return User(state=self._state, data=self._user)

    @property
    def play_count(self) -> int:
        """int: The number of times this sound has been played."""
        return self._play_count

    def increment_play_count(self) -> None:
        """Increments the play count for this sound."""
        self._play_count += 1

    async def analyze_quality(self) -> float:
        """|coro|

        Analyzes the quality of the sound using AI.
        
        Returns
        -------
        :class:`float`
            A quality score between 0 and 1.
        """
        if self._quality_score > 0:
            return self._quality_score
            
        analysis = await self.analyze_with_ai()
        if not analysis:
            return 0.0
            
        # Calculate quality score based on various factors
        duration_score = min(1.0, analysis.get('duration', 0) / 30)  # Prefer sounds under 30 seconds
        format_score = 1.0 if analysis.get('format') in ['mp3', 'wav'] else 0.5
        feature_score = len(analysis.get('features', [])) / 10  # Normalize feature count
        
        self._quality_score = (duration_score + format_score + feature_score) / 3
        return self._quality_score

    async def find_similar_sounds(self, limit: int = 5) -> List[int]:
        """|coro|

        Finds sounds similar to this one using AI analysis.
        
        Parameters
        -----------
        limit: :class:`int`
            The maximum number of similar sounds to return.
            
        Returns
        -------
        List[:class:`int`]
            The IDs of similar sounds.
        """
        if self._similar_sounds:
            return self._similar_sounds[:limit]
            
        analysis = await self.analyze_with_ai()
        if not analysis:
            return []
            
        # Find sounds with similar features
        similar_sounds = []
        for sound in self.guild.soundboard_sounds:
            if sound.id == self.id:
                continue
                
            sound_analysis = await sound.analyze_with_ai()
            if not sound_analysis:
                continue
                
            # Calculate similarity score
            feature_similarity = len(set(analysis.get('features', [])) & 
                                   set(sound_analysis.get('features', []))) / \
                               max(len(analysis.get('features', [])), 
                                   len(sound_analysis.get('features', [])))
                                   
            if feature_similarity > 0.5:  # Only include if significantly similar
                similar_sounds.append((sound.id, feature_similarity))
                
        # Sort by similarity and cache results
        self._similar_sounds = [sound_id for sound_id, _ in 
                              sorted(similar_sounds, key=lambda x: x[1], reverse=True)]
        return self._similar_sounds[:limit]

    async def calculate_popularity(self) -> float:
        """|coro|

        Calculates a popularity score for this sound.
        
        Returns
        -------
        :class:`float`
            A popularity score between 0 and 1.
        """
        if self._popularity_score > 0:
            return self._popularity_score
            
        # Get analytics data
        play_count = self._analytics.play_count
        unique_users = self._analytics.get_unique_users_count()
        play_frequency = self._analytics.get_play_frequency()
        
        # Calculate popularity score
        count_score = min(1.0, play_count / 1000)  # Normalize play count
        user_score = min(1.0, unique_users / 100)  # Normalize unique users
        frequency_score = min(1.0, play_frequency / 10)  # Normalize play frequency
        
        self._popularity_score = (count_score + user_score + frequency_score) / 3
        return self._popularity_score

    async def get_play_insights(self) -> Dict[str, Any]:
        """|coro|

        Gets insights about how this sound is played.
        
        Returns
        -------
        Dict[str, Any]
            Insights about the sound's usage.
        """
        return {
            'total_plays': self._analytics.play_count,
            'unique_users': self._analytics.get_unique_users_count(),
            'play_frequency': self._analytics.get_play_frequency(),
            'average_volume': self._analytics.average_volume,
            'total_duration': self._analytics.total_duration,
            'last_played': self._analytics.last_played,
            'popularity_score': await self.calculate_popularity(),
            'quality_score': await self.analyze_quality(),
            'similar_sounds': await self.find_similar_sounds(),
            'recommendations': await self.get_recommendations()
        }

    async def edit(
        self,
        *,
        name: str = MISSING,
        volume: Optional[float] = MISSING,
        emoji: Optional[EmojiInputType] = MISSING,
        category: Optional[SoundCategory] = MISSING,
        tags: Optional[List[str]] = MISSING,
        reason: Optional[str] = None,
    ):
        """|coro|

        Edits the soundboard sound.

        You must have :attr:`~Permissions.manage_expressions` to edit the sound.
        If the sound was created by the client, you must have either :attr:`~Permissions.manage_expressions`
        or :attr:`~Permissions.create_expressions`.

        Parameters
        ----------
        name: :class:`str`
            The new name of the sound. Must be between 2 and 32 characters.
        volume: Optional[:class:`float`]
            The new volume of the sound. Must be between 0 and 1.
        emoji: Optional[Union[:class:`Emoji`, :class:`PartialEmoji`, :class:`str`]]
            The new emoji of the sound.
        category: Optional[:class:`SoundCategory`]
            The new category of the sound.
        tags: Optional[List[:class:`str`]]
            The new tags for the sound.
        reason: Optional[:class:`str`]
            The reason for editing this sound. Shows up on the audit log.

        Raises
        -------
        Forbidden
            You do not have permissions to edit the soundboard sound.
        HTTPException
            Editing the soundboard sound failed.
        SoundboardValidationError
            If the name or volume validation fails.

        Returns
        -------
        :class:`SoundboardSound`
            The newly updated soundboard sound.
        """
        start_time = time.time()
        try:
            if name is not MISSING:
                if not 2 <= len(name) <= 32:
                    raise SoundboardValidationError(f'Name must be between 2 and 32 characters, got length {len(name)}')

            if volume is not MISSING and volume is not None:
                if not 0 <= volume <= 1:
                    raise SoundboardValidationError(f'Volume must be between 0 and 1, got {volume}')

            payload: Dict[str, Any] = {}

            if name is not MISSING:
                payload['name'] = name

            if volume is not MISSING:
                payload['volume'] = volume

            if emoji is not MISSING:
                if emoji is None:
                    payload['emoji_id'] = None
                    payload['emoji_name'] = None
                else:
                    if isinstance(emoji, _EmojiTag):
                        partial_emoji = emoji._to_partial()
                    elif isinstance(emoji, str):
                        partial_emoji = PartialEmoji.from_str(emoji)
                    else:
                        partial_emoji = None

                    if partial_emoji is not None:
                        if partial_emoji.id is None:
                            payload['emoji_name'] = partial_emoji.name
                        else:
                            payload['emoji_id'] = partial_emoji.id

            if category is not MISSING and category is not None:
                payload['category'] = category.name

            if tags is not MISSING and tags is not None:
                payload['tags'] = list(set(tags))  # Remove duplicates

            data = await self._state.http.edit_soundboard_sound(self.guild.id, self.id, reason=reason, **payload)
            self._metrics.record_operation('edit_sound', time.time() - start_time)
            return SoundboardSound(guild=self.guild, state=self._state, data=data)
        except Exception as e:
            self._metrics.record_operation('edit_sound_error', time.time() - start_time, False, e)
            raise

    async def delete(self, *, reason: Optional[str] = None) -> None:
        """|coro|

        Deletes the soundboard sound.

        You must have :attr:`~Permissions.manage_expressions` to delete the sound.
        If the sound was created by the client, you must have either :attr:`~Permissions.manage_expressions`
        or :attr:`~Permissions.create_expressions`.

        Parameters
        -----------
        reason: Optional[:class:`str`]
            The reason for deleting this sound. Shows up on the audit log.

        Raises
        -------
        Forbidden
            You do not have permissions to delete the soundboard sound.
        HTTPException
            Deleting the soundboard sound failed.
        """
        await self._state.http.delete_soundboard_sound(self.guild.id, self.id, reason=reason)

    async def get_duration(self) -> Optional[float]:
        """|coro|

        Gets the duration of the sound in seconds.
        This requires an API call if not cached.

        Returns
        -------
        Optional[:class:`float`]
            The duration of the sound in seconds, or None if not available.
        """
        if self._cached_duration is not None:
            return self._cached_duration

        try:
            metadata = await self.get_metadata()
            if metadata and 'duration' in metadata:
                self._cached_duration = metadata['duration']
                return self._cached_duration
            return None
        except:
            return None

    def has_tag(self, tag: str) -> bool:
        """Checks if the sound has a specific tag.
        
        Parameters
        -----------
        tag: :class:`str`
            The tag to check for.
            
        Returns
        -------
        :class:`bool`
            Whether the sound has the specified tag.
        """
        return tag.lower() in {t.lower() for t in self.tags}

    def add_tag(self, tag: str) -> None:
        """Adds a tag to the sound.
        
        Parameters
        -----------
        tag: :class:`str`
            The tag to add.
        """
        self.tags.add(tag.lower())

    def remove_tag(self, tag: str) -> None:
        """Removes a tag from the sound.
        
        Parameters
        -----------
        tag: :class:`str`
            The tag to remove.
        """
        self.tags.discard(tag.lower())
