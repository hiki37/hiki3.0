#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты разбора писем Kwork на настоящих письмах из ящика.

Фикстуры рядом - это реальные письма Kwork (дайджест новых проектов и
уведомление о личном сообщении), а не выдуманная вёрстка. Запуск:

    python3 tests/test_kwork_parsing.py
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time as _time_module

import lead_monitor as lm

lm.pick_osm_city_original = lm.pick_osm_city   # до подмены в тестах

HERE = os.path.dirname(os.path.abspath(__file__))
sent = []
real_send_telegram = lm.send_telegram   # настоящая функция, для теста обрезки
real_build_draft = lm.build_draft
real_overpass_get = lm.overpass_get   # до подмены в тестах про карты
real_domain_has_mx = lm.domain_has_mx
real_domain_has_website = lm.domain_has_website

# Проверка контакта ходит в DNS и на сайт. В тестах этого быть не должно:
# на машине без сети они бы проходили, а в CI с сетью - падали на реальном
# ответе про реальный домен (ровно так и случилось). Блоки, которым нужна
# другая проверка, подменяют эти функции сами.
lm.domain_has_mx = lambda domain: True
lm.domain_has_website = lambda domain: False

# Карты в бою выключены (слишком много холодных контактов в чате), но код
# источника остаётся рабочим и проверяется - иначе включить его обратно
# одной строкой было бы страшно.
OSM_ENABLED_BY_DEFAULT = lm.OSM_ENABLED
lm.OSM_ENABLED = True
lm.send_telegram = lambda text, reply_markup=None: sent.append(text)

failures = []


def notify_now(*args, **kwargs):
    """notify + немедленная отправка.

    В бою между ними стоит очередь приоритетов: сначала бот обходит все
    источники, и только потом отправляет накопленное, начиная с самого
    ценного. В большинстве проверок ниже интересна именно доставка, поэтому
    очередь тут же и разгребается.
    """
    result = lm.notify(*args, **kwargs)
    lm.flush_notifications()
    return result


def reset_limits():
    lm._notify_state.update({"sent": 0, "skipped": 0, "by_source": {},
                             "queue": [], "seq": 0})


def check(condition, message):
    if condition:
        print("  ok  - " + message)
    else:
        print("  FAIL- " + message)
        failures.append(message)


def read(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


print("1. Дайджест разбирается на отдельные заказы")
digest = read("fixture_kwork_digest.html")
projects = lm.parse_kwork_digest(digest)
check(len(projects) == 2, "найдено 2 заказа (получено %d)" % len(projects))

first = projects[0]
check(first["id"] == "3216597", "id заказа = 3216597 (получено %r)" % first["id"])
check(first["title"] == "Настройка AI bardeen", "название без мусора (%r)" % first["title"])
check(first["price"] == "3 000 Р", "цена вытащена (%r)" % first["price"])
check(first["buyer"] == "avbv", "покупатель вытащен (%r)" % first["buyer"])
check("47 проектов на бирже" in first["buyer_stats"] and "53% нанято" in first["buyer_stats"],
      "статистика покупателя вытащена (%r)" % first["buyer_stats"])
check(first["link"] == "https://kwork.ru/new_offer?project=3216597",
      "ссылка ведёт на конкретный заказ (%r)" % first["link"])
check(">  >" not in first["category"] and "&gt;" not in first["category"],
      "рубрика без двойных стрелок и HTML-сущностей (%r)" % first["category"])
check(first["category"] == "Разработка и IT > Скрипты, боты и mini apps > ИИ-боты",
      "рубрика целиком (%r)" % first["category"])

second = projects[1]
check(second["id"] == "3216742" and second["title"] == "Установить Hermes Agent на готовый VPS Beget",
      "второй заказ разобран (%r)" % second["title"])

print("\n2. Мусор из шапки письма в заказы не попадает")
for project in projects:
    check("Название" not in project["title"] and "Покупатель" not in project["title"]
          and "Цена" not in project["title"],
          "в названии нет шапки таблицы (%r)" % project["title"])
check(not any(p["id"] in ("441", "5") for p in projects),
      "ссылки на faq/профиль не приняты за заказы")

print("\n3. Уведомление собирается читаемым")
sent[:] = []
lm.handle_kwork_digest(set(), "Новые проекты на бирже Kwork", digest)
lm.flush_notifications()
check(len(sent) == 2, "отправлено 2 уведомления (получено %d)" % len(sent))
if sent:
    msg = sent[0]
    print("  --- как это придёт в Telegram ---")
    for line in msg.splitlines():
        print("  | " + line)
    check("Название Покупатель Цена" not in msg, "в сообщении нет 'Название Покупатель Цена'")
    check("3 000" in msg, "в сообщении есть цена")
    check("kwork.ru/new_offer?project=3216597" in msg, "в сообщении есть ссылка на заказ")

print("\n4. Дедупликация по номеру заказа, а не по письму")
seen = set()
sent[:] = []
lm.handle_kwork_digest(seen, "Новые проекты на бирже Kwork", digest)
lm.flush_notifications()
first_round = len(sent)
lm.handle_kwork_digest(seen, "Новые проекты на бирже Kwork", digest)
lm.flush_notifications()
check(len(sent) == first_round, "повторный дайджест с теми же заказами не дублирует уведомления")

print("\n5. Личное сообщение от покупателя доходит ВСЕГДА, мимо ключевых слов")
message_mail = read("fixture_kwork_message.html")
check(lm.is_kwork_direct_mail("Новые сообщения на Kwork.ru"), "письмо опознано как личное")
check(not lm.is_kwork_direct_mail("Новые проекты на бирже Kwork"), "дайджест не опознан как личный")
check(not lm.matches_keywords(lm.clean_kwork_text(lm.strip_html(message_mail))),
      "в самом сообщении покупателя нет ни одного слова из KEYWORDS - "
      "проходить фильтр ему нечем, поэтому фильтр для таких писем и отключён")
check(lm.matches_keywords(lm.strip_html(message_mail)),
      "раньше письмо цеплялось за фильтр только случайно, словом 'сайт' "
      "из подвала 'Ответить на сайте' - и уходило нечитаемым куском")
sent[:] = []
lm.handle_kwork_direct(set(), "Новые сообщения на Kwork.ru", message_mail)
lm.flush_notifications()
check(len(sent) == 1, "уведомление всё равно отправлено (получено %d)" % len(sent))
if sent:
    msg = sent[0]
    print("  --- как это придёт в Telegram ---")
    for line in msg.splitlines():
        print("  | " + line)
    check("vkira7" in msg.lower(), "в сообщении есть имя покупателя")
    check("kwork.ru/inbox/vkira7" in msg, "в сообщении есть ссылка на переписку")
    check("отписаться от рассылки" not in msg.lower(), "подвал письма вычищен")

print("\n6. strip_html чистит сущности и стили")
check(lm.strip_html("<style>a{color:red}</style>Разработка и IT &gt; Боты &amp; скрипты")
      == "Разработка и IT > Боты & скрипты", "сущности раскодированы, CSS вырезан")

print("\n7. Разбор страницы Telegram t.me/s")
tme = '''
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchannel/451">
<div class="tgme_widget_message_text js-message_text" dir="auto">Нужен телеграм бот<br/>для приёма заявок. Бюджет 15000</div>
<div class="tgme_widget_message_footer compact js-message_footer">footer</div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchannel/452">
<div class="tgme_widget_message_text js-message_text" dir="auto">Продам гараж</div>
<div class="tgme_widget_message_footer compact js-message_footer">footer</div>
</div></div>
'''
posts = lm.parse_tme_page(tme, "testchannel")
check(len(posts) == 2, "найдено 2 поста (получено %d)" % len(posts))
if len(posts) == 2:
    check(posts[0]["link"] == "https://t.me/testchannel/451", "ссылка на пост собрана")
    check("\n" in posts[0]["text"], "переносы строк в посте сохранены")
    check(lm.matches_keywords(posts[0]["text"]), "пост про бота проходит фильтр")
    check(not lm.matches_keywords(posts[1]["text"]), "пост про гараж отсеивается")

print("\n8. Черновики откликов: только для нужных источников")
check(lm.wants_draft("Kwork"), "Kwork - черновик нужен")
check(lm.wants_draft("Kwork — ЛИЧНОЕ ⚡"), "личка Kwork - черновик нужен")
check(lm.wants_draft("Telegram: frilanser_vacansii"), "Telegram - черновик нужен")
check(not lm.wants_draft("FL.ru"), "FL.ru - черновика НЕТ (там отклик платный)")
check(not lm.wants_draft("SuperJob"), "SuperJob - черновика нет")

print("\n9. Черновик попадает в сообщение")
lm.build_draft = lambda source, title, details, link: "Готов сделать бота. Когда нужен результат?"
reset_limits()   # блоки выше уже выбрали потолок по источнику Kwork
sent[:] = []
notify_now("Kwork", "Нужен простой тг-бот для отзывов", "", "https://kwork.ru/new_offer?project=1", details="💰 2 000 Р")
check(len(sent) == 1, "уведомление отправлено")
if sent:
    print("  --- как это придёт в Telegram ---")
    for line in sent[0].splitlines():
        print("  | " + line)
    check("ЧЕРНОВИК ОТКЛИКА" in sent[0], "черновик приложен")
    check("Готов сделать бота" in sent[0], "текст черновика на месте")

sent[:] = []
notify_now("FL.ru", "Небольшой модуль", "", "https://www.fl.ru/projects/1/", details="💰 15 000 ₽")
check(sent and "ЧЕРНОВИК" not in sent[0], "к заказу с FL.ru черновик не прикладывается")

lm.build_draft = real_build_draft   # вернуть настоящий на остальные проверки

print("\n10. Длинное сообщение обрезается, а не теряется")
captured = []
class FakeResponse:
    status_code = 200
    def raise_for_status(self): pass
lm.requests.post = lambda url, data=None, timeout=None: (captured.append(data["text"]), FakeResponse())[1]
lm.time.sleep = lambda s: None
real_send_telegram("я" * 9000)
check(len(captured) == 1 and len(captured[0]) <= 4100,
      "сообщение обрезано до предела Telegram (получено %d символов)"
      % (len(captured[0]) if captured else -1))
check(captured and captured[0].endswith("[...обрезано]"), "обрезка помечена в тексте")

print("\n11. Бесплатные шаблонные черновики (без API-ключа)")
lm.ANTHROPIC_API_KEY = ""
lm.DRAFT_ENGINE = "auto"
del lm.generate_draft   # если движок всё-таки полезет в API - будет видно
lm.generate_draft = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("без ключа в платный API ходить нельзя"))

