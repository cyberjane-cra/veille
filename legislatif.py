#!/usr/bin/env python3
"""
Veille législative automatique (vie-publique.fr, rubrique « Autour de la loi »)
------------------------------------------------------------------------------
- Suit TOUS les textes de la législature (dossiers législatifs de vie-publique) :
  projets et propositions de loi en cours, lois et ordonnances publiées.
- Calcule le stade de chaque texte (dépôt, lectures, CMP, Conseil constitutionnel,
  promulgation) et détecte chaque nouvelle étape -> alerte.
- Tableau de bord Notion (une ligne = un texte ; la page de la ligne = sa fiche technique).
- Fil de veille : toutes les productions des encarts de la page « Autour de la loi »
  (panoramas des lois, dossiers législatifs, débats et consultations, « comprendre
  l'élaboration des lois »), ainsi que les actualités et rapports publics de vie-publique
  liés aux textes suivis.
- Après promulgation : suivi de l'application (échéancier des décrets, arrêtés…).

Usage :
  python legislatif.py                      # passage normal
  python legislatif.py --mode diagnostic    # teste l'accès au site, l'IA et Notion, n'écrit rien
"""
import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import subprocess
import sys
import time

import requests
import yaml

import legi_fiches as F
import legi_parse as P
import veille as V

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
STATE_PATH = os.path.join(STATE_DIR, "legislatif.json")
START = time.time()
NOW = V.NOW
TODAY = NOW.strftime("%Y-%m-%d")
log = logging.getLogger("veille")

RUBRIQUES = {
    "panorama": ("Panorama des lois", "blue"),
    "dossier": ("Dossiers législatifs", "purple"),
    "consultation": ("Débats et consultations", "green"),
    "comprendre": ("Comprendre l'élaboration des lois", "orange"),
    "actualite": ("Actualités", "yellow"),
    "rapport": ("Rapports publics", "brown"),
}
STAGE_COLORS = ["gray", "brown", "orange", "yellow", "pink", "purple", "blue", "red", "green", "green", "default"]


def load_config():
    with open(os.path.join(ROOT, "config-legislatif.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def time_left(cfg):
    return cfg["limites"]["duree_max_minutes"] * 60 - (time.time() - START)


def days_since(d):
    if not d:
        return 9999
    x = V.parse_date(d)
    return (NOW - x).total_seconds() / 86400 if x else 9999


# =====================================================================
# Accès au site (protection anti-robots : navigateur sans écran si besoin)
# =====================================================================
class Fetcher:
    """Lit vie-publique.fr. Si le site répond par une page de vérification (serveurs cloud),
    un vrai navigateur (Chrome sans écran) passe la vérification, puis on réutilise ses cookies ;
    à défaut, toutes les pages sont lues par le navigateur lui-même."""

    def __init__(self):
        self.mode = "direct"
        self.tried = False
        self.pw = self.browser = self.page = None
        self.notes = []
        self.n_ok = self.n_fail = 0

    @staticmethod
    def valid(text, kind):
        return P.is_rss(text) if kind == "xml" else P.is_vp_page(text)

    def _try(self, url, kind):
        if self.mode == "navigateur":
            try:
                time.sleep(0.8)
                r = self.page.evaluate(
                    "async (u) => { const r = await fetch(u, {credentials: 'include'});"
                    " return {s: r.status, t: await r.text()}; }", url)
                if r["s"] == 404:
                    return None, 404
                return (r["t"], r["s"]) if self.valid(r["t"], kind) else (None, r["s"])
            except Exception as e:  # noqa: BLE001
                log.debug("navigateur : %s", e)
                return None, 0
        r = V.http_get(url, timeout=45)
        if r is None:
            return None, 0
        if r.status_code == 404:
            return None, 404
        if r.status_code < 400 and self.valid(r.text, kind):
            return r.text, r.status_code
        return None, r.status_code

    def get(self, url, kind="html"):
        url = P.absu(url)
        text, status = self._try(url, kind)
        if text is None and status != 404 and not self.tried:
            self.unlock()
            text, status = self._try(url, kind)
        if text is None and status != 404:
            text = self._jina(url, kind)
        if text is None:
            self.n_fail += status != 404
        else:
            self.n_ok += 1
        return text

    def _jina(self, url, kind):
        try:
            V.polite("r.jina.ai", 3.2)
            r = requests.get("https://r.jina.ai/" + url, timeout=90,
                             headers={"X-Return-Format": "html", "User-Agent": V.UA})
            if r.status_code == 200 and (kind == "html" and "<" in r.text[:200] or kind == "xml"):
                if self.valid(r.text, kind) or (kind == "html" and "<main" in r.text):
                    return r.text
        except requests.RequestException:
            pass
        return None

    def unlock(self):
        self.tried = True
        log.info("vie-publique.fr demande une vérification : ouverture d'un navigateur sans écran")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.notes.append("Playwright absent : impossible de passer la vérification du site")
            log.warning(self.notes[-1])
            return
        try:
            self.pw = sync_playwright().start()
            for opts in ({"channel": "chrome"}, {}):
                try:
                    self.browser = self.pw.chromium.launch(headless=True, **opts)
                    break
                except Exception as e:  # noqa: BLE001
                    log.debug("Lancement du navigateur %s : %s", opts, e)
            if self.browser is None:
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False, timeout=600)
                self.browser = self.pw.chromium.launch(headless=True)
            ctx = self.browser.new_context(locale="fr-FR", user_agent=V.UA, viewport={"width": 1280, "height": 900})
            self.page = ctx.new_page()
            self.page.goto(P.BASE + "/autour-de-la-loi", wait_until="domcontentloaded", timeout=90000)
            ok = False
            for _ in range(40):
                if "vp_dsfr" in self.page.content():
                    ok = True
                    break
                self.page.wait_for_timeout(1000)
            if not ok:
                self.notes.append("Le navigateur n'a pas passé la vérification du site")
                log.warning(self.notes[-1])
                return
            for c in ctx.cookies():
                V.SESSION.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
            r = V.http_get(P.BASE + "/lois-feeds.xml")
            if r is not None and r.status_code < 400 and P.is_rss(r.text):
                self.mode = "direct (cookies du navigateur)"
                self.notes.append("Vérification passée : lecture directe avec les cookies du navigateur")
            else:
                self.mode = "navigateur"
                self.notes.append("Vérification passée : lecture des pages par le navigateur")
            log.info(self.notes[-1])
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"Navigateur indisponible : {e}")
            log.warning(self.notes[-1])

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:  # noqa: BLE001
            pass


def ext_get(url, pdf_pages=30):
    """Document officiel hors vie-publique (Légifrance, Sénat, Assemblée, Conseil constitutionnel) : texte brut."""
    if not url:
        return ""
    try:
        r = V.http_get(url, timeout=60)
        if r is None or r.status_code >= 400:
            return ""
        ctype = r.headers.get("content-type", "").lower()
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            return V.pdf_to_text(r.content, max_pages=pdf_pages)
        txt = V.trafilatura.extract(r.text, url=r.url, include_comments=False, include_tables=True,
                                    favor_recall=True) or ""
        return V.clean_text(txt)
    except Exception as e:  # noqa: BLE001
        log.debug("Document externe illisible %s : %s", url, e)
        return ""


# =====================================================================
# Mémoire
# =====================================================================
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k, v in (("textes", {}), ("pano", {}), ("seen", {}), ("fil", []), ("notion", {}), ("pending", [])):
        st.setdefault(k, v)
    return st


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    st["fil"] = st["fil"][-4000:]
    st["pending"] = st["pending"][-1500:]
    cutoff = (NOW - dt.timedelta(days=800)).strftime("%Y-%m-%d")
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in st.items() if not k.startswith("_")}, f, ensure_ascii=False,
                  separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


# =====================================================================
# IA
# =====================================================================
def make_llm(st, cfg):
    llm = V.LLM(st, {})
    llm.gkey = V.env("GEMINI_API_KEY_LEGI") or V.env("GEMINI_API_KEY")
    return llm


