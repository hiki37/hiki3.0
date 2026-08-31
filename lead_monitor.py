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

   ВАЖНО: письма от Kwork бывают двух видов. Дайджест "Новые проекты на бирже"
   фильтруется по ключевым словам, а личные письма (покупатель написал в личку,
   оформил заказ, прислал предложение) уходят в Telegram ВСЕГДА, без фильтра -
   это уже не лид, а клиент, который ждёт ответа.

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
import html as html_module
import time
import imaplib
import json
import os
import re
import smtplib
import sys
import uuid
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formataddr

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

# ==================== ЧЕРНОВИКИ ОТКЛИКОВ ====================
#
# К каждому подходящему лиду Claude пишет готовый текст отклика, и он приходит
# в Telegram прямо под заказом. Тебе остаётся открыть ссылку, вставить и
# отправить - секунд десять вместо "писать с нуля".
#
# Почему именно черновик, а не автоматическая отправка: ни у Kwork, ни у
# Telegram-каналов нет API для отклика от лица исполнителя. Единственный
# способ отправить - зайти под твоим логином и нажать кнопку скриптом, что
# у Kwork прямо запрещено правилами и стоит коннектов (они платные). Цена
# ошибки - блокировка аккаунта и сожжённые деньги, причём на том самом
# аккаунте, ради которого всё и затевается. Поэтому отправляешь ты, руками.
# Движок черновиков:
#   "template" - БЕСПЛАТНО. Никаких ключей, подписок и внешних запросов:
#                скрипт сам определяет тип задачи по заголовку и рубрике и
#                подставляет заготовку с уточняющим вопросом под этот тип.
#   "ai"       - через Claude API. Тексты живее и точнее, но нужны платные
#                кредиты на console.anthropic.com (подписка Claude тут НЕ
#                подходит - это разные вещи).
#   "auto"     - "ai", если ключ задан, иначе "template". Ничего не ломается
#                и ничего не тратится, пока ключа нет.
DRAFT_ENGINE = "auto"

USE_AI_DRAFT = True
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DRAFT_MODEL = "claude-opus-5"

# Для каких источников готовить черновик (совпадение по подстроке в названии).
# FL.ru намеренно не включён: там отклик платный, и жечь деньги на заготовку
# к заказу, на который ты всё равно не ответишь, смысла нет.
DRAFT_SOURCES = ("Kwork", "Telegram", "Hacker News", "WeWorkRemotely",
                 "RemoteOK", "Карты", "Maps")

# Потолок только для платного движка "ai" - шаблоны бесплатны и не считаются.
DRAFT_MAX_PER_RUN = 5

# ВАЖНО: отредактируй под себя - отсюда берётся, кто ты и что умеешь.
# Используется движком "ai". Чем конкретнее, тем меньше отклик похож на шаблон.
ABOUT_ME = """
Разработчик. Основное: Python, Telegram-боты, парсеры, интеграции по API,
автоматизация рутины, скрипты обработки данных, несложные веб-задачи.
Работаю с ИИ-инструментами, поэтому небольшие задачи закрываю быстро - часто
в тот же день. Готов начать сразу и показать результат до оплаты. Быстрые MVP, мини апы, лендинги, сайты с подключением хостинга и домена.
"""

# Заготовки под типы задач. Проверяются СВЕРХУ ВНИЗ, побеждает первое
# совпадение, поэтому частные типы стоят выше общих. Правь под себя свободно:
# "what" - что ты сделаешь, "ask" - уточняющий вопрос. Вопрос обязателен и
# должен быть таким, чтобы без ответа заказчика работу не начать: именно
# вопрос отличает отклик от рассылки и вытягивает заказчика в диалог.
DRAFT_TEMPLATES = (
    {
        "keys": ("телеграм", "телеграмм", "telegram", "тг-бот", "тг бот", "тгбот",
                 "mini app", "миниапп", "мини-апп", "tma"),
        "what": "Соберу бота на Python (aiogram): сценарий, хранение данных и "
                "выгрузку туда, где вам удобно их смотреть.",
        "ask": "Подскажите, что бот должен делать в первую очередь и куда "
               "складывать результат - в таблицу, в канал или в админ-чат?",
    },
    {
        "keys": ("парсер", "парсинг", "спарсить", "скрап", "scrap", "выгрузк",
                 "собрать данные", "сбор данных"),
        "what": "Напишу парсер на Python с выгрузкой в удобный формат "
                "(Excel/CSV/база) и повторным запуском по расписанию.",
        "ask": "Скиньте одну ссылку для примера - какие поля нужно снимать со "
               "страницы и в каком виде вам удобнее получить выгрузку?",
    },
    {
        "keys": ("автоматизац", "скрипт", "excel", "гугл таблиц", "google sheets",
                 "таблиц", "отчёт", "отчет", "рутин"),
        "what": "Сделаю скрипт, который закроет эту рутину целиком: данные на "
                "вход, готовый результат на выход, настройки вынесу в конфиг, "
                "чтобы вы меняли их без меня.",
        "ask": "Скажите, на каком шаге сейчас уходит больше всего времени и в "
               "каком виде нужен итоговый файл?",
    },
    {
        "keys": ("интеграц", " api", "api ", "crm", "1с", "битрикс", "amocrm",
                 "вебхук", "webhook", "обмен"),
        "what": "Настрою интеграцию: заберу данные с одной стороны, приведу к "
                "нужному формату и передам на другую, с логом и обработкой "
                "ошибок, чтобы обмен не падал молча.",
        "ask": "Между какими системами нужен обмен и есть ли у вас доступ к их "
               "API - или его тоже предстоит добывать?",
    },
    {
        "keys": ("нейросет", "chatgpt", "gpt", "llm", "ии-", "ии ", "ai-", "ai ",
                 "искусственный интеллект"),
        "what": "Соберу решение на готовой модели по API: промпт, обработку "
                "ответов и понятный интерфейс, чтобы вы им пользовались, а не "
                "разбирались в настройках.",
        "ask": "Опишите в двух словах, что должно быть на входе и что вы хотите "
               "видеть на выходе - на паре реальных примеров сразу покажу результат.",
    },
    {
        "keys": ("сервер", "vps", "docker", "деплой", "deploy", "хостинг",
                 "настроить на своих"),
        "what": "Подниму и настрою всё на вашем сервере, оставлю короткую "
                "инструкцию, чтобы вы могли перезапустить сами.",
        "ask": "Какой у вас сервер и есть ли к нему доступ по SSH?",
    },
    {
        # Ставим ВЫШЕ общего "сайта": лендинг и MVP под ключ - отдельный
        # разговор, там ценно не "поправлю вёрстку", а "заберу задачу целиком,
        # вместе с хостингом и доменом".
        "keys": ("лендинг", "одностраничн", "mvp", "под ключ", "визитк",
                 "с нуля", "мини-апп", "миниапп", "мини апп"),
        "what": "Сделаю под ключ: страницу, подключение хостинга и домена - "
                "чтобы вы получили работающий адрес, а не папку с файлами.",
        "ask": "Есть ли уже текст и картинки, или собирать вместе с вами - и "
               "куплен ли домен?",
    },
    {
        "keys": ("wordpress", "вордпресс", "tilda", "тильда", "верстк",
                 "вёрстк", "сайт", "одностраничн", "интернет-магазин"),
        "what": "Возьмусь за правки по сайту: сделаю аккуратно, не ломая то, что "
                "уже работает, и покажу результат на копии перед публикацией.",
        "ask": "Пришлите ссылку на сайт - что именно нужно поправить?",
    },
    {
        "keys": ("android", "ios", "flutter", "react native", "мобильное приложение"),
        "what": "Возьмусь за доработку мобильного приложения.",
        "ask": "Есть ли исходники проекта и что именно нужно изменить в текущей "
               "версии?",
    },
    {
        "keys": ("unity", "unreal", "godot", "геймдев", "gamedev", "разработка игр"),
        "what": "Возьмусь за задачу по игровому проекту.",
        "ask": "На каком движке проект и что именно нужно сделать?",
    },
)

# Если тип задачи не опознан - общая заготовка. Специально сдержанная: лучше
# честный короткий вопрос, чем уверенные обещания по задаче, которую не понял.
DRAFT_FALLBACK = {
    "what": "Задача мне понятна и подходит по профилю - готов взяться.",
    "ask": "Чтобы не гадать по срокам и цене, опишите чуть подробнее, что должно "
           "получиться на выходе?",
}

