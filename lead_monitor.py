#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Монитор фриланс-лидов -> уведомления в Telegram.

Источники (оба легальные, ни один не логинится на биржу и не отправляет отклики сам):

1. FL.ru  - официальная RSS-лента заказов.
   https://feedback.fl.ru/knowledge-bases/2/articles/17264-podpiska-na-zakazyi-po-rss

2. Kwork  - у Kwork нет публичного API/RSS для третьих лиц. Поэтому источник другой:
   почтовый ящик, куда САМ Kwork присылает уведомления о новых заказах.
   Сначала включи их: на kwork.ru -> Настройки -> Уведомления -> Email, по своим категориям.
   Дальше этот скрипт читает эти письма по IMAP и пересылает их в Telegram.
   Он не логинится на kwork.ru и не имеет отношения к самому сайту вообще.

3. Яндекс Крауд - это не биржа фриланса, а найм в саму Яндекс (сотрудники поддержки,
   продаж, контента, тестирования и т.д. с зарплатой на карту). На их сайте
   crowd.yandex.ru нет категории "разработка" и нет официального RSS/API.
   Зато "Яндекс Крауд" зарегистрирован как отдельный работодатель на hh.ru,
   а у hh.ru есть официальный публичный API без токенов и логина - именно
   через него скрипт и следит за вакансиями этого работодателя.
   Отклик на вакансию - это отклик с резюме на hh.ru, ты делаешь это сам.

Во всех случаях отклик/резюме ты отправляешь сам, вручную, за пару секунд.