# заголовки настоящих заказов из прогонов бота
cases = [
    ("Нужен простой тг-бот для отзывов", "бот"),
    ("БОТ Телеграмм на Python для группы", "бот"),
    ("Спарсить 400к страниц с сайта", "парсер"),
    ("Автоматизация смет с помощью ИИ, Access, Excel, и тд", "скрипт"),
    ("Нам нужен специалист по синхронизации 1с и сайта.", "интеграц"),
    ("Создать сервер VPS с готовым решением Docker", "сервер"),
    ("Срочно доработать сайт на вордпресс", "сайт"),
]
for title, expect in cases:
    draft = lm.build_draft("Kwork", title, "", "")
    ok = draft and expect in draft.lower() and draft.rstrip().endswith("?")
    check(ok, "%-45s -> тип опознан, вопрос в конце" % title[:45])

print("\n  --- пример готового черновика ---")
for line in lm.build_draft("Kwork", "Нужен простой тг-бот для отзывов", "", "").split(". "):
    print("  | " + line.strip() + ("" if line.strip().endswith("?") else "."))

print("\n12. Незнакомая задача - сдержанная заготовка, без обещаний")
draft = lm.build_draft("Kwork", "Помощник по проекту", "", "")
check(draft and draft.rstrip().endswith("?"), "вопрос на месте")
check("готов взяться" in draft.lower(), "общая заготовка, без выдуманной конкретики")

print("\n13. Личное сообщение получает ответ, а не отклик на заказ")
draft = lm.build_draft("Kwork — ЛИЧНОЕ ⚡", "Покупатель vkira7 ждёт ответа", "", "")
check("прошу прощения" in draft.lower(), "это извинение за задержку, а не отклик")
check("покажу рабочий результат" not in draft.lower(), "нет текста отклика на заказ")

mail_draft = lm.build_draft("Личное письмо ⚡ ТЕБЕ ОТВЕТИЛИ", "Re: сайт", "", "")
check("прошу прощения" not in mail_draft.lower(),
      "в ответе на письмо извинений за задержку нет - это ответ на наш же оффер")
check("телеграм-бот" in mail_draft.lower(),
      "ответ сразу предлагает то, что делаем быстро")
print("  | " + mail_draft)

print("\n14. Платный движок падает - лид всё равно уходит с заготовкой")
lm.ANTHROPIC_API_KEY = "sk-test"
lm.generate_draft = lambda *a, **k: None    # как будто кончились кредиты
draft = lm.build_draft("Kwork", "Нужен простой тг-бот для отзывов", "", "")
check(draft and "бот" in draft.lower(), "откат на бесплатный шаблон сработал")
lm.ANTHROPIC_API_KEY = ""

print("\n15. Англоязычные источники: свой фильтр и свой черновик")
check(lm.is_english_source("Hacker News — Seeking freelancer"), "HN опознан как англоязычный")
check(lm.is_english_source("RemoteOK"), "RemoteOK опознан")
check(not lm.is_english_source("Kwork"), "Kwork - не англоязычный")