# Англоязычные заготовки - для источников из ENGLISH_SOURCES. Тон другой,
# чем в русских: американский заказчик ждёт короткое письмо по делу, без
# извинений и без "готов приступить немедленно".
DRAFT_TEMPLATES_EN = (
    {
        "keys": ("telegram", "discord", "chatbot", "chat bot", "slack bot"),
        "what": "I can build this bot in Python - the flow, the data storage, "
                "and a simple way for you to read the results.",
        "ask": "What should it do first, and where would you like the data to "
               "land - a spreadsheet, a database, or a channel?",
    },
    {
        "keys": ("scrap", "crawl", "parser", "parse", "data extraction", "dataset"),
        "what": "I can write the scraper in Python and hand you the data as "
                "CSV, Excel or straight into a database, re-runnable on a schedule.",
        "ask": "Could you send one example URL and the fields you need from the "
               "page?",
    },
    {
        "keys": ("automat", "script", "spreadsheet", "google sheets", "excel",
                 "zapier", "make.com", "workflow", "manual process"),
        "what": "I can automate this end to end: your input goes in, the finished "
                "output comes out, and the settings live in a config file you can "
                "edit without me.",
        "ask": "Which step eats the most time right now, and what should the final "
               "output look like?",
    },
    {
        "keys": ("integrat", "api", "webhook", "crm", "sync", "connect"),
        "what": "I can build the integration with proper logging and error "
                "handling, so it doesn't fail silently at 3am.",
        "ask": "Which two systems need to talk to each other, and do you already "
               "have API access to both?",
    },
    {
        "keys": ("ai", "llm", "gpt", "openai", "anthropic", "claude", "rag",
                 "embedding", "prompt"),
        "what": "I can build this on top of an existing model API - the prompting, "
                "the output handling, and an interface you actually want to use.",
        "ask": "What goes in and what should come out? Send me two real examples "
               "and I'll show you the result on those.",
    },
    {
        "keys": ("server", "docker", "deploy", "devops", "aws", "vps", "hosting",
                 "ci/cd"),
        "what": "I can set this up on your infrastructure and leave you a short "
                "runbook so you can restart it yourself.",
        "ask": "What are you running on right now, and do I get SSH access?",
    },
    {
        "keys": ("landing page", "mvp", "prototype", "from scratch",
                 "new site", "one-pager", "micro app", "mini app"),
        "what": "I can take this end to end - the build plus hosting and the "
                "domain wired up, so you end up with a working URL rather than "
                "a folder of files.",
        "ask": "Do you already have the copy and images, or is that part of the "
               "job - and is the domain bought?",
    },
    {
        "keys": ("website", "wordpress", "webflow", "shopify",
                 "frontend", "front-end", "ui"),
        "what": "I can take this on and work against a staging copy, so nothing "
                "breaks on the live site while I do.",
        "ask": "Could you send the URL and the list of changes you have in mind?",
    },
)

DRAFT_FALLBACK_EN = {
    "what": "This looks like a good fit for what I do - happy to take it on.",
    "ask": "Rather than guess at scope: what does 'done' look like for you?",
}

DRAFT_PROOF_EN = ("I'll show you it working before you pay anything.")

# Бизнес с карт - случай наособицу. Тут никто ничего не заказывал: человек
# просто работает, и у него нет сайта. Значит это не отклик, а первое
# обращение, и оно должно быть коротким, вежливым и с понятным поводом
# ("увидел, что сайта нет"), иначе это обычный спам. Писать такое надо
# ПОШТУЧНО - в мессенджер или звонком, а не рассылкой по списку.
DRAFT_OUTREACH_RU = (
    "Здравствуйте! Нашёл вас в картах и заметил, что сайта у вас нет - только "
    "телефон и адрес. Я делаю простые сайты-визитки под ключ: страница с "
    "услугами и контактами, хостинг и домен подключаю сам, вам остаётся только "
    "давать ссылку клиентам. Могу за свой счёт собрать черновик именно под вас "
    "и показать - если не понравится, ничего не должны. Скажите, вам это "
    "интересно или сайт вам сейчас не нужен?"
)

DRAFT_OUTREACH_EN = (
    "Hi! I found you on the map and noticed you don't have a website yet - just "
    "a phone number and an address. I build simple one-page sites end to end: "
    "your services and contacts, with hosting and the domain wired up, so you "
    "just hand people a link. Happy to put together a draft for your business "
    "at my own cost so you can see it first - no obligation either way. Would "
    "that be useful, or is a website not something you need right now?"
)


def is_maps_source(source):
    low = (source or "").lower()
    return "карты" in low or "maps" in low


# Отдельный случай: покупатель написал лично. Это не отклик на заказ, а ответ
# человеку, который уже ждёт, - и тон тут совсем другой.
DRAFT_DIRECT_REPLY = (
    "Здравствуйте! Прошу прощения за задержку с ответом - ваше сообщение "
    "потерялось у меня в уведомлениях, увидел только сейчас. Если вопрос ещё "
    "актуален, я на связи и готов продолжить. Подскажите, задача ещё в силе?"
)

# ==================== ИСТОЧНИК 1: FL.RU (RSS) ====================

FLRU_ENABLED = True

# Список категорий FL.ru, которые проверяем (полный список категорий сайта
# смотри внизу страницы https://www.fl.ru/projects/, кнопка "RSS"):
#   5    -> Программирование ЦЕЛИКОМ. Раньше тут была только подкатегория
#           37 (веб-программирование), из-за чего мимо проходили боты,
#           скрипты, парсеры, десктоп и мобильная разработка - то есть ровно
#           те разовые задачи, которые быстрее всего закрываются.
#   2    -> Разработка сайтов (отдельная категория)
#   16   -> Разработка игр
#
# По логам GitHub Actions FL.ru - единственный источник, который стабильно
# отдаёт лиды (hh.ru отвечает 403, rsshub лежит), поэтому расширяем именно его.
FLRU_RSS_URLS = [
    "https://www.fl.ru/rss/all.xml?category=5",
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

HH_ENABLED = False   # выключено: hh.ru отвечает 403 на IP GitHub Actions

# id работодателя "Яндекс Крауд" на hh.ru (страница: https://hh.ru/employer/9498112).
# Если вдруг захочешь другого работодателя - открой его страницу на hh.ru,
# id будет в адресной строке.
HH_EMPLOYER_ID = "9498112"

# Публичный API hh.ru не требует токена, но требует представиться в
# User-Agent, причём хочет видеть там контакт. Безымянный User-Agent - одна
# из причин, по которой hh.ru отвечает 403. Свою почту в код не зашиваем:
# задай секрет HH_CONTACT (например, почту) - он подставится в User-Agent.
HH_CONTACT = os.environ.get("HH_CONTACT", "")
HH_USER_AGENT = (
    "lead-monitor/1.0 (%s)" % HH_CONTACT if HH_CONTACT else "lead-monitor/1.0"
)

# ==================== ИСТОЧНИК 4: HH.RU - ШИРОКИЙ ПОИСК ====================

# В отличие от ИСТОЧНИКА 3 (только "Яндекс Крауд"), тут ищем по ключевым
# словам (HH_SEARCH_TEXT выше) сразу у ВСЕХ работодателей на hh.ru, с упором
# на проектную/удалённую занятость - это ближе всего к разовым заказам,
# а не к постоянному найму. Источник расширяет охват без ввода новой биржи.
HH_BROAD_ENABLED = False   # выключено вместе с остальным hh.ru
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


def strip_html(text, keep_newlines=False):
    """HTML -> читаемый текст.

    keep_newlines=True сохраняет переносы строк (нужно для постов из
    Telegram, где перенос - часть смысла), иначе всё схлопывается в одну
    строку (так удобнее матчить по ключевым словам).
    """
    if not text:
        return ""
    # Сначала убираем блоки <style> и <script> целиком вместе с содержимым
    # (именно оттуда вываливается CSS в уведомления от Kwork)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    if keep_newlines:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|tr|li)\s*>", "\n", text, flags=re.IGNORECASE)
    # Теперь убираем оставшиеся HTML-теги
    text = re.sub(r"<[^>]+>", " ", text)
    # &amp; &nbsp; &gt; и прочие сущности -> нормальные символы.
    # Без этого в уведомление уезжает "Разработка и IT &gt; Боты".
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    if keep_newlines:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n[ \t]*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        # Схлопываем множественные пробелы/переносы
        text = re.sub(r"\s+", " ", text)
    return text.strip()


# Англоязычные источники фильтруются своим списком: русский список на них
# почти не срабатывает (там нет ни "бот", ни "разработ"), а общий список из
# двух языков сделал бы русские источники шумнее.
# Список строго технический и без коротких обрывков. Два урока, оплаченных
# боевым прогоном:
#
# 1. Слова про формат работы ("contract", "hourly", "part-time", "freelance")
#    есть в описании ЛЮБОЙ вакансии, поэтому фильтр пропускал всё подряд -
#    в Telegram уехали "Kitchen Porter", "Mail Sorter" и "Gardener".
# 2. Короткие куски ловятся внутри других слов: "api" сидит в "capital" и
#    "therapist", "aws" в "laws", "rag" в "storage" и "average", "excel" в
#    "excellent". Поэтому здесь только цельные термины.
KEYWORDS_EN = [
    # языки и стек
    "python", "javascript", "typescript", "node.js", "nodejs", "react",
    "vue.js", "next.js", "django", "flask", "fastapi", "postgres", "mysql",
    "html", "css", "golang", " rust ",
    # роли
    "frontend", "front-end", "backend", "back-end", "full stack", "fullstack",
    "web developer", "software engineer", "software developer",
    # боты
    "telegram bot", "discord bot", "slack bot", "chatbot", "chat bot",
    # сбор данных
    "scraper", "scraping", "crawler", "data extraction", "data pipeline",
    # автоматизация
    "automate", "automation script", "workflow automation", "spreadsheet",
    "google sheets", "zapier", "make.com", "airtable",
    # интеграции
    "rest api", "api integration", "public api", "webhook", "crm integration",
    # ИИ
    "llm", "gpt-", "openai", "anthropic", "claude", "embedding",
    "prompt engineer", "machine learning", "ai agent", "ai engineer",
    "retrieval augmented",
    # веб
    "landing page", "wordpress", "webflow", "shopify", "web app",
    # инфраструктура
    "docker", "kubernetes", "devops", "terraform",
]

# Источники, где всё по-английски: и фильтр, и черновик отклика.
ENGLISH_SOURCES = ("Hacker News", "WeWorkRemotely", "RemoteOK", "Maps")


def is_english_source(source):
    return any(s.lower() in (source or "").lower() for s in ENGLISH_SOURCES)


def matches_keywords(text, keywords=None):
    words = KEYWORDS if keywords is None else keywords
    if not words:
        return True
    low = text.lower()
    return any(kw.lower() in low for kw in words)


def send_telegram(text, reply_markup=None):
    """Отправка с повторами.

    При очереди сообщений подряд Telegram рвёт соединение ("Connection reset
    by peer") или отвечает 429. Без повтора лид просто терялся: в seen он уже
    записан, значит второго шанса не будет. Поэтому три попытки с паузой, и
    небольшая пауза между обычными отправками, чтобы не упираться в лимит.
    """
    # У Telegram жёсткий предел 4096 символов на сообщение: длинный пост из
    # канала вместе с черновиком отклика легко его перебивает, и тогда
    # приходит не усечённый текст, а ошибка 400 - то есть лид теряется.
    if len(text) > 4000:
        text = text[:4000] + "\n[...обрезано]"

    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code == 429:
                wait = int((r.json().get("parameters") or {}).get("retry_after", 3))
                print(f"[telegram] лимит, жду {wait}с")
                time.sleep(min(wait, 30))
                continue
            r.raise_for_status()
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"[telegram] попытка {attempt + 1}/3 не удалась: {e}")
            time.sleep(2 * (attempt + 1))
    print("[telegram] сообщение отправить не удалось")
    return False


