import os, re, html, sqlite3, logging, asyncio, hashlib, hmac
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from io import BytesIO
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlparse

import requests
import feedparser
import trafilatura
import psycopg
from fastapi import FastAPI, Request, HTTPException
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("moskovskiy_dvizh")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@moscow_dvizh_rus")
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.db")
CRON_SECRET = os.environ["CRON_SECRET"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

client = OpenAI(api_key=OPENAI_API_KEY, timeout=60, max_retries=2)
tz = ZoneInfo(TIMEZONE)
telegram_app = Application.builder().token(BOT_TOKEN).build()
http = requests.Session()
http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MoskovskiyDvizhBot/2.0; +https://t.me/moscow_dvizh_rus)"})
generation_lock = asyncio.Lock()

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Москва+когда:1д&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Москва+происшествия+OR+транспорт+OR+город+когда:1д&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Москва+новости+сегодня&hl=ru&gl=RU&ceid=RU:ru",
]


@contextmanager
def db():
    conn = (psycopg.connect(DATABASE_URL, connect_timeout=15) if DATABASE_URL
            else sqlite3.connect(DB_PATH, timeout=20))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, created_at TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS drafts
            (id TEXT PRIMARY KEY, text TEXT, image_url TEXT, image_credit TEXT,
             image_license TEXT, source TEXT, created_at TEXT, status TEXT)""")
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(conn, sql, params=()):
    return conn.execute(sql.replace("?", "%s") if DATABASE_URL else sql, params)


def is_seen(url):
    with db() as c:
        return execute(c, "SELECT 1 FROM seen WHERE url=?", (url,)).fetchone() is not None


def mark_seen(url):
    with db() as c:
        if DATABASE_URL:
            execute(c, "INSERT INTO seen(url,created_at) VALUES (?,?) ON CONFLICT(url) DO NOTHING",
                    (url, datetime.now(tz).isoformat()))
        else:
            execute(c, "INSERT OR IGNORE INTO seen VALUES (?,?)", (url, datetime.now(tz).isoformat()))


def resolve_article_url(url):
    """Follow Google News redirectors and return the publisher's final URL."""
    try:
        r = http.get(url, timeout=25, allow_redirects=True)
        r.raise_for_status()
        final_url = r.url
        if "news.google.com" in urlparse(final_url).netloc:
            # Some Google News links need a second request before redirecting.
            m = re.search(r"https?%3A%2F%2F[^&]+", final_url)
            if m:
                from urllib.parse import unquote
                final_url = unquote(m.group(0))
        return final_url
    except Exception:
        log.exception("Could not resolve article URL: %s", url)
        return url


def get_candidates():
    items = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url, request_headers={"User-Agent": "Mozilla/5.0"})
            for entry in feed.entries[:30]:
                url = entry.get("link", "").strip()
                title = html.unescape(re.sub("<[^>]+>", " ", entry.get("title", ""))).strip()
                summary = html.unescape(re.sub("<[^>]+>", " ", entry.get("summary", ""))).strip()
                if url and title and not is_seen(url):
                    items.append({"title": title, "url": url, "summary": summary,
                                  "published": entry.get("published", "")})
        except Exception:
            log.exception("RSS fetch failed")
    unique = {}
    for item in items:
        unique.setdefault(item["url"], item)
    return list(unique.values())