# русский список на английском тексте почти не срабатывает - ради этого и заведён KEYWORDS_EN
us_task = "SEEKING FREELANCER | Remote | We need a Python scraper to pull product data"
check(lm.matches_keywords(us_task, lm.KEYWORDS_EN), "английский фильтр ловит задачу")

draft = lm.build_draft("Hacker News — Seeking freelancer", us_task, "", "")
print("  --- черновик для американского заказчика ---")
for line in draft.split(". "):
    print("  | " + line.strip() + ("" if line.strip().endswith("?") else "."))
check(draft.startswith("Hi!"), "черновик по-английски")
check("scraper" in draft.lower(), "тип задачи опознан (scraper)")
check(draft.rstrip().endswith("?"), "вопрос в конце")
check("Здравствуйте" not in draft, "русского текста в англоязычном черновике нет")

check(lm.build_draft("Kwork", "Нужен парсер сайта", "", "").startswith("Здравствуйте"),
      "русский источник по-прежнему получает русский черновик")

print("\n16. HN: берём только тех, кто ИЩЕТ исполнителя")
hits = [
    {"objectID": "1", "author": "acme",
     "comment_text": "SEEKING FREELANCER<p>Remote | We need a Python scraper. Email me at a@b.co"},
    {"objectID": "2", "author": "dev42",
     "comment_text": "SEEKING WORK<p>Python developer, 10 years, available for contract"},
]
sent[:] = []
lm.hn_algolia_get = lambda path, params: (
    {"hits": [{"objectID": "999",
               "title": "Ask HN: Freelancer? Seeking freelancer? (August 2026)"}]}
    if params.get("tags") == "story" else {"hits": hits})
lm.check_hn_freelance(set())
lm.flush_notifications()
check(len(sent) == 1, "отправлен только заказчик, не соискатель (получено %d)" % len(sent))
if sent:
    check("news.ycombinator.com/item?id=1" in sent[0], "ссылка на комментарий заказчика")
    check("acme" in sent[0], "автор указан")

print("\n17. Англоязычный фильтр не пропускает мусор (регрессия из боевого прогона)")
# Ровно эти вакансии RemoteOK прислал в Telegram, пока фильтр был дырявым
junk = ["Kitchen Porter", "Mail Sorter", "Gardener", "Ramp Attendant",
        "Removalist Offsider", "Cook FT Northpoint", "Retail Store Associate",
        "Bell Captain", "Quantity Surveyor", "Post Office Manager",
        "Store Manager", "Vehicle Progressor", "Joiner", "Lead Estimator"]
for title in junk:
    check(not lm.matches_keywords(title, lm.KEYWORDS_EN), "отсеян: %s" % title)

good = ["Senior Backend Engineer Build AI Agents", "Python Developer",
        "Full Stack Engineer", "Need a web scraper for product data",
        "Telegram bot developer", "Wordpress landing page fixes",
        "LLM / prompt engineer for RAG app"]
for title in good:
    check(lm.matches_keywords(title, lm.KEYWORDS_EN), "пропущен: %s" % title)

print("\n18. Короткие куски не ловятся внутри других слов")
traps = [("Corporate capital markets analyst", "api в capital"),
         ("Physical therapist assistant", "api в therapist"),
         ("Employment laws specialist", "aws в laws"),
         ("Warehouse storage operative", "rag в storage"),
         ("Excellent customer service rep", "excel в excellent")]
for text, why in traps:
    check(not lm.matches_keywords(text, lm.KEYWORDS_EN), "не поймался %s" % why)

print("\n19. Один источник не съедает весь лимит за прогон")
reset_limits()
sent[:] = []
for i in range(30):
    lm.notify("RemoteOK", "Job %d" % i, "", "")
for i in range(3):
    lm.notify("Hacker News — Seeking freelancer", "Client %d" % i, "", "")
lm.flush_notifications()
from_remoteok = sum(1 for m in sent if "RemoteOK" in m)
from_hn = sum(1 for m in sent if "Hacker News" in m)
check(from_remoteok <= lm.MAX_NOTIFICATIONS_PER_SOURCE,
      "RemoteOK ограничен %d (отправлено %d)" % (lm.MAX_NOTIFICATIONS_PER_SOURCE, from_remoteok))
check(from_hn == 3, "лиды Hacker News дошли, несмотря на поток RemoteOK (дошло %d)" % from_hn)

print("\n20. HN берёт свежий тред, а не архивный (регрессия из боевого прогона)")
calls = []
def fake_hn(path, params):
    calls.append((path, dict(params)))
    if path == "/search_by_date" and params.get("tags") == "story":
        # search_by_date отдаёт свежее первым
        return {"hits": [
            {"objectID": "new", "title": "Ask HN: Freelancer? Seeking Freelancer? (August 2026)"},
            {"objectID": "old", "title": "Ask HN: Freelancer? Seeking Freelancer? (February 2020)"},
        ]}
    return {"hits": []}
lm.hn_algolia_get = fake_hn
thread_id, thread_title = lm.find_hn_freelance_thread()
check(thread_id == "new", "взят свежий тред (получен %r)" % thread_title)
story_call = [c for c in calls if c[1].get("tags") == "story"][0]
check(story_call[0] == "/search_by_date",
      "поиск идёт по дате, а не по релевантности (было %s)" % story_call[0])
check("created_at_i>" in story_call[1].get("numericFilters", ""),
      "архив старше окна отсечён фильтром по дате")

calls[:] = []
lm.hn_algolia_get = lambda path, params: {"hits": [
    {"objectID": "x", "title": "Ask HN: Who wants to be hired? (August 2026)"}]}
thread_id, _ = lm.find_hn_freelance_thread()
check(thread_id is None, "тред 'Who wants to be hired' не принят за нужный")

print("\n21. Контакт заказчика вытаскивается наверх сообщения")
hn_text = ("SEEKING FREELANCER | Remote | We need a Python scraper. "
           "Email me at sarah@acmelabs.io or ping @acmesarah on telegram")
check(lm.extract_contacts(hn_text) == "sarah@acmelabs.io, @acmesarah",
      "почта и ник найдены (получено %r)" % lm.extract_contacts(hn_text))
check(lm.extract_contacts("no contacts here at all") == "", "пустой текст - пусто")
check(lm.extract_contacts("write to info@kwork.ru") == "",
      "почта самой площадки за контакт заказчика не принимается")

sent[:] = []
lm.build_draft = lambda *a, **k: None
notify_now("Hacker News — Seeking freelancer", "SEEKING FREELANCER | Python scraper",
          "", "https://news.ycombinator.com/item?id=1", details=hn_text)