_DRAFT_SYSTEM = """Ты пишешь отклики на заказы фриланс-бирж от лица исполнителя.

Кто исполнитель:
{about}

Правила отклика:
- По-русски, на "вы", 3-5 предложений, без воды и канцелярита.
- Первая фраза - конкретно про ЭТУ задачу, а не "здравствуйте, готов выполнить".
  Покажи, что прочитал условие: назови, что именно предстоит сделать.
- Если из условия понятен подход - скажи в одну фразу, как будешь делать.
- Ровно один уточняющий вопрос по сути задачи, в конце. Вопрос должен быть
  такой, на который без ответа заказчика работу не начать.
- Никаких выдуманных фактов: не приписывай себе проектов, кейсов, отзывов,
  лет опыта и названий компаний, которых нет в описании исполнителя выше.
- Не называй цену и не обещай срок в часах, если в заказе нет ни бюджета,
  ни объёма. Если бюджет указан - можешь сказать, что он подходит.
- Без эмодзи, без markdown, без подписи и без темы письма. Только текст,
  который можно вставить в поле отклика как есть."""


_draft_state = {"made": 0, "warned": False}


def wants_draft(source):
    if not USE_AI_DRAFT:
        return False
    return any(s.lower() in (source or "").lower() for s in DRAFT_SOURCES)


def pick_template(text, templates=None, fallback=None):
    """Определяет тип задачи по тексту. Возвращает подходящий шаблон или
    fallback, если тип не опознан."""
    low = (text or "").lower()
    for template in (templates if templates is not None else DRAFT_TEMPLATES):
        if any(key in low for key in template["keys"]):
            return template
    return fallback if fallback is not None else DRAFT_FALLBACK


def template_draft(source, title, details):
    """Черновик отклика без единого внешнего запроса - и, значит, бесплатно.

    Это не попытка изобразить живой текст: заготовка честно берёт на себя
    структуру отклика (что сделаю - как проверите - вопрос), а конкретику по
    задаче ты дописываешь одной строкой, когда открываешь заказ. Именно эта
    строка и отличает отклик от рассылки, а всё остальное вокруг неё писать
    каждый раз заново незачем.
    """
    if is_direct_source(source):
        return DRAFT_DIRECT_REPLY

    if is_maps_source(source):
        return DRAFT_OUTREACH_EN if is_english_source(source) else DRAFT_OUTREACH_RU

    text = "%s %s" % (title or "", details or "")

    if is_english_source(source):
        template = pick_template(text, DRAFT_TEMPLATES_EN, DRAFT_FALLBACK_EN)
        return " ".join(["Hi!", template["what"], DRAFT_PROOF_EN, template["ask"]])

    template = pick_template(text)
    return " ".join([
        "Здравствуйте!",
        template["what"],
        "Покажу рабочий результат до оплаты, чтобы вы посмотрели сами.",
        template["ask"],
    ])


def is_direct_source(source):
    """Личное сообщение от покупателя, а не заказ на бирже."""
    return "личное" in (source or "").lower()


def build_draft(source, title, details, link):
    """Выбирает движок и отдаёт черновик. None - если не получилось."""
    engine = DRAFT_ENGINE
    if engine == "auto":
        engine = "ai" if ANTHROPIC_API_KEY else "template"

    if engine == "ai":
        draft = generate_draft(source, title, details, link)
        if draft:
            return draft
        # Ключа нет, кончились кредиты, API прилёг - неважно: лид уйдёт с
        # бесплатной заготовкой, а не без ничего.
        return template_draft(source, title, details)

    return template_draft(source, title, details)


def generate_draft(source, title, details, link):
    """Готовый текст отклика на конкретный заказ. None, если не получилось."""
    if not ANTHROPIC_API_KEY:
        if not _draft_state["warned"]:
            _draft_state["warned"] = True
            print("[ai-draft] ANTHROPIC_API_KEY не задан - работаю на "
                  "бесплатных шаблонах (DRAFT_ENGINE=\"template\"). Если "
                  "захочешь тексты живее: ключ берётся на "
                  "console.anthropic.com, это платные API-кредиты, подписка "
                  "Claude для API не подходит.")
        return None

    if _draft_state["made"] >= DRAFT_MAX_PER_RUN:
        return None

    task = "Источник: %s\nЗаказ: %s" % (source, title)
    if details:
        task += "\nПодробности: %s" % details
    if link:
        task += "\nСсылка: %s" % link

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=DRAFT_MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            system=_DRAFT_SYSTEM.format(about=ABOUT_ME.strip()),
            messages=[{"role": "user", "content": task}],
        )
    except Exception as e:
        print("[ai-draft] ошибка генерации (%s): %s" % (type(e).__name__, e))
        return None

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        return None
    _draft_state["made"] += 1
    return text


# Предохранитель от лавины. Если источник вдруг отдаст сразу сотню новых
# лидов (так бывает при расширении категории или при первом запуске нового
# источника), без потолка бот высыпет сотню сообщений подряд и Telegram его
# просто затротлит. Всё, что сверх потолка, всё равно попадает в seen - то
# есть повторно не придёт, а в логе будет видно, сколько было отброшено.
MAX_NOTIFICATIONS_PER_RUN = 15

# И потолок на ОДИН источник. Без него источник с самой длинной лентой
# (RemoteOK отдаёт сотни вакансий за раз) выбирает общий лимит целиком, и
# лиды с Kwork и Hacker News, которые идут следом, до тебя не доходят.
MAX_NOTIFICATIONS_PER_SOURCE = 5

_notify_state = {"sent": 0, "skipped": 0, "by_source": {}}


# Заказчики на Hacker News и в Telegram-каналах почти всегда оставляют способ
# связи прямо в тексте: почту или @ник. Вытаскиваем его наверх сообщения -
# иначе связь приходится выковыривать из простыни текста руками, а на
# американском рынке именно почта и есть канал: там пишешь напрямую, а не
# жмёшь "откликнуться" на бирже.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TG_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_@/])@([A-Za-z][A-Za-z0-9_]{4,31})")

# Почты самих площадок - не контакты заказчика.
_CONTACT_IGNORE = ("kwork.ru", "fl.ru", "free-lance.ru", "noreply", "no-reply",
                   "example.com", "sentry.io", "github.com")


def extract_contacts(text):
    """Ищет в тексте лида почту и телеграм-ник заказчика."""
    if not text:
        return ""
    found = []
    for email_match in _EMAIL_RE.findall(text):
        low = email_match.lower()
        if any(bad in low for bad in _CONTACT_IGNORE):
            continue
        if email_match not in found:
            found.append(email_match)
    for handle in _TG_HANDLE_RE.findall(text):
        tag = "@" + handle
        if tag not in found:
            found.append(tag)
    return ", ".join(found[:3])


# Очередь писем под кнопкой. main() кладёт сюда состояние, чтобы notify мог
# зарегистрировать кнопку, не таская состояние через все источники.
_pending_state = None


def notify(source, title, description, link, details=None,
           reply_to=None, reply_subject=None, in_reply_to=None):
    """details - уже собранный текст уведомления. Если он задан, description
    не используется: источник сам решил, что и как показывать (у Kwork,
    например, это цена + рубрика + покупатель отдельными строками).

    reply_to - почта, на которую можно ответить письмом. Если она задана и
    черновик получился, к сообщению прицепится кнопка "Отправить письмо".
    """
    body = details if details is not None else (description or "")[:300]
    msg = f"🆕 {source}\n\n{title}"

    contacts = extract_contacts("%s %s" % (title or "", body or ""))
    if contacts:
        msg += f"\n\n📬 Контакт: {contacts}"

    if body:
        msg += f"\n\n{body}"
    if link:
        msg += f"\n\n🔗 {link}"

    draft = None
    if wants_draft(source) and (title or body):
        draft = build_draft(source, title, body, link)
        if draft:
            # Для карт это не отклик на заказ, а первое обращение к человеку,
            # который ничего не заказывал - и идти оно должно звонком или в
            # мессенджер, поштучно. Подпись должна об этом напоминать.
            label = ("✍️ ЧЕРНОВИК ЗВОНКА / СООБЩЕНИЯ (по одному, не рассылкой):"
                     if is_maps_source(source)
                     else "✍️ ЧЕРНОВИК ОТКЛИКА (скопируй, проверь, отправь):")
            msg += "\n\n" + "-" * 20 + "\n" + label + "\n\n" + draft

    # Кнопка появляется, только если есть КУДА писать и ЧТО писать. У карт её
    # не бывает намеренно: там телефон, а не почта, и обращаться туда надо
    # голосом, а не письмом.
    markup = None
    if (SEND_BUTTON_ENABLED and _pending_state is not None
            and reply_to and draft and not is_maps_source(source)):
        markup = register_send_button(
            _pending_state, reply_to,
            reply_subject or ("Re: " + (title or "")[:120]),
            draft, in_reply_to,
        )
        msg += "\n\n👉 Кнопка ниже отправит этот текст на %s" % reply_to

    if _notify_state["sent"] >= MAX_NOTIFICATIONS_PER_RUN:
        _notify_state["skipped"] += 1
        return False

    from_source = _notify_state["by_source"].get(source, 0)
    if from_source >= MAX_NOTIFICATIONS_PER_SOURCE:
        _notify_state["skipped"] += 1
        return False

    send_telegram(msg, reply_markup=markup)
    _notify_state["sent"] += 1
    _notify_state["by_source"][source] = _notify_state["by_source"].get(source, 0) + 1
    print(f"[+] {source}: {title}")
    return True


