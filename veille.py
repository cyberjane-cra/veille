#!/usr/bin/env python3
"""
Veille presse automatique
-------------------------
RSS / pages de publications  ->  lecture du texte intégral (HTML, PDF)
podcasts                      ->  transcription audio (Groq Whisper)
                              ->  synthèse + étiquettes par IA (Gemini, repli Groq)
                              ->  base de données Notion

Usage :
  python veille.py                  # passage normal
  python veille.py --mode diagnostic  # teste toutes les sources et les clés, n'écrit rien dans Notion
  python veille.py --dry-run        # traite sans écrire dans Notion ni sauvegarder la mémoire
"""
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote

import feedparser
import requests
import trafilatura
import urllib3
import yaml
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
REPORT_PATH = os.path.join(STATE_DIR, "rapport_sources.md")
DB_TITLE = "Veille presse"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
NOW = dt.datetime.now(dt.timezone.utc)
START = time.time()

log = logging.getLogger("veille")


# =====================================================================
# Outils généraux
# =====================================================================
class QuotaExhausted(Exception):
    """Plus aucun quota IA disponible pour ce passage."""


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def load_yaml(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config():
    """Lit config.yaml et prépare les listes d'étiquettes et leurs couleurs."""
    c = load_yaml("config.yaml")
    themes, tcol, fams = [], {}, []
    for t in c.get("themes") or []:
        if isinstance(t, dict):
            fams.append({"famille": t.get("famille", ""), "couleur": t.get("couleur", "default"),
                         "etiquettes": list(t.get("etiquettes") or [])})
            for e in t.get("etiquettes") or []:
                themes.append(e)
                tcol[e] = t.get("couleur", "default")
        else:
            themes.append(t)
            tcol[t] = "default"
    regs, rcol = [], {}
    for r in c.get("regions") or []:
        name = r["nom"] if isinstance(r, dict) else r
        regs.append(name)
        rcol[name] = r.get("couleur", "default") if isinstance(r, dict) else "default"
    c["themes"], c["theme_colors"], c["regions"], c["region_colors"] = themes, tcol, regs, rcol
    c["theme_families"] = fams
    c["disciplines"] = c.get("disciplines") or []
    c["discipline_color"] = c.get("disciplines_couleur", "gray")
    return c


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def simplify(s):
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(s or "").lower()).strip()


TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|xtor|at_medium|at_campaign|cmp$|ref$|src$|source$)")


def norm_url(u):
    """Forme canonique d'une adresse, pour repérer les doublons."""
    p = urlparse((u or "").strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k.lower())]
    host = p.netloc.lower()
    host = re.sub(r"^(?:www|fr|en|de|es)\.", "", host)  # fr.site.org = www.site.org
    path = re.sub(r"/+$", "", p.path) or "/"
    return urlunparse(("https", host, path.lower(), "", urlencode(sorted(q)), ""))


def url_key(u):
    return hashlib.sha1(norm_url(u).encode()).hexdigest()[:16]


def parse_date(value):
    """Convertit une date (struct_time, chaîne ISO, « Friday, September 25, 2026 - 15:56 »…) en datetime UTC."""
    if not value:
        return None
    try:
        if isinstance(value, time.struct_time):
            return dt.datetime(*value[:6], tzinfo=dt.timezone.utc)
        s = str(value).strip()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h, mi = int(m.group(4) or 0), int(m.group(5) or 0)
            return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)
        from dateutil import parser as dparser
        d = dparser.parse(s.replace(" - ", " "), fuzzy=True)
        return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OverflowError, ImportError):
        return None


def date_from_url(u):
    """Déduit une date de l'adresse (…/2026/09/30/…, …/2026/09/…, …20260921…)."""
    m = re.search(r"/(20\d\d)/(0[1-9]|1[0-2])/(?:([0-3]\d)/)?", u) or \
        re.search(r"(?<!\d)(20\d\d)-(0[1-9]|1[0-2])-([0-3]\d)(?!\d)", u) or \
        re.search(r"(20\d\d)(0[1-9]|1[0-2])([0-3]\d)(?!\d)", u)
    if not m:
        return None
    try:
        y, mo = int(m.group(1)), int(m.group(2))
        if m.group(3):
            return dt.datetime(y, mo, int(m.group(3)), tzinfo=dt.timezone.utc)
        # mois seul : on prend la fin du mois (sans dépasser aujourd'hui) pour ne pas écarter un article récent
        nxt = dt.datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=dt.timezone.utc)
        return min(nxt - dt.timedelta(days=1), NOW)
    except ValueError:
        return None


def iso(d):
    return d.isoformat() if d else None


def age_days(iso_str):
    d = parse_date(iso_str)
    return None if d is None else (NOW - d).total_seconds() / 86400


def time_left(cfg):
    return cfg["limites"]["duree_max_minutes"] * 60 - (time.time() - START)


def clean_text(s):
    s = re.sub(r"[ \t ]+", " ", s or "")
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def html_to_text(h):
    if not h:
        return ""
    return clean_text(BeautifulSoup(h, "lxml").get_text("\n"))


# =====================================================================
# Réseau
# =====================================================================
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
})
_last_hit = {}
INSECURE_HOSTS = set()  # sites au certificat mal configuré (option « ssl_non_verifie » dans sources.yaml)


def polite(host, delay=1.5):
    t = _last_hit.get(host, 0)
    wait = delay - (time.time() - t)
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def http_get(url, timeout=30, stream=False, headers=None):
    host = urlparse(url).netloc
    for attempt in range(2):
        polite(host)
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True, stream=stream, headers=headers,
                            verify=base_host(url) not in INSECURE_HOSTS)
            if r.status_code in (429, 503) and attempt == 0:
                time.sleep(5)
                continue
            return r
        except requests.RequestException as e:
            log.debug("Erreur réseau %s : %s", url, e)
            time.sleep(2)
    return None


def jina_read(url, links=False):
    """Lecture d'une page via Jina Reader (gère le JavaScript). Gratuit, ~20 req/min sans clé."""
    headers = {"X-Return-Format": "markdown"}
    if links:
        headers["X-With-Links-Summary"] = "true"
    key = env("JINA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        polite("r.jina.ai", 3.2)
    try:
        r = requests.get("https://r.jina.ai/" + url, headers={**headers, "User-Agent": UA}, timeout=60)
        if r.status_code == 200:
            txt = r.text
            if "Markdown Content:" in txt:
                txt = txt.split("Markdown Content:", 1)[1]
            return txt.strip()
        log.debug("Jina %s -> HTTP %s", url, r.status_code)
    except requests.RequestException as e:
        log.debug("Jina erreur %s : %s", url, e)
    return ""


# =====================================================================
# Collecte : flux RSS, découverte automatique, pages de publications
# =====================================================================
AUDIO_RE = re.compile(r"\.(mp3|m4a|aac|ogg|opus|wav)(\?|$)", re.I)


def read_feed(url):
    """Lit un flux RSS/Atom. Renvoie (items, statut, date_plus_récente)."""
    r = http_get(url)
    if r is None:
        return [], "injoignable", None
    if r.status_code >= 400:
        return [], f"HTTP {r.status_code}", None
    fp = feedparser.parse(r.content)
    if not fp.entries:
        return [], "pas un flux valide / vide", None
    items, newest = [], None
    for e in fp.entries:
        link = e.get("link") or ""
        audio, duration = None, None
        for enc in e.get("enclosures", []) or []:
            href = enc.get("href") or enc.get("url")
            if href and ((enc.get("type") or "").startswith("audio") or AUDIO_RE.search(href)):
                audio = href
                break
        if not audio:
            for l in e.get("links", []) or []:
                if (l.get("type") or "").startswith("audio"):
                    audio = l.get("href")
                    break
        d = parse_date(e.get("published_parsed") or e.get("updated_parsed")) or \
            parse_date(e.get("published") or e.get("updated"))
        if d and d > NOW + dt.timedelta(days=2):
            d = None
        if d and (newest is None or d > newest):
            newest = d
        texts = [c.get("value", "") for c in (e.get("content") or [])]
        texts.append(e.get("summary", ""))
        feed_text = max((html_to_text(t) for t in texts), key=len, default="")
        if not link and audio:
            link = audio
        if not link:
            continue
        items.append({
            "url": link, "title": html_to_text(e.get("title", "")), "date": iso(d),
            "feed_text": feed_text[:20000], "audio": audio,
        })
    return items, f"OK ({len(items)} éléments)", newest


def discover_feeds(site_url):
    """Cherche les flux RSS d'un site (balises <link>, liens « RSS », adresses usuelles)."""
    found = []
    r = http_get(site_url)
    if r is not None and r.status_code < 400 and "html" in r.headers.get("content-type", ""):
        soup = BeautifulSoup(r.text, "lxml")
        for l in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            t = (l.get("type") or "").lower()
            if ("rss" in t or "atom" in t) and l.get("href"):
                found.append(urljoin(r.url, l["href"]))
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.search(r"(rss|/feed/?$|atom\.xml|backend)", h, re.I) and "comment" not in h:
                found.append(urljoin(r.url, h))
    base = f"{urlparse(site_url).scheme}://{urlparse(site_url).netloc}"
    for p in ("/feed/", "/rss.xml", "/rss", "/feed.xml", "/atom.xml", "/index.xml"):
        found.append(base + p)
    out, seen = [], set()
    for f in found:
        if "comments" in f or f in seen:
            continue
        seen.add(f)
        out.append(f)
    return out[:10]


DEFAULT_EXCLUDE = re.compile(
    r"/(tag|tags|category|categories|categorie|auteur|author|authors|experts?|people|staff|"
    r"events?|evenements?|agenda|search|recherche|login|contact|about|a-propos|qui-sommes-nous|"
    r"careers?|jobs?|emplois?|newsletter|donate|don|faire-un-don|subscribe|abonnement|"
    r"privacy|cookies?|mentions-legales|legal|page/\d+)(/|$)", re.I)
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|css|js|zip|mp4|ico|xml)(\?|$)", re.I)