check(sent and "📬 Контакт: sarah@acmelabs.io" in sent[0], "контакт попал в сообщение")
if sent:
    print("  --- как это придёт ---")
    for line in sent[0].splitlines()[:6]:
        print("  | " + line)
lm.build_draft = real_build_draft

print("\n22. Шаблон под MVP/лендинг под ключ (то, что добавил о себе)")
ru = lm.build_draft("Kwork", "Нужен лендинг с нуля", "", "")
check("хостинг" in ru.lower() and "домен" in ru.lower(),
      "русский черновик обещает хостинг и домен")
check(ru.rstrip().endswith("?"), "вопрос на месте")
print("  | " + ru)

en = lm.build_draft("Hacker News — Seeking freelancer", "Need a landing page for our MVP", "", "")
check("hosting" in en.lower() and "domain" in en.lower(),
      "английский черновик обещает хостинг и домен")
check(en.startswith("Hi!"), "по-английски")
print("  | " + en)

check("вордпресс" not in ru.lower(), "общий шаблон про сайты не перебил специальный")

print("\n23. Карты: бизнес без сайта")
q = lm.build_osm_query((55.55, 37.35, 55.92, 37.85))
check('[!"website"]' in q and '[!"contact:website"]' in q,
      "запрос требует ОТСУТСТВИЯ сайта")
check('["phone"]' in q and '["contact:phone"]' in q,
      "телефон берём - в мессенджер оффер уходит по нему")
check('["email"]' in q and '["contact:email"]' in q,
      "почту тоже просим: на неё оффер уходит одной кнопкой")

# ...но за один прогон - только одну пару ключей: со всеми четырьмя
# бесплатный Overpass отвечает 504 (проверено на боевом прогоне).
mail_q = lm.build_osm_query((55.55, 37.35, 55.92, 37.85), ("email", "contact:email"))
check('["email"]' in mail_q and '["phone"]' not in mail_q,
      "почтовый прогон не тащит заодно телефонные ветки")
groups = set()
_real_time = _time_module.time
try:
    for quarter in range(4):
        _time_module.time = (lambda q: (lambda: q * 900.0))(quarter)
        groups.add(lm.pick_osm_contacts())
finally:
    _time_module.time = _real_time
check(len(groups) == len(lm.OSM_CONTACT_GROUPS),
      "за час чередуются обе пары ключей (получено %d)" % len(groups))
check('55.55,37.35,55.92,37.85' in q, "рамка города подставлена")

overpass_answer = {"elements": [
    {"type": "node", "id": 111, "tags": {
        "name": "Кофейня Ромашка", "amenity": "cafe", "phone": "+7 495 111-22-33",
        "addr:street": "ул. Баумана", "addr:housenumber": "12"}},
    {"type": "way", "id": 222, "tags": {          # без телефона - пропускаем
        "name": "Бар без связи", "amenity": "bar"}},
    {"type": "node", "id": 333, "tags": {          # без имени - пропускаем
        "amenity": "cafe", "phone": "+7 495 999"}},
]}
lm.overpass_get = lambda query: overpass_answer
lm.pick_osm_city = lambda: {"name": "Москва", "lang": "ru",
                            "bbox": (55.55, 37.35, 55.92, 37.85)}
reset_limits()
sent[:] = []
lm.check_osm_no_website(set())
lm.flush_notifications()
check(len(sent) == 1, "отправлено только пригодное заведение (получено %d)" % len(sent))
if sent:
    print("  --- как это придёт ---")
    for line in sent[0].splitlines():
        print("  | " + line)
    check("Кофейня Ромашка — Москва" in sent[0], "название и город")
    check("+7 495 111-22-33" in sent[0], "телефон на месте")
    check("openstreetmap.org/node/111" in sent[0], "ссылка на объект")
    check("сайта у вас нет" in sent[0], "черновик - обращение, а не отклик на заказ")
    check("Покажу рабочий результат до оплаты" not in sent[0],
          "текст отклика на заказ сюда не подставился")

print("\n24. Для американского города обращение по-английски")
lm.pick_osm_city = lambda: {"name": "Austin", "lang": "en",
                            "bbox": (30.15, -97.95, 30.45, -97.60)}
reset_limits()
sent[:] = []
lm.check_osm_no_website(set())
lm.flush_notifications()
check(sent and "don't have a website" in sent[0], "английское обращение")
check(sent and "Здравствуйте" not in sent[0], "русского текста нет")

print("\n25. Города обходятся по кругу, а не долбится один")
import time as _time
real_time = _time.time
seen_cities = []
try:
    for hour in range(len(lm.OSM_CITIES) * 2):
        _time.time = (lambda h: (lambda: h * 3600.0))(hour)
        seen_cities.append(lm.pick_osm_city_original()["name"])
finally:
    _time.time = real_time
check(len(set(seen_cities)) == len(lm.OSM_CITIES),
      "за сутки обойдены все %d городов (уникальных %d)"
      % (len(lm.OSM_CITIES), len(set(seen_cities))))
check(seen_cities[0] != seen_cities[1], "соседние прогоны берут разные города")

print("\n26. Подпись черновика различает отклик и холодное обращение")
check("ОФФЕР" in sent[0] and "ЧЕРНОВИК ОТКЛИКА" not in sent[0],
      "у карт - оффер под кнопку, а не отклик на заказ")

print("\n27. Карты: лиды сверх потолка НЕ сжигаются")
many = {"elements": [
    {"type": "node", "id": 1000 + i, "tags": {
        "name": "Кафе %d" % i, "amenity": "cafe", "phone": "+7 495 000-00-%02d" % i}}
    for i in range(20)
]}
lm.overpass_get = lambda query: many
lm.pick_osm_city = lambda: {"name": "Москва", "lang": "ru",
                            "bbox": (55.55, 37.35, 55.92, 37.85)}

seen_store = set()
reset_limits()
sent[:] = []
lm.check_osm_no_website(seen_store)
lm.flush_notifications()
first_round = len(sent)
check(first_round == lm.MAX_MAPS_PER_RUN,
      "за прогон ушло ровно %d (получено %d)" % (lm.MAX_MAPS_PER_RUN, first_round))
objects_seen = lambda: sum(1 for k in seen_store if k.startswith("osm:"))
check(objects_seen() == first_round,
      "в seen попали ТОЛЬКО отправленные: %d из 20" % objects_seen())

# следующий круг по тому же городу - должны прийти СЛЕДУЮЩИЕ, а не те же
reset_limits()
sent[:] = []
lm.check_osm_no_website(seen_store)
lm.flush_notifications()
check(len(sent) == 20 - first_round, "на втором круге пришёл остаток (%d)" % len(sent))
check(("Кафе %d" % first_round) in sent[0],
      "пришли следующие по списку, а не повтор (%s)" % sent[0].splitlines()[2])
