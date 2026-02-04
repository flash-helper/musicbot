import asyncio
import logging
import os
import re
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Callable, Awaitable

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, BotCommand, BotCommandScopeDefault,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, FSInputFile
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean,
    DateTime, ForeignKey, Text, JSON, select, func, and_
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from shazamio import Shazam

# VKpymusic
from vkpymusic import Service as VKService

import aiohttp
import aiofiles

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "7970525514:AAGVnTnsbRYaWL06lEnCMPmlaJJmnDwncpU"
ADMIN_IDS = [8112974330]

# VK Token - получите на https://vkhost.github.io/
# Выберите приложение VK Admin или Kate Mobile, авторизуйтесь и скопируйте токен
VK_TOKEN = ""  # ← ВСТАВЬТЕ ВАШ VK ТОКЕН СЮДА

DATABASE_URL = "sqlite+aiosqlite:///music_bot.db"

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    is_banned = Column(Boolean, default=False)
    recognize_enabled = Column(Boolean, default=True)
    playlists_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)

    playlists = relationship("Playlist", back_populates="user", cascade="all, delete-orphan")


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger, unique=True, nullable=False)
    channel_username = Column(String(255))
    channel_title = Column(String(255))
    is_active = Column(Boolean, default=True)


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="playlists")
    tracks = relationship("PlaylistTrack", back_populates="playlist", cascade="all, delete-orphan")


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"

    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id"))
    file_id = Column(String(255), nullable=False)
    title = Column(String(255))
    artist = Column(String(255))
    duration = Column(Integer)
    added_at = Column(DateTime, default=datetime.utcnow)

    playlist = relationship("Playlist", back_populates="tracks")


class VKProfile(Base):
    __tablename__ = "vk_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    vk_user_id = Column(BigInteger)
    vk_url = Column(String(500), nullable=False)
    vk_name = Column(String(255))


class BotSettings(Base):
    __tablename__ = "bot_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text)


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id = Column(Integer, primary_key=True)
    text = Column(Text)
    photo_file_id = Column(String(255))
    buttons = Column(JSON)
    scheduled_at = Column(DateTime)
    is_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SearchHistory(Base):
    __tablename__ = "search_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    query = Column(String(500))
    search_type = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)