ЗАПУСК: этот скрипт больше не крутится в бесконечном цикле сам по себе -
он делает одну проверку и завершается. Расписание (например, раз в 15 минут)
задаёт GitHub Actions (см. .github/workflows/monitor.yml) - это бесплатно
и не требует своего сервера/ПК. Токен и chat_id скрипт берёт из переменных
окружения (секретов), а не хранит в коде - так репозиторий можно спокойно
делать публичным, и лимит бесплатных минут GitHub Actions вообще не действует.
"""

import email
import imaplib
import json
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header

import feedparser
import requests

# ==================== ОБЩИЕ НАСТРОЙКИ ====================

# Токен и chat_id теперь берутся из переменных окружения (GitHub Secrets),
# а не хранятся в самом файле - см. .github/workflows/monitor.yml.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SEEN_FILE = "seen_leads.json"

# Ключевые слова для фильтра (без учёта регистра), общие для всех источников.
# Пустой список [] = не фильтровать, брать всё.
KEYWORDS = [
    # сайты, лендинги, вёрстка
    "сайт", "лендинг", "одностраничник", "одностраничн", "landing", "визитка",
    "html", "css", "javascript", "js", "typescript",
    "react", "vue", "next.js", "nuxt", "фронтенд", "frontend", "backend", "фулстек", "fullstack",
    "верстка", "вёрстка", "wordpress", "tilda", "тильда",
    "разработ", "программист", "верстальщик", "программ",
    # телеграм-боты и мини-приложения
    "telegram bot", "телеграм бот", "тг бот", "чат-бот", "chatbot", "бот для телеграм",
    "mini app", "миниапп", "мини-апп", "tma", "web app", "веб-апп", "телеграм-приложение",
    # ИИ, боты, агенты, automation - категории, которые сам отметил в Kwork
    "ии-бот", "ai-бот", "ии агент", "ai agent", "ии-агент", "ai-агент",
    "искусственный интеллект", "нейросет", "chatgpt", "gpt", "llm",
    "машинное обучение", "machine learning", "интернет вещей", "iot",
    "скрипт", "автоматизация",
    # мобильная разработка
    "мобильное приложение", "ios", "android", "flutter", "react native",
    # сервера, хостинг, десктоп-программы
    "сервер", "хостинг", "деплой", "deploy", "программа на заказ",
    # разработка игр
    "unity", "unreal", "разработка игр", "геймдев", "gamedev", "игровой движок", "godot",
    # смежное
    "интернет-магазин", "квиз", "парсер", "интеграция api", "автоматизация сайта",
]

# Опциональная более широкая фраза для встроенного текстового поиска hh.ru
# (см. ИСТОЧНИК 4 ниже) - можно оставить как есть, работает вместе с KEYWORDS.
HH_SEARCH_TEXT = (
    "лендинг OR фронтенд OR frontend OR fullstack OR "
    '"телеграм бот" OR "telegram bot" OR "mini app" OR верстальщик OR '
    'unity OR unreal OR "разработка игр" OR геймдев OR gamedev'
)

# Черновик отклика от Claude на каждый подходящий лид - платная фича (нужны
# кредиты на console.anthropic.com), поэтому по умолчанию выключено.
USE_AI_DRAFT = False
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ==================== ИСТОЧНИК 1: FL.RU (RSS) ====================

FLRU_ENABLED = True

# Список категорий FL.ru, которые проверяем (полный список категорий сайта
# смотри внизу страницы https://www.fl.ru/projects/, кнопка "RSS"):
#   5/37 -> Программирование > Веб-программирование
#   2    -> Разработка сайтов (отдельная категория, раньше вообще не была подключена)
#   16   -> Разработка игр
FLRU_RSS_URLS = [
    "https://www.fl.ru/rss/all.xml?category=5&subcategory=37",
    "https://www.fl.ru/rss/all.xml?category=2",
    "https://www.fl.ru/rss/all.xml?category=16",
]

# Weblancer - RSS-лента, которая тут была (jobs.rss), перестала существовать
# (проверено напрямую - 404, не разовый сбой). Похоже, разработчики Weblancer
# отключили RSS совсем. Выключаю источник, чтобы не засорял лог ошибкой
# каждый прогон - если они когда-нибудь вернут RSS, включается одной строкой.
WEBLANCER_ENABLED = False
WEBLANCER_RSS_URL = "https://www.weblancer.net/rss/jobs.rss"

# ==================== ИСТОЧНИК 2: KWORK (ПОЧТА) ====================

KWORK_ENABLED = True

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")  # Gmail: imap.gmail.com; Mail.ru: imap.mail.ru
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")  # пароль приложения, не обычный пароль от почты
IMAP_FOLDER = "INBOX"
KWORK_SENDER_MATCH = "kwork"          # подстрока в адресе отправителя письма

# ==================== ИСТОЧНИК 3: ЯНДЕКС КРАУД (через hh.ru) ====================

HH_ENABLED = True

# id работодателя "Яндекс Крауд" на hh.ru (страница: https://hh.ru/employer/9498112).
# Если вдруг захочешь другого работодателя - открой его страницу на hh.ru,
# id будет в адресной строке.
HH_EMPLOYER_ID = "9498112"

# Публичный API hh.ru не требует токена, но просит представиться в User-Agent.
# Можно вписать сюда свою почту - это просто вежливость, не обязательно.
HH_USER_AGENT = "lead-monitor-personal-script/1.0"

# ==================== ИСТОЧНИК 4: HH.RU - ШИРОКИЙ ПОИСК ====================

# В отличие от ИСТОЧНИКА 3 (только "Яндекс Крауд"), тут ищем по ключевым
# словам (HH_SEARCH_TEXT выше) сразу у ВСЕХ работодателей на hh.ru, с упором
# на проектную/удалённую занятость - это ближе всего к разовым заказам,
# а не к постоянному найму. Источник расширяет охват без ввода новой биржи.
HH_BROAD_ENABLED = True
HH_BROAD_PARAMS = {
    "text": HH_SEARCH_TEXT,
    "schedule": "remote",       # удалённая работа - большинству фрилансеров это и нужно
    "per_page": 100,
    "order_by": "publication_time",
}

# =====================================================


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


def strip_html(text):
    if not text:
        return ""
    # Сначала убираем блоки <style> и <script> целиком вместе с содержимым
    # (именно оттуда вываливается CSS в уведомления от Kwork)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Теперь убираем оставшиеся HTML-теги
    text = re.sub(r"<[^>]+>", " ", text)
    # Схлопываем множественные пробелы/переносы
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def matches_keywords(text):
    if not KEYWORDS:
        return True
    low = text.lower()
    return any(kw.lower() in low for kw in KEYWORDS)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[telegram] ошибка отправки: {e}")


def generate_draft(title, description):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            "Заказ на фриланс-бирже.\n"
            f"Название: {title}\nОписание: {description}\n\n"
            "Напиши короткий (2-4 предложения) персональный отклик фрилансера "
            "веб-разработчика на этот заказ. По-русски, без канцелярита, "
            "по существу задачи, с уточняющим вопросом или сроком."
        )
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[ai-draft] ошибка генерации: {e}")
        return None


def notify(source, title, description, link):
    msg = f"🆕 {source}\n\n{title}\n\n{description[:300]}"
    if link:
        msg += f"\n\n🔗 {link}"

    if USE_AI_DRAFT and (title or description):
        draft = generate_draft(title, description)
        if draft:
            msg += f"\n\n✍️ Черновик отклика:\n{draft}"

    send_telegram(msg)
    print(f"[+] {source}: {title}")


def fetch_feed(url, timeout=15):
    """
    Скачивает RSS-ленту с настоящим таймаутом и отдаёт её в feedparser.
    Важно: feedparser.parse(url) сам по себе таймаута не имеет и может
    зависнуть на весь запуск, если сервер ленты подвиснет - та же беда,
    что была с IMAP. Поэтому качаем сами через requests(timeout=...),
    а feedparser только разбирает уже скачанные байты.
    """
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "lead-monitor-personal-script/1.0"})
    r.raise_for_status()
    return feedparser.parse(r.content)


# ---------------------- FL.RU ----------------------

def check_flru(seen):
    if not FLRU_ENABLED:
        return
    for url in FLRU_RSS_URLS:
        try:
            feed = fetch_feed(url)
        except Exception as e:
            print(f"[fl.ru] ошибка загрузки ленты ({url}) ({type(e).__name__}): {e}")
            continue

        for entry in feed.entries:
            uid = "flru:" + (entry.get("id") or entry.get("link", ""))
            if uid == "flru:" or uid in seen:
                continue
            seen.add(uid)

            title = strip_html(entry.get("title", "Без названия"))
            description = strip_html(entry.get("summary", ""))
            link = entry.get("link", "")

            if matches_keywords(f"{title} {description}"):
                notify("FL.ru", title, description, link)


# ---------------------- WEBLANCER ----------------------

def check_weblancer(seen):
    if not WEBLANCER_ENABLED:
        return
    try:
        feed = fetch_feed(WEBLANCER_RSS_URL)
    except Exception as e:
        print(f"[weblancer] ошибка загрузки ленты ({type(e).__name__}): {e}")
        return

    for entry in feed.entries:
        uid = "weblancer:" + (entry.get("id") or entry.get("link", ""))
        if uid == "weblancer:" or uid in seen:
            continue
        seen.add(uid)

        title = strip_html(entry.get("title", "Без названия"))
        description = strip_html(entry.get("summary", ""))
        link = entry.get("link", "")

        if matches_keywords(f"{title} {description}"):
            notify("Weblancer", title, description, link)


# ---------------------- KWORK (ПОЧТА) ----------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(d):
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def decode_mime(value):
    if not value:
        return ""
    parts = decode_header(value)
    result = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                result += text.decode(enc or "utf-8", errors="ignore")
            except (LookupError, TypeError):
                # некоторые сервера присылают нестандартные имена кодировок
                # (например "unknown-8bit") - в этом случае просто берём utf-8
                result += text.decode("utf-8", errors="ignore")
        else:
            result += text
    return result


def get_email_body(msg):
    """Возвращает СЫРОЕ тело письма (HTML как есть, без снятия тегов) -
    это важно для check_kwork_mail, которому нужны настоящие <a href=...>
    ссылки на конкретные заказы. Кто хочет чистый текст - сам вызывает
    strip_html() на результате."""
    if msg.is_multipart():
        plain, html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
        # HTML предпочтительнее, если есть - там и живут ссылки на заказы;
        # plain-текст оставляем только как резерв, если HTML-версии вообще нет.
        return html if html else plain
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="ignore")
        except Exception:
            text = ""
        return text


def check_kwork_mail(seen):
    if not KWORK_ENABLED:
        return
    if not IMAP_USER or not IMAP_PASSWORD:
        print("[kwork-mail] IMAP_USER/IMAP_PASSWORD не заданы - пропускаю")
        return
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=20)
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)

        since = imap_date(datetime.now() - timedelta(hours=3))
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            conn.logout()
            return

        for num in data[0].split():
            # BODY.PEEK - читаем письмо, не помечая его прочитанным у тебя в почте
            status, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            sender = decode_mime(msg.get("From", ""))

            if KWORK_SENDER_MATCH.lower() not in sender.lower():
                continue

            message_id = msg.get("Message-ID") or f"num:{num.decode()}"
            uid = "kwork:" + message_id

            # Дополнительная дедупликация по теме+дате: Kwork шлёт несколько
            # дайджестов подряд с разными Message-ID, но одинаковой темой и датой.
            # Если тема+дата уже видели - пропускаем, даже если Message-ID новый.
            subject_raw = decode_mime(msg.get("Subject", ""))
            date_raw = msg.get("Date", "")[:16]  # точность до часа
            subject_uid = "kwork-subj:" + subject_raw + "|" + date_raw

            if uid in seen or subject_uid in seen:
                seen.add(uid)  # запоминаем Message-ID чтобы не проверять снова
                continue
            seen.add(uid)
            seen.add(subject_uid)

            body = get_email_body(msg)

            # Письма-дайджесты от Kwork на самом деле содержат таблицу с
            # отдельными заказами внутри - каждый со своей ссылкой вида
            # kwork.ru/projects/12345-nazvanie. Раньше весь текст письма
            # (шапка + таблица) просто склеивался в одну кашу после
            # снятия тегов - оттуда и "Название Покупатель Цена Дизайн
            # визитки...". Теперь сначала пробуем вытащить именно ссылки
            # на конкретные заказы и обработать их по отдельности - так
            # же, как FL.ru/Weblancer/hh.ru дают заголовок+ссылку на
            # каждый лид, а не один слипшийся кусок текста.
            project_links = re.findall(
                r'<a[^>]+href="(https?://(?:www\.)?kwork\.ru/new_offer\?project\d+)"[^>]*>(.*?)</a>(.{0,400}?)</td>',
                body, flags=re.IGNORECASE | re.DOTALL,
            )
            project_links = [
                (link, strip_html(title), strip_html(extra))
                for link, title, extra in project_links
                if strip_html(title)
            ]

            if project_links:
                for link, title, category_text in project_links:
                    link_uid = "kwork-link:" + link
                    if link_uid in seen:
                        continue
                    seen.add(link_uid)
                    # Матчим по заголовку + категории (категория от Kwork -
                    # например "Скрипты, боты и mini apps > Парсеры" - часто
                    # даёт более точный сигнал, чем свободный текст заголовка)
                    if matches_keywords(f"{title} {category_text}"):
                        notify("Kwork (почта)", title, category_text, link)
            else:
                # Запасной вариант - вдруг Kwork поменяет вёрстку письма и
                # ссылки не найдутся. Тогда как раньше - весь текст одним куском,
                # лучше грубое уведомление, чем полная тишина.
                full_text = strip_html(body)
                match_text = full_text[:5000]
                snippet = full_text[:400]
                if matches_keywords(f"{subject_raw} {match_text}"):
                    notify("Kwork (почта)", subject_raw, snippet, "")

        conn.close()
        conn.logout()
    except Exception as e:
        print(f"[kwork-mail] ошибка ({type(e).__name__}): {e}")


# ---------------------- ЯНДЕКС КРАУД (через hh.ru API) ----------------------

def hh_get(params):
    """GET к api.hh.ru с понятной диагностикой при ошибке. Возвращает dict или None."""
    try:
        r = requests.get(
            "https://api.hh.ru/vacancies",
            params=params,
            headers={"User-Agent": HH_USER_AGENT},
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[hh.ru] HTTP {r.status_code}: {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[hh.ru] ошибка запроса ({type(e).__name__}): {e}")
        return None


def check_hh_crowd(seen):
    if not HH_ENABLED:
        return
    data = hh_get({"employer_id": HH_EMPLOYER_ID, "per_page": 50})
    if data is None:
        return

    for item in data.get("items", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "hh:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("name", "Без названия")
        snippet = item.get("snippet") or {}
        description = " ".join(
            filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
        )
        description = strip_html(description)
        link = item.get("alternate_url", "")

        if matches_keywords(f"{title} {description}"):
            notify("Яндекс Крауд (hh.ru)", title, description, link)


# ---------------------- HH.RU: ШИРОКИЙ ПОИСК ----------------------

def check_hh_broad(seen):
    if not HH_BROAD_ENABLED:
        return
    data = hh_get(HH_BROAD_PARAMS)
    if data is None:
        return

    for item in data.get("items", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "hhbroad:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("name", "Без названия")
        employer = (item.get("employer") or {}).get("name", "")
        snippet = item.get("snippet") or {}
        description = " ".join(
            filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
        )
        description = strip_html(description)
        link = item.get("alternate_url", "")

        if matches_keywords(f"{title} {description}"):
            label = f"hh.ru ({employer})" if employer else "hh.ru"
            notify(label, title, description, link)


# ---------------------- ИСТОЧНИК 5: SUPERJOB (API) ----------------------
#
# У SuperJob официальный API, но не полностью открытый как у hh.ru: нужно
# зарегистрировать своё приложение на api.superjob.ru/register/ и получить
# Secret key. Для обычного поиска вакансий (без вывода контактов) хватает
# только этого ключа в заголовке X-Api-App-Id - полноценный OAuth-логин
# пользователя тут НЕ нужен.

SUPERJOB_ENABLED = True
SUPERJOB_API_KEY = os.environ.get("SUPERJOB_API_KEY", "")  # Secret key приложения, см. README
SUPERJOB_SEARCH_KEYWORD = "разработчик"  # простой текстовый поиск, можно поменять - один широкий термин надёжнее, чем фраза из нескольких слов (не факт, что SuperJob ищет по фразе так же, как hh.ru)


def check_superjob(seen):
    if not SUPERJOB_ENABLED:
        return
    if not SUPERJOB_API_KEY:
        print("[superjob] SUPERJOB_API_KEY не задан - пропускаю")
        return
    try:
        r = requests.get(
            "https://api.superjob.ru/2.0/vacancies/",
            params={"keyword": SUPERJOB_SEARCH_KEYWORD, "period": 7, "count": 100, "place_of_work": 2},  # place_of_work=2 = "на дому" - т.е. удалённо, а не в офисе/на производстве
            headers={"X-Api-App-Id": SUPERJOB_API_KEY},
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[superjob] HTTP {r.status_code}: {r.text[:300]}")
            return
        data = r.json()
    except Exception as e:
        print(f"[superjob] ошибка запроса ({type(e).__name__}): {e}")
        return

    for item in data.get("objects", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "superjob:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("profession", "Без названия")
        description = strip_html(" ".join(filter(None, [item.get("work"), item.get("candidat")])))
        link = item.get("link", "")

        if matches_keywords(f"{title} {description}"):
            notify("SuperJob", title, description, link)


# ---------------------- ИСТОЧНИК 6: TELEGRAM-КАНАЛЫ (через RSS-мост) ----------------------
#
# У Telegram нет своего RSS для каналов, но есть открытые сервисы-мосты
# (RSSHub и подобные), которые превращают ЛЮБОЙ публичный канал в обычную
# RSS-ленту - ссылка вида https://rsshub.app/telegram/channel/ИМЯ_КАНАЛА.
# Никаких логинов в Telegram, никаких токенов - тот же принцип, что FL.ru
# и Weblancer, просто ещё один источник RSS.
#
# TELEGRAM_CHANNELS ниже - это ДВА канала для примера (нашёл по запросу
# "тг-каналы с заказами для фрилансеров"), сам их не вёл и не проверял
# годами - если знаешь каналы получше, смело замени юзернеймы на свои,
# формат тот же. Общий фильтр KEYWORDS применяется и здесь, так что даже
# смешанный по тематике канал не завалит уведомлениями всем подряд.

TELEGRAM_CHANNELS_ENABLED = True
RSSHUB_BASE = "https://rsshub.app"  # если этот инстанс ляжет - есть и другие публичные, просто замени адрес
TELEGRAM_CHANNELS = [
    "frilanser_vacansii",
    "distantsiya",
]


def check_telegram_channels(seen):
    if not TELEGRAM_CHANNELS_ENABLED:
        return
    for channel in TELEGRAM_CHANNELS:
        url = f"{RSSHUB_BASE}/telegram/channel/{channel}"
        try:
            feed = fetch_feed(url)
        except Exception as e:
            print(f"[tg:{channel}] ошибка загрузки ленты ({type(e).__name__}): {e}")
            continue

        for entry in feed.entries:
            uid = f"tg:{channel}:" + (entry.get("id") or entry.get("link", ""))
            if uid == f"tg:{channel}:" or uid in seen:
                continue
            seen.add(uid)

            title = strip_html(entry.get("title", ""))
            description = strip_html(entry.get("summary", ""))
            link = entry.get("link", "")

            if matches_keywords(f"{title} {description}"):
                notify(f"Telegram: {channel}", title or description[:80], description, link)


# ---------------------- ОДИН ЗАПУСК ----------------------

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ошибка] Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
              "(переменные окружения / GitHub Secrets). Проверка отменена.")
        return

    print("Проверка лидов...")
    seen = load_seen()

    if not seen:
        print("Первый запуск: индексирую текущие заказы/вакансии без уведомлений...")
        if FLRU_ENABLED:
            for url in FLRU_RSS_URLS:
                try:
                    feed = fetch_feed(url)
                    for entry in feed.entries:
                        uid = "flru:" + (entry.get("id") or entry.get("link", ""))
                        if uid != "flru:":
                            seen.add(uid)
                except Exception as e:
                    print(f"[fl.ru] ошибка индексации ({url}) ({type(e).__name__}): {e}")
        if WEBLANCER_ENABLED:
            try:
                feed = fetch_feed(WEBLANCER_RSS_URL)
                for entry in feed.entries:
                    uid = "weblancer:" + (entry.get("id") or entry.get("link", ""))
                    if uid != "weblancer:":
                        seen.add(uid)
            except Exception as e:
                print(f"[weblancer] ошибка индексации ({type(e).__name__}): {e}")
        if HH_ENABLED:
            data = hh_get({"employer_id": HH_EMPLOYER_ID, "per_page": 50})
            for item in (data or {}).get("items", []):
                if item.get("id"):
                    seen.add("hh:" + str(item["id"]))
        if HH_BROAD_ENABLED:
            data = hh_get(HH_BROAD_PARAMS)
            for item in (data or {}).get("items", []):
                if item.get("id"):
                    seen.add("hhbroad:" + str(item["id"]))
        if SUPERJOB_ENABLED and SUPERJOB_API_KEY:
            try:
                r = requests.get(
                    "https://api.superjob.ru/2.0/vacancies/",
                    params={"keyword": SUPERJOB_SEARCH_KEYWORD, "period": 7, "count": 100, "place_of_work": 2},  # place_of_work=2 = "на дому" - т.е. удалённо, а не в офисе/на производстве
                    headers={"X-Api-App-Id": SUPERJOB_API_KEY},
                    timeout=15,
                )
                if r.status_code < 400:
                    for item in r.json().get("objects", []):
                        if item.get("id"):
                            seen.add("superjob:" + str(item["id"]))
                else:
                    print(f"[superjob] HTTP {r.status_code}: {r.text[:300]}")
            except Exception as e:
                print(f"[superjob] ошибка индексации ({type(e).__name__}): {e}")
        if TELEGRAM_CHANNELS_ENABLED:
            for channel in TELEGRAM_CHANNELS:
                try:
                    feed = fetch_feed(f"{RSSHUB_BASE}/telegram/channel/{channel}")
                    for entry in feed.entries:
                        uid = f"tg:{channel}:" + (entry.get("id") or entry.get("link", ""))
                        if uid != f"tg:{channel}:":
                            seen.add(uid)
                except Exception as e:
                    print(f"[tg:{channel}] ошибка индексации ({type(e).__name__}): {e}")
        save_seen(seen)
        print("Проиндексировано. Дальше - только новое: заказы, письма Kwork, вакансии hh.ru.")
        return

    for check_fn in (check_flru, check_kwork_mail, check_hh_crowd, check_weblancer,
                      check_hh_broad, check_superjob, check_telegram_channels):
        print(f"-> {check_fn.__name__}")
        try:
            check_fn(seen)
        except Exception as e:
            print(f"[main] ошибка в {check_fn.__name__}: {e}")

    save_seen(seen)
    print(f"Проверка завершена. Всего в памяти: {len(seen)}.")


if __name__ == "__main__":
    main()