check(objects_seen() == 20, "все 20 просмотрены за два круга")

print("\n28. У остальных источников потолок прежний")
reset_limits()
sent[:] = []
check(notify_now("Kwork", "Тест", "", "") is True, "лид принят в очередь")
check(len(sent) == 1, "и ушёл в Telegram")

lm._notify_state["by_source"]["Kwork"] = lm.MAX_NOTIFICATIONS_PER_SOURCE
sent[:] = []
notify_now("Kwork", "Тест2", "", "")
check(not sent and lm._notify_state["skipped"] == 1,
      "упёрлись в потолок источника - сообщение не ушло")

print("\n29. Отличаем живого человека от рассылки")
# рассылки и роботы - настоящие отправители из ящика
robots = [
    ("no_reply@free-lance.ru", "List-Unsubscribe: <https://fl.ru/unsub>\n"),
    ("collection@avito.ru", ""),
    ("notifications@github.com", ""),
    ("hello@plus.yandex.ru", ""),
    ("no-reply@email.claude.com", ""),
    ("news@kwork.ru", ""),
    ("someone@newsletter.example.org", "Precedence: bulk\n"),
    ("robot@anywhere.com", "Auto-Submitted: auto-replied\n"),
]
for sender, headers in robots:
    check(lm.looks_like_robot_mail(sender, headers), "робот отсеян: %s" % sender)

humans = [
    ("Sarah Chen <sarah@acmelabs.io>", "Subject: Re: your message\n"),
    ("vladimir@mail.ru", ""),
    ("john.doe@gmail.com", "Subject: About the scraper\n"),
    ("Мария <maria@yandex.ru>", ""),   # обычный человек с Яндекс-почты
]
for sender, headers in humans:
    check(not lm.looks_like_robot_mail(sender, headers), "человек пропущен: %s" % sender)

print("\n30. Ответ живого человека приходит громко и без фильтра слов")
sent[:] = []
reset_limits()
reply_text = "Hi! Yes, still looking. Can you do it by next Friday?"
check(not lm.matches_keywords(reply_text), "по ключевым словам такой ответ НЕ проходит")
notify_now("Личное письмо ⚡ ТЕБЕ ОТВЕТИЛИ", "Re: your message", "", "",
          details="От: Sarah Chen <sarah@acmelabs.io>\n\n" + reply_text)
check(len(sent) == 1, "уведомление всё равно отправлено")
if sent:
    print("  --- как это придёт ---")
    for line in sent[0].splitlines():
        print("  | " + line)
    check("ТЕБЕ ОТВЕТИЛИ" in sent[0], "видно, что это ответ, а не очередной лид")
    check("sarah@acmelabs.io" in sent[0], "отправитель на месте")

print("\n31. Рассылки с поддоменов отправки отсеиваются")
# ровно эти три лежали в ящике за ночь
for who in ("info@info.sportmaster.ru", "offers@emails.tinkoff.ru",
            "no-reply@email.claude.com", "promo@mail.example-shop.ru"):
    check(lm.looks_like_robot_mail(who, ""), "рассылка отсеяна: %s" % who)
for who in ("sarah@acmelabs.io", "ivan@mail.ru", "boss@my-company.co.uk"):
    check(not lm.looks_like_robot_mail(who, ""), "человек пропущен: %s" % who)

print("\n31b. Собственное письмо не считается ответом клиента")
lm.IMAP_USER = "me@example.com"
check(lm.looks_like_robot_mail("me@example.com", ""),
      "письмо от самого себя отсеяно")
check(lm.looks_like_robot_mail("Я <ME@example.com>", ""),
      "регистр не мешает")
check(not lm.looks_like_robot_mail("client@example.com", ""),
      "чужое письмо с того же домена проходит")
lm.IMAP_USER = ""

print("\n32. Кнопка отправки: очередь и нажатие")
state = {"offset": 0, "items": {}}
markup = lm.register_send_button(state, "client@acme.io", "Re: your post", "Hi! ...")
key = markup["inline_keyboard"][0][0]["callback_data"].split(":")[1]
check(len(state["items"]) == 1, "письмо положено в очередь")
check(state["items"][key]["to"] == "client@acme.io", "адресат сохранён")
check(markup["inline_keyboard"][0][0]["text"].endswith("Отправить письмо"),
      "кнопка подписана понятно")

tg_calls = []
lm.telegram_api = lambda method, payload, quiet_errors=(): (
    tg_calls.append(method) or (
        {"ok": True, "result": [{"update_id": 7, "callback_query": {
            "id": "cb1", "data": "send:" + key,
            "message": {"message_id": 55, "chat": {"id": 1}}}}]}
        if method == "getUpdates" else {"ok": True}))
mails = []
lm.send_email = lambda to, subject, body, in_reply_to=None: (
    mails.append((to, subject, body)) or True)
sent[:] = []
lm.process_send_buttons(state)
check(len(mails) == 1, "нажатие привело к отправке письма (получено %d)" % len(mails))
if mails:
    check(mails[0][0] == "client@acme.io", "письмо ушло тому, кому надо")
check(state["items"] == {}, "письмо убрано из очереди - повторно не уйдёт")
check(state["offset"] == 8, "позиция чтения сдвинута, нажатие не разберётся дважды")
check(any("Письмо отправлено" in m for m in sent), "в Telegram пришло подтверждение")
check("editMessageReplyMarkup" in tg_calls,
      "кнопка с обработанного сообщения снята")

print("\n33. Повторное нажатие той же кнопки ничего не отправляет")
mails[:] = []
lm.process_send_buttons(state)
check(len(mails) == 0, "второй раз письмо не уходит")

print("\n34. Кнопка вешается там и только там, где есть куда писать")
lm._pending_state = {"offset": 0, "items": {}}
lm.send_telegram = lambda text, reply_markup=None: sent.append((text, reply_markup))
sent[:] = []
reset_limits()
notify_now("Hacker News — Seeking freelancer", "SEEKING FREELANCER | need a scraper",
           "", "", details="write me", reply_to="client@acme.io")
check(sent and sent[0][1] is not None, "у лида с почтой кнопка есть")

sent[:] = []
notify_now("FL.ru", "Заказ без контакта", "", "", details="контакта нет")
check(sent and sent[0][1] is None, "без адреса кнопки нет")

print("\n35. Карты: оффер в компанию уходит одним нажатием")
lm._pending_state = {"offset": 0, "items": {}}
sent[:] = []
reset_limits()
overpass_answer = {"elements": [
    {"type": "node", "id": 777, "tags": {
        "name": "Кофейня Ромашка", "amenity": "cafe",
        "email": "hello@romashka.ru", "phone": "+7 495 111-22-33"}},
    {"type": "node", "id": 888, "tags": {           # только телефон
        "name": "Шаверма", "amenity": "fast_food", "phone": "8 (812) 123-45-67"}},
]}
lm.overpass_get = lambda query: overpass_answer
lm.pick_osm_city = lambda: {"name": "Санкт-Петербург", "lang": "ru", "cc": "7",
                            "bbox": (59.80, 30.10, 60.09, 30.55)}