# Database connection
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ==================== VK MUSIC SERVICE ====================
class VKMusicService:
    """Сервис для работы с VK музыкой через vkpymusic"""

    def __init__(self, token: str):
        self.token = token
        self.service = None
        self._init_service()

    def _init_service(self):
        """Инициализация сервиса"""
        if self.token:
            try:
                user_agent = "VKAndroidApp/5.52-4543 (Android 5.1.1; SDK 22; x86_64; unknown Android SDK built for x86_64; en; 320x240)"
                self.service = VKService(user_agent, self.token)
                logger.info("VK Music Service initialized successfully")
            except Exception as e:
                logger.error(f"VK Service init error: {e}")
                self.service = None

    def is_available(self) -> bool:
        """Проверка доступности сервиса"""
        return self.service is not None

    def search_songs(self, query: str, count: int = 10) -> List[Dict]:
        """Поиск песен"""
        if not self.service:
            return []

        try:
            songs = self.service.search_songs_by_text(query, count)
            return [
                {
                    "id": song.id,
                    "owner_id": song.owner_id,
                    "title": song.title,
                    "artist": song.artist,
                    "duration": song.duration,
                    "url": song.url
                }
                for song in songs
            ]
        except Exception as e:
            logger.error(f"VK search error: {e}")
            return []

    def get_user_songs(self, user_id: int, count: int = 50) -> List[Dict]:
        """Получение аудио пользователя по ID"""
        if not self.service:
            return []

        try:
            songs = self.service.get_songs_by_userid(user_id, count)
            return [
                {
                    "id": song.id,
                    "owner_id": song.owner_id,
                    "title": song.title,
                    "artist": song.artist,
                    "duration": song.duration,
                    "url": song.url
                }
                for song in songs
            ]
        except Exception as e:
            logger.error(f"VK get user songs error: {e}")
            return []

    def get_playlist_songs(self, owner_id: int, playlist_id: int, count: int = 50) -> List[Dict]:
        """Получение аудио из плейлиста"""
        if not self.service:
            return []

        try:
            songs = self.service.get_songs_by_playlist_id(owner_id, playlist_id, count)
            return [
                {
                    "id": song.id,
                    "owner_id": song.owner_id,
                    "title": song.title,
                    "artist": song.artist,
                    "duration": song.duration,
                    "url": song.url
                }
                for song in songs
            ]
        except Exception as e:
            logger.error(f"VK get playlist songs error: {e}")
            return []

    async def download_song(self, url: str) -> Optional[bytes]:
        """Скачивание песни"""
        if not url:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as e:
            logger.error(f"Download error: {e}")
        return None

    @staticmethod
    def parse_vk_url(url: str) -> Optional[Dict]:
        """Парсит VK URL и извлекает информацию"""
        patterns = {
            "profile_id": r"vk\.com/id(\d+)",
            "profile_username": r"vk\.com/([a-zA-Z][a-zA-Z0-9_.]+)(?:\?|$|/)",
            "playlist": r"vk\.com/music/(?:playlist|album)/(-?\d+)_(\d+)",
        }

        for pattern_name, pattern in patterns.items():
            match = re.search(pattern, url)
            if match:
                if pattern_name == "profile_id":
                    return {"type": "profile", "user_id": int(match.group(1))}
                elif pattern_name == "profile_username":
                    username = match.group(1)
                    if username not in ["music", "audio", "feed", "friends", "groups", "im"]:
                        return {"type": "username", "username": username}
                elif pattern_name == "playlist":
                    return {
                        "type": "playlist",
                        "owner_id": int(match.group(1)),
                        "playlist_id": int(match.group(2))
                    }

        return None

    async def resolve_username(self, username: str) -> Optional[int]:
        """Получает user_id по username через VK API"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.vk.com/method/utils.resolveScreenName"
                params = {
                    "screen_name": username,
                    "access_token": self.token,
                    "v": "5.131"
                }
                async with session.get(url, params=params) as resp:
                    data = await resp.json()
                    if "response" in data and data["response"]:
                        obj_type = data["response"].get("type")
                        obj_id = data["response"].get("object_id")
                        if obj_type == "user":
                            return obj_id
                        elif obj_type == "group":
                            return -obj_id
        except Exception as e:
            logger.error(f"Resolve username error: {e}")
        return None


# Инициализация VK сервиса
vk_service = VKMusicService(VK_TOKEN) if VK_TOKEN else None


# ==================== SHAZAM SERVICE ====================
class ShazamService:
    """Сервис распознавания музыки"""

    def __init__(self):
        self.shazam = Shazam()

    async def recognize_from_file(self, file_path: str) -> Optional[Dict]:
        """Распознает трек из файла"""
        try:
            result = await self.shazam.recognize(file_path)

            if result and "track" in result:
                track = result["track"]
                return {
                    "title": track.get("title", "Unknown"),
                    "artist": track.get("subtitle", "Unknown"),
                    "cover": track.get("images", {}).get("coverart", ""),
                    "shazam_url": track.get("url", "")
                }
        except Exception as e:
            logger.error(f"Shazam error: {e}")
        return None


shazam_service = ShazamService()


# ==================== КЛАВИАТУРЫ ====================
def get_subscribe_kb(channels: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.row(InlineKeyboardButton(
            text=f"📢 {channel.channel_title}",
            url=f"https://t.me/{channel.channel_username}"
        ))
    builder.row(InlineKeyboardButton(
        text="✅ Продолжить",
        callback_data="check_subscription"
    ))
    return builder.as_markup()


def get_main_menu() -> ReplyKeyboardMarkup:
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎵 Поиск музыки")],
            [KeyboardButton(text="🎤 Распознать трек")],
            [KeyboardButton(text="📋 Мои плейлисты"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )
    return keyboard


def get_settings_kb(user) -> InlineKeyboardMarkup:
    recognize_status = "✅" if user.recognize_enabled else "❌"
    playlists_status = "✅" if user.playlists_enabled else "❌"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=f"{recognize_status} Распознавание треков",
        callback_data="toggle_recognize"
    ))
    builder.row(InlineKeyboardButton(
        text=f"{playlists_status} Плейлисты",
        callback_data="toggle_playlists"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="back_to_main"
    ))
    return builder.as_markup()


def get_playlists_kb(playlists: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for playlist in playlists:
        track_count = len(playlist.tracks) if hasattr(playlist, 'tracks') else 0
        builder.row(InlineKeyboardButton(
            text=f"🎵 {playlist.name} ({track_count})",
            callback_data=f"playlist_{playlist.id}"
        ))
    builder.row(InlineKeyboardButton(
        text="➕ Создать плейлист",
        callback_data="create_playlist"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="back_to_main"
    ))
    return builder.as_markup()


def get_playlist_actions_kb(playlist_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="🎧 Получить аудио",
        callback_data=f"get_audio_{playlist_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="🔗 Поделиться плейлистом",
        callback_data=f"share_playlist_{playlist_id}"
    ))
    builder.row(
        InlineKeyboardButton(
            text="✏️ Переименовать",
            callback_data=f"rename_playlist_{playlist_id}"
        ),
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"delete_playlist_{playlist_id}"
        )
    )
    builder.row(InlineKeyboardButton(
        text="🔙 К плейлистам",
        callback_data="playlists_menu"
    ))
    return builder.as_markup()


def get_vk_profiles_kb(profiles: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for profile in profiles:
        builder.row(InlineKeyboardButton(
            text=f"👤 {profile.vk_name or 'Профиль VK'}",
            callback_data=f"vk_profile_{profile.id}"
        ))
    builder.row(InlineKeyboardButton(
        text="➕ Добавить профиль/плейлист",
        callback_data="add_vk_profile"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="back_to_main"
    ))
    return builder.as_markup()


def get_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="📊 Статистика",
        callback_data="admin_stats"
    ))
    builder.row(InlineKeyboardButton(
        text="📢 Управление каналами",
        callback_data="admin_channels"
    ))
    builder.row(InlineKeyboardButton(
        text="📨 Рассылка",
        callback_data="admin_broadcast"
    ))
    builder.row(InlineKeyboardButton(
        text="⏰ Отложенные рассылки",
        callback_data="admin_scheduled"
    ))
    builder.row(InlineKeyboardButton(
        text="👋 Приветствие",
        callback_data="admin_welcome"
    ))
    return builder.as_markup()


def get_stats_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 День", callback_data="stats_day"),
        InlineKeyboardButton(text="📆 Неделя", callback_data="stats_week"),
        InlineKeyboardButton(text="🗓 Месяц", callback_data="stats_month")
    )
    builder.row(InlineKeyboardButton(
        text="📈 Общая статистика",
        callback_data="stats_all"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="admin_menu"
    ))
    return builder.as_markup()


def get_channels_kb(channels: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        status = "✅" if channel.is_active else "❌"
        builder.row(InlineKeyboardButton(
            text=f"{status} {channel.channel_title}",
            callback_data=f"toggle_channel_{channel.id}"
        ))
    builder.row(InlineKeyboardButton(
        text="➕ Добавить канал",
        callback_data="add_channel"
    ))
    builder.row(InlineKeyboardButton(
        text="🗑 Удалить канал",
        callback_data="delete_channel_menu"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="admin_menu"
    ))
    return builder.as_markup()


def get_broadcast_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="📝 Создать рассылку",
        callback_data="create_broadcast"
    ))
    builder.row(InlineKeyboardButton(
        text="⏰ Отложить рассылку",
        callback_data="schedule_broadcast"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="admin_menu"
    ))
    return builder.as_markup()


def get_scheduled_broadcasts_kb(broadcasts: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for broadcast in broadcasts:
        text_preview = (broadcast.text or "")[:20]
        builder.row(InlineKeyboardButton(
            text=f"📨 {broadcast.scheduled_at.strftime('%d.%m %H:%M')} - {text_preview}...",
            callback_data=f"edit_broadcast_{broadcast.id}"
        ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="admin_menu"
    ))
    return builder.as_markup()


def get_edit_broadcast_kb(broadcast_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="📝 Изменить текст",
        callback_data=f"bedit_text_{broadcast_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="🖼 Изменить фото",
        callback_data=f"bedit_photo_{broadcast_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="🔘 Изменить кнопки",
        callback_data=f"bedit_buttons_{broadcast_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="⏰ Изменить время",
        callback_data=f"bedit_time_{broadcast_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="▶️ Отправить сейчас",
        callback_data=f"send_now_{broadcast_id}"
    ))
    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"delete_broadcast_{broadcast_id}"
        ),
        InlineKeyboardButton(
            text="🔙 Назад",
            callback_data="admin_scheduled"
        )
    )
    return builder.as_markup()


def build_buttons_from_json(buttons: list) -> Optional[InlineKeyboardMarkup]:
    """Строит клавиатуру из JSON кнопок"""
    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    for btn in buttons:
        builder.row(InlineKeyboardButton(
            text=btn["text"],
            url=btn["url"]
        ))
    return builder.as_markup()


# ==================== MIDDLEWARE ====================
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
            self,
            handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
            event: Message | CallbackQuery,
            data: Dict[str, Any]
    ) -> Any:
        # Пропускаем для админов
        if event.from_user.id in ADMIN_IDS:
            return await handler(event, data)

        # Пропускаем проверку подписки для callback'а проверки
        if isinstance(event, CallbackQuery):
            if event.data == "check_subscription":
                return await handler(event, data)

        # Пропускаем команды start и admin
        if isinstance(event, Message) and event.text:
            if event.text.startswith("/start") or event.text.startswith("/admin"):
                return await handler(event, data)

        user_id = event.from_user.id
        bot = data["bot"]

        async with async_session() as session:
            result = await session.execute(
                select(Channel).where(Channel.is_active == True)
            )
            channels = result.scalars().all()

        if not channels:
            return await handler(event, data)

        not_subscribed = []
        for channel in channels:
            try:
                member = await bot.get_chat_member(channel.channel_id, user_id)
                if member.status in ["left", "kicked"]:
                    not_subscribed.append(channel)
            except Exception:
                continue

        if not_subscribed:
            text = (
                "🔒 <b>Чтобы скачивать треки, подпишись на каналы по кнопкам ниже</b>\n\n"
                "После нажми «Продолжить»!"
            )

            if isinstance(event, Message):
                await event.answer(text, reply_markup=get_subscribe_kb(not_subscribed), parse_mode="HTML")
            else:
                await event.message.edit_text(text, reply_markup=get_subscribe_kb(not_subscribed), parse_mode="HTML")
            return

        return await handler(event, data)


class ActivityMiddleware(BaseMiddleware):
    """Обновляет время последней активности"""

    async def __call__(
            self,
            handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
            event: Message | CallbackQuery,
            data: Dict[str, Any]
    ) -> Any:
        user_id = event.from_user.id

        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.user_id == user_id)
            )
            user = result.scalar_one_or_none()
            if user:
                user.last_active = datetime.utcnow()
                await session.commit()

        return await handler(event, data)


# ==================== ПЛАНИРОВЩИК ====================
class BroadcastScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler()

    def start(self):
        self.scheduler.start()

    async def schedule_broadcast(self, broadcast_id: int, scheduled_at: datetime):
        self.scheduler.add_job(
            self.execute_broadcast,
            DateTrigger(run_date=scheduled_at),
            args=[broadcast_id],
            id=f"broadcast_{broadcast_id}",
            replace_existing=True
        )

    def cancel_broadcast(self, broadcast_id: int):
        job_id = f"broadcast_{broadcast_id}"
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        except Exception:
            pass

    async def execute_broadcast(self, broadcast_id: int) -> int:
        async with async_session() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if not broadcast or broadcast.is_sent:
                return 0

            result = await session.execute(
                select(User).where(User.is_banned == False)
            )
            users = result.scalars().all()

            keyboard = build_buttons_from_json(broadcast.buttons) if broadcast.buttons else None

            success_count = 0
            for user in users:
                try:
                    if broadcast.photo_file_id:
                        await self.bot.send_photo(
                            chat_id=user.user_id,
                            photo=broadcast.photo_file_id,
                            caption=broadcast.text,
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                    else:
                        await self.bot.send_message(
                            chat_id=user.user_id,
                            text=broadcast.text,
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                    success_count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Broadcast error to {user.user_id}: {e}")
                    continue

            broadcast.is_sent = True
            await session.commit()

            for admin_id in ADMIN_IDS:
                try:
                    await self.bot.send_message(
                        admin_id,
                        f"✅ Рассылка #{broadcast_id} завершена!\n"
                        f"📤 Отправлено: {success_count}/{len(users)}"
                    )
                except Exception:
                    pass

            return success_count

    async def load_scheduled_broadcasts(self):
        async with async_session() as session:
            result = await session.execute(
                select(Broadcast).where(
                    Broadcast.is_sent == False,
                    Broadcast.scheduled_at != None,
                    Broadcast.scheduled_at > datetime.utcnow()
                )
            )
            broadcasts = result.scalars().all()

            for broadcast in broadcasts:
                await self.schedule_broadcast(broadcast.id, broadcast.scheduled_at)
                logger.info(f"Loaded scheduled broadcast #{broadcast.id}")


# ==================== FSM STATES ====================
class PlaylistStates(StatesGroup):
    waiting_name = State()
    waiting_new_name = State()


class VKStates(StatesGroup):
    waiting_vk_url = State()


class AdminStates(StatesGroup):
    waiting_channel = State()
    waiting_broadcast_text = State()
    waiting_broadcast_photo = State()
    waiting_broadcast_buttons = State()
    waiting_schedule_time = State()
    waiting_welcome_text = State()
    waiting_welcome_photo = State()
    editing_broadcast_text = State()
    editing_broadcast_photo = State()
    editing_broadcast_buttons = State()
    editing_broadcast_time = State()


# ==================== ХЕЛПЕРЫ ====================
# Кэш для результатов поиска
search_cache: Dict[int, List[Dict]] = {}


async def get_welcome_message() -> tuple:
    async with async_session() as session:
        result = await session.execute(
            select(BotSettings).where(BotSettings.key == "welcome_text")
        )
        text_setting = result.scalar_one_or_none()

        result = await session.execute(
            select(BotSettings).where(BotSettings.key == "welcome_photo")
        )
        photo_setting = result.scalar_one_or_none()

        text = text_setting.value if text_setting else (
            "👋 <b>Привет!</b>\n\n"
            "Я бот для поиска и скачивания музыки.\n\n"
            "🎵 Отправь мне название трека или имя исполнителя\n"
            "🎤 Или отправь голосовое/видео для распознавания"
        )
        photo = photo_setting.value if photo_setting else None

        return text, photo


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def log_search(user_id: int, query: str, search_type: str):
    async with async_session() as session:
        history = SearchHistory(
            user_id=user_id,
            query=query,
            search_type=search_type
        )
        session.add(history)
        await session.commit()


# ==================== РОУТЕРЫ ====================
user_router = Router()
admin_router = Router()
music_router = Router()
recognize_router = Router()
playlist_router = Router()
vk_router = Router()


# ==================== USER HANDLERS ====================
@user_router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == user.id)
        )
        db_user = result.scalar_one_or_none()

        if not db_user:
            db_user = User(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name
            )
            session.add(db_user)
            await session.commit()

        result = await session.execute(
            select(Channel).where(Channel.is_active == True)
        )
        channels = result.scalars().all()

    not_subscribed = []
    for channel in channels:
        try:
            member = await message.bot.get_chat_member(channel.channel_id, user.id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel)
        except Exception:
            continue

    if not_subscribed:
        text = (
            "🔒 <b>Чтобы скачивать треки, подпишись на каналы по кнопкам ниже</b>\n\n"
            "После нажми «Продолжить»!"
        )
        await message.answer(text, reply_markup=get_subscribe_kb(not_subscribed), parse_mode="HTML")
        return

    text, photo = await get_welcome_message()

    if photo:
        await message.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=get_main_menu(),
            parse_mode="HTML"
        )
    else:
        await message.answer(text, reply_markup=get_main_menu(), parse_mode="HTML")


@user_router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    user = callback.from_user

    async with async_session() as session:
        result = await session.execute(
            select(Channel).where(Channel.is_active == True)
        )
        channels = result.scalars().all()

    not_subscribed = []
    for channel in channels:
        try:
            member = await callback.bot.get_chat_member(channel.channel_id, user.id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel)
        except Exception:
            continue

    if not_subscribed:
        await callback.answer("❌ Вы не подписались на все каналы!", show_alert=True)
        return

    await callback.answer("✅ Отлично! Теперь вы можете пользоваться ботом!")

    text, photo = await get_welcome_message()

    try:
        await callback.message.delete()
    except Exception:
        pass

    if photo:
        await callback.message.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=get_main_menu(),
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(text, reply_markup=get_main_menu(), parse_mode="HTML")


@user_router.message(Command("settings"))
@user_router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await message.answer("Пользователь не найден. Нажмите /start")
            return

        text = (
            "⚙️ <b>Настройки</b>\n\n"
            "Здесь вы можете включить или отключить функции бота:"
        )

        await message.answer(text, reply_markup=get_settings_kb(user), parse_mode="HTML")


@user_router.callback_query(F.data == "toggle_recognize")
async def toggle_recognize(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        if user:
            user.recognize_enabled = not user.recognize_enabled
            await session.commit()

            status = "включено" if user.recognize_enabled else "отключено"
            await callback.answer(f"Распознавание треков {status}")

            await callback.message.edit_text(
                "⚙️ <b>Настройки</b>\n\nЗдесь вы можете включить или отключить функции бота:",
                reply_markup=get_settings_kb(user),
                parse_mode="HTML"
            )


@user_router.callback_query(F.data == "toggle_playlists")
async def toggle_playlists(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        if user:
            user.playlists_enabled = not user.playlists_enabled
            await session.commit()

            status = "включены" if user.playlists_enabled else "отключены"
            await callback.answer(f"Плейлисты {status}")

            await callback.message.edit_text(
                "⚙️ <b>Настройки</b>\n\nЗдесь вы можете включить или отключить функции бота:",
                reply_markup=get_settings_kb(user),
                parse_mode="HTML"
            )


@user_router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "🎵 Выберите действие:",
        reply_markup=get_main_menu()
    )


@user_router.message(Command("help"))
async def cmd_help(message: Message):
    text = """<b>Что умеет этот бот?</b>