def ask(llm, prompt, quality="lite", max_tokens=8192):
    models = llm.gemini_models()
    lite = [m for m in models if "lite" in m and "gemma" not in m]
    flash = [m for m in models if "lite" not in m and "gemma" not in m]
    gemma = [m for m in models if "gemma" in m]
    order = (lite + flash + gemma) if quality == "lite" else (flash + lite)
    for model in order:
        if model in llm.exhausted or model in llm.skip:
            continue
        try:
            return llm._call_gemini(model, prompt, max_tokens)
        except V.QuotaExhausted as e:
            log.info("Gemini %s : quota du jour épuisé (%s)", model, str(e)[:100])
            llm.exhausted[model] = llm.today
        except (ValueError, KeyError) as e:
            log.warning("Gemini %s : réponse illisible (%s)", model, e)
        except (RuntimeError, requests.RequestException) as e:
            log.warning("Gemini %s indisponible : %s", model, str(e)[:200])
            llm.skip.add(model)
    if quality == "lite":
        for model in llm.groq_models():
            key = "groq:" + model
            if key in llm.exhausted or key in llm.skip:
                continue
            try:
                return llm._call_groq(model, prompt[:22000])
            except V.QuotaExhausted:
                llm.exhausted[key] = llm.today
            except (ValueError, KeyError, RuntimeError, requests.RequestException) as e:
                log.warning("Groq %s : %s", model, str(e)[:200])
                llm.skip.add(key)
    raise V.QuotaExhausted("aucun modèle d'IA gratuit disponible pour le moment")


# =====================================================================
# Notion
# =====================================================================
def rt(text):
    return V.rt(text)


def title_prop(text):
    return {"title": rt(str(text)[:1900])[:1]}


def text_prop(text):
    return {"rich_text": rt(str(text or "")[:1990])[:1]}


class LegiNotion:
    def __init__(self, cfg):
        self.n = V.Notion(cfg)
        self.n.page = V.env("NOTION_PAGE_ID_LEGI") or V.env("NOTION_LEGI_PAGE_ID")
        self.req = self.n.req
        self.page = self.n.clean_id(self.n.page) if self.n.page else ""
        self.ids = {}

    # ---------- reconnaissance des bases (par leurs colonnes, pas par leur nom) ----------
    @staticmethod
    def kind_of(d):
        p = d.get("properties") or {}
        if d.get("archived") or d.get("in_trash"):
            return None
        if "ID dossier" in p and "Stade" in p:
            return "dash"
        if "Type d'événement" in p and "Texte" in p:
            return "alerts"
        if "Rubrique" in p and "Texte lié" in p:
            return "fil"
        return None

    def find_dbs(self):
        found, cursor = {}, None
        while True:
            body = {"filter": {"property": "object", "value": "database"}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = self.req("POST", "/search", body)
            for d in res.get("results", []):
                k = self.kind_of(d)
                if not k:
                    continue
                on_page = d.get("parent", {}).get("page_id", "").replace("-", "") == self.page
                if k not in found or (on_page and not found[k][1]):
                    found[k] = (d["id"], on_page)
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        return {k: v[0] for k, v in found.items()}

    def ensure(self, st):
        if not self.n.token:
            raise RuntimeError("NOTION_TOKEN manquant")
        mem = st["notion"]
        ok = {}
        for k in ("dash", "alerts", "fil"):
            if mem.get(k):
                try:
                    d = self.req("GET", f"/databases/{mem[k]}")
                    if self.kind_of(d) == k:
                        ok[k] = d["id"]
                except RuntimeError:
                    pass
        if len(ok) < 3:
            found = self.find_dbs()
            for k, v in found.items():
                ok.setdefault(k, v)
        if len(ok) < 3 and not self.page:
            raise RuntimeError("NOTION_PAGE_ID_LEGI manquant : impossible de créer le tableau de bord")
        if len(ok) < 3 and not mem.get("summary"):
            self.ensure_summary(st)
        if "dash" not in ok:
            ok["dash"] = self.create_dash()
        if "alerts" not in ok:
            ok["alerts"] = self.create_alerts(ok["dash"])
        if "fil" not in ok:
            ok["fil"] = self.create_fil(ok["dash"])
        if mem.get("dash") and mem["dash"] != ok["dash"]:
            log.info("Nouveau tableau de bord Notion : les fiches seront recréées")
            for t in st["textes"].values():
                t.pop("page", None)
                t.pop("props_sig", None)
                t.get("fiche", {}).pop("container", None)
        mem.update(ok)
        self.ids = ok
        if not mem.get("summary"):
            self.ensure_summary(st)
        return ok

    def ensure_summary(self, st):
        """Encadré de synthèse en haut de la page (titre fixe + bloc remplacé à chaque passage)."""
        if not self.page:
            return
        res = self.req("PATCH", f"/blocks/{self.page}/children", {"children": [
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rt("📊 Synthèse du jour")}},
            {"object": "block", "type": "synced_block", "synced_block": {"synced_from": None, "children": [
                F.B("paragraph", "La synthèse apparaîtra après le premier passage.", color="gray")]}}]})
        st["notion"]["summary_anchor"] = res["results"][0]["id"]
        st["notion"]["summary"] = res["results"][1]["id"]

    def _create(self, title, props, icon):
        d = self.req("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": self.page}, "is_inline": True,
            "icon": {"type": "emoji", "emoji": icon},
            "title": [{"type": "text", "text": {"content": title}}], "properties": props})
        log.info("Base Notion créée : %s", title)
        return d["id"]

    def create_dash(self):
        props = {
            "Texte": {"title": {}},
            "Stade": {"select": {"options": [{"name": s, "color": STAGE_COLORS[i]}
                                             for i, s in enumerate(P.ALL_STAGES)]}},
            "Avancement": {"number": {"format": "percent"}},
            "Progression": {"rich_text": {}},
            "Dernière étape": {"rich_text": {}},
            "Date de l'étape": {"date": {}},
            "Alerte": {"select": {"options": [{"name": "🔔 Nouvelle étape", "color": "red"},
                                              {"name": "🆕 Nouveau texte", "color": "blue"},
                                              {"name": "📜 Application", "color": "green"},
                                              {"name": "📰 Panorama mis à jour", "color": "yellow"}]}},
            "Dernier événement": {"rich_text": {}},
            "Nature": {"select": {"options": [{"name": n} for n in
                                              ("Projet de loi", "Proposition de loi", "Loi", "Ordonnance")]}},
            "Vecteur": {"select": {}},
            "Dépôt": {"select": {"options": [{"name": "Assemblée nationale", "color": "green"},
                                             {"name": "Sénat", "color": "red"}]}},
            "Procédure accélérée": {"checkbox": {}},
            "Conseil constitutionnel": {"select": {"options": [
                {"name": "Conforme", "color": "green"}, {"name": "Partiellement conforme", "color": "orange"},
                {"name": "Non conforme", "color": "red"}, {"name": "Décision rendue", "color": "gray"}]}},
            "Application": {"rich_text": {}},
            "Taux d'application": {"number": {"format": "percent"}},
            "Thèmes": {"multi_select": {}},
            "Fiche": {"select": {"options": [{"name": "Complète", "color": "green"},
                                             {"name": "Allégée", "color": "gray"},
                                             {"name": "En préparation", "color": "default"}]}},
            "Suivi prioritaire": {"checkbox": {}},
            "Mise à jour": {"date": {}},
            "Intitulé officiel": {"rich_text": {}},
            "Dossier vie-publique": {"url": {}},
            "Panorama": {"url": {}},
            "Légifrance": {"url": {}},
            "Assemblée nationale": {"url": {}},
            "Sénat": {"url": {}},
            "NOR": {"rich_text": {}},
            "ID dossier": {"rich_text": {}},
        }
        return self._create("Tableau de bord législatif", props, "📊")

    def create_alerts(self, dash):
        props = {
            "Événement": {"title": {}},
            "Type d'événement": {"select": {}},
            "Date": {"date": {}},
            "Texte": {"relation": {"database_id": dash, "single_property": {}}},
            "Stade": {"select": {"options": [{"name": s, "color": STAGE_COLORS[i]}
                                             for i, s in enumerate(P.ALL_STAGES)]}},
            "Lien": {"url": {}},
            "Lu": {"checkbox": {}},
            "Détecté le": {"created_time": {}},
        }
        return self._create("Alertes législatives", props, "🔔")

    def create_fil(self, dash):
        props = {
            "Titre": {"title": {}},
            "Rubrique": {"select": {"options": [{"name": v[0], "color": v[1]} for v in RUBRIQUES.values()]}},
            "Type": {"select": {}},
            "Date": {"date": {}},
            "Résumé": {"rich_text": {}},
            "Texte lié": {"relation": {"database_id": dash, "single_property": {}}},
            "Thèmes": {"multi_select": {}},
            "Autorité": {"rich_text": {}},
            "Fin de consultation": {"date": {}},
            "Lien": {"url": {}},
            "Lu": {"checkbox": {}},
            "Ajouté le": {"created_time": {}},
        }
        return self._create("Fil de veille législative", props, "📰")

    # ---------- lignes ----------
    def create_page(self, db, props, children=None):
        body = {"parent": {"database_id": db}, "properties": props}
        if children:
            body["children"] = children[:100]
        return self.req("POST", "/pages", body)

    def update_page(self, pid, props):
        return self.req("PATCH", f"/pages/{pid}", {"properties": props})