lm.check_osm_no_website(set())
lm.flush_notifications()
check(len(sent) == 2, "оба заведения дошли (получено %d)" % len(sent))

with_mail = [m for m in sent if "Кофейня Ромашка" in m[0]]
check(len(with_mail) == 1, "лид с почтой на месте")
if with_mail:
    text, markup = with_mail[0]
    print("  --- как это придёт ---")
    for line in text.splitlines():
        print("  | " + line)
    labels = [b["text"] for row in (markup or {}).get("inline_keyboard", []) for b in row]
    print("  | кнопки: %s" % labels)
    check(any("Отправить письмо" in x for x in labels), "кнопка отправки письма есть")
    check(any("WhatsApp" in x for x in labels), "кнопка WhatsApp тоже есть")
    queued = list(lm._pending_state["items"].values())
    check(len(queued) == 1, "письмо положено в очередь под кнопку")
    if queued:
        check(queued[0]["to"] == "hello@romashka.ru", "адресат - почта из карт")
        check(queued[0]["subject"] == "Сайт для Кофейня Ромашка",
              "тема письма про конкретную компанию (%s)" % queued[0]["subject"])
        check("Кофейня Ромашка" in queued[0]["body"],
              "в тексте оффера есть название компании")
        check("телеграм-бот" in queued[0]["body"],
              "оффер начинается с того, что делаем быстро: бот и лендинг")

phone_only = [m for m in sent if "Шаверма" in m[0]]
check(len(phone_only) == 1, "лид с одним телефоном тоже дошёл")
if phone_only:
    markup = phone_only[0][1] or {}
    buttons = [b for row in markup.get("inline_keyboard", []) for b in row]
    check(len(buttons) == 1 and "url" in buttons[0],
          "без почты остаётся только ссылка в мессенджер")
    if buttons and "url" in buttons[0]:
        check(buttons[0]["url"].startswith("https://wa.me/78121234567?text="),
              "местный номер превращён в международный (%s)" % buttons[0]["url"][:45])
        check("%D0%A8%D0%B0%D0%B2%D0%B5%D1%80%D0%BC%D0%B0" in buttons[0]["url"],
              "название компании подставлено прямо в текст сообщения")

print("\n35b. Одна и та же компания не получает два оффера")
lm._pending_state = {"offset": 0, "items": {}, "deferred": []}
sent[:] = []
reset_limits()
chain = {"elements": [
    # сеть: три точки на карте, почта одна - письмо должно уйти ОДНО
    {"type": "node", "id": 901, "tags": {"name": "Чио Чио", "shop": "hairdresser",
                                         "email": "info@chio.ru"}},
    {"type": "way", "id": 902, "tags": {"name": "Чио Чио", "shop": "hairdresser",
                                        "email": "INFO@chio.ru"}},
    {"type": "node", "id": 903, "tags": {"name": "Чио Чио на Ленина",
                                         "shop": "hairdresser",
                                         "contact:email": "info@chio.ru"}},
    # и та же история с телефоном, записанным по-разному
    {"type": "node", "id": 904, "tags": {"name": "Гермес", "amenity": "cafe",
                                         "phone": "+7 383 111-22-33"}},
    {"type": "node", "id": 905, "tags": {"name": "Гермес", "amenity": "cafe",
                                         "phone": "8 (383) 111-22-33"}},
]}
lm.overpass_get = lambda query: chain
lm.pick_osm_city = lambda: {"name": "Новосибирск", "lang": "ru", "cc": "7",
                            "bbox": (54.95, 82.80, 55.13, 83.10)}
seen_chain = set()
lm.check_osm_no_website(seen_chain)
lm.flush_notifications()
check(len(sent) == 2, "два оффера на две компании, а не пять на пять точек (получено %d)"
      % len(sent))
check(sum(1 for m in sent if "Чио Чио" in m[0]) == 1, "сеть с одной почтой - одно письмо")
check(sum(1 for m in sent if "Гермес" in m[0]) == 1, "один телефон - одно сообщение")
check("osm-mail:info@chio.ru" in seen_chain and "osm-tel:73831112233" in seen_chain,
      "контакт запомнен - дубль не всплывёт и на следующем круге")

# следующий круг: объекты новые, но контакты те же - ничего не уходит
sent[:] = []
reset_limits()
lm.check_osm_no_website(seen_chain)
lm.flush_notifications()
check(not sent, "на втором круге той же компании повторно не пишем")

print("\n35c. Пустой ответ инстанса не считается ответом")
calls = []
def fake_post(url, data=None, headers=None, timeout=None):
    calls.append(url)
    class R:
        status_code = 200
        def json(self_inner):
            # первый инстанс отвечает пустотой, второй - настоящими данными
            return ({"elements": []} if len(calls) == 1
                    else {"elements": [{"type": "node", "id": 1, "tags": {}}]})
    return R()
real_post = lm.requests.post
lm.requests.post = fake_post
try:
    data = real_overpass_get("[out:json];")
finally:
    lm.requests.post = real_post
check(len(calls) == 2, "после пустого ответа спрошен следующий инстанс")
check(data and len(data.get("elements", [])) == 1, "взяты данные того, у кого они есть")

print("\n36. Номера телефонов из карт превращаются в ссылки")
check(lm.osm_phone_digits("+7 (950) 002-05-99", "7") == "79500020599", "международный номер")
check(lm.osm_phone_digits("8 812 123 45 67", "7") == "78121234567", "русская восьмёрка")
check(lm.osm_phone_digits("(305) 555-1234", "1") == "13055551234", "местный номер США")
check(lm.osm_phone_digits("+49 30 1234567", "49") == "49301234567", "Берлин")
check(lm.osm_phone_digits("+7 495 111-22-33; +7 495 111-22-34", "7") == "74951112233",
      "из списка номеров берётся первый")
check(lm.osm_phone_digits("звоните", "7") == "", "мусор вместо номера - без кнопки")
check(lm.whatsapp_button("звоните", "7", "текст") is None, "кнопки на мусор не будет")

print("\n37. Быстрые задачи уходят раньше тяжёлых")
lm.send_telegram = lambda text, reply_markup=None: sent.append(text)
sent[:] = []
reset_limits()
lm.MAX_NOTIFICATIONS_PER_RUN, saved_run_cap = 3, lm.MAX_NOTIFICATIONS_PER_RUN
lm.notify("RemoteOK", "Senior Kubernetes microservices engineer", "", "")
lm.notify("RemoteOK", "Unity game developer, full-time", "", "")
lm.notify("Kwork", "Нужен парсер сайта с выгрузкой в Excel", "", "")
lm.notify("Kwork", "Нужен телеграм-бот для записи клиентов", "", "")
lm.notify("Личное письмо ⚡ ТЕБЕ ОТВЕТИЛИ", "Re: your message", "", "",
          details="Sarah: still looking?")