<b>Поиск музыки:</b>
Отправь имя артиста или название композиции и бот найдет для тебя музыку.

<b>Распознавание треков:</b>
Отправьте голосовое сообщение или кружок, содержащий песню, и бот сообщит её название и исполнителя.
<i>(если не работает — включите функцию в настройках)</i>

/profiles - аудиозаписи профилей вк
/playlists - плейлисты
/settings - настройки
/help - помощь"""

    await message.answer(text, parse_mode="HTML")


# ==================== MUSIC HANDLERS ====================
@music_router.message(F.text == "🎵 Поиск музыки")
async def search_prompt(message: Message):
    await message.answer(
        "🔍 <b>Введите название трека или имя исполнителя:</b>",
        parse_mode="HTML"
    )


@music_router.message(F.text & ~F.text.startswith("/"))
async def search_music(message: Message):
    menu_texts = ["🎵 Поиск музыки", "🎤 Распознать трек", "📋 Мои плейлисты", "⚙️ Настройки"]
    if message.text in menu_texts:
        return

    query = message.text.strip()

    if len(query) < 2:
        await message.answer("❌ Слишком короткий запрос")
        return

    searching_msg = await message.answer("🔍 Ищу треки...")

    await log_search(message.from_user.id, query, "music")

    try:
        tracks = []

        # Пробуем VK если токен есть
        if vk_service and vk_service.is_available():
            tracks = vk_service.search_songs(query, count=10)

        # Если VK не работает, пробуем Deezer
        if not tracks:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.deezer.com/search?q={query}&limit=10"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("data", []):
                            tracks.append({
                                "id": item["id"],
                                "title": item["title"],
                                "artist": item["artist"]["name"],
                                "duration": item["duration"],
                                "url": item.get("preview", ""),
                                "source": "deezer"
                            })

        if not tracks:
            await searching_msg.edit_text("😔 По вашему запросу ничего не найдено")
            return

        builder = InlineKeyboardBuilder()

        for i, track in enumerate(tracks[:10]):
            artist = track.get("artist", "Unknown")
            title = track.get("title", "Unknown")
            duration = track.get("duration", 0)

            minutes = duration // 60
            seconds = duration % 60

            button_text = f"🎵 {artist} - {title} ({minutes}:{seconds:02d})"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."

            builder.row(InlineKeyboardButton(text=button_text, callback_data=f"dl_{i}"))

        search_cache[message.from_user.id] = tracks

        source_text = "VK" if vk_service and vk_service.is_available() and tracks[0].get(
            "source") != "deezer" else "Deezer (30 сек превью)"

        await searching_msg.edit_text(
            f"🎵 <b>Результаты поиска:</b> {query}\n"
            f"📀 Источник: {source_text}\n\n"
            "Выберите трек для скачивания:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Search error: {e}")
        await searching_msg.edit_text(f"❌ Произошла ошибка при поиске")


@music_router.callback_query(F.data.startswith("dl_"))
async def download_track(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю трек...")

    try:
        index = int(callback.data.split("_")[1])
        user_id = callback.from_user.id

        if user_id not in search_cache or index >= len(search_cache[user_id]):
            await callback.message.answer("❌ Трек не найден. Попробуйте поискать снова.")
            return

        track = search_cache[user_id][index]
        url = track.get("url")

        if not url:
            await callback.message.answer("❌ Ссылка на трек недоступна")
            return

        # Скачиваем
        if vk_service:
            audio_data = await vk_service.download_song(url)
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                    else:
                        audio_data = None

        if not audio_data:
            await callback.message.answer("❌ Не удалось скачать трек")
            return

        artist = track.get("artist", "Unknown")
        title = track.get("title", "Unknown")
        duration = track.get("duration", 0)

        audio_file = BufferedInputFile(
            audio_data,
            filename=f"{artist} - {title}.mp3"
        )

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="➕ Добавить в плейлист",
            callback_data=f"addpl_{index}"
        ))

        is_preview = track.get("source") == "deezer"

        sent_audio = await callback.message.answer_audio(
            audio=audio_file,
            title=title,
            performer=artist,
            duration=min(duration, 30) if is_preview else duration,
            reply_markup=builder.as_markup(),
            caption="⚠️ 30-секундное превью" if is_preview else None
        )

        # Сохраняем file_id для плейлиста
        if user_id in search_cache and index < len(search_cache[user_id]):
            search_cache[user_id][index]["file_id"] = sent_audio.audio.file_id

    except Exception as e:
        logger.error(f"Download error: {e}")
        await callback.message.answer(f"❌ Ошибка при загрузке трека")


# ==================== RECOGNIZE HANDLERS ====================
async def check_recognize_enabled(user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()
        return user.recognize_enabled if user else True


@recognize_router.message(F.text == "🎤 Распознать трек")
async def recognize_prompt(message: Message):
    if not await check_recognize_enabled(message.from_user.id):
        await message.answer(
            "❌ Распознавание треков отключено.\n"
            "Включите его в /settings"
        )
        return

    await message.answer(
        "🎤 <b>Отправьте голосовое сообщение, кружок или видео с музыкой</b>\n\n"
        "Бот попытается распознать трек и сообщит название и исполнителя.",
        parse_mode="HTML"
    )


@recognize_router.message(F.voice)
async def recognize_voice(message: Message, bot: Bot):
    if not await check_recognize_enabled(message.from_user.id):
        await message.answer("❌ Распознавание отключено. Включите в /settings")
        return

    processing_msg = await message.answer("🔍 Распознаю трек...")

    try:
        file = await bot.get_file(message.voice.file_id)
        file_path = f"/tmp/voice_{message.from_user.id}_{datetime.now().timestamp()}.ogg"
        await bot.download_file(file.file_path, file_path)

        result = await shazam_service.recognize_from_file(file_path)

        try:
            os.remove(file_path)
        except Exception:
            pass

        await log_search(message.from_user.id, "voice_recognize", "recognize")

        if result:
            text = (
                f"🎵 <b>Трек найден!</b>\n\n"
                f"🎤 <b>Исполнитель:</b> {result['artist']}\n"
                f"🎶 <b>Название:</b> {result['title']}\n"
            )

            if result.get('shazam_url'):
                text += f"\n🔗 <a href=\"{result['shazam_url']}\">Открыть в Shazam</a>"

            builder = InlineKeyboardBuilder()
            search_query = f"{result['artist']} {result['title']}"[:50]
            builder.row(InlineKeyboardButton(
                text="🔍 Найти этот трек",
                callback_data=f"search_{search_query}"
            ))

            await processing_msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True,
                                           reply_markup=builder.as_markup())
        else:
            await processing_msg.edit_text(
                "😔 Не удалось распознать трек.\n"
                "Попробуйте отправить более длинный или чёткий фрагмент."
            )

    except Exception as e:
        logger.error(f"Recognition error: {e}")
        await processing_msg.edit_text(f"❌ Ошибка при распознавании")


@recognize_router.message(F.video_note)
async def recognize_video_note(message: Message, bot: Bot):
    if not await check_recognize_enabled(message.from_user.id):
        await message.answer("❌ Распознавание отключено. Включите в /settings")
        return

    processing_msg = await message.answer("🔍 Распознаю трек из видео...")

    try:
        file = await bot.get_file(message.video_note.file_id)
        file_path = f"/tmp/video_note_{message.from_user.id}_{datetime.now().timestamp()}.mp4"
        await bot.download_file(file.file_path, file_path)

        result = await shazam_service.recognize_from_file(file_path)

        try:
            os.remove(file_path)
        except Exception:
            pass

        await log_search(message.from_user.id, "video_note_recognize", "recognize")

        if result:
            text = (
                f"🎵 <b>Трек найден!</b>\n\n"
                f"🎤 <b>Исполнитель:</b> {result['artist']}\n"
                f"🎶 <b>Название:</b> {result['title']}\n"
            )

            builder = InlineKeyboardBuilder()
            search_query = f"{result['artist']} {result['title']}"[:50]
            builder.row(InlineKeyboardButton(
                text="🔍 Найти этот трек",
                callback_data=f"search_{search_query}"
            ))

            await processing_msg.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        else:
            await processing_msg.edit_text("😔 Не удалось распознать трек.")

    except Exception as e:
        logger.error(f"Recognition error: {e}")
        await processing_msg.edit_text(f"❌ Ошибка при распознавании")


@recognize_router.message(F.video)
async def recognize_video(message: Message, bot: Bot):
    if not await check_recognize_enabled(message.from_user.id):
        await message.answer("❌ Распознавание отключено. Включите в /settings")
        return

    processing_msg = await message.answer("🔍 Распознаю трек из видео...")

    try:
        file = await bot.get_file(message.video.file_id)
        file_path = f"/tmp/video_{message.from_user.id}_{datetime.now().timestamp()}.mp4"
        await bot.download_file(file.file_path, file_path)

        result = await shazam_service.recognize_from_file(file_path)

        try:
            os.remove(file_path)
        except Exception:
            pass

        await log_search(message.from_user.id, "video_recognize", "recognize")

        if result:
            text = (
                f"🎵 <b>Трек найден!</b>\n\n"
                f"🎤 <b>Исполнитель:</b> {result['artist']}\n"
                f"🎶 <b>Название:</b> {result['title']}\n"
            )
            await processing_msg.edit_text(text, parse_mode="HTML")
        else:
            await processing_msg.edit_text("😔 Не удалось распознать трек.")

    except Exception as e:
        logger.error(f"Recognition error: {e}")
        await processing_msg.edit_text(f"❌ Ошибка при распознавании")


@recognize_router.callback_query(F.data.startswith("search_"))
async def search_from_recognition(callback: CallbackQuery):
    query = callback.data[7:]
    await callback.answer("🔍 Ищу...")

    tracks = []

    if vk_service and vk_service.is_available():
        tracks = vk_service.search_songs(query, count=10)

    if not tracks:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.deezer.com/search?q={query}&limit=10"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("data", []):
                        tracks.append({
                            "id": item["id"],
                            "title": item["title"],
                            "artist": item["artist"]["name"],
                            "duration": item["duration"],
                            "url": item.get("preview", ""),
                            "source": "deezer"
                        })

    if not tracks:
        await callback.message.answer("😔 По вашему запросу ничего не найдено")
        return

    builder = InlineKeyboardBuilder()

    for i, track in enumerate(tracks[:10]):
        artist = track.get("artist", "Unknown")
        title = track.get("title", "Unknown")
        duration = track.get("duration", 0)

        minutes = duration // 60
        seconds = duration % 60

        button_text = f"🎵 {artist} - {title} ({minutes}:{seconds:02d})"
        if len(button_text) > 60:
            button_text = button_text[:57] + "..."

        builder.row(InlineKeyboardButton(text=button_text, callback_data=f"dl_{i}"))

    search_cache[callback.from_user.id] = tracks

    await callback.message.answer(
        f"🎵 <b>Результаты поиска:</b>\n\nВыберите трек для скачивания:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


# ==================== PLAYLIST HANDLERS ====================
@playlist_router.message(Command("playlists"))
@playlist_router.message(F.text == "📋 Мои плейлисты")
async def cmd_playlists(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await message.answer("Нажмите /start для начала работы")
            return

        if not user.playlists_enabled:
            await message.answer("❌ Плейлисты отключены. Включите их в /settings")
            return

        result = await session.execute(
            select(Playlist).where(Playlist.user_id == user.id)
        )
        playlists = result.scalars().all()

    text = "📋 <b>Ваши плейлисты:</b>\n\n"

    if not playlists:
        text += "У вас пока нет плейлистов.\nСоздайте первый!"
    else:
        text += f"Всего: {len(playlists)} плейлист(ов)"

    await message.answer(text, reply_markup=get_playlists_kb(playlists), parse_mode="HTML")


@playlist_router.callback_query(F.data == "playlists_menu")
async def playlists_menu(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        result = await session.execute(
            select(Playlist).where(Playlist.user_id == user.id)
        )
        playlists = result.scalars().all()

    await callback.message.edit_text(
        "📋 <b>Ваши плейлисты:</b>",
        reply_markup=get_playlists_kb(playlists),
        parse_mode="HTML"
    )


@playlist_router.callback_query(F.data == "create_playlist")
async def create_playlist_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 <b>Введите название для нового плейлиста:</b>",
        parse_mode="HTML"
    )
    await state.set_state(PlaylistStates.waiting_name)


@playlist_router.message(PlaylistStates.waiting_name)
async def create_playlist_name(message: Message, state: FSMContext):
    name = message.text.strip()

    if len(name) > 100:
        await message.answer("❌ Название слишком длинное (макс. 100 символов)")
        return

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        playlist = Playlist(user_id=user.id, name=name)
        session.add(playlist)
        await session.commit()

        result = await session.execute(
            select(Playlist).where(Playlist.user_id == user.id)
        )
        playlists = result.scalars().all()

    await state.clear()

    await message.answer(
        f"✅ Плейлист <b>«{name}»</b> создан!",
        reply_markup=get_playlists_kb(playlists),
        parse_mode="HTML"
    )


@playlist_router.callback_query(F.data.startswith("playlist_"))
async def view_playlist(callback: CallbackQuery):
    playlist_id = int(callback.data.split("_")[1])

    async with async_session() as session:
        playlist = await session.get(Playlist, playlist_id)

        if not playlist:
            await callback.answer("Плейлист не найден", show_alert=True)
            return

        result = await session.execute(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
        )
        tracks = result.scalars().all()

    text = f"🎵 <b>{playlist.name}</b>\n\n"

    if tracks:
        text += f"Треков: {len(tracks)}\n\n"
        for i, track in enumerate(tracks[:10], 1):
            text += f"{i}. {track.artist} - {track.title}\n"

        if len(tracks) > 10:
            text += f"\n... и ещё {len(tracks) - 10} треков"
    else:
        text += "Плейлист пуст.\nДобавьте треки через поиск!"

    await callback.message.edit_text(
        text,
        reply_markup=get_playlist_actions_kb(playlist_id),
        parse_mode="HTML"
    )


@playlist_router.callback_query(F.data.startswith("get_audio_"))
async def get_playlist_audio(callback: CallbackQuery):
    playlist_id = int(callback.data.split("_")[2])

    await callback.answer("⏳ Отправляю треки...")

    async with async_session() as session:
        result = await session.execute(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
        )
        tracks = result.scalars().all()

    if not tracks:
        await callback.message.answer("❌ Плейлист пуст")
        return

    for track in tracks:
        try:
            await callback.message.answer_audio(
                audio=track.file_id,
                title=track.title,
                performer=track.artist
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending track: {e}")
            continue


@playlist_router.callback_query(F.data.startswith("share_playlist_"))
async def share_playlist(callback: CallbackQuery):
    playlist_id = int(callback.data.split("_")[2])

    bot_info = await callback.bot.get_me()
    share_link = f"https://t.me/{bot_info.username}?start=playlist_{playlist_id}"

    await callback.message.answer(
        f"🔗 <b>Ссылка на плейлист:</b>\n\n{share_link}",
        parse_mode="HTML"
    )
    await callback.answer()


@playlist_router.callback_query(F.data.startswith("rename_playlist_"))
async def rename_playlist_start(callback: CallbackQuery, state: FSMContext):
    playlist_id = int(callback.data.split("_")[2])

    await state.update_data(rename_playlist_id=playlist_id)
    await state.set_state(PlaylistStates.waiting_new_name)

    await callback.message.edit_text(
        "📝 <b>Введите новое название плейлиста:</b>",
        parse_mode="HTML"
    )


@playlist_router.message(PlaylistStates.waiting_new_name)
async def rename_playlist_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    playlist_id = data.get("rename_playlist_id")
    new_name = message.text.strip()

    async with async_session() as session:
        playlist = await session.get(Playlist, playlist_id)

        if playlist:
            playlist.name = new_name
            await session.commit()

    await state.clear()
    await message.answer(f"✅ Плейлист переименован в <b>«{new_name}»</b>", parse_mode="HTML")


@playlist_router.callback_query(F.data.startswith("delete_playlist_"))
async def delete_playlist(callback: CallbackQuery):
    playlist_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        playlist = await session.get(Playlist, playlist_id)

        if playlist:
            await session.delete(playlist)
            await session.commit()

    await callback.answer("✅ Плейлист удалён")

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        result = await session.execute(
            select(Playlist).where(Playlist.user_id == user.id)
        )
        playlists = result.scalars().all()

    await callback.message.edit_text(
        "📋 <b>Ваши плейлисты:</b>",
        reply_markup=get_playlists_kb(playlists),
        parse_mode="HTML"
    )


@playlist_router.callback_query(F.data.startswith("addpl_"))
async def add_to_playlist_menu(callback: CallbackQuery):
    index = int(callback.data.split("_")[1])

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await callback.answer("Нажмите /start", show_alert=True)
            return

        result = await session.execute(
            select(Playlist).where(Playlist.user_id == user.id)
        )
        playlists = result.scalars().all()

    if not playlists:
        await callback.answer("У вас нет плейлистов. Создайте в /playlists", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for playlist in playlists:
        builder.row(InlineKeyboardButton(
            text=f"📋 {playlist.name}",
            callback_data=f"savepl_{playlist.id}_{index}"
        ))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_addpl"))

    await callback.message.answer(
        "📋 <b>Выберите плейлист:</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@playlist_router.callback_query(F.data == "cancel_addpl")
async def cancel_add_to_playlist(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@playlist_router.callback_query(F.data.startswith("savepl_"))
async def save_to_playlist(callback: CallbackQuery):
    parts = callback.data.split("_")
    playlist_id = int(parts[1])
    track_index = int(parts[2])

    user_id = callback.from_user.id

    if user_id not in search_cache or track_index >= len(search_cache[user_id]):
        await callback.answer("Трек не найден. Поищите заново.", show_alert=True)
        return

    track = search_cache[user_id][track_index]

    if not track.get("file_id"):
        await callback.answer("Сначала скачайте трек!", show_alert=True)
        return

    async with async_session() as session:
        result = await session.execute(
            select(PlaylistTrack).where(
                PlaylistTrack.playlist_id == playlist_id,
                PlaylistTrack.title == track.get("title"),
                PlaylistTrack.artist == track.get("artist")
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            await callback.answer("Трек уже в этом плейлисте!", show_alert=True)
            return

        playlist_track = PlaylistTrack(
            playlist_id=playlist_id,
            file_id=track.get("file_id"),
            title=track.get("title", "Unknown"),
            artist=track.get("artist", "Unknown"),
            duration=track.get("duration", 0)
        )
        session.add(playlist_track)
        await session.commit()

    await callback.answer("✅ Трек добавлен в плейлист!")
    try:
        await callback.message.delete()
    except Exception:
        pass


# ==================== VK HANDLERS ====================
@vk_router.message(Command("profiles"))
async def cmd_profiles(message: Message):
    if not vk_service or not vk_service.is_available():
        await message.answer(
            "❌ <b>VK сервис недоступен</b>\n\n"
            "Для работы с профилями VK необходимо добавить VK токен в настройки бота.",
            parse_mode="HTML"
        )
        return

    async with async_session() as session:
        result = await session.execute(
            select(VKProfile).where(VKProfile.user_id == message.from_user.id)
        )
        profiles = result.scalars().all()

    text = "👤 <b>Профили и плейлисты ВКонтакте:</b>\n\n"

    if not profiles:
        text += "У вас нет сохранённых профилей.\n\nДобавьте ссылку на профиль или плейлист VK."
    else:
        text += f"Сохранено: {len(profiles)}"

    await message.answer(text, reply_markup=get_vk_profiles_kb(profiles), parse_mode="HTML")


@vk_router.callback_query(F.data == "add_vk_profile")
async def add_vk_profile_start(callback: CallbackQuery, state: FSMContext):
    if not vk_service or not vk_service.is_available():
        await callback.answer("VK сервис недоступен", show_alert=True)
        return

    await callback.message.edit_text(
        "🔗 <b>Отправьте ссылку на профиль или плейлист ВКонтакте:</b>\n\n"
        "Примеры:\n"
        "• https://vk.com/id123456789\n"
        "• https://vk.com/durov\n"
        "• https://vk.com/music/playlist/-123456_789",
        parse_mode="HTML"
    )
    await state.set_state(VKStates.waiting_vk_url)


@vk_router.message(VKStates.waiting_vk_url)
async def add_vk_profile_url(message: Message, state: FSMContext):
    url = message.text.strip()

    if "vk.com" not in url:
        await message.answer("❌ Это не похоже на ссылку ВКонтакте")
        return

    parsed = VKMusicService.parse_vk_url(url)

    if not parsed:
        await message.answer("❌ Не удалось распознать ссылку. Проверьте формат.")
        return

    processing_msg = await message.answer("⏳ Проверяю ссылку...")

    vk_user_id = None
    vk_name = None

    try:
        if parsed["type"] == "profile":
            vk_user_id = parsed["user_id"]
            vk_name = f"Профиль id{vk_user_id}"

        elif parsed["type"] == "username":
            vk_user_id = await vk_service.resolve_username(parsed["username"])
            if vk_user_id:
                vk_name = f"@{parsed['username']}"
            else:
                await processing_msg.edit_text("❌ Пользователь не найден")
                await state.clear()
                return

        elif parsed["type"] == "playlist":
            vk_user_id = parsed["owner_id"]
            vk_name = f"Плейлист {parsed['playlist_id']}"

        # Проверяем доступность аудио
        if parsed["type"] == "playlist":
            songs = vk_service.get_playlist_songs(parsed["owner_id"], parsed["playlist_id"], count=1)
        else:
            songs = vk_service.get_user_songs(vk_user_id, count=1)

        if not songs:
            await processing_msg.edit_text(
                "⚠️ <b>Внимание:</b> Аудиозаписи недоступны или профиль закрыт.\n"
                "Ссылка всё равно сохранена.",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"VK check error: {e}")

    async with async_session() as session:
        profile = VKProfile(
            user_id=message.from_user.id,
            vk_user_id=vk_user_id,
            vk_url=url,
            vk_name=vk_name
        )
        session.add(profile)
        await session.commit()

        result = await session.execute(
            select(VKProfile).where(VKProfile.user_id == message.from_user.id)
        )
        profiles = result.scalars().all()

    await state.clear()

    try:
        await processing_msg.delete()
    except Exception:
        pass

    await message.answer(
        f"✅ <b>{vk_name}</b> добавлен!",
        reply_markup=get_vk_profiles_kb(profiles),
        parse_mode="HTML"
    )


@vk_router.callback_query(F.data.startswith("vk_profile_"))
async def view_vk_profile(callback: CallbackQuery):
    profile_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        profile = await session.get(VKProfile, profile_id)

    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="🎵 Получить аудиозаписи",
        callback_data=f"get_vk_audio_{profile_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="🔗 Открыть в VK",
        url=profile.vk_url
    ))
    builder.row(InlineKeyboardButton(
        text="🗑 Удалить",
        callback_data=f"delete_vk_{profile_id}"
    ))
    builder.row(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="back_to_vk_profiles"
    ))

    await callback.message.edit_text(
        f"👤 <b>{profile.vk_name}</b>\n"
        f"🔗 {profile.vk_url}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@vk_router.callback_query(F.data.startswith("get_vk_audio_"))
async def get_vk_audio(callback: CallbackQuery):
    if not vk_service or not vk_service.is_available():
        await callback.answer("VK сервис недоступен", show_alert=True)
        return

    profile_id = int(callback.data.split("_")[3])

    async with async_session() as session:
        profile = await session.get(VKProfile, profile_id)

    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    await callback.answer("⏳ Загружаю аудиозаписи...")

    try:
        parsed = VKMusicService.parse_vk_url(profile.vk_url)

        if parsed and parsed["type"] == "playlist":
            songs = vk_service.get_playlist_songs(parsed["owner_id"], parsed["playlist_id"], count=20)
        else:
            songs = vk_service.get_user_songs(profile.vk_user_id, count=20)

        if not songs:
            await callback.message.answer("😔 Аудиозаписи недоступны или профиль закрыт")
            return

        builder = InlineKeyboardBuilder()

        for i, song in enumerate(songs[:15]):
            artist = song.get("artist", "Unknown")
            title = song.get("title", "Unknown")

            button_text = f"🎵 {artist} - {title}"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."

            builder.row(InlineKeyboardButton(text=button_text, callback_data=f"vkdl_{i}"))

        # Сохраняем в кэш
        search_cache[callback.from_user.id] = songs

        await callback.message.edit_text(
            f"🎵 <b>Аудиозаписи:</b> {profile.vk_name}\n\n"
            f"Найдено: {len(songs)} треков",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Get VK audio error: {e}")
        await callback.message.answer(f"❌ Ошибка: {str(e)}")


@vk_router.callback_query(F.data.startswith("vkdl_"))
async def download_vk_track(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю трек...")

    try:
        index = int(callback.data.split("_")[1])
        user_id = callback.from_user.id

        if user_id not in search_cache or index >= len(search_cache[user_id]):
            await callback.message.answer("❌ Трек не найден")
            return

        track = search_cache[user_id][index]
        url = track.get("url")

        if not url:
            await callback.message.answer("❌ Ссылка на трек недоступна")
            return

        audio_data = await vk_service.download_song(url)

        if not audio_data:
            await callback.message.answer("❌ Не удалось скачать трек")
            return

        artist = track.get("artist", "Unknown")
        title = track.get("title", "Unknown")
        duration = track.get("duration", 0)

        audio_file = BufferedInputFile(
            audio_data,
            filename=f"{artist} - {title}.mp3"
        )

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="➕ Добавить в плейлист",
            callback_data=f"addpl_{index}"
        ))

        sent_audio = await callback.message.answer_audio(
            audio=audio_file,
            title=title,
            performer=artist,
            duration=duration,
            reply_markup=builder.as_markup()
        )

        # Сохраняем file_id
        if user_id in search_cache and index < len(search_cache[user_id]):
            search_cache[user_id][index]["file_id"] = sent_audio.audio.file_id

    except Exception as e:
        logger.error(f"VK download error: {e}")
        await callback.message.answer("❌ Ошибка при загрузке трека")


@vk_router.callback_query(F.data == "back_to_vk_profiles")
async def back_to_vk_profiles(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(
            select(VKProfile).where(VKProfile.user_id == callback.from_user.id)
        )
        profiles = result.scalars().all()

    await callback.message.edit_text(
        "👤 <b>Профили и плейлисты ВКонтакте:</b>",
        reply_markup=get_vk_profiles_kb(profiles),
        parse_mode="HTML"
    )


@vk_router.callback_query(F.data.startswith("delete_vk_"))
async def delete_vk_profile(callback: CallbackQuery):
    profile_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        profile = await session.get(VKProfile, profile_id)
        if profile:
            await session.delete(profile)
            await session.commit()

    await callback.answer("✅ Удалено")

    async with async_session() as session:
        result = await session.execute(
            select(VKProfile).where(VKProfile.user_id == callback.from_user.id)
        )
        profiles = result.scalars().all()

    await callback.message.edit_text(
        "👤 <b>Профили и плейлисты ВКонтакте:</b>",
        reply_markup=get_vk_profiles_kb(profiles),
        parse_mode="HTML"
    )


# ==================== ADMIN HANDLERS ====================
@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "🔧 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=get_admin_menu(),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "🔧 <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=get_admin_menu(),
        parse_mode="HTML"
    )


# ===== СТАТИСТИКА =====
@admin_router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "📊 <b>Статистика</b>\n\nВыберите период:",
        reply_markup=get_stats_kb(),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data.startswith("stats_"))
async def show_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    period = callback.data.split("_")[1]

    now = datetime.utcnow()

    if period == "day":
        start_date = now - timedelta(days=1)
        period_name = "за день"
    elif period == "week":
        start_date = now - timedelta(weeks=1)
        period_name = "за неделю"
    elif period == "month":
        start_date = now - timedelta(days=30)
        period_name = "за месяц"
    else:
        start_date = None
        period_name = "за всё время"

    async with async_session() as session:
        # Всего пользователей
        result = await session.execute(select(func.count(User.id)))
        total_users = result.scalar()

        # Новых за период
        if start_date:
            result = await session.execute(
                select(func.count(User.id)).where(User.created_at >= start_date)
            )
        else:
            result = await session.execute(select(func.count(User.id)))
        new_users = result.scalar()

        # Активных за период
        if start_date:
            result = await session.execute(
                select(func.count(User.id)).where(User.last_active >= start_date)
            )
        else:
            result = await session.execute(select(func.count(User.id)))
        active_users = result.scalar()

        # Поисков за период
        if start_date:
            result = await session.execute(
                select(func.count(SearchHistory.id)).where(SearchHistory.created_at >= start_date)
            )
        else:
            result = await session.execute(select(func.count(SearchHistory.id)))
        search_count = result.scalar()

        # Распознаваний
        if start_date:
            result = await session.execute(
                select(func.count(SearchHistory.id)).where(
                    and_(
                        SearchHistory.created_at >= start_date,
                        SearchHistory.search_type == "recognize"
                    )
                )
            )
        else:
            result = await session.execute(
                select(func.count(SearchHistory.id)).where(SearchHistory.search_type == "recognize")
            )
        recognize_count = result.scalar()

        # Плейлистов
        result = await session.execute(select(func.count(Playlist.id)))
        total_playlists = result.scalar()

        # Треков в плейлистах
        result = await session.execute(select(func.count(PlaylistTrack.id)))
        total_tracks = result.scalar()

    text = f"""📊 <b>Статистика {period_name}</b>