def base_host(u):
    h = urlparse(u).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def looks_like_article(u):
    path = urlparse(u).path
    last = [s for s in path.split("/") if s]
    if not last:
        return False
    if re.search(r"/20\d\d/", path) and len(last[-1]) > 8:
        return True
    return last[-1].count("-") >= 3 or path.lower().endswith(".pdf")


def filter_links(pairs, page_url, pattern, exclude, limit):
    host = base_host(page_url)
    rx = re.compile(pattern, re.I) if pattern else None
    ex = re.compile(exclude, re.I) if exclude else None
    out, seen = [], set()
    for href, text in pairs:
        u = urljoin(page_url, href.strip()).split("#")[0]
        if not u.startswith("http") or SKIP_EXT.search(u):
            continue
        if norm_url(u) == norm_url(page_url):
            continue
        if rx:
            if not rx.search(u):
                continue
        else:
            bh = base_host(u)
            if not (bh == host or bh.endswith("." + host) or host.endswith("." + bh)):
                continue
            if DEFAULT_EXCLUDE.search(urlparse(u).path) or not looks_like_article(u):
                continue
        if ex and ex.search(u):
            continue
        k = norm_url(u)
        if k in seen:
            continue
        seen.add(k)
        out.append({"url": u, "title": clean_text(text)[:300], "date": None, "feed_text": "", "audio": None})
        if len(out) >= limit:
            break
    return out


def links_in_html(html, base, pattern, exclude, limit):
    """Liens d'articles d'une page HTML : balises <a>, puis adresses cachées dans le code (pages en JavaScript)."""
    soup = BeautifulSoup(html, "lxml")
    for junk in soup.find_all(["header", "footer", "nav"]):
        junk.decompose()
    zone = soup.find("main") or soup.body or soup
    links = filter_links([(a["href"], a.get_text(" ")) for a in zone.find_all("a", href=True)],
                         base, pattern, exclude, limit)
    if len(links) < 3 and zone is not soup:
        links = filter_links([(a["href"], a.get_text(" ")) for a in soup.find_all("a", href=True)],
                             base, pattern, exclude, limit)
    if len(links) < 3:
        txt = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
        cands = re.findall(r"(?:https?:)?//[^\s\"'<>\\]+|(?<=[\"'])/[A-Za-z0-9][^\s\"'<>\\]*", txt)
        raw = filter_links([(c, "") for c in cands], base, pattern, exclude, limit)
        if len(raw) > len(links):
            links = raw
    return links


def scrape_page(page, exclude, limit, history_days=0):
    """Liste les liens d'articles présents sur une page « publications » (et ses pages suivantes en rattrapage)."""
    url, pattern, render = page["url"], page.get("motif"), page.get("rendu", False)
    links, how = [], ""
    if not render:
        r = http_get(url)
        if r is not None and r.status_code < 400:
            links = links_in_html(r.text, r.url, pattern, exclude, limit)
            how = f"HTTP {r.status_code}"
        else:
            how = f"HTTP {r.status_code}" if r is not None else "injoignable"
    if len(links) < 3:
        md = jina_read(url, links=True)
        if md:
            pairs = re.findall(r"\[([^\]]{0,300})\]\((https?://[^)\s]+)\)", md)
            pairs = [(h, t) for t, h in pairs]
            links2 = filter_links(pairs, url, pattern, exclude, limit)
            if len(links2) > len(links):
                links, how = links2, (how + " + " if how else "") + "rendu Jina"
    # Rattrapage : pages suivantes de la liste (?page=2, /page/2/…)
    if history_days and page.get("pagination") and links:
        cutoff = NOW - dt.timedelta(days=history_days)
        known = {norm_url(l["url"]) for l in links}
        n_pages = 1
        for n in range(int(page.get("debut", 2)), int(page.get("pages_max", 15)) + 1):
            r = http_get(page["pagination"].format(n=n))
            if r is None or r.status_code >= 400:
                break
            more = [l for l in links_in_html(r.text, r.url, pattern, exclude, limit) if norm_url(l["url"]) not in known]
            if not more:
                break
            known.update(norm_url(l["url"]) for l in more)
            links += more
            n_pages += 1
            dates = [date_from_url(l["url"]) for l in more]
            if dates and all(dates) and min(dates) < cutoff:
                break
        how += f" ({n_pages} pages lues)"
    for l in links:
        l["from_page"] = True
    return links, f"{how} → {len(links)} liens"


def itunes_feeds(cfg, state):
    """Trouve les flux podcast d'un éditeur via l'annuaire Apple Podcasts (mis en cache 7 jours)."""
    cache = state.setdefault("itunes_cache", {})
    key = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    c = cache.get(key)
    if c and (age_days(c["date"]) or 99) < 7:
        return c["feeds"]
    feeds = []
    try:
        r = requests.get("https://itunes.apple.com/search", timeout=30, params={
            "term": cfg["recherche"], "media": "podcast", "entity": "podcast", "limit": 50})
        for res in r.json().get("results", []):
            who = simplify(res.get("artistName", "") + " " + res.get("collectionName", ""))
            if simplify(cfg.get("auteur_contient", cfg["recherche"])) in who and res.get("feedUrl"):
                feeds.append(res["feedUrl"])
    except Exception as e:  # noqa: BLE001
        log.warning("Recherche Apple Podcasts impossible : %s", e)
        return c["feeds"] if c else []
    cache[key] = {"date": iso(NOW), "feeds": feeds}
    return feeds


def read_wp_api(base, history_days=0):
    """Lit les articles d'un site WordPress via son API publique (/wp-json/wp/v2/posts) : dates + texte intégral."""
    per = 50
    after = (NOW - dt.timedelta(days=history_days or 30)).strftime("%Y-%m-%dT%H:%M:%S")
    items, status = [], "aucune réponse"
    for n in range(1, (12 if history_days else 1) + 1):
        url = (f"{base}{'&' if '?' in base else '?'}per_page={per}&page={n}&orderby=date&order=desc"
               f"&after={after}&_fields=date_gmt,date,link,title,content")
        r = http_get(url)
        if r is None or r.status_code >= 400:
            status = f"HTTP {r.status_code}" if r is not None else "injoignable"
            break
        try:
            data = r.json()
        except ValueError:
            status = "réponse illisible"
            break
        if not isinstance(data, list) or not data:
            break
        for d in data:
            items.append({"url": d.get("link", ""), "title": html_to_text((d.get("title") or {}).get("rendered", "")),
                          "date": iso(parse_date(d.get("date_gmt") or d.get("date"))),
                          "feed_text": html_to_text((d.get("content") or {}).get("rendered", ""))[:20000],
                          "audio": None})
        status = f"OK ({len(items)} articles)"
        if len(data) < per:
            break
    return [i for i in items if i["url"]], status


def feed_history(f, its, days):
    """Rattrapage : remonte les pages suivantes d'un flux (WordPress : ?paged=2, 3…) jusqu'à `days` jours."""
    known = {norm_url(i["url"]) for i in its}
    cutoff = NOW - dt.timedelta(days=days)
    extra = []
    for n in range(2, 31):
        oldest = min((parse_date(i["date"]) for i in its + extra if i.get("date")), default=None)
        if oldest is None or oldest < cutoff:
            break
        more, _, _ = read_feed(f + ("&" if "?" in f else "?") + f"paged={n}")
        more = [m for m in more if norm_url(m["url"]) not in known]
        if not more:
            break
        known.update(norm_url(m["url"]) for m in more)
        extra += more
    return extra