USER_AGENT = "lead-monitor-personal-script/1.0"


def fetch_url(url, timeout=15):
    """Обычный GET с таймаутом и User-Agent. Возвращает текст страницы."""
    r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.text


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


# Письма от Kwork бывают двух разных сортов, и путать их нельзя:
#
#  1. Дайджест "Новые проекты на бирже Kwork" - список чужих заказов, которые
#     ещё нужно отфильтровать по ключевым словам (интересно далеко не всё).
#
#  2. Личные письма: покупатель написал тебе в личку, прислал предложение,
#     оформил заказ. Это не "лид", это уже КЛИЕНТ, который ждёт ответа.
#     Такие письма отправляются в Telegram ВСЕГДА, мимо фильтра по ключевым
#     словам - раньше они молча выбрасывались, потому что в тексте письма
#     ("Получены новые сообщения от Vkira7") нет ни одного слова из KEYWORDS.
KWORK_DIRECT_SUBJECT_MARKERS = (
    "новые сообщения",
    "новое сообщение",
    "заказ",
    "предложени",
    "отклик",
    "оплат",
    "сделка",
    "арбитраж",
)

# Куски вёрстки письма, которые не несут смысла и раньше уезжали в Telegram
# вперемешку с названием заказа ("Название Покупатель Цена Заполнить...").
_KWORK_BOILERPLATE = (
    "перейти на kwork.ru",
    "Название Покупатель Цена",
    "Ваши настройки на бирже",
    "Доступно коннектов",
    "Дата пополнения",
    "Любимых рубрик",
    "Уведомления",
    "Что это?",
    "Изменить",
    "Настроить",
    "Отписаться",
    "Ответить на сайте",
    "Если вы не хотите получать письма от нас, вы можете отписаться от рассылки",
    "Будьте всегда на связи и не пропускайте важные события.",
    "Скачайте приложение Kwork.",
)


def clean_kwork_text(text):
    """Убирает из текста письма шапку/подвал/подписи Kwork."""
    for junk in _KWORK_BOILERPLATE:
        text = text.replace(junk, " ")
    # "+3 новых подходящих проекта За последний час на бирже Kwork размещено
    # 18 новых проектов. В ваших любимых рубриках ... доступно 3 новых проекта."
    text = re.sub(r"\+?\d+\s+нов\w+\s+подходящ\w+\s+проект\w*", " ", text)
    text = re.sub(r"За последни\w+[^.]*\.", " ", text)
    text = re.sub(r"В ваших любимых рубриках[^.]*\.", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_kwork_direct_mail(subject):
    low = (subject or "").lower()
    if "новые проекты на бирже" in low:
        return False
    return any(marker in low for marker in KWORK_DIRECT_SUBJECT_MARKERS)


# Ссылка на конкретный заказ внутри дайджеста. Kwork отдаёт её как
# https://kwork.ru/new_offer?project=3216597 - обрати внимание на "=",
# из-за которого старое выражение (ожидавшее "project3216597") не
# срабатывало НИ РАЗУ, и каждое письмо уходило в аварийную ветку одним
# слипшимся куском текста. "=?" оставлен на случай, если Kwork вернёт
# старый формат без знака равенства.
_KWORK_PROJECT_HREF = re.compile(
    r'href="(https?://(?:www\.)?kwork\.ru/new_offer\?project=?(\d+)[^"]*)"',
    re.IGNORECASE,
)


def parse_kwork_digest(body):
    """Разбирает HTML дайджеста Kwork на отдельные проекты.

    Каждый заказ в письме - это строка таблицы <tr> из трёх ячеек:
    название+рубрика, покупатель со статистикой, цена. Возвращает список
    словарей; пустой список означает "вёрстку письма разобрать не удалось".
    """
    projects = []
    for row in re.findall(r"<tr\b.*?</tr>", body, flags=re.DOTALL | re.IGNORECASE):
        href = _KWORK_PROJECT_HREF.search(row)
        if not href:
            continue
        link, project_id = href.group(1), href.group(2)

        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.DOTALL | re.IGNORECASE)
        first_cell = cells[0] if cells else row

        title_match = re.search(
            r'<a[^>]+href="[^"]*new_offer\?project=?\d+[^"]*"[^>]*>(.*?)</a>',
            row, flags=re.DOTALL | re.IGNORECASE,
        )
        title = strip_html(title_match.group(1)) if title_match else ""
        if not title:
            continue

        # Рубрика лежит в отдельном <div> сразу после ссылки на заказ.
        category = ""
        cat_match = re.search(
            r"</a>\s*</div>\s*<div[^>]*>(.*?)</div>", first_cell,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if cat_match:
            category = strip_html(cat_match.group(1))
        else:
            rest = strip_html(first_cell).replace(title, " ").strip()
            category = rest
        # Kwork иногда ставит двойную стрелку: "Скрипты, боты > > ИИ-боты"
        category = re.sub(r"(?:\s*>\s*)+", " > ", category).strip(" >")

        buyer, buyer_stats = "", ""
        if len(cells) > 1:
            user_match = re.search(r"kwork\.ru/user/([^\"?/]+)", cells[1], re.IGNORECASE)
            if user_match:
                buyer = user_match.group(1)
            stats_text = strip_html(cells[1])
            bits = []
            projects_match = re.search(r"(\d+)\s+проект\w*\s+на\s+бирже", stats_text)
            if projects_match:
                bits.append(projects_match.group(0))
            hired_match = re.search(r"(\d+)%\s+нанято", stats_text)
            if hired_match:
                bits.append(hired_match.group(0))
            buyer_stats = ", ".join(bits)

        price = strip_html(cells[2]) if len(cells) > 2 else ""

        projects.append({
            "id": project_id,
            "title": title,
            "category": category,
            "buyer": buyer,
            "buyer_stats": buyer_stats,
            "price": price,
            "link": link,
        })
    return projects


def format_kwork_details(project):
    lines = []
    if project["price"]:
        lines.append("💰 " + project["price"])
    if project["category"]:
        lines.append("📂 " + project["category"])
    if project["buyer"]:
        who = "👤 " + project["buyer"]
        if project["buyer_stats"]:
            who += " (" + project["buyer_stats"] + ")"
        lines.append(who)
    return "\n".join(lines)


def handle_kwork_digest(seen, subject, body):
    projects = parse_kwork_digest(body)

    if not projects:
        # Аварийная ветка: Kwork поменял вёрстку. Лучше грубое уведомление,
        # чем тишина - но хотя бы без шапки, подвала и слова "Цена".
        print("[kwork-mail] ВНИМАНИЕ: не удалось разобрать письмо на отдельные "
              "заказы - шлю письмо целиком. Похоже, Kwork поменял вёрстку.")
        text = clean_kwork_text(strip_html(body))
        if matches_keywords(subject + " " + text[:5000]):
            notify("Kwork (письмо не разобрано)", subject, "", "https://kwork.ru/projects",
                   details=text[:400])
        return

    for project in projects:
        # Дедупликация по номеру заказа, а не по письму: один и тот же заказ
        # приходит в нескольких дайджестах подряд, а уведомить надо один раз.
        uid = "kwork-project:" + project["id"]
        if uid in seen:
            continue
        seen.add(uid)
        if matches_keywords(project["title"] + " " + project["category"]):
            notify("Kwork", project["title"], "", project["link"],
                   details=format_kwork_details(project))


def handle_kwork_direct(seen, subject, body):
    """Личное письмо: покупатель написал/заказал. Шлём всегда, без фильтра."""
    text = clean_kwork_text(strip_html(body))

    author = ""
    author_match = re.search(r"kwork\.ru/inbox/([^\"?/]+)", body, re.IGNORECASE)
    if author_match:
        author = author_match.group(1)
    else:
        author_match = re.search(r"\bот\s+([A-Za-z0-9_.\-]{2,})", text)
        if author_match:
            author = author_match.group(1)

    link_match = re.search(
        r'href="(https?://(?:www\.)?kwork\.ru/(?:inbox|track|new_offer|payer_orders)[^"]*)"',
        body, re.IGNORECASE,
    )
    link = link_match.group(1) if link_match else "https://kwork.ru/inbox"

    title = subject
    if author:
        title = "Покупатель " + author + " ждёт ответа"

    notify("Kwork — ЛИЧНОЕ ⚡", title, "", link, details=text[:500])


def kwork_search(conn, since):
    """Ищем письма от Kwork за нужный период.

    Фильтр по отправителю отдаём самому серверу (FROM) - иначе скрипт качает
    подряд ВСЮ почту за сутки и отбрасывает лишнее уже у себя. Если сервер
    такой поиск не поддерживает, откатываемся на поиск только по дате.
    """
    for criteria in ('(SINCE "%s" FROM "kwork")' % since, '(SINCE "%s")' % since):
        try:
            status, data = conn.search(None, criteria)
        except Exception as e:
            print("[kwork-mail] поиск %s не сработал (%s): %s" % (criteria, type(e).__name__, e))
            continue
        if status == "OK" and data and data[0] is not None:
            return data[0].split()
    return []


def check_kwork_mail(seen):
    if not KWORK_ENABLED:
        return
    if not IMAP_USER or not IMAP_PASSWORD:
        print("[kwork-mail] IMAP_USER/IMAP_PASSWORD не заданы - пропускаю")
        return

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=20)
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)

        # Сутки, а не 3 часа: SINCE в IMAP работает с точностью до ДАТЫ, и при
        # запуске сразу после полуночи трёхчасовое окно теряло вечернюю почту.
        since = imap_date(datetime.now() - timedelta(days=1))

        for num in kwork_search(conn, since):
            # BODY.PEEK - читаем письмо, не помечая его прочитанным у тебя в почте
            status, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            sender = decode_mime(msg.get("From", ""))
            if KWORK_SENDER_MATCH.lower() not in sender.lower():
                continue

            # Дедупликация ТОЛЬКО по Message-ID письма. Раньше здесь была ещё
            # проверка "тема + дата", но дата бралась как Date[:16] - а это
            # "Wed, 27 Aug 2026", то есть точность до СУТОК, а не до часа.
            # Тема у всех дайджестов одна и та же, поэтому из десятка писем
            # за день обрабатывалось ровно одно, а остальные молча выкидывались.
            # Именно поэтому бот "перестал присылать сообщения".
            message_id = msg.get("Message-ID") or ("num:" + num.decode())
            uid = "kwork:" + message_id
            if uid in seen:
                continue
            seen.add(uid)

            subject = decode_mime(msg.get("Subject", ""))
            body = get_email_body(msg)

            if is_kwork_direct_mail(subject):
                handle_kwork_direct(seen, subject, body)
            else:
                handle_kwork_digest(seen, subject, body)

    except Exception as e:
        print("[kwork-mail] ошибка (%s): %s" % (type(e).__name__, e))
    finally:
        # Раньше соединение закрывалось только при удачном проходе: любая
        # ошибка в середине оставляла висеть открытый IMAP-сокет.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass


# ==================== КНОПКА "ОТПРАВИТЬ" В TELEGRAM ====================
#
# Смысл: убрать из цепочки копипаст, но оставить решение человеку. Лид, до
# которого можно дописаться письмом, приходит с кнопкой. Нажал - на следующем
# прогоне бот сам отправит письмо с твоего ящика.
#
# Почему не отправлять сразу, без кнопки: за один день фильтры дважды
# пропустили мусор - Hacker News подцепил тред 2020 года, RemoteOK прислал
# "Kitchen Porter". Обе ошибки поймали и починили, но при автоотправке это
# ушло бы живым людям с твоего адреса. Кнопка - ровно одна точка, где человек
# смотрит глазами; всё до и после неё делает бот.
#
# Своего сервера не нужно: на каждом прогоне бот спрашивает у Telegram
# getUpdates и разбирает нажатия, случившиеся с прошлого раза. Задержка до
# 15 минут - цена того, что всё бесплатно и без хостинга.

SEND_BUTTON_ENABLED = True
PENDING_FILE = "pending_sends.json"
PENDING_TTL_DAYS = 7          # ненажатые кнопки протухают
MAX_SENDS_PER_RUN = 5         # предохранитель: больше пяти писем за раз не уйдёт

SMTP_HOST = os.environ.get("SMTP_HOST", "")   # пусто -> выводим из IMAP_HOST
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "")


def load_pending():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("offset", 0)
                data.setdefault("items", {})
                return data
        except Exception as e:
            print("[send] не смог прочитать %s (%s) - начинаю с чистого листа"
                  % (PENDING_FILE, e))
    return {"offset": 0, "items": {}}


def save_pending(state):
    # Выкидываем протухшее, чтобы файл не рос вечно.
    deadline = time.time() - PENDING_TTL_DAYS * 24 * 3600
    state["items"] = {k: v for k, v in state.get("items", {}).items()
                      if v.get("created", 0) >= deadline}
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def smtp_host():
    if SMTP_HOST:
        return SMTP_HOST
    # imap.gmail.com -> smtp.gmail.com, imap.yandex.ru -> smtp.yandex.ru
    return IMAP_HOST.replace("imap.", "smtp.", 1)


def send_email(to_addr, subject, body, in_reply_to=None):
    """Письмо с твоего ящика тем же паролем приложения, которым бот уже
    читает почту по IMAP. Возвращает True/False."""
    if not IMAP_USER or not IMAP_PASSWORD:
        print("[send] нет доступа к почте - отправить не могу")
        return False

    message = MIMEText(body, "plain", "utf-8")
    message["From"] = (formataddr((MAIL_FROM_NAME, IMAP_USER))
                       if MAIL_FROM_NAME else IMAP_USER)
    message["To"] = to_addr
    message["Subject"] = subject
    if in_reply_to:
        # Чтобы ответ подклеился к переписке, а не пришёл отдельным письмом.
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to

    try:
        with smtplib.SMTP_SSL(smtp_host(), SMTP_PORT, timeout=30) as smtp:
            smtp.login(IMAP_USER, IMAP_PASSWORD)
            smtp.sendmail(IMAP_USER, [to_addr], message.as_string())
        print("[send] письмо отправлено: %s" % to_addr)
        return True
    except Exception as e:
        print("[send] не отправилось на %s (%s): %s"
              % (to_addr, type(e).__name__, e))
        return False


def telegram_api(method, payload):
    url = "https://api.telegram.org/bot%s/%s" % (TELEGRAM_BOT_TOKEN, method)
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code >= 400:
            print("[telegram] %s -> HTTP %s: %s"
                  % (method, r.status_code, r.text[:200]))
            return None
        return r.json()
    except Exception as e:
        print("[telegram] %s -> ошибка (%s): %s" % (method, type(e).__name__, e))
        return None


def register_send_button(state, to_addr, subject, body, in_reply_to=None):
    """Кладёт письмо в очередь и отдаёт разметку кнопки для сообщения."""
    key = uuid.uuid4().hex[:12]
    state.setdefault("items", {})[key] = {
        "to": to_addr, "subject": subject, "body": body,
        "in_reply_to": in_reply_to, "created": time.time(),
    }
    return {"inline_keyboard": [[
        {"text": "📤 Отправить письмо", "callback_data": "send:" + key},
    ]]}


def process_send_buttons(state):
    """Разбирает нажатия кнопок, случившиеся с прошлого прогона."""
    if not SEND_BUTTON_ENABLED:
        return

    data = telegram_api("getUpdates", {
        "offset": state.get("offset", 0),
        "timeout": 0,
        "allowed_updates": ["callback_query"],
    })
    if not data or not data.get("ok"):
        return

    sent = 0
    for update in data.get("result", []):
        state["offset"] = max(state.get("offset", 0),
                              update.get("update_id", 0) + 1)

        callback = update.get("callback_query")
        if not callback:
            continue
        payload = callback.get("data") or ""
        if not payload.startswith("send:"):
            continue

        key = payload[len("send:"):]
        item = state.get("items", {}).pop(key, None)
        if item is None:
            telegram_api("answerCallbackQuery", {
                "callback_query_id": callback.get("id"),
                "text": "Это письмо уже отправлено или устарело",
            })
            continue

        if sent >= MAX_SENDS_PER_RUN:
            # Возвращаем в очередь: отправим на следующем прогоне.
            state["items"][key] = item
            telegram_api("answerCallbackQuery", {
                "callback_query_id": callback.get("id"),
                "text": "Отправлю на следующем прогоне",
            })
            continue

        ok = send_email(item["to"], item["subject"], item["body"],
                        item.get("in_reply_to"))
        if ok:
            sent += 1

        telegram_api("answerCallbackQuery", {
            "callback_query_id": callback.get("id"),
            "text": "Отправлено" if ok else "Не отправилось, смотри лог",
        })
        head = "✅ Письмо отправлено" if ok else "❌ Не удалось отправить"
        send_telegram("%s\n\nКому: %s\nТема: %s\n\n%s"
                      % (head, item["to"], item["subject"], item["body"]))

    if sent:
        print("[send] отправлено писем за прогон: %d" % sent)


# ---------------------- ЛИЧНЫЕ ПИСЬМА: ТЕБЕ ОТВЕТИЛИ ----------------------
#
# Дыра, которая ломала всю автоматизацию. Бот читал в ящике ТОЛЬКО письма от
# Kwork, а всё остальное игнорировал. То есть если заказчик с Hacker News
# отвечал на твоё письмо - бот молчал, и ответ лежал в почте, пока ты сам
# туда не заглянешь. Искать лиды круглосуточно и при этом проспать ответ
# живого человека - худшее, что может делать такой бот.
#
# Теперь любое ПИСЬМО ОТ ЖИВОГО ЧЕЛОВЕКА приходит в Telegram отдельным
# громким уведомлением, мимо всех фильтров по ключевым словам.