👥 <b>Пользователи:</b>
├ Всего: {total_users}
├ Новых: {new_users}
└ Активных: {active_users}

🔍 <b>Активность:</b>
├ Поисков: {search_count}
└ Распознаваний: {recognize_count}

📋 <b>Контент:</b>
├ Плейлистов: {total_playlists}
└ Треков в плейлистах: {total_tracks}

🎵 <b>VK Music:</b> {"✅ Активен" if vk_service and vk_service.is_available() else "❌ Не настроен"}"""

    await callback.message.edit_text(text, reply_markup=get_stats_kb(), parse_mode="HTML")


# ===== КАНАЛЫ =====
@admin_router.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        result = await session.execute(select(Channel))
        channels = result.scalars().all()

    await callback.message.edit_text(
        "📢 <b>Управление каналами для подписки:</b>\n\n"
        "✅ - канал активен\n"
        "❌ - канал отключён\n\n"
        "Нажмите на канал чтобы вкл/выкл",
        reply_markup=get_channels_kb(channels),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "add_channel")
async def add_channel_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "📢 <b>Добавление канала</b>\n\n"
        "Перешлите сообщение из канала или отправьте @username\n\n"
        "⚠️ Бот должен быть админом канала!",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_channel)


@admin_router.message(AdminStates.waiting_channel)
async def add_channel_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        if message.forward_from_chat:
            chat = message.forward_from_chat
            channel_id = chat.id
            channel_username = chat.username
            channel_title = chat.title
        elif message.text and message.text.startswith("@"):
            channel_username = message.text[1:]
            chat = await message.bot.get_chat(f"@{channel_username}")
            channel_id = chat.id
            channel_title = chat.title
        else:
            await message.answer("❌ Перешлите сообщение из канала или отправьте @username")
            return

        async with async_session() as session:
            result = await session.execute(
                select(Channel).where(Channel.channel_id == channel_id)
            )
            existing = result.scalar_one_or_none()

            if existing:
                await message.answer("❌ Канал уже добавлен")
                await state.clear()
                return

            channel = Channel(
                channel_id=channel_id,
                channel_username=channel_username,
                channel_title=channel_title
            )
            session.add(channel)
            await session.commit()

            result = await session.execute(select(Channel))
            channels = result.scalars().all()

        await state.clear()
        await message.answer(
            f"✅ Канал <b>{channel_title}</b> добавлен!",
            reply_markup=get_channels_kb(channels),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Add channel error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.clear()


@admin_router.callback_query(F.data.startswith("toggle_channel_"))
async def toggle_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    channel_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        channel = await session.get(Channel, channel_id)

        if channel:
            channel.is_active = not channel.is_active
            await session.commit()

        result = await session.execute(select(Channel))
        channels = result.scalars().all()

    status = "включён" if channel.is_active else "отключён"
    await callback.answer(f"Канал {status}")
    await callback.message.edit_reply_markup(reply_markup=get_channels_kb(channels))


@admin_router.callback_query(F.data == "delete_channel_menu")
async def delete_channel_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        result = await session.execute(select(Channel))
        channels = result.scalars().all()

    if not channels:
        await callback.answer("Нет каналов", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.row(InlineKeyboardButton(
            text=f"🗑 {channel.channel_title}",
            callback_data=f"delchan_{channel.id}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_channels"))

    await callback.message.edit_text(
        "🗑 <b>Выберите канал для удаления:</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data.startswith("delchan_"))
async def delete_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    channel_id = int(callback.data.split("_")[1])

    async with async_session() as session:
        channel = await session.get(Channel, channel_id)
        if channel:
            await session.delete(channel)
            await session.commit()

        result = await session.execute(select(Channel))
        channels = result.scalars().all()

    await callback.answer("✅ Канал удалён")
    await callback.message.edit_text(
        "📢 <b>Управление каналами:</b>",
        reply_markup=get_channels_kb(channels),
        parse_mode="HTML"
    )


# ===== РАССЫЛКИ =====
@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "📨 <b>Рассылка</b>\n\nВыберите действие:",
        reply_markup=get_broadcast_kb(),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "create_broadcast")
async def create_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "📝 <b>Создание рассылки</b>\n\nОтправьте текст (поддерживается HTML):",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_broadcast_text)
    await state.update_data(is_scheduled=False)


@admin_router.callback_query(F.data == "schedule_broadcast")
async def schedule_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "📝 <b>Отложенная рассылка</b>\n\nОтправьте текст (поддерживается HTML):",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_broadcast_text)
    await state.update_data(is_scheduled=True)


@admin_router.message(AdminStates.waiting_broadcast_text)
async def broadcast_text_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(broadcast_text=message.text or message.caption or "")

    await message.answer(
        "🖼 Отправьте фото или напишите <b>пропустить</b>:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_broadcast_photo)


@admin_router.message(AdminStates.waiting_broadcast_photo)
async def broadcast_photo_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if message.photo:
        await state.update_data(broadcast_photo=message.photo[-1].file_id)
    else:
        await state.update_data(broadcast_photo=None)

    await message.answer(
        "🔘 <b>Добавьте кнопки</b>\n\n"
        "Формат: Текст | URL\n"
        "Каждая кнопка с новой строки.\n\n"
        "Пример:\n"
        "<code>Наш канал | https://t.me/channel</code>\n\n"
        "Или напишите <b>пропустить</b>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_broadcast_buttons)


@admin_router.message(AdminStates.waiting_broadcast_buttons)
async def broadcast_buttons_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    buttons = []

    if message.text and message.text.lower() != "пропустить":
        lines = message.text.strip().split("\n")
        for line in lines:
            if "|" in line:
                parts = line.split("|")
                if len(parts) == 2:
                    buttons.append({
                        "text": parts[0].strip(),
                        "url": parts[1].strip()
                    })

    await state.update_data(broadcast_buttons=buttons if buttons else None)

    data = await state.get_data()

    if data.get("is_scheduled"):
        await message.answer(
            "⏰ <b>Укажите время рассылки</b>\n\n"
            "Формат: ДД.ММ.ГГГГ ЧЧ:ММ\n"
            "Пример: <code>25.12.2024 15:30</code>",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_schedule_time)
    else:
        await execute_broadcast_now(message, state)


@admin_router.message(AdminStates.waiting_schedule_time)
async def schedule_time_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        scheduled_at = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")

        if scheduled_at <= datetime.now():
            await message.answer("❌ Время должно быть в будущем!")
            return

        data = await state.get_data()

        async with async_session() as session:
            broadcast = Broadcast(
                text=data.get("broadcast_text"),
                photo_file_id=data.get("broadcast_photo"),
                buttons=data.get("broadcast_buttons"),
                scheduled_at=scheduled_at
            )
            session.add(broadcast)
            await session.commit()

            await broadcast_scheduler.schedule_broadcast(broadcast.id, scheduled_at)

        await state.clear()
        await message.answer(
            f"✅ <b>Рассылка запланирована!</b>\n\n"
            f"📅 {scheduled_at.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=get_admin_menu(),
            parse_mode="HTML"
        )

    except ValueError:
        await message.answer("❌ Неверный формат. Используйте: ДД.ММ.ГГГГ ЧЧ:ММ")


async def execute_broadcast_now(message: Message, state: FSMContext):
    data = await state.get_data()

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.is_banned == False)
        )
        users = result.scalars().all()

    await state.clear()

    progress_msg = await message.answer(f"📨 Рассылка для {len(users)} пользователей...")

    keyboard = build_buttons_from_json(data.get("broadcast_buttons"))

    success = 0
    failed = 0

    for user in users:
        try:
            if data.get("broadcast_photo"):
                await message.bot.send_photo(
                    chat_id=user.user_id,
                    photo=data.get("broadcast_photo"),
                    caption=data.get("broadcast_text"),
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
            else:
                await message.bot.send_message(
                    chat_id=user.user_id,
                    text=data.get("broadcast_text"),
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Broadcast error: {e}")
            failed += 1

    await progress_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📤 Отправлено: {success}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML"
    )


# ===== ОТЛОЖЕННЫЕ =====
@admin_router.callback_query(F.data == "admin_scheduled")
async def admin_scheduled(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        result = await session.execute(
            select(Broadcast).where(
                Broadcast.is_sent == False,
                Broadcast.scheduled_at != None
            ).order_by(Broadcast.scheduled_at)
        )
        broadcasts = result.scalars().all()

    if not broadcasts:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu"))

        await callback.message.edit_text(
            "⏰ <b>Отложенные рассылки</b>\n\nНет запланированных.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    await callback.message.edit_text(
        "⏰ <b>Отложенные рассылки:</b>",
        reply_markup=get_scheduled_broadcasts_kb(broadcasts),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data.startswith("edit_broadcast_"))
async def edit_broadcast_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        broadcast = await session.get(Broadcast, broadcast_id)

    if not broadcast:
        await callback.answer("Не найдена", show_alert=True)
        return

    text = (
        f"📨 <b>Рассылка #{broadcast.id}</b>\n\n"
        f"📅 {broadcast.scheduled_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"📝 {(broadcast.text or '')[:100]}...\n"
        f"🖼 Фото: {'Да' if broadcast.photo_file_id else 'Нет'}\n"
        f"🔘 Кнопок: {len(broadcast.buttons) if broadcast.buttons else 0}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_edit_broadcast_kb(broadcast_id),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data.startswith("bedit_text_"))
async def edit_broadcast_text_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])
    await state.update_data(edit_broadcast_id=broadcast_id)
    await state.set_state(AdminStates.editing_broadcast_text)

    await callback.message.edit_text("📝 <b>Введите новый текст:</b>", parse_mode="HTML")


@admin_router.message(AdminStates.editing_broadcast_text)
async def edit_broadcast_text_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    broadcast_id = data.get("edit_broadcast_id")

    async with async_session() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast:
            broadcast.text = message.text
            await session.commit()

    await state.clear()
    await message.answer("✅ Текст обновлён!", reply_markup=get_admin_menu())


@admin_router.callback_query(F.data.startswith("bedit_photo_"))
async def edit_broadcast_photo_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])
    await state.update_data(edit_broadcast_id=broadcast_id)
    await state.set_state(AdminStates.editing_broadcast_photo)

    await callback.message.edit_text(
        "🖼 <b>Отправьте фото или 'удалить':</b>",
        parse_mode="HTML"
    )


@admin_router.message(AdminStates.editing_broadcast_photo)
async def edit_broadcast_photo_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    broadcast_id = data.get("edit_broadcast_id")

    async with async_session() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast:
            if message.photo:
                broadcast.photo_file_id = message.photo[-1].file_id
            elif message.text and message.text.lower() == "удалить":
                broadcast.photo_file_id = None
            await session.commit()

    await state.clear()
    await message.answer("✅ Фото обновлено!", reply_markup=get_admin_menu())


@admin_router.callback_query(F.data.startswith("bedit_buttons_"))
async def edit_broadcast_buttons_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])
    await state.update_data(edit_broadcast_id=broadcast_id)
    await state.set_state(AdminStates.editing_broadcast_buttons)

    await callback.message.edit_text(
        "🔘 <b>Введите кнопки:</b>\n\nТекст | URL\n\nИли 'удалить'",
        parse_mode="HTML"
    )


@admin_router.message(AdminStates.editing_broadcast_buttons)
async def edit_broadcast_buttons_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    broadcast_id = data.get("edit_broadcast_id")

    buttons = []
    if message.text and message.text.lower() != "удалить":
        for line in message.text.strip().split("\n"):
            if "|" in line:
                parts = line.split("|")
                if len(parts) == 2:
                    buttons.append({"text": parts[0].strip(), "url": parts[1].strip()})

    async with async_session() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast:
            broadcast.buttons = buttons if buttons else None
            await session.commit()

    await state.clear()
    await message.answer("✅ Кнопки обновлены!", reply_markup=get_admin_menu())


@admin_router.callback_query(F.data.startswith("bedit_time_"))
async def edit_broadcast_time_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])
    await state.update_data(edit_broadcast_id=broadcast_id)
    await state.set_state(AdminStates.editing_broadcast_time)

    await callback.message.edit_text(
        "⏰ <b>Новое время:</b>\n\nФормат: ДД.ММ.ГГГГ ЧЧ:ММ",
        parse_mode="HTML"
    )


@admin_router.message(AdminStates.editing_broadcast_time)
async def edit_broadcast_time_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        scheduled_at = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")

        if scheduled_at <= datetime.now():
            await message.answer("❌ Время должно быть в будущем!")
            return

        data = await state.get_data()
        broadcast_id = data.get("edit_broadcast_id")

        async with async_session() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast:
                broadcast.scheduled_at = scheduled_at
                await session.commit()

                broadcast_scheduler.cancel_broadcast(broadcast_id)
                await broadcast_scheduler.schedule_broadcast(broadcast_id, scheduled_at)

        await state.clear()
        await message.answer(
            f"✅ Время изменено: {scheduled_at.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=get_admin_menu()
        )

    except ValueError:
        await message.answer("❌ Неверный формат")


@admin_router.callback_query(F.data.startswith("send_now_"))
async def send_broadcast_now(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])

    await callback.answer("📨 Отправляю...")

    broadcast_scheduler.cancel_broadcast(broadcast_id)
    success_count = await broadcast_scheduler.execute_broadcast(broadcast_id)

    await callback.message.edit_text(
        f"✅ Отправлено: {success_count}",
        reply_markup=get_admin_menu()
    )


@admin_router.callback_query(F.data.startswith("delete_broadcast_"))
async def delete_broadcast(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    broadcast_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast:
            await session.delete(broadcast)
            await session.commit()

    broadcast_scheduler.cancel_broadcast(broadcast_id)

    await callback.answer("✅ Удалено")

    async with async_session() as session:
        result = await session.execute(
            select(Broadcast).where(
                Broadcast.is_sent == False,
                Broadcast.scheduled_at != None
            )
        )
        broadcasts = result.scalars().all()

    if broadcasts:
        await callback.message.edit_text(
            "⏰ <b>Отложенные рассылки:</b>",
            reply_markup=get_scheduled_broadcasts_kb(broadcasts),
            parse_mode="HTML"
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu"))
        await callback.message.edit_text(
            "⏰ Нет рассылок",
            reply_markup=builder.as_markup()
        )


# ===== ПРИВЕТСТВИЕ =====
@admin_router.callback_query(F.data == "admin_welcome")
async def admin_welcome(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "👋 <b>Изменение приветствия</b>\n\nОтправьте новый текст (HTML):",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_welcome_text)


@admin_router.message(AdminStates.waiting_welcome_text)
async def welcome_text_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(welcome_text=message.text)

    await message.answer(
        "🖼 Отправьте фото или <b>пропустить</b>:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_welcome_photo)


@admin_router.message(AdminStates.waiting_welcome_photo)
async def welcome_photo_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()

    async with async_session() as session:
        result = await session.execute(
            select(BotSettings).where(BotSettings.key == "welcome_text")
        )
        text_setting = result.scalar_one_or_none()

        if text_setting:
            text_setting.value = data.get("welcome_text")
        else:
            session.add(BotSettings(key="welcome_text", value=data.get("welcome_text")))

        result = await session.execute(
            select(BotSettings).where(BotSettings.key == "welcome_photo")
        )
        photo_setting = result.scalar_one_or_none()

        photo_id = message.photo[-1].file_id if message.photo else None

        if photo_setting:
            photo_setting.value = photo_id
        else:
            session.add(BotSettings(key="welcome_photo", value=photo_id))

        await session.commit()

    await state.clear()
    await message.answer(
        "✅ <b>Приветствие обновлено!</b>",
        reply_markup=get_admin_menu(),
        parse_mode="HTML"
    )


# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
broadcast_scheduler = BroadcastScheduler(bot)


async def set_commands():
    commands = [
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="profiles", description="👤 Профили Вк"),
        BotCommand(command="playlists", description="🎵 Плейлисты"),
        BotCommand(command="settings", description="⚙️ Настройки"),
        BotCommand(command="help", description="🆘 Помощь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())


async def on_startup():
    logger.info("Initializing database...")
    await init_db()

    logger.info("Setting bot commands...")
    await set_commands()

    logger.info("Starting broadcast scheduler...")
    broadcast_scheduler.start()
    await broadcast_scheduler.load_scheduled_broadcasts()

    # Проверяем VK сервис
    if vk_service and vk_service.is_available():
        logger.info("✅ VK Music Service is ready!")
    else:
        logger.warning("⚠️ VK Music Service is NOT configured. Add VK_TOKEN to enable full music features.")

    logger.info("Bot started successfully!")


async def on_shutdown():
    logger.info("Shutting down...")
    try:
        broadcast_scheduler.scheduler.shutdown()
    except Exception:
        pass


async def main():
    # Middleware
    dp.message.middleware(ActivityMiddleware())
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    # Роутеры (порядок важен!)
    dp.include_router(admin_router)
    dp.include_router(user_router)
    dp.include_router(recognize_router)
    dp.include_router(playlist_router)
    dp.include_router(vk_router)
    dp.include_router(music_router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