def collect_source(src, state, cfg, history_days=0):
    """Rassemble les éléments récents d'une source. Renvoie (items, lignes_de_rapport)."""
    lim = cfg["limites"]
    if src.get("ssl_non_verifie"):
        for u in [src.get("site")] + list(src.get("flux") or []) + [p["url"] for p in src.get("pages") or []]:
            if u:
                INSECURE_HOSTS.add(base_host(u))
    items, report = [], []
    feeds = list(src.get("flux") or [])
    if src.get("itunes"):
        found = itunes_feeds(src["itunes"], state)
        report.append(f"Apple Podcasts : {len(found)} flux trouvé(s)")
        feeds += found
    fresh_feed = False
    transient = False
    if src.get("wp_api"):
        its, status = read_wp_api(src["wp_api"], history_days)
        report.append(f"API WordPress {src['wp_api']} : {status}")
        if its:
            items += its
            fresh_feed = True
    for f in feeds:
        its, status, newest = read_feed(f)
        if re.match(r"HTTP (429|5\d\d)|injoignable", status):
            transient = True
        fresh = bool(its) and (newest is None or (NOW - newest).days <= 60)
        if its and not fresh:
            status += f" — PÉRIMÉ (dernier : {newest:%d/%m/%Y})"
        elif newest:
            status += f" — dernier : {newest:%d/%m/%Y}"
        report.append(f"flux {f} : {status}")
        if fresh:
            items += its
            fresh_feed = True
            if history_days and not src.get("podcast"):
                older = feed_history(f, its, history_days)
                if older:
                    report.append(f"  rattrapage : +{len(older)} publications plus anciennes")
                    items += older
    pages = list(src.get("pages") or [])
    if not fresh_feed and not transient and src.get("site") and not src.get("podcast"):
        # Découverte automatique d'un flux (résultat mémorisé 7 jours pour gagner du temps)
        cache = state.setdefault("discovered", {})
        c = cache.get(src["nom"])
        if c and (age_days(c["date"]) or 99) < 7:
            candidates = [c["feed"]] if c.get("feed") else []
        else:
            candidates = [f for f in discover_feeds(src["site"]) if f not in feeds]
            cache[src["nom"]] = {"date": iso(NOW), "feed": None}
        for f in candidates:
            its, status, newest = read_feed(f)
            if its and (newest is None or (NOW - newest).days <= 60):
                report.append(f"flux découvert automatiquement {f} : {status}")
                items += its
                fresh_feed = True
                cache[src["nom"]] = {"date": iso(NOW), "feed": f}
                break
        if candidates and not fresh_feed:
            report.append("aucun flux RSS récent trouvé automatiquement")
        if not fresh_feed and not pages:
            pages = [{"url": src["site"]}]
    for p in pages:
        links, status = scrape_page(p, src.get("exclure"), lim["liens_max_par_page"], history_days)
        report.append(f"page {p['url']} : {status}")
        items += links
    if src.get("podcast"):
        counts = {}
        for it in items:
            counts[it["url"]] = counts.get(it["url"], 0) + 1
        for it in items:
            if it.get("audio") and counts[it["url"]] > 1:
                it["url"] = it["audio"]  # lien identique pour tous les épisodes : on identifie par le fichier audio
    for it in items:
        if not it.get("date"):
            it["date"] = iso(date_from_url(it["url"]))
        it["source"] = src["nom"]
        it["podcast"] = bool(src.get("podcast"))
        it["langue_audio"] = src.get("langue_audio")
    if src.get("exclure"):
        ex = re.compile(src["exclure"], re.I)
        items = [i for i in items if not ex.search(i["url"])]
    return items, report


# =====================================================================
# Lecture du texte intégral
# =====================================================================
def pdf_to_text(data, max_pages=60):
    try:
        import pymupdf
    except ImportError:  # anciennes versions
        import fitz as pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        parts = [doc[i].get_text() for i in range(min(len(doc), max_pages))]
        return clean_text("\n".join(parts))
    except Exception as e:  # noqa: BLE001
        log.debug("PDF illisible : %s", e)
        return ""


def fetch_fulltext(item):
    """Renvoie (texte, méthode, métadonnées). Essaie : flux → page HTML → PDF → Jina."""
    feed_text = item.get("feed_text") or ""
    meta = {}
    if len(feed_text) >= 3000:
        return feed_text, "flux (texte complet)", meta
    best, method = "", ""
    url = item["url"]
    r = http_get(url, timeout=45)
    if r is not None and r.status_code < 400:
        ctype = r.headers.get("content-type", "").lower()
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            best, method = pdf_to_text(r.content), "PDF"
        else:
            html = r.text
            txt = trafilatura.extract(html, url=r.url, include_comments=False, include_tables=True,
                                      favor_recall=True) or ""
            best, method = clean_text(txt), "page web"
            try:
                md = trafilatura.extract_metadata(html)
                if md:
                    meta = {"title": md.title, "date": md.date}
            except Exception:  # noqa: BLE001
                pass
            # Page de présentation d'un rapport : on lit le PDF associé
            if len(best) < 2500:
                soup = BeautifulSoup(html, "lxml")
                pdfs = [urljoin(r.url, a["href"]) for a in soup.find_all("a", href=True)
                        if re.search(r"\.pdf(\?|$)", a["href"], re.I)]
                pdfs = [p for p in pdfs if base_host(p) == base_host(r.url)] or pdfs
                if pdfs:
                    pr = http_get(pdfs[0], timeout=60)
                    if pr is not None and pr.status_code < 400:
                        ptxt = pdf_to_text(pr.content)
                        if len(ptxt) > len(best):
                            best, method = ptxt, "PDF associé"
    if len(best) < 1200:
        jt = jina_read(url)
        if len(jt) > len(best) * 1.3:
            best, method = clean_text(jt), "page web (via Jina)"
    if len(feed_text) > len(best):
        best, method = feed_text, "résumé du flux"
    return best, method, meta


# =====================================================================
# Transcription des podcasts (Groq Whisper, gratuit)
# =====================================================================
def transcribe(audio_url, lang=None):
    key = env("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY manquante : transcription impossible")
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "source")
        with requests.get(audio_url, stream=True, timeout=120, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            size = 0
            with open(src, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > 500 * 1024 * 1024:
                        raise RuntimeError("fichier audio trop volumineux")
                    f.write(chunk)
        # mono 16 kHz 32 kb/s, découpé en tranches de 10 min (~2,4 Mo chacune)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1",
                        "-ar", "16000", "-b:a", "32k", "-f", "segment", "-segment_time", "600",
                        os.path.join(tmp, "part%03d.mp3")], check=True, timeout=900)
        parts = sorted(p for p in os.listdir(tmp) if p.startswith("part"))
        texts = []
        for p in parts:
            texts.append(_groq_transcribe_file(os.path.join(tmp, p), lang, key))
        return clean_text("\n".join(texts))


def _groq_transcribe_file(path, lang, key):
    for attempt in range(6):
        with open(path, "rb") as f:
            data = {"model": env("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"), "response_format": "text",
                    "temperature": "0"}
            if lang:
                data["language"] = lang
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {key}"},
                              files={"file": (os.path.basename(path), f, "audio/mpeg")}, data=data, timeout=300)
        if r.status_code == 200:
            return r.text
        if r.status_code == 429:
            wait = float(r.headers.get("retry-after", "30") or 30)
            if wait > 90:
                raise QuotaExhausted(f"quota de transcription Groq atteint ({r.text[:200]})")
            time.sleep(wait + 1)
            continue
        if r.status_code >= 500:
            time.sleep(10)
            continue
        raise RuntimeError(f"Transcription Groq : HTTP {r.status_code} {r.text[:300]}")
    raise QuotaExhausted("transcription Groq indisponible")


# =====================================================================
# IA : synthèse + classement (Mistral ou Gemini, repli Groq)
# =====================================================================
BAD_MODEL_WORDS = ("image", "tts", "audio", "live", "embedding", "native", "exp", "computer", "robotics",
                   "veo", "imagen", "aqa", "learnlm", "lyria", "vision", "nano", "e2b", "e4b", "1b")


def _version_key(name):
    nums = re.findall(r"(\d+(?:\.\d+)?)", name)
    v = float(nums[0]) if nums else 0
    size = float(nums[-1]) if "gemma" in name and len(nums) > 1 else 0
    return (v, size, "preview" not in name, "latest" in name)