PERSONAL_MAIL_ENABLED = True

# Рассылки и роботы. Живой человек так себя не ведёт.
_ROBOT_SENDER_MARKERS = ("noreply", "no-reply", "no_reply", "donotreply",
                         "do-not-reply", "notifications", "notification@",
                         "mailer-daemon", "postmaster", "bounce", "newsletter",
                         "mailings", "info@kwork.ru", "news@kwork.ru")

# Сервисы, чьи письма никогда не являются заказчиком.
_SERVICE_DOMAINS = ("kwork.ru", "avito.ru", "free-lance.ru", "fl.ru",
                    "github.com", "ggsel.com", "ggsel.net", "claude.com",
                    "anthropic.com", "plus.yandex.ru", "google.com",
                    "youtube.com", "apple.com", "telegram.org")


def looks_like_robot_mail(sender, headers_text):
    """Отличает рассылку/робота от письма живого человека.

    Главный признак - заголовок List-Unsubscribe: он есть практически у любой
    легальной рассылки и почти никогда у обычного письма. Плюс Auto-Submitted
    и Precedence, которыми помечают автоответы и списки рассылки.
    """
    low_headers = (headers_text or "").lower()
    for marker in ("list-unsubscribe:", "auto-submitted: auto",
                   "precedence: bulk", "precedence: list",
                   "precedence: junk", "x-auto-response-suppress:"):
        if marker in low_headers:
            return True

    low_sender = (sender or "").lower()
    if any(marker in low_sender for marker in _ROBOT_SENDER_MARKERS):
        return True
    if any(domain in low_sender for domain in _SERVICE_DOMAINS):
        return True

    # Рассылки почти всегда уходят с отдельного поддомена: info.sportmaster.ru,
    # emails.tinkoff.ru, email.claude.com - ровно эти три и лежали в ящике за
    # сутки. Живой человек пишет с обычного домена (gmail.com, mail.ru, свой
    # рабочий), а не с "email.чего-то".
    match = re.search(r"@([a-z0-9.\-]+)", low_sender)
    if match:
        domain = match.group(1)
        if domain.count(".") >= 2:
            prefix = domain.split(".")[0]
            if prefix in ("email", "emails", "mail", "mailing", "mailings",
                          "info", "news", "newsletter", "notify",
                          "notifications", "send", "sender", "smtp",
                          "marketing", "mktg"):
                return True
    return False


def check_personal_mail(seen):
    """Письмо от живого человека -> громкое уведомление в Telegram."""
    if not PERSONAL_MAIL_ENABLED:
        return
    if not IMAP_USER or not IMAP_PASSWORD:
        return

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=20)
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)

        since = imap_date(datetime.now() - timedelta(days=1))
        status, data = conn.search(None, '(SINCE "%s")' % since)
        if status != "OK" or not data or data[0] is None:
            return

        for num in data[0].split():
            # Сначала только заголовки - они дешёвые. Тело письма качаем
            # лишь у тех, кто прошёл отсев, иначе за сутки скачивали бы
            # десятки рассылок целиком ради пары живых писем.
            status, head_data = conn.fetch(
                num,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID "
                "LIST-UNSUBSCRIBE AUTO-SUBMITTED PRECEDENCE "
                "X-AUTO-RESPONSE-SUPPRESS)])",
            )
            if status != "OK" or not head_data or not head_data[0]:
                continue

            raw_headers = head_data[0][1].decode("utf-8", errors="ignore")
            head_msg = email.message_from_string(raw_headers)
            sender = decode_mime(head_msg.get("From", ""))
            subject = decode_mime(head_msg.get("Subject", ""))
            message_id = head_msg.get("Message-ID") or ("num:" + num.decode())

            uid = "mail:" + message_id
            if uid in seen:
                continue

            if looks_like_robot_mail(sender, raw_headers):
                seen.add(uid)      # рассылку второй раз не разбираем
                continue

            seen.add(uid)

            status, body_data = conn.fetch(num, "(BODY.PEEK[])")
            body = ""
            if status == "OK" and body_data and body_data[0]:
                body = get_email_body(email.message_from_bytes(body_data[0][1]))
            text = strip_html(body, keep_newlines=True)[:600]

            notify("Личное письмо ⚡ ТЕБЕ ОТВЕТИЛИ", subject or "(без темы)",
                   "", "", details="От: %s\n\n%s" % (sender, text))

    except Exception as e:
        print("[mail] ошибка (%s): %s" % (type(e).__name__, e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass


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
        if r.status_code == 403:
            print("[hh.ru] HTTP 403 forbidden. Чаще всего это значит, что hh.ru "
                  "блокирует IP-адреса дата-центров, а GitHub Actions работает "
                  "именно с таких. Помогает либо секрет HH_CONTACT в User-Agent, "
                  "либо запуск монитора не из GitHub Actions. Если 403 держится "
                  "постоянно - просто выключи HH_ENABLED/HH_BROAD_ENABLED/"
                  "HH_PROJECT_ENABLED, чтобы не засорять лог.")
            return None
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


# ---------------------- ИСТОЧНИК 6: TELEGRAM-КАНАЛЫ ----------------------
#
# У Telegram нет своего RSS для каналов. Раньше тут стоял сторонний мост
# RSSHub (rsshub.app) - публичный бесплатный инстанс, который почти всегда
# отдаёт 429/403 из чужого CI. То есть источник формально был, а работал
# примерно никогда.
#
# Теперь читаем канал напрямую, без посредников: у КАЖДОГО публичного канала
# есть веб-версия https://t.me/s/ИМЯ_КАНАЛА - обычная HTML-страница с
# последними постами. Ни логина, ни токена, ни бота, ни стороннего сервиса.
# Это та же страница, которую видит любой человек без Telegram.
#
# Каналы ниже - просто пример; смело меняй список на свои, формат тот же
# (только имя канала, без "@" и без "t.me/"). Общий фильтр KEYWORDS
# применяется и здесь, так что канал со смешанной тематикой не завалит
# уведомлениями подряд.

TELEGRAM_CHANNELS_ENABLED = True
TME_BASE = "https://t.me/s"
TELEGRAM_CHANNELS = [
    "frilanser_vacansii",
    # "distantsiya" убран: по логам страница отдаётся, но постов в ней нет -
    # канал закрыт, переименован или стал приватным. Добавляй сюда свои,
    # формат тот же: только имя канала, без "@" и без "t.me/".
]

# Максимум постов из одного канала за прогон - страница t.me/s отдаёт около
# 20 последних, и если канал внезапно окажется новым (ничего не в seen),
# без этого потолка можно получить сразу двадцать уведомлений подряд.
TELEGRAM_MAX_POSTS_PER_RUN = 8


def parse_tme_page(page, channel):
    """Достаёт посты из HTML-страницы https://t.me/s/КАНАЛ.

    Каждый пост обёрнут в <div class="tgme_widget_message_wrap ...">, внутри
    есть data-post="канал/НОМЕР" и текст в div.tgme_widget_message_text.
    """
    posts = []
    blocks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for block in blocks:
        post_match = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not post_match:
            continue
        post_id = post_match.group(2)

        text_match = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)'
            r'(?:<div class="tgme_widget_message_footer'
            r'|<span class="tgme_widget_message_meta'
            r'|<div class="tgme_widget_message_bubble_end)',
            block, flags=re.DOTALL,
        )
        if not text_match:
            continue
        text = strip_html(text_match.group(1), keep_newlines=True)
        if not text:
            continue

        posts.append({
            "id": post_id,
            "text": text,
            "link": "https://t.me/%s/%s" % (channel, post_id),
        })
    return posts


def check_telegram_channels(seen):
    if not TELEGRAM_CHANNELS_ENABLED:
        return
    for channel in TELEGRAM_CHANNELS:
        try:
            page = fetch_url("%s/%s" % (TME_BASE, channel))
        except Exception as e:
            print("[tg:%s] ошибка загрузки страницы (%s): %s" % (channel, type(e).__name__, e))
            continue

        posts = parse_tme_page(page, channel)
        if not posts:
            print("[tg:%s] постов не найдено - канал закрыт, переименован "
                  "или Telegram поменял вёрстку" % channel)
            continue

        sent = 0
        for post in posts:
            uid = "tg:%s:%s" % (channel, post["id"])
            if uid in seen:
                continue
            seen.add(uid)
            if sent >= TELEGRAM_MAX_POSTS_PER_RUN:
                continue
            # Матчим только начало поста (заголовок вакансии/заказа), а не
            # весь текст: в подвале поста почти всегда есть слова вроде
            # "сайт" и "разработка" из описания компании, из-за чего мимо
            # фильтра проезжали "Мерч-дизайнер" и "Ассистент SMM-менеджера".
            if matches_keywords(post["text"][:200]):
                first_line = post["text"].split("\n", 1)[0][:100]
                notify("Telegram: " + channel, first_line, "", post["link"],
                       details=post["text"][:400])
                sent += 1


# ---------------------- ИСТОЧНИК 7: HH.RU - ПРОЕКТНАЯ РАБОТА ----------------------
#
# НОВЫЙ источник. Отличается от ИСТОЧНИКА 4 не ключевыми словами, а типом
# занятости: там ищутся обычные удалённые вакансии (наём в штат), а тут -
# employment=project ("проектная работа") и employment=part ("частичная
# занятость"). Это ровно та прослойка, где сидит не работодатель, а ЗАКАЗЧИК
# с разовой задачей: сделать бота, скрипт, интеграцию, лендинг - то есть
# работа, которую реально закрыть за вечер с Claude Code, а не выйти в офис.
#
# Тот же публичный API hh.ru, что уже используется выше: без токена, без
# логина, без ключей - нужен только вежливый User-Agent.