lm.flush_notifications()
lm.MAX_NOTIFICATIONS_PER_RUN = saved_run_cap
check(len(sent) == 3, "ушло ровно столько, сколько разрешено (получено %d)" % len(sent))
order = [m.splitlines()[2] for m in sent]
print("  --- порядок отправки ---")
for line in order:
    print("  | " + line)
check("Re: your message" in order[0], "первым - живой человек, который ждёт ответа")
check("телеграм-бот" in order[1], "вторым - телеграм-бот")
check("парсер" in order[2], "третьим - парсер")
check(not any("Kubernetes" in m or "Unity" in m for m in sent),
      "тяжёлая долгая разработка вытеснена, а не наоборот")
check(lm._notify_state["skipped"] == 2, "отброшенное посчитано")

print("\n38. Нажатия сверх предохранителя не теряются, а ждут следующего прогона")
state = {"offset": 0, "items": {}, "deferred": []}
keys = []
for i in range(lm.MAX_SENDS_PER_RUN + 2):
    markup = lm.register_send_button(state, "client%d@acme.io" % i, "Тема %d" % i, "текст")
    keys.append(markup["inline_keyboard"][0][0]["callback_data"].split(":")[1])

pending_updates = [
    {"update_id": 100 + i, "callback_query": {
        "id": "cb%d" % i, "data": "send:" + key,
        "message": {"message_id": 500 + i, "chat": {"id": 1}}}}
    for i, key in enumerate(keys)
]
lm.telegram_api = lambda method, payload, quiet_errors=(): (
    {"ok": True, "result": pending_updates} if method == "getUpdates" else {"ok": True})
mails[:] = []
sent[:] = []
lm.process_send_buttons(state)
check(len(mails) == lm.MAX_SENDS_PER_RUN,
      "за прогон ушло не больше предохранителя (%d)" % len(mails))
check(len(state["deferred"]) == 2, "два нажатия отложены, а не потеряны")
check(len(state["items"]) == 2, "их письма остались в очереди")

# следующий прогон: новых нажатий нет, но отложенные должны уйти сами
lm.telegram_api = lambda method, payload, quiet_errors=(): (
    {"ok": True, "result": []} if method == "getUpdates" else {"ok": True})
mails[:] = []
lm.process_send_buttons(state)
check(len(mails) == 2, "отложенные письма ушли на следующем прогоне (получено %d)"
      % len(mails))
check(state["items"] == {} and state["deferred"] == [], "очередь разгребена до конца")

print("\n39. Ответ клиенту уходит той же перепиской")
lm._pending_state = {"offset": 0, "items": {}, "deferred": []}
lm.send_telegram = lambda text, reply_markup=None: sent.append((text, reply_markup))
sent[:] = []
reset_limits()
notify_now("Личное письмо ⚡ ТЕБЕ ОТВЕТИЛИ", "Про сайт для кофейни", "", "",
           details="От: Сергей <sergey@romashka.ru>\n\nДобрый день, интересно",
           reply_to="sergey@romashka.ru",
           reply_subject="Re: Про сайт для кофейни",
           in_reply_to="<abc123@mail.ru>")
check(sent and sent[0][1] is not None, "к письму клиента прицеплена кнопка ответа")
queued = list(lm._pending_state["items"].values())
check(len(queued) == 1, "ответ лежит в очереди")
if queued:
    check(queued[0]["in_reply_to"] == "<abc123@mail.ru>",
          "ответ подклеится к переписке, а не придёт отдельным письмом")
    check(queued[0]["subject"] == "Re: Про сайт для кофейни", "тема - ответная")

print("\n40. Почта проверяется ДО того, как лид уйдёт в бота")
lm._contact_cache.clear()
lm.domain_has_mx = lambda domain: True
lm.domain_has_website = lambda domain: False
check(not lm.contact_email_ok("rkk@")[0], "битый синтаксис отсеян")
check(not lm.contact_email_ok("noreply@chef-lunch.ru")[0], "служебный адрес отсеян")
check(lm.contact_email_ok("rkk@chef-lunch.ru")[0], "живой адрес проходит")

lm._contact_cache.clear()
lm.domain_has_mx = lambda domain: False
ok, why = lm.contact_email_ok("info@mertvyi-domen.ru", set())
check(not ok and "сервер" in why, "домен без MX отсеян (%s)" % why)

lm._contact_cache.clear()
lm.domain_has_mx = lambda domain: True
lm.domain_has_website = lambda domain: True
seen_sites = set()
ok, why = lm.contact_email_ok("info@u-nih-est-sait.ru", seen_sites)
check(not ok and "сайт" in why, "компания с живым сайтом отсеяна (%s)" % why)
check("has-site:u-nih-est-sait.ru" in seen_sites, "домен запомнен - второй раз не проверяем")

lm._contact_cache.clear()
site_checks = []
lm.domain_has_website = lambda domain: site_checks.append(domain) or True
check(lm.contact_email_ok("73297819@mail.ru", set())[0], "ящик на mail.ru проходит")
check(not site_checks, "сайт mail.ru не проверяем - это не сайт компании")

lm._contact_cache.clear()
lm.domain_has_website = lambda domain: False
dead = {"dead-mail:rkk@chef-lunch.ru"}
check(not lm.contact_email_ok("rkk@chef-lunch.ru", dead)[0],
      "адрес, с которого пришла отбивка, больше не предлагается")
check(lm.contact_email_ok("RKK@CHEF-LUNCH.RU", dead)[0] is False, "регистр не обходит запрет")