class LLM:
    def __init__(self, state, cfg):
        self.state = state
        self.cfg = cfg
        self.gkey = env("GEMINI_API_KEY")
        self.qkey = env("GROQ_API_KEY")
        self.mkey = env("MISTRAL_API_KEY")  # si présente, Mistral remplace Gemini (une seule clé pour les 3 veilles)
        self._m_last = 0.0
        today = NOW.strftime("%Y-%m-%d")
        ex = state.setdefault("exhausted", {})
        if not state.get("quota_fix_v1"):  # une ancienne version marquait à tort des modèles comme épuisés
            ex.clear()
            state["quota_fix_v1"] = True
        for k in [k for k, v in ex.items() if v != today]:
            del ex[k]
        self.exhausted = ex
        self.today = today
        self._gemini = None
        self._groq = None
        self.calls = {}
        self.skip = set()  # modèles en erreur pendant ce passage

    # ----- liste des modèles -----
    def gemini_models(self):
        if self._gemini is not None:
            return self._gemini
        if self.mkey:
            # Deux modèles fixes, un par usage (pas de changement de modèle pour contourner une limite) :
            # « léger » pour le tri, les notes et les résumés d'articles ; « principal » pour les fiches.
            self._gemini = [env("MISTRAL_MODELE_LEGER", "mistral-small-latest") + "~lite",
                            env("MISTRAL_MODELE", "mistral-large-latest")]
            return self._gemini
        forced = env("GEMINI_MODELS")
        if forced:
            self._gemini = [m.strip() for m in forced.split(",") if m.strip()]
            return self._gemini
        models = []
        if self.gkey:
            try:
                r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                                 params={"pageSize": 1000}, headers={"x-goog-api-key": self.gkey}, timeout=30)
                for m in r.json().get("models", []):
                    name = m["name"].split("/", 1)[-1]
                    if "generateContent" not in m.get("supportedGenerationMethods", []):
                        continue
                    low = name.lower()
                    if not ("flash" in low or "gemma" in low) or any(w in low for w in BAD_MODEL_WORDS):
                        continue
                    if "gemma" in low and not low.endswith("-it"):
                        continue
                    models.append(name)
            except Exception as e:  # noqa: BLE001
                log.warning("Liste des modèles Gemini indisponible : %s", e)
        lite = sorted([m for m in models if "lite" in m and "gemma" not in m], key=_version_key, reverse=True)
        flash = sorted([m for m in models if "lite" not in m and "gemma" not in m], key=_version_key, reverse=True)
        # Un seul modèle par usage (le plus récent « lite » et le plus récent « flash ») : pas d'enchaînement
        # de modèles pour cumuler les quotas gratuits.
        self._gemini = lite[:1] + flash[:1]
        return self._gemini

    def groq_models(self):
        if self._groq is not None:
            return self._groq
        wanted = [m.strip() for m in env("GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",")]
        avail = set()
        if self.qkey:
            try:
                r = requests.get("https://api.groq.com/openai/v1/models",
                                 headers={"Authorization": f"Bearer {self.qkey}"}, timeout=30)
                avail = {m["id"] for m in r.json().get("data", [])}
            except Exception as e:  # noqa: BLE001
                log.warning("Liste des modèles Groq indisponible : %s", e)
        if avail:
            extra = sorted(m for m in avail if re.search(r"qwen|llama-4|llama-3\.3|kimi", m, re.I)
                           and not re.search(r"guard|whisper|tts|vision", m, re.I))
            self._groq = [m for m in wanted if m in avail] + [m for m in extra if m not in wanted]
        else:
            self._groq = wanted if self.qkey else []
        return self._groq

    # ----- prompt -----
    def build_prompt(self, docs):
        c = self.cfg
        lines = [
            "Tu es un analyste de veille stratégique et géopolitique. Pour CHAQUE document ci-dessous, "
            f"rédige en {c['langue_synthese']} une fiche de veille fidèle au texte (n'invente rien).",
            "Champs attendus pour chaque document :",
            '- "id" : l\'identifiant fourni (texte)',
            '- "titre_fr" : le titre, traduit en français s\'il est dans une autre langue',
            '- "resume" : synthèse de 5 à 8 phrases : thèse principale, arguments, faits et chiffres clés, '
            "conclusions ou recommandations. Pour un podcast, résume la discussion et nomme les intervenants.",
            '- "points_cles" : liste de 3 à 5 points clés (phrases courtes) : les arguments principaux du document',
            '- "chiffres" : 0 à 5 données chiffrées clés présentes dans le document, chacune en une phrase avec '
            'unité, date et acteur (ex. « Les dépenses de défense de l\'UE ont atteint 343 Md€ en 2024 (+19 %) ») ; '
            '[] s\'il n\'y en a pas',
            f'- "disciplines" : 1 à 3 disciplines dont relève le document, choisies UNIQUEMENT dans : '
            f'{json.dumps(c["disciplines"], ensure_ascii=False)}',
            f'- "themes" : 1 à 5 étiquettes thématiques précises, choisies UNIQUEMENT dans : '
            f'{json.dumps(c["themes"], ensure_ascii=False)}',
            '- "pays" : pays principalement concernés (0 à 6), sous la forme [{"nom": "France", "region": "Europe"}] ; '
            'noms usuels en français ("États-Unis", "Chine", "Royaume-Uni"…) ; "region" choisie UNIQUEMENT dans : '
            f'{json.dumps(c["regions"], ensure_ascii=False)} ; pour l\'UE en tant qu\'institution : '
            '{"nom": "Union européenne", "region": "Europe"}',
            f'- "regions" : 1 à 3 éléments choisis UNIQUEMENT dans : {json.dumps(c["regions"], ensure_ascii=False)}',
            f'- "type" : un seul élément choisi dans : {json.dumps(c["types"], ensure_ascii=False)}',
            '- "langue" : code de la langue du document (fr, en, de…)',
            'Réponds UNIQUEMENT avec un objet JSON de la forme {"documents": [ {...}, {...} ]}, '
            "avec une fiche par document, sans aucun texte autour.",
            "",
        ]
        for d in docs:
            lines += [f"=== DOCUMENT id={d['id']} ===",
                      f"Source : {d['source']}", f"Titre : {d['title']}",
                      f"Date : {d.get('date') or 'inconnue'}", f"Adresse : {d['url']}",
                      "Texte :", d["text"], ""]
        return "\n".join(lines)

    # ----- appels -----
    def summarize(self, docs):
        """docs : liste de dicts {id, source, title, date, url, text}. Renvoie {id: fiche}."""
        prompt = self.build_prompt(docs)
        for model in self.gemini_models():
            if model in self.exhausted or model in self.skip:
                continue
            try:
                out = self._call_gemini(model, prompt)
                return self._index(out)
            except QuotaExhausted as e:
                log.info("IA %s : quota épuisé (%s) → modèle suivant", model, str(e)[:120])
                self.exhausted[model] = self.today
            except (ValueError, KeyError) as e:
                log.warning("IA %s : réponse illisible (%s)", model, e)
            except (RuntimeError, requests.RequestException) as e:
                log.warning("IA %s indisponible : %s", model, str(e)[:200])
                self.skip.add(model)
        # Repli Groq : un document à la fois (limite de 8 000 jetons/minute)
        results = {}
        for d in docs:
            small = dict(d, text=d["text"][:14000])
            p = self.build_prompt([small])
            done = False
            for model in self.groq_models():
                if "groq:" + model in self.exhausted or "groq:" + model in self.skip:
                    continue
                try:
                    out = self._call_groq(model, p)
                    results.update(self._index(out))
                    done = True
                    break
                except QuotaExhausted:
                    log.info("Groq %s : quota du jour épuisé → modèle suivant", model)
                    self.exhausted["groq:" + model] = self.today
                except (ValueError, KeyError) as e:
                    log.warning("Groq %s : réponse illisible (%s)", model, e)
                except (RuntimeError, requests.RequestException) as e:
                    log.warning("Groq %s indisponible : %s", model, str(e)[:200])
                    if "trop long" not in str(e):
                        self.skip.add("groq:" + model)
            if not done:
                if results:
                    return results
                raise QuotaExhausted("tous les modèles gratuits sont épuisés pour aujourd'hui")
        return results

    def ask_json(self, prompt, max_tokens=16384):
        """Question libre (fiches de révision) : modèles Gemini « flash » d'abord (meilleure qualité), puis « lite »."""
        models = self.gemini_models()
        ordered = [m for m in models if "lite" not in m and "gemma" not in m] + [m for m in models if "lite" in m]
        for model in ordered:
            if model in self.exhausted or model in self.skip:
                continue
            try:
                return self._call_gemini(model, prompt, max_tokens)
            except QuotaExhausted as e:
                log.info("IA %s : quota épuisé (%s) → modèle suivant", model, str(e)[:120])
                self.exhausted[model] = self.today
            except (ValueError, KeyError) as e:
                log.warning("IA %s : réponse illisible (%s)", model, e)
            except (RuntimeError, requests.RequestException) as e:
                log.warning("IA %s indisponible : %s", model, str(e)[:200])
                self.skip.add(model)
        raise QuotaExhausted("aucun modèle Gemini disponible pour les fiches")

    @staticmethod
    def _index(out):
        docs = out.get("documents") if isinstance(out, dict) else out
        if isinstance(docs, dict):
            docs = [docs]
        if not isinstance(docs, list):
            raise ValueError("format inattendu")
        return {str(d.get("id")): d for d in docs if isinstance(d, dict)}

    def provider(self):
        return "Mistral" if self.mkey else "Gemini"

    @staticmethod
    def is_mistral(model):
        return model.endswith("~lite") or "istral" in model

    def _call_gemini(self, model, prompt, max_tokens=8192):
        if self.is_mistral(model):
            return self._call_mistral(model, prompt, max_tokens)
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}}
        if "gemma" not in model:
            body["generationConfig"]["responseMimeType"] = "application/json"
        # Réflexion minimale : réponses beaucoup plus rapides (et moins de minutes GitHub consommées)
        if re.search(r"gemini-[3-9]", model):
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
        elif "2.5" in model:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0 if "lite" in model else 512}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        last = ""
        for attempt in range(4):
            r = requests.post(url, json=body, headers={"x-goog-api-key": self.gkey}, timeout=300)
            self.calls[model] = self.calls.get(model, 0) + 1
            if r.status_code == 200:
                data = r.json()
                cand = (data.get("candidates") or [{}])[0]
                parts = cand.get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not text:
                    raise ValueError(f"réponse vide ({cand.get('finishReason')})")
                return parse_json(text)
            msg = r.text
            last = f"HTTP {r.status_code} {msg[:200]}"
            if r.status_code == 429:
                if re.search(r"PerDay|per day|limit: 0\b|\"limit\": 0", msg, re.I):
                    for m in self.gemini_models():  # quota du jour atteint : plus d'appel Gemini aujourd'hui
                        self.exhausted[m] = self.today
                    raise QuotaExhausted(msg[:300])
                m = re.search(r'"retryDelay":\s*"(\d+)', msg)
                wait = int(m.group(1)) if m else 30
                if wait > 90:
                    raise QuotaExhausted(msg[:300])
                time.sleep(wait + 1)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(20)
                continue
            if r.status_code == 400 and "thinkingConfig" in body["generationConfig"] and "think" in msg.lower():
                del body["generationConfig"]["thinkingConfig"]
                continue
            if r.status_code == 400 and "responseMimeType" in body["generationConfig"] and "mime" in msg.lower():
                del body["generationConfig"]["responseMimeType"]
                continue
            raise RuntimeError(f"HTTP {r.status_code} {msg[:300]}")
        # Refus répétés (limite par minute, serveur surchargé) : l'IA Gemini est mise de côté pour ce passage
        if "HTTP 429" in last:
            self.skip.update(self.gemini_models())
        raise RuntimeError(f"temporairement indisponible ({last})")

    def _call_mistral(self, model, prompt, max_tokens=8192):
        """Mistral (offre gratuite « Experiment ») : environ 1 requête par seconde. Le programme respecte
        ce rythme et, en cas de refus, ATTEND le délai indiqué ; si la limite persiste, il arrête l'IA
        Mistral pour ce passage (aucun autre modèle n'est essayé pour la contourner)."""
        name = model.split("~")[0]
        body = {"model": name, "temperature": 0.2, "max_tokens": min(int(max_tokens), 32000),
                "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        interval = float(env("MISTRAL_INTERVALLE_SECONDES", "2"))
        waited, last = 0.0, ""
        for attempt in range(8):
            gap = interval - (time.time() - self._m_last)
            if gap > 0:
                time.sleep(gap)
            self._m_last = time.time()
            r = requests.post("https://api.mistral.ai/v1/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.mkey}"}, timeout=600)
            self.calls[name] = self.calls.get(name, 0) + 1
            if r.status_code == 200:
                ch = (r.json().get("choices") or [{}])[0]
                content = (ch.get("message") or {}).get("content") or ""
                if isinstance(content, list):  # réponse en morceaux (modèles « raisonnants »)
                    content = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if not content:
                    raise ValueError(f"réponse vide ({ch.get('finish_reason')})")
                return parse_json(content)
            msg = r.text
            last = f"HTTP {r.status_code} {msg[:200]}"
            if r.status_code == 429:
                if re.search(r"month|monthly|mensuel", msg, re.I):
                    for m in self.gemini_models():  # quota mensuel atteint : plus d'appel Mistral aujourd'hui
                        self.exhausted[m] = self.today
                    raise QuotaExhausted("Mistral : quota mensuel de l'offre gratuite atteint")
                try:
                    wait = float(r.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                wait = wait or min(60.0, 5.0 * 2 ** attempt)
                waited += wait
                if waited > 300:
                    break
                log.info("Mistral : limite de débit atteinte, attente de %.0f s", wait)
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(20)
                continue
            if r.status_code == 400 and "response_format" in body and "response_format" in msg:
                del body["response_format"]
                continue
            if r.status_code == 400 and "max_tokens" in msg and body["max_tokens"] > 8192:
                body["max_tokens"] = 8192
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"clé Mistral refusée (HTTP {r.status_code}) : vérifiez le secret MISTRAL_API_KEY")
            raise RuntimeError(f"HTTP {r.status_code} {msg[:300]}")
        # Limite persistante : l'IA Mistral est mise de côté jusqu'au prochain passage
        self.skip.update(m for m in self.gemini_models() if self.is_mistral(m))
        raise RuntimeError(f"Mistral : limite de débit persistante, reprise au prochain passage ({last})")

    def _call_groq(self, model, prompt):
        body = {"model": model, "temperature": 0.2, "max_completion_tokens": 2500,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}}
        for attempt in range(4):
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.qkey}"}, timeout=180)
            self.calls["groq:" + model] = self.calls.get("groq:" + model, 0) + 1
            if r.status_code == 200:
                return parse_json(r.json()["choices"][0]["message"]["content"])
            msg = r.text
            if r.status_code == 429:
                if re.search(r"per day|\(RPD\)|\(TPD\)", msg, re.I):
                    raise QuotaExhausted(msg[:300])
                wait = float(r.headers.get("retry-after", "20") or 20)
                if wait > 90:
                    raise QuotaExhausted(msg[:300])
                time.sleep(wait + 1)
                continue
            if r.status_code == 413:
                raise RuntimeError("texte trop long pour Groq")
            if r.status_code == 400 and "response_format" in body:
                del body["response_format"]
                continue
            if r.status_code >= 500:
                time.sleep(10)
                continue
            raise RuntimeError(f"HTTP {r.status_code} {msg[:300]}")
        raise QuotaExhausted("trop de refus successifs")


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        a, b = t.find("["), t.rfind("]")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise ValueError("JSON introuvable dans la réponse")