HH_PROJECT_ENABLED = False   # выключено вместе с остальным hh.ru
HH_PROJECT_PARAMS = {
    "text": HH_SEARCH_TEXT,
    "employment": ["project", "part"],
    "per_page": 100,
    "order_by": "publication_time",
}


def check_hh_project(seen):
    if not HH_PROJECT_ENABLED:
        return
    data = hh_get(HH_PROJECT_PARAMS)
    if data is None:
        return

    for item in data.get("items", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "hhproject:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("name", "Без названия")
        employer = (item.get("employer") or {}).get("name", "")
        snippet = item.get("snippet") or {}
        description = strip_html(" ".join(
            filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
        ))
        link = item.get("alternate_url", "")

        salary = item.get("salary") or {}
        details = []
        if salary.get("from") or salary.get("to"):
            money = "💰 "
            if salary.get("from"):
                money += "от %s " % salary["from"]
            if salary.get("to"):
                money += "до %s " % salary["to"]
            money += salary.get("currency") or ""
            details.append(money.strip())
        if employer:
            details.append("👤 " + employer)
        if description:
            details.append(description[:300])

        if matches_keywords(title + " " + description):
            notify("hh.ru — проектная работа", title, "", link,
                   details="\n".join(details))


# ---------------------- ИСТОЧНИК 8: HACKER NEWS (США) ----------------------
#
# Раз в месяц на Hacker News выходит тред "Ask HN: Freelancer? Seeking
# freelancer?". Комментарии, начинающиеся с SEEKING FREELANCER, - это
# заказчики, которые ищут исполнителя: почти всегда США или Западная Европа,
# с бюджетами в долларах и почтой для связи прямо в тексте. Для выхода на
# американский рынок это самый прямой бесплатный канал: не биржа с
# комиссией и коннектами, а живой заказчик, которому пишешь на почту.
#
# Данные берём через публичный API поиска Algolia - без токена и регистрации.

HN_ENABLED = True
HN_ALGOLIA = "https://hn.algolia.com/api/v1"
HN_THREAD_QUERY = "Freelancer? Seeking freelancer?"
HN_MAX_COMMENTS = 100


def hn_algolia_get(path, params):
    try:
        r = requests.get(HN_ALGOLIA + path, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code >= 400:
            print("[hn] HTTP %s: %s" % (r.status_code, r.text[:200]))
            return None
        return r.json()
    except Exception as e:
        print("[hn] ошибка запроса (%s): %s" % (type(e).__name__, e))
        return None


# Тред выходит раз в месяц, поэтому окно в 75 дней гарантированно накрывает
# текущий и предыдущий - и при этом отсекает архив за годы.
HN_THREAD_MAX_AGE_DAYS = 75


def find_hn_freelance_thread():
    """Находит АКТУАЛЬНЫЙ месячный тред 'Freelancer? Seeking freelancer?'.

    Важно: ищем через /search_by_date, а не через /search. Обычный /search
    сортирует по релевантности, и первый боевой прогон из-за этого прицепился
    к треду за февраль 2020 года - заказчики оттуда искали исполнителя шесть
    лет назад. search_by_date отдаёт свежее первым, а numericFilters режет
    всё старше окна, чтобы такое не повторилось даже случайно.
    """
    oldest = int(time.time()) - HN_THREAD_MAX_AGE_DAYS * 24 * 3600
    data = hn_algolia_get("/search_by_date", {
        "tags": "story",
        "query": HN_THREAD_QUERY,
        "hitsPerPage": 20,
        "numericFilters": "created_at_i>%d" % oldest,
    })
    if not data:
        return None, None

    for hit in data.get("hits", []):
        title = (hit.get("title") or "").lower()
        # именно тред про поиск фрилансера, а не "Who wants to be hired"
        if "seeking freelancer" not in title:
            continue
        return hit.get("objectID"), hit.get("title")

    print("[hn] свежий тред 'Seeking freelancer' за последние %d дней не найден "
          "- возможно, в этом месяце его ещё не опубликовали"
          % HN_THREAD_MAX_AGE_DAYS)
    return None, None


def check_hn_freelance(seen):
    if not HN_ENABLED:
        return

    thread_id, thread_title = find_hn_freelance_thread()
    if not thread_id:
        return

    data = hn_algolia_get("/search_by_date", {
        "tags": "comment,story_%s" % thread_id, "hitsPerPage": HN_MAX_COMMENTS,
    })
    if not data:
        return

    hits = data.get("hits", [])
    print("[hn] тред: %s (комментариев получено: %d)" % (thread_title, len(hits)))

    for hit in hits:
        comment_id = hit.get("objectID")
        if not comment_id:
            continue
        uid = "hn:" + str(comment_id)
        if uid in seen:
            continue
        seen.add(uid)

        text = strip_html(hit.get("comment_text") or "", keep_newlines=True)
        if not text:
            continue

        # Нас интересуют только те, кто ИЩЕТ исполнителя. Заголовок стоит в
        # начале комментария, поэтому проверяем именно начало: иначе поймаем
        # исполнителей, упомянувших "seeking freelancer" в тексте о себе.
        head = text[:120].lower().replace("-", " ")
        if "seeking freelancer" not in head:
            continue

        title = " ".join(text.split("\n")[0].split()[:14])
        link = "https://news.ycombinator.com/item?id=%s" % comment_id
        author = hit.get("author") or ""

        if matches_keywords(text, KEYWORDS_EN):
            details = text[:600]
            if author:
                details = "author: " + author + "\n" + details
            # Заказчик почти всегда оставляет почту прямо в комментарии - на
            # неё и вешаем кнопку. Берём первую: их редко бывает больше одной,
            # а гадать, на какую из двух писать, хуже, чем не предлагать кнопку.
            emails = [c for c in extract_contacts(text).split(", ")
                      if "@" in c and not c.startswith("@")]
            notify("Hacker News — Seeking freelancer", title, "", link,
                   details=details,
                   reply_to=emails[0] if emails else None,
                   reply_subject="Freelance developer — re: your HN post")


# ---------------------- ИСТОЧНИК 9: WE WORK REMOTELY (США) ----------------------
#
# Крупная англоязычная доска удалённой работы с открытыми RSS-лентами по
# категориям. Ни ключа, ни регистрации. Здесь больше найма в штат, чем
# разовых заказов, но контрактные и part-time позиции попадаются регулярно.

WWR_ENABLED = True
WWR_RSS_URLS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
]


def check_wwr(seen):
    if not WWR_ENABLED:
        return
    for url in WWR_RSS_URLS:
        try:
            feed = fetch_feed(url)
        except Exception as e:
            print("[wwr] ошибка загрузки ленты (%s) (%s): %s"
                  % (url, type(e).__name__, e))
            continue

        if not feed.entries:
            print("[wwr] лента пуста: %s" % url)
            continue

        for entry in feed.entries:
            uid = "wwr:" + (entry.get("id") or entry.get("link", ""))
            if uid == "wwr:" or uid in seen:
                continue
            seen.add(uid)

            title = strip_html(entry.get("title", ""))
            description = strip_html(entry.get("summary", ""))
            link = entry.get("link", "")

            if matches_keywords(title, KEYWORDS_EN):
                notify("WeWorkRemotely", title, "", link, details=description[:400])


# ---------------------- ИСТОЧНИК 10: REMOTEOK (США) ----------------------
#
# Открытый JSON без ключа. Первый элемент ответа - юридическая заметка, а не
# вакансия, её пропускаем. Просит внятный User-Agent.

REMOTEOK_ENABLED = True
REMOTEOK_URL = "https://remoteok.com/api"


def check_remoteok(seen):
    if not REMOTEOK_ENABLED:
        return
    try:
        r = requests.get(REMOTEOK_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code >= 400:
            print("[remoteok] HTTP %s: %s" % (r.status_code, r.text[:200]))
            return
        data = r.json()
    except Exception as e:
        print("[remoteok] ошибка запроса (%s): %s" % (type(e).__name__, e))
        return

    if not isinstance(data, list):
        print("[remoteok] неожиданный формат ответа - пропускаю")
        return

    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue    # первая запись - дисклеймер, а не вакансия
        uid = "remoteok:" + str(item["id"])
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("position") or item.get("title") or "Без названия"
        company = item.get("company") or ""
        description = strip_html(item.get("description") or "")
        link = item.get("url") or ""
        tags = " ".join(item.get("tags") or [])

        # Матчим ТОЛЬКО заголовок и теги. В описании вакансии всегда полно
        # общих слов, из-за которых через фильтр проезжала любая вакансия,
        # вплоть до "Kitchen Porter". Теги у RemoteOK - самый чистый сигнал.
        if matches_keywords(" ".join([title, tags]), KEYWORDS_EN):
            details = []
            if company:
                details.append("company: " + company)
            if tags:
                details.append("tags: " + tags)
            if description:
                details.append(description[:300])
            notify("RemoteOK", title, "", link, details="\n".join(details))


# ---------------------- ИСТОЧНИК 11: КАРТЫ (OSM) - БИЗНЕС БЕЗ САЙТА ----------------------
#
# Здесь принцип другой, чем во всех источниках выше. Там заказчик сам написал,
# что ему нужен исполнитель. Здесь - никто ничего не просил: мы сами находим
# бизнес, у которого В КАРТАХ НЕТ САЙТА, только телефон и адрес. Это и есть
# повод обратиться, и одновременно то, что ты продаёшь: лендинг под ключ с
# хостингом и доменом.
#
# Почему именно "без сайта", а не все подряд с карты: адрес с карты сам по
# себе не лид, обращение к нему - спам. Отсутствие сайта - конкретная причина
# написать конкретному человеку и конкретная вещь, которую можно сделать.
#
# ВАЖНО про канал: такие контакты берут ЗВОНКОМ или сообщением в мессенджер,
# поштучно. Массовая рассылка по этому списку с личной почты угробит ящик и
# ничего не принесёт.
#
# Данные - OpenStreetMap через Overpass API: открытые, без ключа, без
# регистрации и без привязки карты.

OSM_ENABLED = True
OSM_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",   # запасной инстанс
)
OSM_MAX_RESULTS = 60

# Категории, которым сайт-визитка реально нужна и у которых есть бюджет.
OSM_AMENITIES = ("cafe|restaurant|bar|fast_food|dentist|doctors|clinic|"
                 "veterinary|driving_school|pharmacy")

# Города обходятся по очереди - по одному за прогон, чтобы не долбить
# бесплатный Overpass. Список правь свободно: bbox это (юг, запад, север,
# восток), "lang" решает, на каком языке будет черновик обращения.
OSM_CITIES = [
    {"name": "Москва",           "lang": "ru", "bbox": (55.55, 37.35, 55.92, 37.85)},
    {"name": "Санкт-Петербург",  "lang": "ru", "bbox": (59.80, 30.10, 60.09, 30.55)},
    {"name": "Казань",           "lang": "ru", "bbox": (55.70, 48.98, 55.87, 49.28)},
    {"name": "Екатеринбург",     "lang": "ru", "bbox": (56.75, 60.50, 56.92, 60.72)},
    {"name": "Новосибирск",      "lang": "ru", "bbox": (54.95, 82.80, 55.13, 83.10)},
    {"name": "New York",         "lang": "en", "bbox": (40.55, -74.05, 40.92, -73.70)},
    {"name": "Los Angeles",      "lang": "en", "bbox": (33.90, -118.50, 34.20, -118.15)},
    {"name": "Chicago",          "lang": "en", "bbox": (41.75, -87.85, 42.02, -87.55)},
    {"name": "Austin",           "lang": "en", "bbox": (30.15, -97.95, 30.45, -97.60)},
    {"name": "Miami",            "lang": "en", "bbox": (25.70, -80.30, 25.86, -80.13)},
    {"name": "London",           "lang": "en", "bbox": (51.42, -0.25, 51.60, 0.02)},
    {"name": "Berlin",           "lang": "en", "bbox": (52.42, 13.25, 52.58, 13.55)},
]


def pick_osm_city():
    """Один город за прогон, по кругу - примерно раз в полсуток на город."""
    if not OSM_CITIES:
        return None
    return OSM_CITIES[int(time.time() // 3600) % len(OSM_CITIES)]


def build_osm_query(bbox):
    """Overpass QL: с именем и телефоном, но БЕЗ сайта.

    Телефон требуется на стороне сервера: без него лид бесполезен, звонить
    некуда. Отдельные ветки для phone и contact:phone - в OSM встречаются оба
    написания, а ИЛИ внутри одного фильтра Overpass не умеет.
    """
    box = "%s,%s,%s,%s" % bbox
    parts = []
    for selector in ('["amenity"~"^(%s)$"]' % OSM_AMENITIES, '["shop"]'):
        for phone_key in ("phone", "contact:phone"):
            parts.append(
                'nwr["name"][!"website"][!"contact:website"]["%s"]%s(%s);'
                % (phone_key, selector, box)
            )
    return "[out:json][timeout:60];(%s);out center %d;" % ("".join(parts), OSM_MAX_RESULTS)


def overpass_get(query):
    """Спрашивает Overpass, при неудаче пробует запасной инстанс."""
    for endpoint in OSM_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": query},
                              headers={"User-Agent": USER_AGENT}, timeout=75)
            if r.status_code >= 400:
                print("[osm] %s -> HTTP %s" % (endpoint, r.status_code))
                continue
            return r.json()
        except Exception as e:
            print("[osm] %s -> ошибка (%s): %s" % (endpoint, type(e).__name__, e))
    return None


def check_osm_no_website(seen):
    if not OSM_ENABLED:
        return

    city = pick_osm_city()
    if city is None:
        return

    data = overpass_get(build_osm_query(city["bbox"]))
    if data is None:
        print("[osm] ни один инстанс Overpass не ответил - пропускаю")
        return

    elements = data.get("elements", [])
    print("[osm] %s: заведений без сайта найдено %d" % (city["name"], len(elements)))

    source = ("Maps — business without a website" if city["lang"] == "en"
              else "Карты — бизнес без сайта")

    for element in elements:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
        if not name or not phone:
            continue

        uid = "osm:%s/%s" % (element.get("type"), element.get("id"))
        if uid in seen:
            continue

        category = tags.get("amenity") or tags.get("shop") or ""
        street = " ".join(filter(None, [tags.get("addr:street"),
                                        tags.get("addr:housenumber")])).strip()

        details = ["📞 " + phone]
        if street:
            details.append("📍 " + street)
        if category:
            details.append("🏷 " + category)

        link = "https://www.openstreetmap.org/%s/%s" % (element.get("type"),
                                                        element.get("id"))
        # Помечаем просмотренным ТОЛЬКО то, что реально ушло в Telegram.
        # У остальных источников наоборот - там лид протухает сам (вакансию
        # закрыли, заказ разобрали), и второй раз он не нужен. А кафе без
        # сайта останется без сайта и через месяц: если списать со счёта всё,
        # что не влезло в потолок, мы сожжём полсотни живых контактов за
        # прогон. Не влезло - вернёмся к нему на следующем круге по городам.
        if not notify(source, "%s — %s" % (name, city["name"]), "", link,
                      details="\n".join(details)):
            print("[osm] потолок выбран, остальные %s оставляю на следующий круг"
                  % city["name"])
            return
        seen.add(uid)


# ---------------------- ОДИН ЗАПУСК ----------------------

def send_test_button(state):
    """Проверка кнопки без риска для посторонних: письмо уйдёт ТЕБЕ ЖЕ.

    Нажми кнопку под сообщением - и на следующем прогоне (до 15 минут) бот
    отправит письмо на твой собственный адрес. Если оно пришло, значит
    работает вся цепочка: кнопка, очередь, SMTP.
    """
    body = ("Это проверка кнопки в боте лидов.\n\n"
            "Если ты читаешь это письмо - значит нажатие кнопки в Telegram "
            "дошло до бота, очередь отработала и отправка письма с твоего "
            "ящика настроена верно. Дальше та же кнопка будет появляться под "
            "лидами с Hacker News, где заказчик оставил почту.")
    markup = register_send_button(
        state, IMAP_USER, "Проверка кнопки: бот лидов", body,
    )
    send_telegram(
        "🧪 ПРОВЕРКА КНОПКИ\n\n"
        "Нажми кнопку ниже. На следующем прогоне (до 15 минут) бот отправит "
        "письмо на твой же адрес %s - никто посторонний его не получит.\n\n"
        "Придёт письмо - значит вся цепочка работает." % IMAP_USER,
        reply_markup=markup,
    )
    print("[test] тестовое сообщение с кнопкой отправлено")


def main():
    global _pending_state

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ошибка] Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
              "(переменные окружения / GitHub Secrets). Проверка отменена.")
        return

    # Сначала разбираем нажатия кнопок с прошлого прогона: человек уже принял
    # решение, письмо не должно ждать, пока бот обойдёт все источники.
    _pending_state = load_pending()
    try:
        process_send_buttons(_pending_state)
    except Exception as e:
        print("[send] ошибка разбора нажатий (%s): %s" % (type(e).__name__, e))

    if "--test-button" in sys.argv:
        send_test_button(_pending_state)
        save_pending(_pending_state)
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
        if HH_PROJECT_ENABLED:
            data = hh_get(HH_PROJECT_PARAMS)
            for item in (data or {}).get("items", []):
                if item.get("id"):
                    seen.add("hhproject:" + str(item["id"]))
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
                    for post in parse_tme_page(fetch_url(f"{TME_BASE}/{channel}"), channel):
                        seen.add(f"tg:{channel}:{post['id']}")
                except Exception as e:
                    print(f"[tg:{channel}] ошибка индексации ({type(e).__name__}): {e}")
        save_seen(seen)
        print("Проиндексировано. Дальше - только новое: заказы, письма Kwork, вакансии hh.ru.")
        return

    for check_fn in (check_personal_mail, check_kwork_mail, check_flru,
                      check_hn_freelance,
                      check_wwr, check_remoteok, check_osm_no_website,
                      check_hh_crowd, check_weblancer, check_hh_broad,
                      check_hh_project, check_superjob,
                      check_telegram_channels):
        print(f"-> {check_fn.__name__}")
        try:
            check_fn(seen)
        except Exception as e:
            print(f"[main] ошибка в {check_fn.__name__}: {e}")

    save_seen(seen)
    save_pending(_pending_state)
    if _draft_state["made"]:
        print(f"[draft] черновиков через Claude API: {_draft_state['made']}")
    if _notify_state["skipped"]:
        print(f"[!] отправлено {_notify_state['sent']}, отброшено по потолку "
              f"{_notify_state['skipped']}. Они уже в seen и повторно не придут.")
    print(f"Проверка завершена. Всего в памяти: {len(seen)}.")


if __name__ == "__main__":
    main()