def short_title(t):
    return (t.get("fiche", {}).get("data") or {}).get("intitule_court") or \
        (t.get("pano_data") or {}).get("title") or re.sub(r"\s*\([A-Z]{4}\d{7}[A-Z]\)\s*$", "", t["title"])


def dash_props(t, star):
    short = short_title(t)
    e = t.get("eche") or {}
    jl = next((j["url"] for j in t.get("jorf", []) if j["kind"] == "promulgation"), None)
    props = {
        "Texte": title_prop((F.STAR if star else "") + short),
        "Stade": {"select": {"name": t["stage"]}},
        "Avancement": {"number": round(P.progress_of(t["stage"]), 3)},
        "Progression": text_prop(P.progress_bar(t["stage"])),
        "Dernière étape": text_prop(t.get("stage_label", "")),
        "Nature": {"select": {"name": t["nature"]}},
        "Vecteur": {"select": {"name": V.opt(t["vecteur"])}},
        "Procédure accélérée": {"checkbox": bool(t.get("accel"))},
        "Intitulé officiel": text_prop(t["title"]),
        "Dossier vie-publique": {"url": t["url"]},
        "Panorama": {"url": t.get("pano_url") or None},
        "Légifrance": {"url": jl},
        "Assemblée nationale": {"url": (t.get("parl") or {}).get("Assemblée nationale")},
        "Sénat": {"url": (t.get("parl") or {}).get("Sénat")},
        "NOR": text_prop(t.get("nor") or ""),
        "ID dossier": text_prop(t["id"]),
        "Fiche": {"select": {"name": t.get("fiche", {}).get("level") if t.get("fiche", {}).get("data")
                             else "En préparation"}},
        "Mise à jour": {"date": {"start": t.get("upd") or TODAY}},
        "Thèmes": {"multi_select": [{"name": V.opt(k)} for k in (t.get("themes") or [])[:8] if V.opt(k)]},
        "Dernier événement": text_prop(t.get("last_event", "")),
        "Alerte": {"select": {"name": t["alert"]} if t.get("alert") and star else None},
        "Conseil constitutionnel": {"select": {"name": t["cc"]} if t.get("cc") else None},
        "Dépôt": {"select": {"name": t["dep"]} if t.get("dep") else None},
        "Application": text_prop((f"{e['pub']}/{e['total']} mesures publiées" if e.get("total") else
                                  ("Sans décret d'application" if t.get("no_decree") else ""))),
        "Taux d'application": {"number": round(e["pub"] / e["total"], 3) if e.get("total") else None},
        "Date de l'étape": {"date": {"start": t["stage_date"]} if t.get("stage_date") else None},
    }
    return props