def pick(values, allowed, maxn):
    """Ne garde que les étiquettes autorisées (tolère accents / majuscules)."""
    stop = {"et", "and", "de", "des", "du", "la", "le", "les", "l", "d", "en", "of", "the"}

    def toks(x):
        return frozenset(t for t in simplify(x).split() if t not in stop)

    idx = {toks(a): a for a in allowed}
    out = []
    for v in values or []:
        if not isinstance(v, str):
            continue
        s = toks(v)
        hit = idx.get(s)
        if not hit and s:
            for k, a in idx.items():
                if k and (s <= k or k <= s):
                    hit = a
                    break
        if hit and hit not in out:
            out.append(hit)
    return out[:maxn]


def truncate_for_ai(text, n):
    if len(text) <= n:
        return text
    head = int(n * 0.8)
    return text[:head] + "\n[…]\n" + text[-(n - head):]


# =====================================================================
# Notion
# =====================================================================
THEME_COLORS = ["blue", "red", "orange", "purple", "green", "yellow", "pink", "brown", "gray", "default"]


def rt(text):
    """Texte Notion (morceaux de 2 000 caractères max)."""
    text = text or ""
    return [{"type": "text", "text": {"content": text[i:i + 1900]}} for i in range(0, len(text), 1900)][:90] or \
        [{"type": "text", "text": {"content": ""}}]


def opt(name):
    return re.sub(r"\s+", " ", (name or "").replace(",", " ")).strip()[:100]


