#!/usr/bin/env python3
"""
Telegram Video Downloader Bot - Advanced Version
Features:
- Unlimited video downloads
- Auto-cleanup after 24 hours OR archive for recovery
- Profile-based monitoring (no need for tweet links)
- Backup bot connectivity
- Fully autonomous operation
"""

import os
import re
import json
import time
import asyncio
import logging
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Set
from concurrent.futures import ThreadPoolExecutor
import threading
import signal
import sys

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    JobQueue,
)

BOT_TOKEN = "8297133358:AAEjk-NnjUYceXfC-RRiveCJwTJwK_jkVMw"
BACKUP_BOT_TOKEN = ""
BACKUP_BOT_URL = ""

BASE_DIR = Path("/workspace/bot_data")
DOWNLOAD_DIR = BASE_DIR / "downloads"
ARCHIVE_DIR = BASE_DIR / "archive"
DB_PATH = BASE_DIR / "bot_database.db"
COOKIES_PATH = BASE_DIR / "cookies.txt"

MAX_FILE_SIZE = 2048 * 1024 * 1024
CLEANUP_AFTER_HOURS = 24
ARCHIVE_AFTER_HOURS = 48
CHECK_INTERVAL_MINUTES = 5
MAX_CONCURRENT_DOWNLOADS = 3