def article_text(url, rss_summary=""):
    final_url = resolve_article_url(url)
    try:
        downloaded = trafilatura.fetch_url(final_url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if text and len(text.strip()) >= 400:
                return text[:14000], final_url
    except Exception:
        log.exception("Article extraction failed: %s", final_url)
    # RSS description is a fallback only; never pad it with invented details.
    clean_summary = re.sub(r"\s+", " ", rss_summary).strip()
    return clean_summary[:5000], final_url


def source_name(url, candidate_title):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    title_host = candidate_title.rsplit(" - ", 1)
    if len(title_host) == 2 and len(title_host[1]) < 60:
        return title_host[1].strip()
    return host or "источник из RSS"


def make_post(candidate, body, final_url):
    prompt = f"""
Ты редактор Telegram-канала «Московский Движ». Составь короткую новостную заметку.
Используй ТОЛЬКО факты из материалов ниже. Не додумывай детали, числа, даты,
причины, имена или цитаты. Новость должна непосредственно относиться к Москве.
Если данных недостаточно или связь с Москвой не подтверждена, верни ровно NOT_ENOUGH_INFO.
Не называй предположение фактом.
Формат: заголовок с уместным эмодзи, затем 2–4 коротких абзаца.
Не добавляй ссылки, хештеги, сведения о фото или строку источника.

Заголовок RSS: {candidate['title']}
Описание RSS: {candidate['summary']}
Дата RSS: {candidate.get('published','')}
Текст статьи или RSS-описание:
{body}
"""
    result = client.responses.create(model=MODEL, input=prompt)
    post = result.output_text.strip()
    if post != "NOT_ENOUGH_INFO":
        post += f"\n\nИсточник: {source_name(final_url, candidate['title'])}."
    return post


def clean_meta(value):
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def allowed_license(name):
    n = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if "nc" in n or "nd" in n or "noncommercial" in n or "noderivatives" in n:
        return False
    return n == "cc0" or "public domain" in n or bool(re.match(r"^cc by(?:-sa)?(?:\s|$)", n))


def image_search_terms(title):
    # Keep the query tied to the actual subject; avoid unrelated generic city views.
    words = re.findall(r"[А-Яа-яЁёA-Za-z0-9-]{3,}", title)
    stop = {"москва", "москве", "москвы", "москвой", "сегодня", "стало", "будет", "после", "новый", "новая", "новое", "россии", "россия"}
    terms = [w for w in words if w.lower() not in stop][:5]
    return "Moscow " + " ".join(terms) if terms else "Moscow city"


def find_free_image(query):
    api = "https://commons.wikimedia.org/w/api.php"
    params = {"action": "query", "format": "json", "generator": "search",
              "gsrsearch": f'filetype:bitmap {query}', "gsrnamespace": 6, "gsrlimit": 20,
              "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 1600}
    try:
        response = http.get(api, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()
        for page in data.get("query", {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata", {})
            license_name = clean_meta(meta.get("LicenseShortName", {}).get("value", ""))
            if not allowed_license(license_name):
                continue
            artist = clean_meta(meta.get("Artist", {}).get("value", ""))
            if not artist:
                continue
            image_url = info.get("thumburl") or info.get("url")
            page_url = page.get("canonicalurl", "")
            if image_url and page_url:
                return {"url": image_url, "credit": artist[:300],
                        "license": license_name, "page": page_url}
    except Exception:
        log.exception("Wikimedia search failed")
    return None


def create_draft():
    candidates = get_candidates()
    for item in candidates:
        body, final_url = article_text(item["url"], item["summary"])
        if len(body) < 350:
            log.info("Skipping: insufficient article/RSS text: %s", item["title"])
            continue
        try:
            post = make_post(item, body, final_url)
        except Exception:
            log.exception("OpenAI generation failed")
            continue
        if post == "NOT_ENOUGH_INFO" or len(post) < 180:
            mark_seen(item["url"])
            continue
        image = find_free_image(image_search_terms(item["title"]))
        if not image:
            log.info("No suitably licensed Wikimedia image for: %s", item["title"])
            continue
        # Credit and exact license are verified metadata, not model-generated.
        post += (f"\n\nФото: {image['credit']} / Wikimedia Commons.\n"
                 f"Лицензия: {image['license']}.")
        draft_id = hashlib.sha1((item["url"] + datetime.now(tz).isoformat()).encode()).hexdigest()[:10]
        with db() as c:
            execute(c, "INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?)",
                    (draft_id, post, image["url"], image["credit"], image["license"],
                     final_url, datetime.now(tz).isoformat(), "pending"))
        mark_seen(item["url"])
        return draft_id, post, image
    return None


def download_image(url):
    r = http.get(url, timeout=35)
    r.raise_for_status()
    if not r.headers.get("Content-Type", "").lower().startswith("image/"):
        raise ValueError("Wikimedia URL did not return an image")
    return r.content


async def send_draft_to_admin():
    if generation_lock.locked():
        log.warning("Draft generation already running; skipping duplicate trigger")
        return
    async with generation_lock:
        result = await asyncio.to_thread(create_draft)
        if not result:
            await telegram_app.bot.send_message(chat_id=ADMIN_USER_ID,
                text="Не удалось найти новость с достаточным количеством проверяемых фактов и подходящей фотографией с разрешённой лицензией. Ничего не опубликовано.")
            return
        draft_id, post, image = result
        try:
            photo_bytes = await asyncio.to_thread(download_image, image["url"])
            photo = BytesIO(photo_bytes)
            photo.name = "moscow.jpg"
            # Photo and editable text are deliberately sent as separate messages.
            await telegram_app.bot.send_photo(chat_id=ADMIN_USER_ID, photo=photo)
            await telegram_app.bot.send_message(chat_id=ADMIN_USER_ID, text=post,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")
                ]]))
            await telegram_app.bot.send_message(chat_id=ADMIN_USER_ID,
                text=f"Страница изображения и лицензия: {image['page']}")
        except Exception:
            log.exception("Could not deliver draft to admin")
            with db() as c:
                execute(c, "UPDATE drafts SET status='delivery_error' WHERE id=?", (draft_id,))
            raise


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Бот доступен только администратору.")
        return
    await update.message.reply_text("Бот «Московский Движ» работает.\nКоманды: /id, /now, /status.")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text("Ищу новость и готовлю черновик…")
    await send_draft_to_admin()


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    with db() as c:
        pending = execute(c, "SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0]
    await update.message.reply_text(f"Бот работает.\nКанал: {CHANNEL_ID}\nЧерновиков на рассмотрении: {pending}\nРасписание: 09:00, 14:00 и 19:00 по Москве.")


async def decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_USER_ID:
        await q.edit_message_reply_markup(reply_markup=None)
        return
    action, draft_id = q.data.split(":", 1)
    with db() as c:
        row = execute(c, "SELECT text,image_url,image_credit,image_license,source,status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row or row[5] != "pending":
            await q.edit_message_text("Этот черновик уже обработан.")
            return
        post, image_url, credit, license_name, source, _ = row
        if action == "reject":
            execute(c, "UPDATE drafts SET status='rejected' WHERE id=?", (draft_id,))
            await q.edit_message_text("❌ Черновик отклонён.")
            return
        if action == "publish":
            try:
                photo_data = await asyncio.to_thread(download_image, image_url)
                photo = BytesIO(photo_data)
                photo.name = "moscow.jpg"
                # Keep image and text as separate channel messages, matching the review layout.
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=photo)
                await context.bot.send_message(chat_id=CHANNEL_ID, text=post)
                execute(c, "UPDATE drafts SET status='published' WHERE id=?", (draft_id,))
                await q.edit_message_text("✅ Опубликовано в канале.")
            except Exception:
                log.exception("Publishing failed")
                await q.message.reply_text("Не удалось опубликовать. Проверьте, что бот — администратор канала и у него есть право публикации.")


async def on_startup():
    if not BASE_URL:
        raise RuntimeError("Render не передал RENDER_EXTERNAL_URL")
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("id", id_cmd))
    telegram_app.add_handler(CommandHandler("now", now_cmd))
    telegram_app.add_handler(CommandHandler("status", status_cmd))
    telegram_app.add_handler(CallbackQueryHandler(decision, pattern=r"^(publish|reject):"))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(url=f"{BASE_URL}/telegram", secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    log.info("Telegram webhook configured")


async def on_shutdown():
    await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db() as c:
        pass
    await on_startup()
    yield
    await on_shutdown()

api = FastAPI(lifespan=lifespan)

@api.get("/")
async def root():
    return {"service": "Moskovskiy Dvizh bot", "status": "ok"}

@api.get("/health")
async def health():
    return {"status": "ok"}

@api.post("/telegram")
async def telegram_webhook(request: Request):
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    update = Update.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@api.get("/wake")
async def wake():
    return {"status": "awake"}

@api.get("/cron")
async def cron(request: Request):
    key = request.query_params.get("key", "")
    if not hmac.compare_digest(key, CRON_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    if generation_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(send_draft_to_admin())
    return {"status": "draft_generation_started"}

if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=PORT)