class Notion:
    API = "https://api.notion.com/v1"

    def __init__(self, cfg):
        self.token = env("NOTION_TOKEN")
        self.page = env("NOTION_PAGE_ID")
        self.db = env("NOTION_DATABASE_ID")
        self.cfg = cfg
        self.h = {"Authorization": f"Bearer {self.token}", "Notion-Version": env("NOTION_VERSION", "2022-06-28"),
                  "Content-Type": "application/json"}

    def req(self, method, path, body=None):
        for attempt in range(5):
            time.sleep(0.35)
            r = requests.request(method, self.API + path, headers=self.h, json=body, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("retry-after", "3") or 3) + attempt * 2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Notion {method} {path} : HTTP {r.status_code} {r.text[:400]}")
            return r.json()
        raise RuntimeError("Notion ne répond pas")

    @staticmethod
    def clean_id(x):
        """Identifiant Notion à partir d'un identifiant ou d'un lien (…/Veille-1a2b…?pvs=4)."""
        last = x.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1].replace("-", "")
        m = re.search(r"([0-9a-fA-F]{32})$", last)
        return m.group(1).lower() if m else x

    @staticmethod
    def is_our_db(d):
        """Reconnaît le tableau de veille à ses colonnes (et non à son nom, que vous pouvez changer)."""
        props = d.get("properties") or {}
        return (not d.get("archived") and not d.get("in_trash")
                and props.get("Lien", {}).get("type") == "url" and "Lecture" in props and "Titre original" in props)

    def ensure_db(self, known_id=None):
        if not self.token:
            raise RuntimeError("NOTION_TOKEN manquant")
        if self.db:
            self.db = self.clean_id(self.db)
            return self.db
        if known_id:  # tableau mémorisé lors des passages précédents
            try:
                d = self.req("GET", f"/databases/{known_id}")
                if self.is_our_db(d):
                    self.db = d["id"]
                    return self.db
            except RuntimeError:
                pass
        found, cursor = [], None
        while True:
            body = {"filter": {"property": "object", "value": "database"}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = self.req("POST", "/search", body)
            found += [d for d in res.get("results", []) if self.is_our_db(d)]
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        if found:
            page = self.clean_id(self.page) if self.page else ""
            found.sort(key=lambda d: (d.get("parent", {}).get("page_id", "").replace("-", "") != page,
                                      d.get("created_time", "")))
            self.db = found[0]["id"]
            return self.db
        if not self.page:
            raise RuntimeError("NOTION_PAGE_ID manquant : impossible de créer la base")
        c = self.cfg
        # L'ordre ci-dessous est l'ordre des colonnes à la création de la base
        props = {
            "Titre": {"title": {}},
            "Thèmes": {"multi_select": {"options": [{"name": opt(t), "color": c["theme_colors"].get(t, "default")}
                                                     for t in c["themes"]]}},
            "Discipline": {"multi_select": {"options": [{"name": opt(t), "color": c["discipline_color"]}
                                                         for t in c["disciplines"]]}},
            "Pays": {"multi_select": {}},
            "Région": {"multi_select": {"options": [{"name": opt(t), "color": c["region_colors"].get(t, "default")}
                                                     for t in c["regions"]]}},
            "Source": {"select": {}},
            "Type": {"select": {"options": [{"name": opt(t)} for t in c["types"]]}},
            "Date": {"date": {}},
            "Résumé": {"rich_text": {}},
            "Lien": {"url": {}},
            "Lu": {"checkbox": {}},
            "Lecture": {"select": {"options": [{"name": "Texte intégral", "color": "green"},
                                               {"name": "Transcription audio", "color": "blue"},
                                               {"name": "Extrait seulement", "color": "orange"}]}},
            "Titre original": {"rich_text": {}},
            "Ajouté le": {"created_time": {}},
        }
        d = self.req("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": self.clean_id(self.page)},
            "title": [{"type": "text", "text": {"content": DB_TITLE}}],
            "is_inline": True, "properties": props})
        self.db = d["id"]
        log.info("Base Notion créée : %s", d.get("url"))
        return self.db

    def load_schema(self):
        """Mémorise les options existantes (pays, thèmes…) pour leur attribuer une couleur à la création."""
        d = self.req("GET", f"/databases/{self.db}")
        self.props = d.get("properties", {})
        self.opts = {}
        for name, pr in self.props.items():
            if pr.get("type") in ("multi_select", "select"):
                self.opts[name] = {o["name"]: o for o in pr[pr["type"]].get("options", [])}

    def ensure_options(self, prop, wanted):
        """Ajoute à la colonne `prop` les options manquantes [(nom, couleur)], sans toucher aux existantes."""
        if not hasattr(self, "opts"):
            self.load_schema()
        if prop not in self.props:
            return
        known = self.opts.setdefault(prop, {})
        missing = [(n, c) for n, c in wanted if n and n not in known]
        if not missing:
            return
        if len(known) + len(missing) > 100:
            # Notion n'accepte pas plus de 100 options par mise à jour : les nouveaux pays
            # sont alors créés automatiquement par Notion, avec une couleur par défaut.
            return
        kind = self.props[prop]["type"]
        options = [{"id": o["id"], "name": o["name"], "color": o.get("color", "default")} for o in known.values()]
        options += [{"name": n, "color": c or "default"} for n, c in missing]
        try:
            d = self.req("PATCH", f"/databases/{self.db}", {"properties": {prop: {kind: {"options": options}}}})
            pr = d["properties"][prop]
            self.opts[prop] = {o["name"]: o for o in pr[kind].get("options", [])}
        except RuntimeError as e:
            log.warning("Couleurs Notion non appliquées (%s) : %s", prop, e)

    def all_pages(self):
        pages, cursor = [], None
        while True:
            body = {"page_size": 100, "sorts": [{"timestamp": "created_time", "direction": "ascending"}]}
            if cursor:
                body["start_cursor"] = cursor
            res = self.req("POST", f"/databases/{self.db}/query", body)
            pages += res.get("results", [])
            if not res.get("has_more"):
                return pages
            cursor = res.get("next_cursor")

    def remove_duplicates(self, st=None):
        """Met à la corbeille Notion (récupérable 30 jours) les fiches en double ; garde la plus ancienne."""
        def text(p, name):
            pr = p["properties"].get(name) or {}
            if pr.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in pr["title"])
            if pr.get("type") == "rich_text":
                return "".join(t.get("plain_text", "") for t in pr["rich_text"])
            if pr.get("type") == "select":
                return (pr["select"] or {}).get("name", "")
            if pr.get("type") == "url":
                return pr.get("url") or ""
            return ""
        keys, by_source, removed = set(), {}, 0
        for p in self.all_pages():
            src, titre, orig, lien = text(p, "Source"), text(p, "Titre"), text(p, "Titre original"), text(p, "Lien")
            ks = {("u", norm_url(lien))} if lien else set()
            if len(simplify(titre)) >= 15:
                ks.add(("t", src, simplify(titre)))
            if len(simplify(orig)) >= 15:
                ks.add(("o", src, simplify(orig)))
            toks = title_tokens(titre)
            dup = bool(ks & keys) or (len(toks) >= 4 and any(jaccard(toks, t) >= 0.75 for t in by_source.get(src, [])))
            if dup:
                self.req("PATCH", f"/pages/{p['id']}", {"archived": True})
                removed += 1
                log.info("  doublon supprimé de Notion : %s", titre)
                continue
            keys |= ks
            by_source.setdefault(src, []).append(toks)
            if st is not None:  # mémorise les fiches existantes pour éviter de futurs doublons
                remember_titles(st, {"source": src, "title": orig}, {"titre_fr": titre})
        return removed

    def exists(self, url):
        res = self.req("POST", f"/databases/{self.db}/query",
                       {"filter": {"property": "Lien", "url": {"equals": url}}, "page_size": 1})
        return bool(res.get("results"))

    def set_relation(self, page_id, ids):
        self.req("PATCH", f"/pages/{page_id}", {"properties": {"Articles liés": {
            "relation": [{"id": i} for i in ids[:8]]}}})

    def ensure_relation(self):
        """Ajoute au tableau la colonne « Articles liés » (lien vers les articles sur le même sujet)."""
        if "Articles liés" not in self.props:
            self.req("PATCH", f"/databases/{self.db}", {"properties": {"Articles liés": {
                "relation": {"database_id": self.db, "single_property": {}}}}})
            self.load_schema()

    def add(self, item, fiche, lecture, transcript=None, related=None):
        title = fiche.get("titre_fr") or item.get("title") or item["url"]
        props = {
            "Titre": {"title": rt(title[:1900])[:1]},
            "Source": {"select": {"name": opt(item["source"])}},
            "Lien": {"url": item["url"][:1999]},
            "Résumé": {"rich_text": rt(fiche.get("resume", "")[:1990])[:1]},
            "Lecture": {"select": {"name": lecture}},
            "Titre original": {"rich_text": rt((item.get("title") or "")[:1900])[:1]},
        }
        if fiche.get("type"):
            props["Type"] = {"select": {"name": opt(fiche["type"])}}
        c = self.cfg
        props["Thèmes"] = {"multi_select": [{"name": opt(t)} for t in fiche.get("themes", [])]}
        if fiche.get("disciplines") is not None and "Discipline" in getattr(self, "props", {"Discipline": 1}):
            props["Discipline"] = {"multi_select": [{"name": opt(t)} for t in fiche.get("disciplines", [])]}
        props["Région"] = {"multi_select": [{"name": opt(t)} for t in fiche.get("regions", [])]}
        pays = fiche.get("pays", [])
        self.ensure_options("Pays", [(n, c["region_colors"].get(r, "gray") if r else "gray") for n, r in pays])
        props["Pays"] = {"multi_select": [{"name": n} for n, _ in pays]}
        if item.get("date"):
            props["Date"] = {"date": {"start": item["date"][:10]}}
        if related and "Articles liés" in getattr(self, "props", {}):
            props["Articles liés"] = {"relation": [{"id": i} for i in related[:8]]}
        children = [
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rt("Synthèse")}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(fiche.get("resume", ""))}},
        ]
        if fiche.get("points_cles"):
            children.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rt("Points clés")}})
            for p in fiche["points_cles"][:8]:
                children.append({"object": "block", "type": "bulleted_list_item",
                                 "bulleted_list_item": {"rich_text": rt(str(p))}})
        if fiche.get("chiffres"):
            children.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rt("Chiffres clés")}})
            for p in fiche["chiffres"][:6]:
                children.append({"object": "block", "type": "bulleted_list_item",
                                 "bulleted_list_item": {"rich_text": rt(str(p))}})
        children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "→ Lire la publication originale", "link": {"url": item["url"][:1999]}}}]}})
        if transcript:
            paras = [transcript[i:i + 1900] for i in range(0, len(transcript), 1900)][:95]
            children.append({"object": "block", "type": "heading_3", "heading_3": {
                "rich_text": rt("Transcription intégrale (cliquer pour déplier)"), "is_toggleable": True,
                "children": [{"object": "block", "type": "paragraph",
                              "paragraph": {"rich_text": rt(p)}} for p in paras]}})
        return self.req("POST", "/pages", {"parent": {"database_id": self.db}, "properties": props,
                                           "children": children})


# =====================================================================
# Mémoire (articles déjà traités, file d'attente)
# =====================================================================
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("seen", {})
    st.setdefault("pending", [])
    st.setdefault("initialized", False)
    return st


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    cutoff = (NOW - dt.timedelta(days=400)).strftime("%Y%m%d")
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    st["pending"] = [{k: v for k, v in p.items() if not k.startswith("_")} for p in st["pending"][-3000:]]
    st = {k: v for k, v in st.items() if not k.startswith("_")}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def title_tokens(t):
    return {w for w in simplify(t).split() if len(w) > 2}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def title_key(source, title):
    return hashlib.sha1((simplify(source) + "|" + simplify(title)).encode()).hexdigest()[:16]