BASE_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(BASE_DIR / "bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_url TEXT UNIQUE NOT NULL,
                username TEXT,
                added_at TEXT,
                last_check TEXT,
                last_video_id TEXT,
                status TEXT DEFAULT 'active',
                total_videos INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_id TEXT UNIQUE,
                profile_url TEXT,
                file_path TEXT,
                file_size INTEGER,
                title TEXT,
                downloaded_at TEXT,
                uploaded_at TEXT,
                status TEXT DEFAULT 'downloaded',
                archived INTEGER DEFAULT 0,
                archive_path TEXT,
                checksum TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER,
                archived_at TEXT,
                archive_path TEXT,
                restored INTEGER DEFAULT 0,
                restored_at TEXT,
                FOREIGN KEY (video_id) REFERENCES videos(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backup_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER,
                sent_to_backup INTEGER DEFAULT 0,
                sent_at TEXT,
                backup_confirm TEXT,
                FOREIGN KEY (video_id) REFERENCES videos(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def add_profile(self, profile_url: str, username: str = "") -> bool:
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO profiles (profile_url, username, added_at, last_check)
                VALUES (?, ?, ?, ?)
            """, (profile_url, username, datetime.now().isoformat(), datetime.now().isoformat()))
            self.conn.commit()
            return True
        except:
            return False

    def remove_profile(self, profile_url: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM profiles WHERE profile_url = ?", (profile_url,))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_active_profiles(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM profiles WHERE status = 'active'")
        return [dict(row) for row in cursor.fetchall()]

    def update_profile_check(self, profile_url: str, last_video_id: str = None):
        cursor = self.conn.cursor()
        if last_video_id:
            cursor.execute("""
                UPDATE profiles SET last_check = ?, last_video_id = ?, total_videos = total_videos + 1
                WHERE profile_url = ?
            """, (datetime.now().isoformat(), last_video_id, profile_url))
        else:
            cursor.execute("UPDATE profiles SET last_check = ? WHERE profile_url = ?",
                          (datetime.now().isoformat(), profile_url))
        self.conn.commit()

    def add_video(self, tweet_id: str, profile_url: str, file_path: str,
                  file_size: int, title: str, checksum: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO videos
            (tweet_id, profile_url, file_path, file_size, title, downloaded_at, status, checksum)
            VALUES (?, ?, ?, ?, ?, ?, 'downloaded', ?)
        """, (tweet_id, profile_url, file_path, file_size, title, datetime.now().isoformat(), checksum))
        self.conn.commit()
        return cursor.lastrowid

    def get_video_by_tweet_id(self, tweet_id: str) -> Optional[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM videos WHERE tweet_id = ?", (tweet_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def mark_video_uploaded(self, tweet_id: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE videos SET uploaded_at = ?, status = 'uploaded' WHERE tweet_id = ?
        """, (datetime.now().isoformat(), tweet_id))
        self.conn.commit()

    def get_recent_videos(self, hours: int = 24) -> List[Dict]:
        cursor = self.conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        cursor.execute("""
            SELECT * FROM videos
            WHERE downloaded_at > ? AND status = 'uploaded' AND archived = 0
            ORDER BY downloaded_at DESC
        """, (cutoff,))
        return [dict(row) for row in cursor.fetchall()]

    def archive_video(self, video_id: int, archive_path: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE videos SET archived = 1, archive_path = ? WHERE id = ?
        """, (archive_path, video_id))
        cursor.execute("""
            INSERT INTO archive (video_id, archived_at, archive_path)
            VALUES (?, ?, ?)
        """, (video_id, datetime.now().isoformat(), archive_path))
        self.conn.commit()

    def get_video_to_archive(self) -> Optional[Dict]:
        cursor = self.conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=ARCHIVE_AFTER_HOURS)).isoformat()
        cursor.execute("""
            SELECT * FROM videos
            WHERE downloaded_at < ? AND archived = 0 AND status = 'uploaded'
            ORDER BY downloaded_at ASC LIMIT 1
        """, (cutoff,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_video_to_cleanup(self) -> Optional[Dict]:
        cursor = self.conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=CLEANUP_AFTER_HOURS)).isoformat()
        cursor.execute("""
            SELECT * FROM videos
            WHERE downloaded_at < ? AND archived = 0 AND status = 'uploaded'
            ORDER BY downloaded_at ASC LIMIT 1
        """, (cutoff,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def restore_from_archive(self, tweet_id: str) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM videos WHERE tweet_id = ? AND archived = 1
        """, (tweet_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("""
                UPDATE archive SET restored = 1, restored_at = ? WHERE video_id = ?
            """, (datetime.now().isoformat(), row['id']))
            self.conn.commit()
            return row['archive_path']
        return None

    def add_backup_record(self, video_id: int):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO backup_records (video_id, sent_at)
            VALUES (?, ?)
        """, (video_id, datetime.now().isoformat()))
        self.conn.commit()

    def set_setting(self, key: str, value: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)
        """, (key, value))
        self.conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row['value'] if row else None

db = Database()

def calculate_checksum(file_path: str) -> str:
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_file_size(file_path: str) -> int:
    return Path(file_path).stat().st_size if Path(file_path).exists() else 0

class VideoDownloader:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)
        self.downloading: Set[str] = set()
        self.lock = asyncio.Lock()

    async def download(self, url: str) -> Optional[Dict]:
        async with self.lock:
            if url in self.downloading:
                logger.info(f"Already downloading: {url}")
                return None
            self.downloading.add(url)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_template = str(DOWNLOAD_DIR / f"video_{timestamp}.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4][height>=720]/bestvideo[height>=480]/bestvideo/best[ext=mp4]/best',
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'writeinfojson': False,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }

        if COOKIES_PATH.exists():
            ydl_opts['cookiefile'] = str(COOKIES_PATH)

        try:
            loop = asyncio.get_event_loop()

            def _download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return info

            info = await loop.run_in_executor(self.executor, _download)

            if not info:
                return None

            ext = info.get('ext', 'mp4')
            files = list(DOWNLOAD_DIR.glob(f"video_{timestamp}.*"))
            if files:
                file_path = str(files[0])
                file_size = get_file_size(file_path)
                checksum = calculate_checksum(file_path)

                return {
                    'file_path': file_path,
                    'file_size': file_size,
                    'title': info.get('title', 'Video'),
                    'tweet_id': info.get('display_id', info.get('id', '')),
                    'checksum': checksum,
                }

        except Exception as e:
            logger.error(f"Download error for {url}: {e}")
        finally:
            self.downloading.discard(url)

        return None

    def cleanup_file(self, file_path: str):
        try:
            if Path(file_path).exists():
                Path(file_path).unlink()
                logger.info(f"Cleaned up: {file_path}")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

downloader = VideoDownloader()

class TwitterScraper:
    @staticmethod
    def extract_profile_videos(profile_url: str) -> List[str]:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'skip_download': True,
            'ignoreerrors': True,
        }

        if COOKIES_PATH.exists():
            ydl_opts['cookiefile'] = str(COOKIES_PATH)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(profile_url, download=False)

                if not info:
                    return []

                if 'entries' in info:
                    entries = list(info['entries'])
                elif isinstance(info, dict) and 'id' in info:
                    entries = [info]
                else:
                    entries = []

                video_urls = []
                for entry in entries:
                    if entry and entry.get('id'):
                        tweet_id = entry['id']
                        if entry.get('media_metadata') or entry.get('thumbnail'):
                            video_url = f"https://x.com/user/status/{tweet_id}"
                            video_urls.append(video_url)

                return video_urls

        except Exception as e:
            logger.error(f"Profile scrape error for {profile_url}: {e}")
            return []

    @staticmethod
    def get_latest_videos(profile_url: str, last_known_id: str = None) -> List[str]:
        videos = TwitterScraper.extract_profile_videos(profile_url)

        if not last_known_id:
            return videos[:10]

        new_videos = []
        for video_url in videos:
            tweet_id = video_url.split('/status/')[-1]
            if tweet_id != last_known_id:
                new_videos.append(video_url)
            else:
                break

        return new_videos

async def upload_to_telegram(context: ContextTypes.DEFAULT_TYPE, file_path: str,
                            caption: str, target_channel: str = None) -> bool:
    try:
        with open(file_path, 'rb') as video_file:
            if target_channel:
                await context.bot.send_video(
                    chat_id=target_channel,
                    video=video_file,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=600,
                    write_timeout=600
                )
            else:
                logger.warning("No target channel configured")
                return False
        return True
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return False

async def send_to_backup_bot(video_info: Dict) -> bool:
    if not BACKUP_BOT_TOKEN or not BACKUP_BOT_URL:
        return False

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BACKUP_BOT_URL}/backup",
                json={
                    'tweet_id': video_info['tweet_id'],
                    'file_path': video_info['file_path'],
                    'checksum': video_info['checksum']
                },
                headers={'Authorization': f'Bearer {BACKUP_BOT_TOKEN}'}
            ) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"Backup send error: {e}")
        return False

async def check_profiles_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Checking profiles for new videos...")

    profiles = db.get_active_profiles()
    target_channel = db.get_setting("target_channel")

    for profile in profiles:
        try:
            last_video_id = profile.get('last_video_id')
            new_videos = TwitterScraper.get_latest_videos(
                profile['profile_url'],
                last_video_id
            )

            for video_url in new_videos:
                tweet_id = video_url.split('/status/')[-1]

                if db.get_video_by_tweet_id(tweet_id):
                    continue

                logger.info(f"New video found: {video_url}")

                video_info = await downloader.download(video_url)

                if video_info:
                    video_id = db.add_video(
                        tweet_id=tweet_id,
                        profile_url=profile['profile_url'],
                        file_path=video_info['file_path'],
                        file_size=video_info['file_size'],
                        title=video_info['title'],
                        checksum=video_info['checksum']
                    )

                    if target_channel:
                        caption = f"🎬 {video_info['title']}"
                        success = await upload_to_telegram(
                            context,
                            video_info['file_path'],
                            caption,
                            target_channel
                        )

                        if success:
                            db.mark_video_uploaded(tweet_id)
                            db.add_backup_record(video_id)
                            logger.info(f"Uploaded: {tweet_id}")

                    db.update_profile_check(profile['profile_url'], tweet_id)

                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Profile check error for {profile['profile_url']}: {e}")

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running cleanup job...")

    video_to_archive = db.get_video_to_archive()
    if video_to_archive:
        try:
            archive_name = f"{Path(video_to_archive['file_path']).stem}_archived.mp4"
            archive_path = ARCHIVE_DIR / archive_name

            import shutil
            shutil.copy2(video_to_archive['file_path'], archive_path)

            db.archive_video(video_to_archive['id'], str(archive_path))
            logger.info(f"Archived: {video_to_archive['tweet_id']}")
        except Exception as e:
            logger.error(f"Archive error: {e}")

    video_to_cleanup = db.get_video_to_cleanup()
    if video_to_cleanup:
        downloader.cleanup_file(video_to_cleanup['file_path'])
        logger.info(f"Cleaned up: {video_to_cleanup['tweet_id']}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """
🎬 **أهلا بك في بوت تنزيل الفيديوهات الذكي!**

هذا البوت يعمل تلقائياً بدون أي تدخل منك!

**المميزات:**
✅ تنزيل لا نهائي من الملفات المتتبعه
✅ حذف الروابط التلقائي
✅ أرشفة بعد 48 ساعة
✅ حذف تلقائي بعد 24 ساعة
✅ دعم بوت احتياطي
✅ يعمل 24/7

**للبدء:**
1. أضف قناة الهدف أولاً
2. أضف ملف شخصي للتبع
3. البوت سيعمل تلقائياً!
"""
    keyboard = [
        [InlineKeyboardButton("📺 إضافة قناة", callback_data="add_channel")],
        [InlineKeyboardButton("👤 إضافة ملف شخصي", callback_data="add_profile")],
        [InlineKeyboardButton("📋 الملفات المتتبعه", callback_data="list_profiles")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 **دليل الاستخدام:**

**الأوامر:**
/start - بدء البوت
/addprofile [رابط] - إضافة ملف شخصي للتبع
/listprofiles - عرض الملفات المتتبعه
/removeprofile [رابط] - حذف ملف شخصي
/setchannel - ضبط القناة المستهدفة
/status - حالة البوت
/forcecheck - فحص فوري للملفات
/restartcleanup - بدء التنظيف اليدوي
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def add_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ **يرجى إدخال الرابط!**\n\nمثال: `/addprofile https://x.com/username`")
        return

    url = ' '.join(context.args)
    url = url.replace('twitter.com', 'x.com')

    if db.add_profile(url, url.split('/')[-1]):
        await update.message.reply_text(f"✅ **تم إضافة الملف الشخصي!**\n\n🔗 {url}")
    else:
        await update.message.reply_text("ℹ️ الملف الشخصي موجود بالفعل!")

async def list_profiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profiles = db.get_active_profiles()

    if not profiles:
        await update.message.reply_text("📋 **لا توجد ملفات متتبعه!**")
        return

    text = "📋 **الملفات المتتبعه:**\n\n"
    for profile in profiles:
        text += f"🔗 {profile['profile_url']}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def remove_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ **يرجى إدخال الرابط!**")
        return

    url = ' '.join(context.args)
    url = url.replace('twitter.com', 'x.com')

    if db.remove_profile(url):
        await update.message.reply_text(f"✅ **تم حذف الملف!**\n\n🔗 {url}")
    else:
        await update.message.reply_text("❌ الملف غير موجود!")

async def set_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ['private']:
        await update.message.reply_text("📺 **لتعيين القناة:**\n\n1. أضف البوت للقناة كمسؤول\n2. أرسل `/setchannel` من القناة")
        return

    channel_id = update.effective_chat.id
    db.set_setting("target_channel", str(channel_id))

    await update.message.reply_text(f"✅ **تم تعيين القناة!**\n\n📺 {update.effective_chat.title}\nID: `{channel_id}`")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profiles = db.get_active_profiles()
    recent_videos = db.get_recent_videos(24)
    target_channel = db.get_setting("target_channel")

    status_text = f"""
📊 **حالة البوت:**

🤖 **الحالة:** يعمل
📺 **القناة:** {'✓ محددة' if target_channel else '✗ غير محددة'}
👥 **الملفات المتتبعه:** {len(profiles)}
📥 **فيديوهات (24h):** {len(recent_videos)}
"""
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def force_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🔄 **جاري فحص الملفات...**")
    try:
        await check_profiles_job(context)
        await status_msg.edit_text("✅ **تم الفحص بنجاح!**")
    except Exception as e:
        await status_msg.edit_text(f"❌ **حدث خطأ:** {str(e)}")

async def restart_cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🧹 **جاري التنظيف...**")
    try:
        await cleanup_job(context)
        await status_msg.edit_text("✅ **تم التنظيف بنجاح!**")
    except Exception as e:
        await status_msg.edit_text(f"❌ **حدث خطأ:** {str(e)}")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ **يرجى إدخال معرف التغريدة!**\n\nمثال: `/restore 123456789`")
        return

    tweet_id = context.args[0]
    archive_path = db.restore_from_archive(tweet_id)

    if archive_path and Path(archive_path).exists():
        with open(archive_path, 'rb') as video_file:
            await update.message.reply_video(video=video_file, caption=f"📦 فيديو مسترجع من الأرشيف")
    else:
        await update.message.reply_text("❌ **الفيديو غير موجود في الأرشيف!**")

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "add_channel":
        await query.edit_message_text("📺 **إضافة قناة:**\n\n1. أضف البوت للقناة كمسؤول\n2. أرسل `/setchannel` من القناة")
    elif query.data == "add_profile":
        await query.edit_message_text("👤 **إضافة ملف شخصي:**\n\nأرسل الرابط بالأمر:\n`/addprofile https://x.com/username`")
    elif query.data == "list_profiles":
        profiles = db.get_active_profiles()
        if not profiles:
            await query.edit_message_text("📋 **لا توجد ملفات متتبعه!**")
            return
        text = "📋 **الملفات المتتبعه:**\n\n"
        for profile in profiles:
            text += f"🔗 {profile['profile_url']}\n"
        await query.edit_message_text(text, parse_mode="Markdown")

def main():
    logger.info("🤖 Bot is starting...")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addprofile", add_profile_command))
    application.add_handler(CommandHandler("listprofiles", list_profiles_command))
    application.add_handler(CommandHandler("removeprofile", remove_profile_command))
    application.add_handler(CommandHandler("setchannel", set_channel_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("forcecheck", force_check_command))
    application.add_handler(CommandHandler("restartcleanup", restart_cleanup_command))
    application.add_handler(CommandHandler("restore", restore_command))
    application.add_handler(CallbackQueryHandler(handle_callbacks))

    job_queue = application.job_queue

    job_queue.run_repeating(check_profiles_job, interval=CHECK_INTERVAL_MINUTES * 60, first=30)
    job_queue.run_repeating(cleanup_job, interval=60 * 60, first=60)

    logger.info("✅ Bot is running!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