# =====================================================================
# Le programme
# =====================================================================
class Legi:
    def __init__(self, cfg, st, fetch, notion, llm, dry=False):
        self.cfg, self.st, self.fetch, self.notion, self.llm, self.dry = cfg, st, fetch, notion, llm, dry
        self.lim = cfg["limites"]
        self.leg = str(cfg.get("legislature", 17))
        self.events_run = []
        self.counts = {"textes_nouveaux": 0, "dossiers_lus": 0, "evenements": 0, "fil": 0, "fiches": 0}

    # ------------------------------------------------------------------
    # 1. Inventaire des dossiers législatifs de la législature
    # ------------------------------------------------------------------
    def inventory(self):
        html = self.fetch.get(f"{P.BASE}/liste/dossierslegislatifs/{self.leg}")
        if not html:
            log.warning("Liste des dossiers législatifs illisible")
            return 0
        items = P.parse_list_dossiers(html)
        T = self.st["textes"]
        initialized = self.st.get("initialized")
        new = 0
        for it in items:
            t = T.get(it["id"])
            published = P.norm(it["section"]).startswith("textes publies")
            if t is None:
                nature, vect = P.nature_of(it["title"], it["cat"])
                t = T[it["id"]] = {"id": it["id"], "url": it["url"], "title": it["title"], "cat": it["cat"],
                                   "nature": nature, "vecteur": vect, "published": published,
                                   "stage": P.STAGE_ORDONNANCE if nature == "Ordonnance" else
                                   (P.STAGES[8] if published else P.STAGES[0]),
                                   "stage_label": "", "steps": [], "jorf": [], "events": [], "next": "",
                                   "first_seen": TODAY, "baseline": True}
                new += 1
                if initialized:
                    self.add_event(t, "Nouveau texte", f"Nouveau dossier législatif : {it['title']}", it["url"],
                                   TODAY, alert="🆕 Nouveau texte")
                    self.fil_add({"key": "dos:" + it["id"], "rub": "dossier", "type": t["nature"],
                                  "title": it["title"], "date": TODAY, "url": it["url"], "textes": [it["id"]],
                                  "resume": f"Nouveau dossier législatif ouvert sur vie-publique ({it['cat']} "
                                            f"{it['year']}, {it['section'].lower()})."})
            else:
                if published and not t.get("published"):
                    t["published"] = True
                    t["next"] = ""  # vérification immédiate
                if t.get("title") != it["title"]:
                    t["title"] = it["title"]
                    t["nature"], t["vecteur"] = P.nature_of(it["title"], it["cat"])
                    t["next"] = ""
                t["cat"] = it["cat"]
            t["in_list"] = TODAY
        self.counts["textes_nouveaux"] = new
        log.info("Inventaire : %d textes dans la %se législature (%d nouveaux)", len(items), self.leg, new)
        return len(items)

    # ------------------------------------------------------------------
    # 2. Panoramas des lois (mis à jour au fil de la procédure)
    # ------------------------------------------------------------------
    def panoramas(self):
        start = self.cfg.get("debut_legislature", "2024-07-18")
        known = self.st["pano"]
        first = not self.st.get("pano_init")
        pages = int(self.lim.get("pages_panoramas_premier_passage", 30)) if first else 2
        changed = 0
        for n in range(pages):
            html = self.fetch.get(f"{P.BASE}/loi" + (f"?page={n}" if n else ""))
            if not html:
                break
            cards = [c for c in P.parse_cards(html) if P.pano_id(c["url"])]
            if not cards:
                break
            stop = False
            for c in cards:
                pid = P.pano_id(c["url"])
                k = known.get(pid)
                if c["date"] and c["date"] < start and not k:
                    stop = True
                    continue
                if k and k.get("card_date") == c["date"]:
                    continue
                known.setdefault(pid, {})
                known[pid].update({"url": c["url"], "title": c["title"], "card_date": c["date"],
                                   "tags": [x.strip() for x in " ".join(c["details"][:1]).split(" - ") if x.strip()],
                                   "chapo": c["desc"], "todo": True})
                changed += 1
            if stop:
                break
        # le flux RSS en complément
        xml = self.fetch.get(f"{P.BASE}/lois-feeds.xml", "xml")
        for it in P.parse_rss(xml or ""):
            pid = P.pano_id(it["url"])
            if not pid:
                continue
            k = known.setdefault(pid, {})
            if k.get("rss_date") != it["date"]:
                k.update({"url": it["url"], "title": it["title"], "rss_date": it["date"], "todo": True,
                          "chapo": k.get("chapo") or it["desc"],
                          "tags": k.get("tags") or [x.strip() for x in it["subjects"].split(",") if x.strip()]})
                changed += 1
        self.st["pano_init"] = True
        log.info("Panoramas des lois : %d nouveaux ou mis à jour", changed)

    def process_panoramas(self):
        """Lit les panoramas nouveaux ou modifiés, les rattache à leur dossier et prépare le fil."""
        done = 0
        for pid, k in list(self.st["pano"].items()):
            if not k.get("todo"):
                continue
            if time_left(self.cfg) < 600:
                break
            html = self.fetch.get(k["url"])
            if not html:
                k["tries"] = k.get("tries", 0) + 1
                if k["tries"] >= 3:
                    k["todo"] = False
                continue
            p = P.parse_panorama(html, k["url"])
            h = P.pano_hash(p)
            k["todo"] = False
            did = p.get("dossier") or k.get("dossier")
            k["dossier"] = did
            k["title"] = p["title"] or k.get("title")
            old_h = k.get("hash")
            k["hash"] = h
            done += 1
            t = self.st["textes"].get(did) if did else None
            if t is not None:
                first_link = t.get("pano_id") != pid
                t["pano_id"], t["pano_url"] = pid, P.clean_vp_url(k["url"])
                old_status = (t.get("pano_data") or {}).get("status")
                t["pano_data"] = {kk: p[kk] for kk in ("title", "chapo", "status", "historique", "timeline",
                                                         "sources", "keywords", "published")}
                t["themes"] = p["keywords"] or k.get("tags") or t.get("themes")
                t["pano_hash"] = h
                t["next"] = ""  # relire le dossier dans ce passage
                if not t.get("baseline") and old_h and old_h != h:
                    if p["status"] and p["status"] != old_status:
                        self.add_event(t, "Panorama mis à jour", f"Panorama vie-publique : {p['status']}",
                                       t["pano_url"], TODAY, alert="📰 Panorama mis à jour")
                    else:
                        t["fiche_dirty"] = True
                elif first_link and not t.get("baseline") and self.st.get("initialized"):
                    self.add_event(t, "Nouveau panorama", f"Nouveau panorama des lois : {p['title']}",
                                   t["pano_url"], TODAY, alert="📰 Panorama mis à jour")
            # fil de veille
            date = k.get("card_date") or k.get("rss_date") or p.get("published") or TODAY
            if days_since(date) <= self.lim["jours_historique_fil"]:
                key = f"pano:{pid}:{h}"
                if key not in self.st["seen"]:
                    is_new = not old_h
                    self.fil_add({"key": key, "rub": "panorama", "type": "Panorama" if is_new else
                                  "Panorama mis à jour", "title": ("" if is_new else "Mise à jour — ") + p["title"],
                                  "date": date, "url": P.clean_vp_url(k["url"]), "textes": [did] if t else [],
                                  "themes": p["keywords"][:6],
                                  "resume": p["chapo"] + (f" — État : {p['status']}" if p["status"] else "")})
        log.info("Panoramas lus : %d", done)

    # ------------------------------------------------------------------
    # 3. Lecture des dossiers (étapes, échéancier) et détection des évolutions
    # ------------------------------------------------------------------
    def due_dossiers(self):
        out = []
        for t in self.st["textes"].values():
            if t.get("next", "") <= NOW.isoformat():
                prio = 0 if not t.get("fetched") else (1 if t.get("next") == "" else 2)
                out.append((prio, t.get("next", ""), t["id"]))
        out.sort()
        return [x[2] for x in out]

    def schedule(self, t):
        h = self.lim
        if t["stage"] in (P.STAGES[8], P.STAGE_ORDONNANCE):
            age = days_since(t.get("stage_date"))
            hours = h["heures_entre_lectures_promulgue"] if age < 1100 else 24 * 7
        elif t["stage"] == P.STAGE_REJET:
            hours = 24 * 7
        else:
            act = days_since(max([s.get("date") or "" for s in t.get("steps", [])] + [t.get("modified") or ""]))
            hours = h["heures_entre_lectures_actif"] if act < 150 else h["heures_entre_lectures_dormant"]
        t["next"] = (NOW + dt.timedelta(hours=hours)).isoformat()

    def refresh_dossiers(self):
        ids = self.due_dossiers()
        cap = int(self.lim["dossiers_par_passage"])
        log.info("Dossiers à relire : %d (au plus %d dans ce passage)", len(ids), cap)
        for did in ids[:cap]:
            if time_left(self.cfg) < self.lim.get("reserve_minutes_fiches", 15) * 60:
                log.info("Temps réservé aux fiches : la relecture des dossiers continuera au prochain passage")
                break
            t = self.st["textes"][did]
            try:
                self.refresh_one(t)
            except Exception as e:  # noqa: BLE001
                log.warning("Dossier %s : erreur %s", did, e)
                t["next"] = (NOW + dt.timedelta(hours=6)).isoformat()

    def refresh_one(self, t):
        html = self.fetch.get(t["url"])
        if html is None:
            t["next"] = (NOW + dt.timedelta(hours=12)).isoformat()
            return
        d = P.parse_dossier(html, t["url"])
        self.counts["dossiers_lus"] += 1
        old_steps = {s["k"] for s in t.get("steps", [])}
        old_jorf = {j["url"] for j in t.get("jorf", [])}
        old_groups = {g["groupe"] for g in t.get("documents", [])}
        old_stage = t.get("stage")
        for k in ("steps", "jorf", "documents", "debats", "parl", "no_decree", "nor", "modified"):
            t[k] = d[k]
        t["debats"] = [{"groupe": g["groupe"], "items": g["items"][:6]} for g in d["debats"]][:20]
        t["tabs"] = [x["label"] for x in d["tabs"]]
        if d["title"]:
            t["title"] = d["title"]
        if d.get("panorama") and not t.get("pano_url"):
            pid = P.pano_id(d["panorama"])
            if pid:
                k = self.st["pano"].setdefault(pid, {"url": d["panorama"]})
                k["dossier"] = t["id"]
                if not k.get("hash"):
                    k["todo"] = True
                t["pano_url"], t["pano_id"] = P.clean_vp_url(d["panorama"]), pid
        st = P.compute_stage(d, t.get("pano_data"), t.get("published"))
        t["stage"], t["stage_label"], t["stage_date"] = st["stage"], st["label"], st["date"]
        t["dep"], t["accel"] = P.deposit_info(d, t.get("pano_data"))
        t["cc"] = P.cc_outcome(d)
        # échéancier d'application
        old_pub = set((t.get("eche") or {}).get("pub_keys", []))
        if any("echeancier" in P.norm(x) for x in t["tabs"]):
            eh = self.fetch.get(t["url"] + "?detailType=CONTENU&detailId=1")
            if eh:
                rows = P.parse_echeancier(eh)
                stats = P.echeancier_stats(rows)
                t["eche"] = {"rows": rows, "pub": stats["pub"], "total": stats["total"],
                             "pub_keys": [r["k"] for r in rows if r["etat"] == "publiée"]}
        t["fetched"] = NOW.isoformat()
        t["upd"] = TODAY
        self.schedule(t)
        if t.get("baseline"):
            t["baseline"] = False
            t["eche_seen"] = bool(t.get("eche"))
            return
        # ---- évolutions depuis la lecture précédente ----
        stage_txt = f" → stade : {t['stage']}" if t["stage"] != old_stage else ""
        for s in d["steps"]:
            if s["k"] not in old_steps and s["kind"] not in ("autre",):
                lab = P.STEP_LABELS.get(s["kind"], "Étape")
                who = {"AN": " (Assemblée nationale)", "Sénat": " (Sénat)"}.get(s["chamber"], "")
                self.add_event(t, lab + who, s["label"][:300] + stage_txt, s["url"], s.get("date") or TODAY)
                stage_txt = ""
        for j in d["jorf"]:
            if j["url"] not in old_jorf:
                lab = "Promulgation" if j["kind"] == "promulgation" else "Décision du Conseil constitutionnel"
                self.add_event(t, lab, j["label"][:300] + stage_txt, j["url"], j.get("date") or TODAY)
                stage_txt = ""
        for g in d["documents"]:
            ng = P.norm(g["groupe"])
            if g["groupe"] not in old_groups and "commission mixte" in ng:
                ok = "desaccord" not in ng and "echec" not in ng
                self.add_event(t, "CMP : accord" if ok else "CMP : désaccord", g["groupe"] + stage_txt,
                               g["items"][0]["url"] if g["items"] else t["url"], TODAY)
                stage_txt = ""
        if stage_txt:
            self.add_event(t, "Changement de stade", t["stage_label"] + stage_txt, t["url"], TODAY)
        new_pub = [r for r in (t.get("eche") or {}).get("rows", []) if r["etat"] == "publiée" and r["k"] not in old_pub]
        if new_pub and t.get("eche_seen"):
            by = {}
            for r in new_pub:
                for mz in r["mesures"] or [{"label": "Mesure publiée", "url": t["url"]}]:
                    by.setdefault((mz["label"], mz.get("url")), []).append(r)
            for (lab, url), rows in list(by.items())[:10]:
                objets = " ; ".join(r["objet"][:120] for r in rows[:3])
                self.add_event(t, "Mesure d'application publiée", f"{lab} — application de {rows[0]['article']}"
                               f"{' et ' + str(len(rows) - 1) + ' autre(s) ligne(s)' if len(rows) > 1 else ''} : "
                               f"{objets}", url, P.last_date_fr(lab) or TODAY, alert="📜 Application")
        if t.get("eche"):
            t["eche_seen"] = True

    def add_event(self, t, kind, text, url, date, alert="🔔 Nouvelle étape"):
        ev = {"date": date, "type": kind, "text": text, "url": url or t["url"], "seen": TODAY, "stage": t["stage"]}
        t.setdefault("events", []).append(ev)
        t["events"] = t["events"][-80:]
        t["star"] = NOW.isoformat()
        t["alert"] = alert
        t["last_event"] = f"{F.fr_date(date)} — {kind} : {text}"[:1900]
        t["fiche_dirty"] = True
        self.events_run.append((t["id"], ev))
        self.counts["evenements"] += 1
        log.info("  🔔 %s — %s : %s", t["title"][:70], kind, text[:120])

    # ------------------------------------------------------------------
    # 4. Fil de veille : consultations, « comprendre », actualités, rapports
    # ------------------------------------------------------------------
    def fil_add(self, item):
        """Ajoute un élément à publier dans le fil (publication Notion différée)."""
        if item["key"] in self.st["seen"]:
            return
        self.st["seen"][item["key"]] = TODAY
        self.st["pending"].append(item)

    def collect_fil(self):
        days = self.lim["jours_historique_fil"]
        first = not self.st.get("fil_init")
        cands = []
        # Débats et consultations : flux + pages de liste
        lst = P.parse_rss(self.fetch.get(f"{P.BASE}/debats-consultations-feeds.xml", "xml") or "")
        pages = int(self.lim.get("pages_historique", 8)) if first else 1
        for n in range(pages):
            html = self.fetch.get(f"{P.BASE}/consultations" + (f"?page={n}" if n else ""))
            cards = P.parse_cards(html or "")
            lst += [{"url": c["url"], "title": c["title"], "date": c["date"]} for c in cards
                    if "/consultations/" in c["url"]]
            if not cards or (cards[-1]["date"] and days_since(cards[-1]["date"]) > days):
                break
        for it in lst:
            cands.append(("consultation", it))
        # Autour de la loi : tous les encarts
        html = self.fetch.get(f"{P.BASE}/autour-de-la-loi")
        for it in P.parse_autour_de_la_loi(html or ""):
            if "/consultations/" in it["url"] or "/loi/" in it["url"] or "/dossierlegislatif/" in it["url"]:
                continue  # déjà couverts par les autres rubriques
            cands.append(("comprendre", {"url": it["url"], "title": it["title"], "date": None,
                                         "encart": it["encart"]}))
        # Actualités et rapports
        for rub, feed, listing in (("actualite", "actualites-feeds.xml", "actualites"),
                                   ("rapport", "rapports-feeds.xml", "rapports")):
            lst = P.parse_rss(self.fetch.get(f"{P.BASE}/{feed}", "xml") or "")
            for n in range(pages):
                html = self.fetch.get(f"{P.BASE}/{listing}" + (f"?page={n}" if n else ""))
                cards = P.parse_cards(html or "")
                lst += [{"url": c["url"], "title": c["title"], "date": c["date"], "desc": c["desc"]} for c in cards]
                if not cards or (cards[-1]["date"] and days_since(cards[-1]["date"]) > days):
                    break
            for it in lst:
                cands.append((rub, it))
        # nouveaux seulement, dans la période ; ils rejoignent la file d'attente du fil
        queue = self.st.setdefault("fil_queue", [])
        n = 0
        for rub, it in cands:
            u = P.clean_vp_url(it["url"])
            key = f"{rub}:{P.vp_id(u) or u}"
            if key in self.st["seen"]:
                continue
            self.st["seen"][key] = TODAY
            if it.get("date") and days_since(it["date"]) > days and rub != "comprendre":
                continue
            queue.append({k: v for k, v in dict(it, url=u, key=key, rub=rub).items() if v is not None})
            n += 1
        self.st["fil_init"] = True
        log.info("Fil de veille : %d nouveaux éléments (file d'attente : %d)", n, len(queue))

    def text_index(self):
        """Index de vocabulaire des textes suivis (rattachement des actualités, rapports, consultations)."""
        stop = set("loi lois projet proposition visant vise relative relatif portant diverses dispositions mesures "
                   "ratifiant ordonnance ordonnances code article articles renforcer ameliorer creation creer "
                   "matiere cadre national nationale francaise france etat public publique publics publiques "
                   "pour dans avec sans leurs entre afin certaines plusieurs adaptation application organique "
                   "texte transposition directive parlement europeen conseil".split())
        idx = {}
        for t in self.st["textes"].values():
            words = P.norm(t["title"] + " " + " ".join(t.get("themes") or []) + " " +
                           ((t.get("pano_data") or {}).get("title") or ""))
            toks = {w for w in re.findall(r"[a-z]{4,}", words) if w not in stop}
            idx[t["id"]] = toks
        df = {}
        for toks in idx.values():
            for w in toks:
                df[w] = df.get(w, 0) + 1
        n = max(len(idx), 1)
        self._idf = {w: math.log(1 + n / c) for w, c in df.items()}
        self._tidx = idx

    def candidates(self, text, k=6):
        toks = set(re.findall(r"[a-z]{4,}", P.norm(text)))
        scored = []
        for did, tt in self._tidx.items():
            inter = toks & tt
            if len(inter) < 2:
                continue
            sc = sum(self._idf.get(w, 0) for w in inter)
            if sc >= self.lim.get("seuil_rattachement", 6.0):
                scored.append((sc, did))
        scored.sort(reverse=True)
        return [d for _, d in scored[:k]]

    LEGI_RE = re.compile(r"\b(lois?|projet de loi|proposition de loi|ordonnances?|d[ée]crets?|parlement|s[ée]nat|"
                         r"assembl[ée]e nationale|conseil constitutionnel|l[ée]gislati\w*|amendements?|"
                         r"commission mixte|49\.3|promulg\w*|application des lois|r[ée]forme|code )", re.I)

    def process_fil(self):
        queue = self.st.setdefault("fil_queue", [])
        if not queue:
            return
        self.text_index()
        lim = self.lim
        # les plus récents d'abord (les éléments de « Comprendre » sans date passent en premier)
        queue.sort(key=lambda i: i.get("date") or "9999", reverse=True)
        maxn = int(lim["elements_fil_par_passage"])
        ready, done = [], set()
        for it in list(queue):
            if len(ready) >= maxn or time_left(self.cfg) < lim.get("reserve_minutes_fiches", 15) * 60:
                break
            rub = it["rub"]
            pre = (it.get("title") or "") + " " + (it.get("desc") or "")
            if rub in ("actualite", "rapport") and not self.candidates(pre) and not self.LEGI_RE.search(pre):
                done.add(it["key"])  # sans lien avec l'activité législative
                continue
            html = self.fetch.get(it["url"])
            if not html:
                it["tries"] = it.get("tries", 0) + 1
                if it["tries"] >= 3:
                    done.add(it["key"])
                continue
            done.add(it["key"])
            if rub == "consultation":
                c = P.parse_consultation(html, it["url"])
                it.update(title=c["title"] or it["title"], date=it.get("date") or c["online"] or c["start"],
                          autorite=c["autorite"] or it.get("autorite", ""), fin=c["end"], type=c["type"] or
                          "Consultation", text=(f"Type : {c['type']}. Fondement : {c['fondement']}. Autorité : "
                                                f"{c['autorite']}. Du {c['start']} au {c['end']}. {c['statut']}\n"
                                                + c["body"]), acces=c["acces"])
            else:
                a = P.parse_article(html, it["url"])
                it.update(title=a["title"] or it["title"], date=it.get("date") or a["date"] or TODAY,
                          text=(a["chapo"] + "\n" + a["text"]), themes=a["tags"][:6],
                          type=it.get("type") or (it["url"].split("vie-publique.fr/")[-1].split("/")[0]
                                                   .replace("-", " ").capitalize()))
                if days_since(it["date"]) > lim["jours_historique_fil"] and rub != "comprendre":
                    continue
            it["cands"] = self.candidates(it["title"] + " " + it["text"][:3000])
            ready.append(it)
        # résumés et rattachements par l'IA, par groupes
        size = int(lim.get("elements_par_requete_ia", 6))
        for i in range(0, len(ready), size):
            batch = ready[i:i + size]
            try:
                res = self.summarize(batch)
            except V.QuotaExhausted as e:
                log.warning("Fil : quota IA épuisé (%s) — reprise au prochain passage", e)
                for it in ready[i:]:
                    done.discard(it["key"])
                break
            for j, it in enumerate(batch):
                r = res.get(str(j + 1)) or {}
                pert = r.get("pertinent", True) if it["rub"] in ("actualite", "rapport") else True
                if not pert:
                    continue
                codes = {f"T{n + 1}": did for n, did in enumerate(it["cands"])}
                linked = [codes[c] for c in (r.get("textes") or []) if c in codes]
                self.fil_add_now(dict(it, resume=r.get("resume") or it["text"][:600], textes=linked))
        self.st["fil_queue"] = [{k: v for k, v in i.items() if k not in ("text", "cands")}
                                for i in queue if i["key"] not in done]

    def fil_add_now(self, item):
        self.st["seen"][item["key"]] = TODAY
        self.st["pending"].append({k: v for k, v in item.items() if k not in ("text", "cands", "desc")})

    def summarize(self, batch):
        lines = [
            "Tu assistes un haut fonctionnaire qui suit l'activité législative française. Pour CHAQUE publication "
            "ci-dessous (site officiel vie-publique.fr), réponds en JSON.",
            F.NEUTRALITE,
            "Champs pour chaque publication :",
            '- "id" : identifiant fourni',
            '- "resume" : 2 à 4 phrases factuelles et neutres : objet, mesures ou constats principaux, '
            "acteur institutionnel, portée normative (texte législatif ou réglementaire concerné)",
            '- "pertinent" : true si la publication concerne l\'élaboration, l\'examen, le contrôle ou '
            "l'application d'une loi ou d'une ordonnance (y compris rapports d'évaluation ou de contrôle "
            "liés à un texte), false sinon",
            '- "textes" : codes des textes suivis (T1, T2…) auxquels la publication se rapporte directement, '
            "choisis UNIQUEMENT dans la liste proposée pour cette publication ([] si aucun)",
            'Réponds UNIQUEMENT avec {"items": [{...}, ...]}.',
            "",
        ]
        T = self.st["textes"]
        for j, it in enumerate(batch, 1):
            lines += [f"=== PUBLICATION id={j} ===", f"Rubrique : {RUBRIQUES[it['rub']][0]}",
                      f"Titre : {it['title']}", f"Date : {it.get('date') or 'inconnue'}"]
            if it["cands"]:
                lines.append("Textes suivis proposés : " + " | ".join(
                    f"T{n + 1} = {T[d]['title'][:160]}" for n, d in enumerate(it["cands"])))
            lines += ["Texte :", V.truncate_for_ai(it["text"], int(self.lim.get("caracteres_par_element", 5000))), ""]
        out = ask(self.llm, "\n".join(lines), "lite")
        items = out.get("items") if isinstance(out, dict) else out
        res = {}
        for x in items or []:
            if isinstance(x, dict):
                res[str(x.get("id"))] = x
        return res

    def publish_fil(self):
        """Écrit dans Notion les éléments du fil en attente."""
        if self.dry:
            for it in self.st["pending"]:
                print(json.dumps({k: it.get(k) for k in ("rub", "title", "date", "url", "resume", "textes")},
                                 ensure_ascii=False))
            return
        db = self.notion.ids["fil"]
        remaining = []
        for it in self.st["pending"]:
            if time_left(self.cfg) < 240:
                remaining.append(it)
                continue
            rel = [{"id": self.st["textes"][d]["page"]} for d in it.get("textes", [])
                   if d in self.st["textes"] and self.st["textes"][d].get("page")]
            props = {
                "Titre": title_prop(it["title"] or it["url"]),
                "Rubrique": {"select": {"name": RUBRIQUES[it["rub"]][0]}},
                "Résumé": text_prop(it.get("resume", "")),
                "Lien": {"url": it["url"]},
                "Texte lié": {"relation": rel[:10]},
            }
            if it.get("type"):
                props["Type"] = {"select": {"name": V.opt(it["type"])[:90]}}
            if it.get("date"):
                props["Date"] = {"date": {"start": it["date"][:10]}}
            if it.get("themes"):
                props["Thèmes"] = {"multi_select": [{"name": V.opt(x)} for x in it["themes"][:6] if V.opt(x)]}
            if it.get("autorite"):
                props["Autorité"] = text_prop(it["autorite"])
            if it.get("fin"):
                props["Fin de consultation"] = {"date": {"start": it["fin"][:10]}}
            try:
                page = self.notion.create_page(db, props)
            except RuntimeError as e:
                log.warning("Fil : échec Notion pour %s : %s", it["url"], e)
                it["tries"] = it.get("tries", 0) + 1
                if it["tries"] < 3:
                    remaining.append(it)
                continue
            self.counts["fil"] += 1
            self.st["fil"].append({"i": it["key"], "rub": RUBRIQUES[it["rub"]][0], "t": it["title"][:300],
                                   "d": (it.get("date") or "")[:10], "u": it["url"],
                                   "r": (it.get("resume") or "")[:1500], "x": it.get("textes", []),
                                   "p": page.get("id")})
            for d in it.get("textes", []):
                if d in self.st["textes"]:
                    self.st["textes"][d]["fiche_dirty"] = True
        self.st["pending"] = remaining

    def linked_items(self, did):
        return sorted([f for f in self.st["fil"] if did in f.get("x", [])], key=lambda f: f.get("d", ""),
                      reverse=True)

    # ------------------------------------------------------------------
    # 5. Notion : tableau de bord et alertes
    # ------------------------------------------------------------------
    def starred(self, t):
        return bool(t.get("star")) and days_since(t["star"]) <= self.cfg["fiches"].get("etoile_jours", 3)

    def sync_dashboard(self):
        if self.dry:
            return
        db = self.notion.ids["dash"]
        n_new = n_upd = 0
        for t in sorted(self.st["textes"].values(), key=lambda x: x.get("first_seen", "")):
            if time_left(self.cfg) < 300:
                break
            if not t.get("fetched"):
                continue  # pas encore lu : sa ligne sera créée au passage suivant
            star = self.starred(t)
            props = dash_props(t, star)
            sig = P.sha(json.dumps(props, ensure_ascii=False, sort_keys=True))
            if t.get("page") and t.get("props_sig") == sig:
                continue
            try:
                if t.get("page"):
                    self.notion.update_page(t["page"], props)
                    n_upd += 1
                else:
                    blocks = F.render(t, None, [], NOW)
                    page = self.notion.create_page(db, props)
                    t["page"] = page["id"]
                    t.setdefault("fiche", {})
                    F.write(self.notion, t["page"], t, blocks)
                    t["fiche"]["det_sig"] = self.det_sig(t)
                    n_new += 1
                t["props_sig"] = sig
            except RuntimeError as e:
                log.warning("Tableau de bord : échec pour %s : %s", t["title"][:60], e)
                if "Could not find" in str(e) or "archived" in str(e):
                    t.pop("page", None)
        log.info("Tableau de bord : %d lignes créées, %d mises à jour", n_new, n_upd)

    def publish_alerts(self):
        if self.dry:
            return
        db = self.notion.ids["alerts"]
        queue = self.st.setdefault("alerts_queue", []) + [{"did": d, **e} for d, e in self.events_run]
        rest = []
        for a in queue:
            t = self.st["textes"].get(a["did"])
            if not t or not t.get("page"):
                a["tries"] = a.get("tries", 0) + 1
                if a["tries"] < 5:
                    rest.append(a)
                continue
            short = short_title(t)
            props = {"Événement": title_prop(f"{a['type']} — {short}"[:300]),
                     "Type d'événement": {"select": {"name": V.opt(a["type"])}},
                     "Date": {"date": {"start": (a.get("date") or TODAY)[:10]}},
                     "Texte": {"relation": [{"id": t["page"]}]},
                     "Stade": {"select": {"name": a.get("stage") or t["stage"]}},
                     "Lien": {"url": a.get("url") or t["url"]}}
            try:
                self.notion.create_page(db, props, [F.B("paragraph", a["text"])])
            except RuntimeError as e:
                log.warning("Alerte non publiée : %s", e)
                rest.append(a)
        self.st["alerts_queue"] = rest[-500:]

    # ------------------------------------------------------------------
    # 6. Fiches
    # ------------------------------------------------------------------
    def det_sig(self, t):
        e = t.get("eche") or {}
        return P.sha([s["k"] for s in t.get("steps", [])], [j["url"] for j in t.get("jorf", [])], t.get("stage"),
                     e.get("pub"), e.get("total"), (t.get("pano_data") or {}).get("status"),
                     len(t.get("events", [])), t.get("page"), self.starred(t))

    def level(self, t):
        if t.get("pano_url") or t["stage"] == P.STAGES[8]:
            return "Complète"
        if t["stage"] in (P.STAGE_REJET, P.STAGE_ORDONNANCE) or days_since(t.get("stage_date")) >= 540:
            return "Allégée"
        if t["stage"] == P.STAGES[0] and t.get("nature") != "Projet de loi":
            return "Allégée"
        return "Complète"

    def fetch_panorama_text(self, t):
        html = self.fetch.get(t["pano_url"]) if t.get("pano_url") else None
        if not html:
            return ""
        p = P.parse_panorama(html, t["pano_url"])
        return "\n".join(x for x in (p["title"], p["chapo"], "État : " + p["status"], "Historique : " +
                                     p["historique"], p["body"]) if x)

    def fetch_tab(self, t, kind):
        if kind == "EXPOSE_MOTIFS" and not any("expose" in P.norm(x) for x in t.get("tabs", [])):
            return ""
        html = self.fetch.get(t["url"] + "?detailType=EXPOSE_MOTIFS&detailId=")
        return P.parse_detail_text(html or "")

    def fiches(self):
        if self.dry:
            return
        fc = self.cfg["fiches"]
        jobs = []
        for t in self.st["textes"].values():
            if not t.get("page"):
                continue
            fs = t.setdefault("fiche", {})
            lvl = self.level(t)
            recent = bool(t.get("star")) and days_since(t["star"]) <= 1
            if not fs.get("data"):
                prio = (0 if recent else 1 if lvl == "Complète" and t["stage"] != P.STAGES[8] else
                        2 if lvl == "Complète" else 3)
                jobs.append((prio, days_since(t.get("stage_date")), t["id"], lvl))
            elif t.get("fiche_dirty") or fs.get("level") != lvl:
                if recent or days_since(fs.get("upd")) >= fc.get("jours_entre_mises_a_jour", 2):
                    jobs.append((0 if recent else 4, 0, t["id"], lvl))
            elif fs.get("det_sig") != self.det_sig(t):
                jobs.append((9, 0, t["id"], None))  # simple remise à jour des parties calculées
        jobs.sort(key=lambda j: (j[0], j[1]))
        n_ai = 0
        for prio, _, did, lvl in jobs:
            if time_left(self.cfg) < 180:
                break
            t = self.st["textes"][did]
            if lvl is None:
                try:
                    self.render_fiche(t, t["fiche"].get("data"), t["fiche"].get("sources", []))
                except RuntimeError as e:
                    log.warning("Fiche %s non remise à jour : %s", t["title"][:60], e)
                continue
            if n_ai >= fc.get("fiches_par_passage", 12):
                continue
            try:
                self.build_fiche(t, lvl)
                n_ai += 1
                self.counts["fiches"] += 1
            except V.QuotaExhausted as e:
                log.warning("Fiches : quota IA épuisé (%s) — reprise au prochain passage", e)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("Fiche « %s » non rédigée : %s", t["title"][:60], e)
            save_state(self.st)

    def build_fiche(self, t, lvl):
        sources, texts = F.collect_materials(self, t, self.fetch, ext_get, self.cfg)
        fs = t["fiche"]
        prompt = F.build_prompt(t, lvl, fs.get("data"), sources, texts)
        data = F.clean_data(ask(self.llm, prompt, "flash", 16384), lvl)
        fs.update(data=data, level=lvl, upd=NOW.isoformat(), sources=sources)
        t["fiche_dirty"] = False
        self.render_fiche(t, data, sources)
        t["props_sig"] = None  # intitulé court et colonne « Fiche » à mettre à jour
        log.info("  fiche %s « %s » rédigée (%d sources)", lvl.lower(), data.get("intitule_court") or t["title"][:60],
                 len(sources))

    def render_fiche(self, t, data, sources):
        recent = [e for e in t.get("events", []) if days_since(e.get("seen")) <= self.cfg["fiches"].get("etoile_jours", 3)]
        blocks = F.render(t, data, sources, NOW, recent)
        F.write(self.notion, t["page"], t, blocks)
        t["fiche"]["det_sig"] = self.det_sig(t)

    # ------------------------------------------------------------------
    # 7. Synthèse en haut de page
    # ------------------------------------------------------------------
    def summary(self):
        if self.dry:
            return
        mem = self.st["notion"]
        if not mem.get("summary_anchor"):
            return
        T = list(self.st["textes"].values())
        counts = {}
        for t in T:
            counts[t["stage"]] = counts.get(t["stage"], 0) + 1
        blocks = [F.B("paragraph", f"Au {NOW:%d/%m/%Y} à {(NOW + dt.timedelta(hours=2)):%H:%M} : {len(T)} textes "
                                   f"suivis ({self.cfg.get('legislature', 17)}e législature).", color="gray")]
        en_cours = [s for s in P.STAGES[:8]]
        line = " · ".join(f"{s.split(' · ')[-1]} : {counts.get(s, 0)}" for s in en_cours if counts.get(s))
        if line:
            blocks.append(F.callout("En cours d'examen — " + line, "🏛️", "blue_background"))
        promul = counts.get(P.STAGES[8], 0)
        blocks.append(F.callout(f"Lois promulguées : {promul} · Ordonnances : {counts.get(P.STAGE_ORDONNANCE, 0)} · "
                                f"Rejetés ou retirés : {counts.get(P.STAGE_REJET, 0)}", "📜", "green_background"))
        apps = [t for t in T if t["stage"] == P.STAGES[8] and (t.get("eche") or {}).get("total")]
        if apps:
            tot = sum(t["eche"]["total"] for t in apps)
            pub = sum(t["eche"]["pub"] for t in apps)
            blocks.append(F.callout(f"Application des lois suivies : {pub} mesures réglementaires publiées sur {tot} "
                                    f"attendues ({round(100 * pub / tot)} %).", "⚙️", "gray_background"))
        # Où en sont les textes ? Une liste dépliable par stade (les plus récemment actifs d'abord)
        blocks.append(F.B("heading_3", "Textes en cours, par stade"))
        for s in P.STAGES[:8]:
            ts = sorted([t for t in T if t["stage"] == s], key=lambda x: x.get("stage_date") or "", reverse=True)
            if not ts:
                continue
            kids = []
            for t in ts[:60]:
                url = "https://www.notion.so/" + t["page"].replace("-", "") if t.get("page") else t["url"]
                kids.append(F.B("bulleted_list_item", "", [
                    F.link_rt(short_title(t)[:180], url),
                    F.plain_rt(f" — {t.get('stage_label', '')}" + (f" ({F.fr_date(t.get('stage_date'))})"
                                                                     if t.get("stage_date") else ""), "gray")]))
            if len(ts) > 60:
                kids.append(F.B("paragraph", f"… et {len(ts) - 60} autres (filtrez le tableau de bord sur ce stade).",
                                color="gray"))
            blocks.append({"object": "block", "type": "toggle", "toggle": {
                "rich_text": [F.plain_rt(f"{s} — {len(ts)} texte(s)", bold=True)], "children": kids}})
        evs = sorted([(e.get("seen", ""), e.get("date") or "", t, e) for t in T for e in t.get("events", [])],
                     key=lambda x: (x[0], x[1]), reverse=True)[:15]
        if evs:
            blocks.append(F.B("heading_3", "Dernières alertes"))
            for _, _, t, e in evs:
                short = short_title(t)
                url = "https://www.notion.so/" + t["page"].replace("-", "") if t.get("page") else t["url"]
                blocks.append(F.B("bulleted_list_item", "", [
                    F.plain_rt(F.fr_date(e.get("date")) + " — ", "gray"), F.plain_rt(e["type"] + " : ", bold=True),
                    F.link_rt(short[:150], url), F.plain_rt(" — " + e["text"][:250], "gray")]))
        sig = P.sha(json.dumps(blocks, ensure_ascii=False))
        if sig == mem.get("summary_sig"):
            return
        try:
            if mem.get("summary"):
                try:
                    self.notion.req("DELETE", f"/blocks/{mem['summary']}")
                except RuntimeError:
                    pass
            res = self.notion.req("PATCH", f"/blocks/{self.notion.page}/children", {
                "after": mem["summary_anchor"], "children": [{"object": "block", "type": "synced_block",
                                                             "synced_block": {"synced_from": None,
                                                                              "children": blocks[:100]}}]})
            mem["summary"] = res["results"][0]["id"]
            mem["summary_sig"] = sig
        except RuntimeError as e:
            log.warning("Synthèse non mise à jour : %s", e)

    def refresh_stars(self):
        """Retire l'étoile des textes dont la dernière alerte date de plus de N jours."""
        for t in self.st["textes"].values():
            if t.get("star") and not self.starred(t):
                t["star"] = None
                t["props_sig"] = None