def known_title(st, source, title):
    return bool(title) and len(simplify(title)) >= 15 and title_key(source, title) in st.setdefault("titles", {})


def is_near_duplicate(st, source, titre_fr):
    """Même source + titre français presque identique (ex. version allemande et anglaise d'une même étude)."""
    toks = title_tokens(titre_fr)
    if len(toks) < 4:
        return False
    return any(jaccard(toks, set(prev)) >= 0.75 for prev in st.setdefault("recent_fr", {}).get(source, []))


def remember_titles(st, item, fiche):
    day = NOW.strftime("%Y%m%d")
    for t in (item.get("title"), fiche.get("titre_fr")):
        if t and len(simplify(t)) >= 15:
            st.setdefault("titles", {})[title_key(item["source"], t)] = day
    toks = sorted(title_tokens(fiche.get("titre_fr") or ""))
    if toks:
        lst = st.setdefault("recent_fr", {}).setdefault(item["source"], [])
        lst.append(toks)
        del lst[:-300]


def mark_seen(st, url):
    st["seen"][url_key(url)] = NOW.strftime("%Y%m%d")


# =====================================================================
# Traitement
# =====================================================================
# Rattachements imposés pour les cas ambigus (nom simplifié → région)
REGION_OVERRIDES = {
    "union europeenne": "Europe", "ue": "Europe", "ukraine": "Europe", "moldavie": "Europe",
    "russie": "Russie & Eurasie", "bielorussie": "Russie & Eurasie", "georgie": "Russie & Eurasie",
    "armenie": "Russie & Eurasie", "azerbaidjan": "Russie & Eurasie", "kazakhstan": "Russie & Eurasie",
    "ouzbekistan": "Russie & Eurasie", "kirghizistan": "Russie & Eurasie", "tadjikistan": "Russie & Eurasie",
    "turkmenistan": "Russie & Eurasie", "turquie": "Moyen-Orient & Afrique du Nord",
    "israel": "Moyen-Orient & Afrique du Nord", "iran": "Moyen-Orient & Afrique du Nord",
    "egypte": "Moyen-Orient & Afrique du Nord", "soudan": "Afrique subsaharienne",
    "etats unis": "Amérique du Nord", "canada": "Amérique du Nord", "mexique": "Amérique latine & Caraïbes",
    "groenland": "Arctique", "afghanistan": "Asie-Pacifique", "pakistan": "Asie-Pacifique",
    "inde": "Asie-Pacifique", "chine": "Asie-Pacifique", "taiwan": "Asie-Pacifique",
    "australie": "Asie-Pacifique",
}


def finalize_fiche(fiche, item, cfg):
    f = dict(fiche or {})
    order = lambda lst: (lambda x: lst.index(x))
    f["themes"] = sorted(pick(f.get("themes"), cfg["themes"], 5), key=order(cfg["themes"]))
    f["disciplines"] = sorted(pick(f.get("disciplines"), cfg["disciplines"], 3), key=order(cfg["disciplines"]))
    f["regions"] = sorted(pick(f.get("regions"), cfg["regions"], 3), key=order(cfg["regions"]))
    t = pick([f.get("type")] if f.get("type") else [], cfg["types"], 1)
    f["type"] = "Podcast" if item.get("podcast") and "Podcast" in cfg["types"] else (t[0] if t else None)
    pays = []
    for p in f.get("pays") or []:
        if isinstance(p, dict) and p.get("nom"):
            name, reg = str(p["nom"]), p.get("region")
        elif isinstance(p, str):
            name, reg = p, None
        else:
            continue
        reg = REGION_OVERRIDES.get(simplify(name)) or (pick([reg], cfg["regions"], 1) or [None])[0]
        if opt(name) and opt(name) not in [x[0] for x in pays]:
            pays.append((opt(name), reg))
    f["pays"] = pays[:6]
    ch = f.get("chiffres") or []
    f["chiffres"] = [str(x) for x in (ch if isinstance(ch, list) else [ch]) if x][:5]
    pc = f.get("points_cles") or []
    f["points_cles"] = [str(x) for x in (pc if isinstance(pc, list) else [pc])]
    f["resume"] = str(f.get("resume") or "")
    return f


def publish(item, fiche, lecture, notion, st, cfg, dry, transcript=None):
    fiche = finalize_fiche(fiche, item, cfg)
    if is_near_duplicate(st, item["source"], fiche.get("titre_fr") or "") or \
            known_title(st, item["source"], fiche.get("titre_fr")):
        log.info("  doublon ignoré : %s", fiche.get("titre_fr"))
        mark_seen(st, item["url"])
        return
    import fiches as fiches_mod
    probe = {"t": fiche.get("titre_fr") or item.get("title") or "", "r": fiche.get("resume") or "",
             "th": fiche.get("themes", []), "py": [[n, r] for n, r in fiche.get("pays", [])]}
    related = fiches_mod.related_for(st, probe, simplify, now=NOW, cache=st.setdefault("_tokcache", {}))
    if dry:
        print(json.dumps({"source": item["source"], "url": item["url"], "lecture": lecture, **fiche,
                          "articles_lies": [b["t"] for b in related]}, ensure_ascii=False, indent=2))
        page_id = "dry-" + url_key(item["url"])
    else:
        page = notion.add(item, fiche, lecture,
                          transcript if cfg["limites"]["transcription_complete_dans_notion"] else None,
                          related=[b["p"] for b in related])
        page_id = (page or {}).get("id", "")
    mark_seen(st, item["url"])
    remember_titles(st, item, fiche)
    if page_id:
        fiches_mod.add_to_corpus(st, page_id, item, fiche)
        st["corpus"][-1].update(rel=[b["p"] for b in related], rl=True)
        # lien réciproque : l'article ancien pointe aussi vers le nouveau
        for b in related:
            b["rel"] = ([page_id] + [x for x in b.get("rel", []) if x != page_id])[:8]
            if not dry:
                try:
                    notion.set_relation(b["p"], b["rel"])
                except RuntimeError as e:
                    log.debug("Lien réciproque non posé : %s", e)