print("\n41. Отбивка о недоставке разбирается сама")
lm.IMAP_USER = "me@example.com"
check(lm.looks_like_bounce("Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                           "Сайт для Chef Lunch - Адрес не найден"),
      "настоящая отбивка опознана")
check(lm.looks_like_bounce("noreply@yandex.ru", "Undelivered Mail Returned to Sender"),
      "англоязычная отбивка тоже")
check(not lm.looks_like_bounce("sarah@acmelabs.io", "Re: your offer"),
      "обычное письмо клиента отбивкой не считается")
bounce_body = ("Адрес не найден. Сообщение не доставлено, так как адрес "
               "rkk@chef-lunch.ru не найден или не принимает входящие письма. "
               "Ответ удаленного сервера: 550 5.1.1. Исходное письмо от me@example.com")
check(lm.bounced_address(bounce_body, "me@example.com") == "rkk@chef-lunch.ru",
      "адрес, до которого не дошло, вытащен (получено %r)"
      % lm.bounced_address(bounce_body, "me@example.com"))
check(lm.bounced_address("ничего похожего", "me@example.com") == "",
      "без адреса внутри - ничего не выдумываем")
lm.IMAP_USER = ""

print("\n42. Лид с мёртвой почтой не выкидывается, если есть телефон")
lm._contact_cache.clear()
lm._pending_state = {"offset": 0, "items": {}, "deferred": []}
lm.domain_has_mx = lambda domain: False          # почта нерабочая
lm.domain_has_website = lambda domain: False
sent[:] = []
reset_limits()
lm.overpass_get = lambda query: {"elements": [
    {"type": "node", "id": 611, "tags": {"name": "Чайхана", "amenity": "cafe",
                                         "email": "info@mertvyi.ru",
                                         "phone": "+7 495 111-22-33"}},
    {"type": "node", "id": 612, "tags": {"name": "Только почта", "amenity": "cafe",
                                         "email": "info@mertvyi2.ru"}},
]}
lm.pick_osm_city = lambda: {"name": "Москва", "lang": "ru", "cc": "7",
                            "bbox": (55.55, 37.35, 55.92, 37.85)}
seen_dead = set()
lm.check_osm_no_website(seen_dead)
lm.flush_notifications()
check(len(sent) == 1, "ушёл только тот, до кого есть как достучаться (получено %d)"
      % len(sent))
if sent:
    labels = [b["text"] for row in (sent[0][1] or {}).get("inline_keyboard", []) for b in row]
    check("Чайхана" in sent[0][0], "это лид с рабочим телефоном")
    check(not any("письмо" in x.lower() for x in labels),
          "кнопки письма нет - адрес мёртвый (кнопки: %s)" % labels)
    check(any("WhatsApp" in x for x in labels), "остался мессенджер")
check("osm:node/612" in seen_dead, "лид совсем без связи закрыт, чтобы не всплывал")

print("\n43. У компании уже есть сайт - лид не показываем вообще")
lm._contact_cache.clear()
lm.domain_has_mx = lambda domain: True
lm.domain_has_website = lambda domain: True
sent[:] = []
reset_limits()
lm.overpass_get = lambda query: {"elements": [
    {"type": "node", "id": 613, "tags": {"name": "Chef Lunch", "amenity": "cafe",
                                         "email": "rkk@chef-lunch.ru",
                                         "phone": "+7 495 111-22-33"}},
]}
seen_site = set()
lm.check_osm_no_website(seen_site)
lm.flush_notifications()
check(not sent, "в карты данные устарели: сайт есть - предлагать нечего")
check("osm:node/613" in seen_site, "и второй раз он не всплывёт")

print("\n44. Обрубок номера не превращается в кнопку")
check(lm.osm_phone_digits("+7 495 999", "7") == "", "короткий номер отброшен")
check(lm.osm_phone_digits("+7 495 111-22-33-44", "7") == "", "слишком длинный тоже")
check(lm.osm_phone_digits("+7 495 111-22-33", "7") == "74951112233", "нормальный проходит")

print("\n44b. Русский фильтр не ловится внутри других слов")
ru_traps = ["Работа с документами в офисе", "Требуется работник склада",
            "Подработка курьером"]
for trap in ru_traps:
    check(not lm.matches_keywords(trap), "не поймано на 'бота' внутри слова: %s" % trap)
ru_good = ["Нужен телеграм-бот для записи", "Сделать тг-бота под заказы",
           "Бот на aiogram для приёма заявок", "Нужен лендинг под ключ",
           "Требуется парсер маркетплейса", "Мини-апп в телеграм"]
for good in ru_good:
    check(lm.matches_keywords(good), "поймано как надо: %s" % good)

print("\n44c. В каналах ловим заказы, а не найм в штат")
tg_junk = ["#ios #office", "#Moskva #android #fulltime",
           "\u200b\u200b #ищу #мерч #дизайнер",
           "#вакансия #middle #разработчик #удаленка #Java",
           "Ищу работу, python-разработчик, резюме внутри"]
for post in tg_junk:
    check(not lm.tg_post_fits(post), "отсеяно: %s" % post.strip()[:45])
tg_good = ["Нужен телеграм-бот для записи клиентов, оплата 15000",
           "Ищу разработчика лендинга под ключ, есть текст и фото",
           "Нужен парсер маркетплейса на python, выгрузка в excel",
           "Требуется мини-апп в телеграм для приёма заявок"]
for post in tg_good:
    check(lm.tg_post_fits(post), "пропущено: %s" % post[:45])

print("\n45. Настройки потока: карты выключены, потолки под фриланс")
check(OSM_ENABLED_BY_DEFAULT is False,
      "источник карт выключен по умолчанию - в чат идут только заказы")
check(lm.MAX_NOTIFICATIONS_PER_RUN <= 12,
      "потолок за прогон не больше 12 (сейчас %d)" % lm.MAX_NOTIFICATIONS_PER_RUN)
check(len(lm.TELEGRAM_CHANNELS) >= 3,
      "в списке только живые каналы: %d" % len(lm.TELEGRAM_CHANNELS))
check(hasattr(lm, "probe_sources"),
      "есть режим разведки: новые ленты и каналы проверяются в CI, а не в чате")
check(lm.RU_FEEDS_ENABLED and len(lm.RU_FEEDS) >= 3,
      "русские биржи по RSS подключены (%d лент)" % len(lm.RU_FEEDS))

print("\n46. Лента биржи: мимо фильтра не проходит лишнее")
sent[:] = []
reset_limits()
lm.send_telegram = lambda text, reply_markup=None: sent.append(text)

class FakeEntry(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

class FakeFeed:
    entries = [
        FakeEntry(id="1", link="https://freelance.ru/projects/1",
                  title="Нужен телеграм-бот для записи клиентов",
                  summary="Бот на aiogram, приём заявок"),
        FakeEntry(id="2", link="https://freelance.ru/projects/2",
                  title="Требуется бухгалтер на полставки",
                  summary="Ведение первичной документации"),
    ]

real_fetch_feed = lm.fetch_feed
lm.fetch_feed = lambda url, timeout=15, user_agent=None: FakeFeed()
lm.RU_FEEDS = [("Freelance.ru", "https://freelance.ru/rss/projects")]
try:
    lm.check_ru_feeds(set())
    lm.flush_notifications()
finally:
    lm.fetch_feed = real_fetch_feed
check(len(sent) == 1, "прошла только задача по профилю (получено %d)" % len(sent))
check(sent and "телеграм-бот" in sent[0], "это заказ на бота")
check(not any("бухгалтер" in m for m in sent), "бухгалтерия отсеяна")

print("\n" + "=" * 60)
if failures:
    print("ПРОВАЛЕНО проверок: %d" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("Все проверки пройдены.")