# =====================================================================
# Passage normal
# =====================================================================
def run(args):
    cfg = load_config()
    st = load_state()
    fetch = Fetcher()
    notion = LegiNotion(cfg)
    if not args.dry_run:
        notion.ensure(st)
    llm = make_llm(st, cfg)
    L = Legi(cfg, st, fetch, notion, llm, args.dry_run)
    try:
        L.inventory()
        L.panoramas()
        L.process_panoramas()
        L.refresh_dossiers()
        L.process_panoramas()  # panoramas découverts dans les dossiers
        st["initialized"] = True
        if not args.dry_run:
            save_state(st)
        L.refresh_stars()
        L.sync_dashboard()
        L.publish_alerts()
        if not args.dry_run:
            save_state(st)
        if time_left(cfg) > 900:
            L.collect_fil()
            L.process_fil()
        L.publish_fil()
        if not args.dry_run:
            save_state(st)
        L.fiches()
        L.sync_dashboard()  # intitulés courts et colonnes « Fiche »
        L.summary()
    finally:
        fetch.close()
        if not args.dry_run:
            save_state(st)
    c = L.counts
    msg = (f"Terminé : {c['textes_nouveaux']} nouveaux textes, {c['dossiers_lus']} dossiers relus, "
           f"{c['evenements']} alerte(s), {c['fil']} élément(s) ajoutés au fil, {c['fiches']} fiche(s) rédigée(s). "
           f"Accès au site : {fetch.mode} ({fetch.n_ok} pages lues, {fetch.n_fail} échecs). Appels IA : {llm.calls}")
    log.info(msg)
    summ = V.env("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"### Veille législative\n\n{msg}\n\n" + "\n".join(f"- {n}" for n in fetch.notes) + "\n")


# =====================================================================
# Diagnostic
# =====================================================================
def diagnostic(args):
    cfg = load_config()
    out = [f"# Diagnostic de la veille législative — {NOW:%d/%m/%Y %H:%M} UTC", ""]
    fetch = Fetcher()
    try:
        out.append("## Accès à vie-publique.fr")
        r = V.http_get(P.BASE + "/lois-feeds.xml")
        if r is None:
            out.append("- Lecture directe : site injoignable")
        else:
            out.append(f"- Lecture directe : HTTP {r.status_code}, "
                       f"{'flux RSS valide' if P.is_rss(r.text) else 'PAS un flux RSS (vérification anti-robots ?)'}")
            if not P.is_rss(r.text):
                debut = re.sub(r"\s+", " ", r.text[:300])
                out.append(f"  - début de la réponse : `{debut}`")
        tests = [("Autour de la loi", "/autour-de-la-loi", "html"), ("Flux panoramas", "/lois-feeds.xml", "xml"),
                 ("Flux consultations", "/debats-consultations-feeds.xml", "xml"),
                 ("Flux actualités", "/actualites-feeds.xml", "xml"), ("Flux rapports", "/rapports-feeds.xml", "xml"),
                 ("Liste des dossiers", f"/liste/dossierslegislatifs/{cfg.get('legislature', 17)}", "html"),
                 ("Liste des panoramas", "/loi", "html"), ("Liste des consultations", "/consultations", "html")]
        pages = {}
        for name, path, kind in tests:
            t0 = time.time()
            txt = fetch.get(P.BASE + path, kind)
            pages[path] = txt
            if txt is None:
                out.append(f"- {name} : ÉCHEC")
                continue
            if kind == "xml":
                n = len(P.parse_rss(txt))
                detail = f"{n} éléments"
            elif "dossierslegislatifs" in path:
                items = P.parse_list_dossiers(txt)
                cats = {}
                for i in items:
                    cats[f"{i['section']} / {i['cat']}"] = cats.get(f"{i['section']} / {i['cat']}", 0) + 1
                detail = f"{len(items)} dossiers — " + ", ".join(f"{k} : {v}" for k, v in cats.items())
            elif path == "/autour-de-la-loi":
                detail = f"{len(P.parse_autour_de_la_loi(txt))} liens de contenu"
            else:
                detail = f"{len(P.parse_cards(txt))} cartes"
            out.append(f"- {name} : OK ({detail}, {time.time() - t0:.1f} s)")
        out.append(f"- Mode d'accès retenu : **{fetch.mode}**")
        out += [f"- {n}" for n in fetch.notes]
        out.append("")
        out.append("## Lecture d'exemples")
        lst = P.parse_list_dossiers(pages.get(f"/liste/dossierslegislatifs/{cfg.get('legislature', 17)}") or "")
        samples = [x for x in lst if "preparation" in P.norm(x["section"])][:2] + \
                  [x for x in lst if P.norm(x["cat"]).startswith("lois")][:2]
        for s in samples:
            html = fetch.get(s["url"])
            if not html:
                out.append(f"- {s['title'][:90]} : illisible")
                continue
            d = P.parse_dossier(html, s["url"])
            stg = P.compute_stage(d, None, P.norm(s["section"]).startswith("textes publies"))
            out.append(f"- **{s['title'][:110]}** — {len(d['steps'])} étapes, stade « {stg['stage']} » "
                       f"({stg['label']}), panorama : {'oui' if d['panorama'] else 'non'}, onglets : "
                       f"{', '.join(x['label'] for x in d['tabs']) or 'aucun'}")
            if any("echeancier" in P.norm(x["label"]) for x in d["tabs"]):
                eh = fetch.get(s["url"] + "?detailType=CONTENU&detailId=1")
                rows = P.parse_echeancier(eh or "")
                stt = P.echeancier_stats(rows)
                out.append(f"  - échéancier : {len(rows)} lignes, {stt['pub']}/{stt['total']} mesures publiées")
            if d["panorama"]:
                ph = fetch.get(d["panorama"])
                p = P.parse_panorama(ph or "", d["panorama"])
                out.append(f"  - panorama : « {p['title'][:90]} », frise {len(p['timeline'])} étapes, état : "
                           f"{p['status'][:150]}")
        out.append("")
        out.append("## IA et Notion")
        st = load_state()
        llm = make_llm({}, cfg)
        out.append(f"- Gemini : {'clé GEMINI_API_KEY_LEGI' if V.env('GEMINI_API_KEY_LEGI') else 'clé GEMINI_API_KEY (partagée avec la veille presse)'}"
                   f" — modèles : {', '.join(llm.gemini_models()) or 'aucun'}")
        try:
            res = ask(llm, 'Réponds {"ok": true} en JSON.', "lite", 50)
            out.append(f"- Test IA : OK ({res})")
        except Exception as e:  # noqa: BLE001
            out.append(f"- Test IA : ÉCHEC — {e}")
        try:
            n = LegiNotion(cfg)
            found = n.find_dbs() if n.n.token else {}
            out.append(f"- Notion : jeton {'présent' if n.n.token else 'MANQUANT'}, page "
                       f"{'renseignée' if n.page else 'MANQUANTE (secret NOTION_PAGE_ID_LEGI)'}, bases déjà "
                       f"présentes : {', '.join(found) or 'aucune (elles seront créées au premier passage)'}")
            if n.page:
                p = n.req("GET", f"/pages/{n.page}")
                out.append("  - accès à la page « Veille législative » : OK")
        except Exception as e:  # noqa: BLE001
            out.append(f"- Notion : ÉCHEC — {e}")
        out.append(f"- Mémoire : {len(st['textes'])} textes déjà suivis")
    finally:
        fetch.close()
    text = "\n".join(out)
    summ = V.env("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


def main():
    ap = argparse.ArgumentParser(description="Veille législative automatique")
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