def run(args):
    cfg = load_config()
    sources = load_yaml("sources.yaml")
    lim = cfg["limites"]
    st = load_state()
    first = not st["initialized"]
    dry = args.dry_run

    notion = Notion(cfg)
    if not dry:
        notion.ensure_db(st.get("notion_db"))
        notion.load_schema()
        try:
            notion.ensure_relation()
        except RuntimeError as e:
            log.warning("Colonne « Articles liés » non créée : %s", e)
        if not st.get("nettoyage_doublons_fait"):
            try:
                n = notion.remove_duplicates(st)
                log.info("Nettoyage : %d doublon(s) mis à la corbeille Notion", n)
                st["nettoyage_doublons_fait"] = True
            except Exception as e:  # noqa: BLE001
                log.warning("Nettoyage des doublons impossible : %s", e)
        if st.get("notion_db") != notion.db and (st.get("notion_db") or st["initialized"]):
            log.info("Nouvelle base Notion : la mémoire est réinitialisée (reprise de l'historique)")
            st.update(seen={}, pending=[], initialized=False)
        st["notion_db"] = notion.db
        first = not st["initialized"]
    # Rattrapage unique des sites lus via leurs pages (liens non datés écartés par les versions précédentes)
    rescan = not first and not st.get("rattrapage_pages_v1")
    if rescan:
        log.info("Rattrapage des sites sans flux RSS (pages de publications et pages suivantes)")

    # ---------- 1. Collecte ----------
    queue_keys = {url_key(p["url"]) for p in st["pending"]}
    new = []
    for src in sources:
        try:
            items, _ = collect_source(src, st, cfg, lim["jours_premier_passage"] if (first or rescan) else 0)
        except Exception as e:  # noqa: BLE001
            log.warning("Source %s : erreur %s", src.get("nom"), e)
            continue
        n = 0
        for it in items:
            k = url_key(it["url"])
            if k in queue_keys or (k in st["seen"] and not (rescan and it.get("from_page"))):
                continue
            if known_title(st, src["nom"], it.get("title")):
                mark_seen(st, it["url"])
                continue
            a = age_days(it.get("date"))
            if a is not None and a > lim["age_max_jours"]:
                continue
            if (first or (rescan and it.get("from_page"))) and a is not None and a > lim["jours_premier_passage"]:
                mark_seen(st, it["url"])  # au-delà de la période de rattrapage
                continue
            if first and a is None and not it.get("from_page"):
                mark_seen(st, it["url"])
                continue
            # (les liens non datés des pages sont gardés : leur date sera lue à l'ouverture de l'article)
            queue_keys.add(k)
            it["tries"] = 0
            new.append(it)
            n += 1
        log.info("%-45s %3d nouveaux", src["nom"][:45], n)
    st["pending"].extend(new)
    st["rattrapage_pages_v1"] = True
    # Les publications les plus récentes passent en premier ; l'historique se complète au fil des passages
    st["pending"].sort(key=lambda i: i.get("date") or "9999", reverse=True)
    st["initialized"] = True
    if not dry:
        save_state(st)
    log.info("File d'attente : %d éléments (%d nouveaux)", len(st["pending"]), len(new))

    llm = LLM(st, cfg)
    done_articles = done_podcasts = 0
    quota_out = False

    def drop(item):
        st["pending"] = [p for p in st["pending"] if p["url"] != item["url"]]

    # ---------- 2. Articles ----------
    articles = [p for p in st["pending"] if not p.get("podcast")][: lim["articles_par_passage"]]
    ready = []

    def flush(batch):
        nonlocal quota_out, done_articles
        docs = [{"id": str(i + 1), "source": b["source"], "title": b["title"], "date": b.get("date"),
                 "url": b["url"], "text": truncate_for_ai(b["_text"], lim["caracteres_max_article"])}
                for i, b in enumerate(batch)]
        try:
            res = llm.summarize(docs)
        except QuotaExhausted as e:
            log.warning("Quota IA épuisé : %s — le reste attendra le prochain passage", e)
            quota_out = True
            return
        for i, b in enumerate(batch):
            fiche = res.get(str(i + 1))
            if not fiche:
                b["tries"] = b.get("tries", 0) + 1
                if b["tries"] < 3:
                    continue
                fiche = {"resume": b["_text"][:700] + "…", "titre_fr": b["title"]}
            try:
                publish(b, fiche, b["_lecture"], notion, st, cfg, dry)
                drop(b)
                done_articles += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Notion : échec pour %s : %s", b["url"], e)
                b["tries"] = b.get("tries", 0) + 1
        if not dry:
            save_state(st)

    for item in articles:
        if quota_out or time_left(cfg) < 120:
            break
        try:
            if not dry and notion.exists(item["url"]):
                mark_seen(st, item["url"])
                drop(item)
                continue
            text, method, meta = fetch_fulltext(item)
        except Exception as e:  # noqa: BLE001
            log.warning("Lecture impossible %s : %s", item["url"], e)
            item["tries"] = item.get("tries", 0) + 1
            if item["tries"] >= 3:
                mark_seen(st, item["url"])
                drop(item)
            continue
        if meta.get("title") and len(item.get("title") or "") < 15:
            item["title"] = meta["title"]
        if not item.get("date") and meta.get("date"):
            item["date"] = iso(parse_date(meta["date"]))
            a = age_days(item["date"])
            if a is not None and a > lim["age_max_jours"]:
                mark_seen(st, item["url"])  # ancien article resté en « une » d'un site
                drop(item)
                continue
        if len(text) < 200 and not item.get("title"):
            mark_seen(st, item["url"])
            drop(item)
            continue
        item["_text"] = text or item.get("title", "")
        item["_lecture"] = "Texte intégral" if len(text) >= 1500 else "Extrait seulement"
        log.info("  lu (%s, %d car.) : %s", method, len(text), item["url"])
        ready.append(item)
        size = sum(min(len(r["_text"]), lim["caracteres_max_article"]) for r in ready)
        if len(ready) >= lim["articles_par_requete_ia"] or size > 60000:
            flush(ready)
            ready = []
    if ready and not quota_out:
        flush(ready)
    for p in st["pending"]:
        p.pop("_text", None)
        p.pop("_lecture", None)

    # ---------- 3. Podcasts ----------
    podcasts = [p for p in st["pending"] if p.get("podcast")][: lim["podcasts_par_passage"]]
    for item in podcasts:
        if quota_out or time_left(cfg) < 600:
            break
        if not item.get("audio"):
            mark_seen(st, item["url"])
            drop(item)
            continue
        try:
            if not dry and notion.exists(item["url"]):
                mark_seen(st, item["url"])
                drop(item)
                continue
            log.info("  transcription : %s", item["title"])
            transcript = transcribe(item["audio"], item.get("langue_audio"))
        except QuotaExhausted as e:
            log.warning("Transcription reportée (quota) : %s", e)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Transcription impossible %s : %s", item["url"], e)
            item["tries"] = item.get("tries", 0) + 1
            if item["tries"] >= 3:
                transcript = ""
            else:
                continue
        text = transcript or item.get("feed_text") or ""
        doc = {"id": "1", "source": item["source"], "title": item["title"], "date": item.get("date"),
               "url": item["url"], "text": truncate_for_ai(
                   ("Description de l'épisode : " + (item.get("feed_text") or "") + "\n\nTranscription :\n" + text),
                   lim["caracteres_max_podcast"])}
        try:
            res = llm.summarize([doc])
        except QuotaExhausted as e:
            log.warning("Quota IA épuisé : %s", e)
            quota_out = True
            break
        fiche = res.get("1") or next(iter(res.values()), None)
        if not fiche:
            continue
        try:
            publish(item, fiche, "Transcription audio" if transcript else "Extrait seulement", notion, st, cfg, dry,
                    transcript=transcript)
            drop(item)
            done_podcasts += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Notion : échec pour %s : %s", item["url"], e)
        if not dry:
            save_state(st)

    if not dry:
        save_state(st)

    # ---------- 4. Fiches de révision ----------
    done_fiches = 0
    if not dry and time_left(cfg) > 300:
        import fiches as fiches_mod
        try:
            done_fiches = fiches_mod.update(sys.modules[__name__], st, cfg, notion, llm, lambda: time_left(cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("Fiches de révision : erreur %s", e)
        save_state(st)

    msg = (f"Terminé : {done_articles} articles et {done_podcasts} podcasts ajoutés à Notion, "
           f"{done_fiches} fiche(s) de révision mise(s) à jour. "
           f"En attente : {len(st['pending'])}. Appels IA : {llm.calls}")
    log.info(msg)
    summary = env("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Veille presse\n\n{msg}\n")


# =====================================================================
# Diagnostic
# =====================================================================
def diagnostic(args):
    cfg = load_config()
    sources = load_yaml("sources.yaml")
    st = load_state()
    out = [f"# Rapport de diagnostic — {NOW:%d/%m/%Y %H:%M} UTC", ""]

    out.append("## Clés et services")
    llm = LLM({}, cfg)
    out.append(f"- IA principale : {llm.provider()} ({'clé présente' if (llm.mkey or llm.gkey) else 'CLÉ MANQUANTE'})"
               f" — modèles : {', '.join(m.split('~')[0] for m in llm.gemini_models()) or 'aucun'}")
    out.append(f"- Groq : {'clé présente' if llm.qkey else 'CLÉ MANQUANTE'} — modèles : "
               f"{', '.join(llm.groq_models()) or 'aucun'}")
    if llm.mkey or llm.gkey or llm.qkey:
        try:
            res = llm.summarize([{"id": "1", "source": "Test", "title": "Test", "date": None, "url": "https://example.org",
                                  "text": "La Commission européenne a présenté un plan de 800 milliards d'euros pour "
                                          "renforcer la défense européenne face à la Russie."}])
            f = finalize_fiche(res.get("1"), {}, cfg)
            out.append(f"- Test IA : OK → disciplines {f['disciplines']}, thèmes {f['themes']}, "
                       f"pays {[n for n, _ in f['pays']]} (appels : {llm.calls})")
        except Exception as e:  # noqa: BLE001
            out.append(f"- Test IA : ÉCHEC — {e}")
    try:
        n = Notion(cfg)
        dbid = n.ensure_db()
        out.append(f"- Notion : OK (base « {DB_TITLE} » : {dbid})")
    except Exception as e:  # noqa: BLE001
        out.append(f"- Notion : ÉCHEC — {e}")
    out.append("")
    out.append("## Sources")
    ok_count = 0
    for src in sources:
        out.append(f"### {src['nom']}")
        try:
            items, report = collect_source(src, st, cfg)
        except Exception as e:  # noqa: BLE001
            items, report = [], [f"erreur : {e}"]
        out += [f"- {r}" for r in report]
        dated = sorted([i for i in items if i.get("date")], key=lambda i: i["date"], reverse=True)
        out.append(f"- **Total : {len(items)} éléments** ({len(dated)} datés)")
        sample = (dated or items)[:1]
        if sample and not src.get("podcast"):
            s = sample[0]
            try:
                text, method, meta = fetch_fulltext(s)
                out.append(f"- Test de lecture : {len(text)} caractères via {method} — {s['url']}")
            except Exception as e:  # noqa: BLE001
                out.append(f"- Test de lecture : échec ({e})")
        elif sample:
            out.append(f"- Dernier épisode : {sample[0]['title']} ({(sample[0].get('date') or '')[:10]}) — "
                       f"audio {'OK' if sample[0].get('audio') else 'ABSENT'}")
        if items:
            ok_count += 1
        else:
            out.append("- ⚠️ **Aucun élément trouvé : source à corriger**")
        out.append("")
    out.insert(2, f"**{ok_count}/{len(sources)} sources fonctionnent.**\n")
    text = "\n".join(out)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    summary = env("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)


def main():
    ap = argparse.ArgumentParser(description="Veille presse automatique")
    ap.add_argument("--mode", default="normal", choices=["normal", "diagnostic"])
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien dans Notion ni dans la mémoire")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    if args.mode == "diagnostic":
        diagnostic(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
