#!/usr/bin/env python3
"""
Veille des rapports publics (veille « administrative »)
-------------------------------------------------------
Suit les rapports des grands organes publics (Cour des comptes, CPO, HCFP, Sénat,
Assemblée nationale, Haut-commissariat à la stratégie et au plan, HCFiPS, HCAAM,
HCFEA, Haut Conseil pour le climat, COR, CAE, CESE, inspections générales, autorités
indépendantes…) et la rubrique « Rapports publics » de vie-publique.fr.

Pour chaque nouveau rapport :
  1. alerte immédiate dans Notion (ligne ⭐ avec le lien vers le rapport) ;
  2. lecture du rapport intégral (PDF) ;
  3. relevé MOT POUR MOT des recommandations, directement dans le texte du rapport ;
  4. fiche très détaillée rédigée par l'IA (contexte, chiffres clés, constats et
     argumentaire, recommandations, enjeux) ;
  5. rattachement aux textes de loi en cours ou récents suivis par la veille législative.

Usage :
  python rapports.py                      # passage normal
  python rapports.py --mode diagnostic    # teste chaque organe, l'IA et Notion ; n'écrit rien
"""
import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import time
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup

import legi_fiches as F
import legi_parse as P
import legislatif as L
import veille as V

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
STATE_PATH = os.path.join(STATE_DIR, "rapports.json")
LEGI_STATE_PATH = os.path.join(STATE_DIR, "legislatif.json")
START = time.time()
NOW = V.NOW
TODAY = NOW.strftime("%Y-%m-%d")
log = logging.getLogger("veille")

FAMILLES = {
    "Juridictions financières": "red",
    "Parlement": "blue",
    "Hauts conseils et organes d'expertise": "purple",
    "Inspections générales": "orange",
    "Autorités indépendantes": "green",
    "Autres organes publics": "gray",
}


def load_config():
    with open(os.path.join(ROOT, "config-rapports.yaml"), encoding="utf-8") as f:
        c = yaml.safe_load(f) or {}
    with open(os.path.join(ROOT, "sources-rapports.yaml"), encoding="utf-8") as f:
        c["sources"] = yaml.safe_load(f) or []
    return c


def time_left(cfg):
    return cfg["limites"]["duree_max_minutes"] * 60 - (time.time() - START)


def days_since(d):
    x = V.parse_date(d) if d else None
    return (NOW - x).total_seconds() / 86400 if x else 9999


# =====================================================================
# Recommandations : relevé mot pour mot dans le texte du rapport
# =====================================================================
REC_START = re.compile(
    r"^\s*(?:[-•▪►]\s*)?(?:\d{1,3}[.)]\s+)?(?P<kind>Recommandations?|Propositions?|Préconisations?|Orientations?|Pistes?)"
    r"\s*(?:n\s*°|no|N°|numéro)?\s*(?P<num>\d{1,3})(?P<sub>\s*[a-z](?![a-z]))?\s*"
    r"(?P<sep>[:.\-–—)]|\((?P<dest>[^)]{0,200})\)\s*[:.\-–—]?)?\s*(?P<rest>.*)$", re.I)
REC_SHORT = re.compile(r"^\s*(?P<kind>[RP])\s?(?P<num>\d{1,3})\s*[:.\-–—)]\s*(?P<rest>.+)$")
HEADING = re.compile(r"^\s*(?:(?:chapitre|partie|titre|annexe|introduction|conclusion|sommaire|synthèse|"
                     r"liste des|tableau|graphique|figure|source|note)\b|[IVX]+\s*[-.–]\s|\d+(?:\.\d+)+\s)", re.I)


def extract_recommendations(text, max_items=150):
    """Relève les recommandations / propositions numérotées, mot pour mot.
    Renvoie [{"num": "1", "kind": "Recommandation", "dest": "...", "texte": "..."}] (première occurrence
    de chaque numéro, qui est en général la liste récapitulative)."""
    if not text:
        return []
    lines = [l.rstrip() for l in text.splitlines()]
    found, cur = [], None

    def close():
        nonlocal cur
        if cur:
            t = re.sub(r"\s+", " ", " ".join(cur["parts"])).strip()
            t = re.sub(r"\s*\.{4,}\s*\d+\s*$", "", t)  # points de conduite d'un sommaire
            if len(t) >= 25:
                found.append({"num": cur["num"], "kind": cur["kind"], "dest": cur.get("dest") or "", "texte": t[:1500]})
            cur = None

    for raw in lines:
        line = raw.strip()
        m = REC_START.match(line)
        short = None if m else REC_SHORT.match(line)
        if m and (m.group("sep") or m.group("rest")):
            kind = m.group("kind").capitalize().rstrip("s")
            if kind.lower().startswith("piste"):
                kind = "Piste"
            close()
            num = m.group("num") + ((m.group("sub") or "").strip())
            cur = {"num": num, "kind": kind, "dest": (m.group("dest") or "").strip(),
                   "parts": [m.group("rest").strip()] if m.group("rest") else []}
            continue
        if short:
            close()
            cur = {"num": short.group("num"), "kind": "Recommandation" if short.group("kind") == "R" else "Proposition",
                   "dest": "", "parts": [short.group("rest").strip()]}
            continue
        if cur is None:
            continue
        if not line:
            if cur["parts"] and re.search(r"[.;:!?»)]$", cur["parts"][-1]):
                close()
            continue
        if HEADING.match(line) or len(" ".join(cur["parts"])) > 1400:
            close()
            continue
        cur["parts"].append(line)
    close()
    # première occurrence de chaque (type, numéro) ; on garde la série la plus fournie
    by_kind = {}
    for r in found:
        by_kind.setdefault(r["kind"], {})
        by_kind[r["kind"]].setdefault(r["num"], r)
    if not by_kind:
        return []
    kind = max(by_kind, key=lambda k: len(by_kind[k]))
    recs = list(by_kind[kind].values())

    def key(r):
        m = re.match(r"(\d+)\s*([a-z]?)", r["num"])
        return (int(m.group(1)), m.group(2)) if m else (9999, "")
    recs.sort(key=key)
    # une seule recommandation « 1 » isolée est souvent un faux positif
    if len(recs) == 1 and len(recs[0]["texte"]) < 60:
        return []
    return recs[:max_items]


# =====================================================================
# Lecture d'un rapport (page + PDF)
# =====================================================================
PDF_RE = re.compile(r"\.pdf(\?|#|$)", re.I)


def pdf_candidates(html, base):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"].strip())
        lab = P.norm(a.get_text(" ", strip=True) + " " + (a.get("title") or "") + " " + (a.get("aria-label") or ""))
        if not (PDF_RE.search(href) or "download" in href.lower() and ("rapport" in lab or "pdf" in lab)):
            continue
        h = P.norm(href)
        score = 0
        if "rapport" in lab or "rapport" in h:
            score += 3
        if "telecharger" in lab or "download" in lab:
            score += 2
        if "integral" in lab or "complet" in lab:
            score += 3
        if "synthese" in lab or "synthese" in h or "essentiel" in lab:
            score -= 1
        if re.search(r"annexe|communique|cp_|dossier de presse|presse|infographie|errat", lab + " " + h):
            score -= 4
        if V.base_host(href) == V.base_host(base):
            score += 1
        out.append((score, href, lab))
    seen, res = set(), []
    for s, h, lab in sorted(out, key=lambda x: -x[0]):
        if h not in seen:
            seen.add(h)
            res.append({"url": h, "score": s, "synthese": "synthese" in lab or "essentiel" in lab})
    return res


def read_report(url, fetch_vp=None, max_pages=400):
    """Renvoie {"html_text", "pdf_text", "pdf_url", "synth_text", "title", "date"}."""
    out = {"html_text": "", "pdf_text": "", "pdf_url": None, "synth_text": "", "title": "", "date": None}
    html = None
    if PDF_RE.search(url):
        r = V.http_get(url, timeout=120)
        if r is not None and r.status_code < 400:
            out["pdf_text"] = V.pdf_to_text(r.content, max_pages=max_pages)
            out["pdf_url"] = url
        return out
    if fetch_vp and "vie-publique.fr" in url:
        html = fetch_vp.get(url)
        final = url
    else:
        r = V.http_get(url, timeout=60)
        if r is not None and r.status_code < 400 and "pdf" in r.headers.get("content-type", "").lower():
            out["pdf_text"] = V.pdf_to_text(r.content, max_pages=max_pages)
            out["pdf_url"] = r.url
            return out
        html = r.text if r is not None and r.status_code < 400 else None
        final = r.url if r is not None else url
    if not html:
        return out
    try:
        out["html_text"] = V.clean_text(V.trafilatura.extract(html, url=final, include_comments=False,
                                                              include_tables=True, favor_recall=True) or "")
        md = V.trafilatura.extract_metadata(html)
        if md:
            out["title"], out["date"] = md.title or "", md.date
    except Exception:  # noqa: BLE001
        pass
    cands = pdf_candidates(html, final)
    main = next((c for c in cands if not c["synthese"]), None) or (cands[0] if cands else None)
    synth = next((c for c in cands if c["synthese"] and c is not main), None)
    for c, key in ((main, "pdf_text"), (synth, "synth_text")):
        if not c:
            continue
        r = V.http_get(c["url"], timeout=150)
        if r is not None and r.status_code < 400 and (b"%PDF" in r.content[:1024]):
            out[key] = V.pdf_to_text(r.content, max_pages=max_pages if key == "pdf_text" else 40)
            if key == "pdf_text":
                out["pdf_url"] = c["url"]
    return out


def compose_for_ai(doc, recs, budget):
    """Texte envoyé à l'IA : début (synthèse, introduction), recommandations, milieu échantillonné, fin."""
    full = doc["pdf_text"] if len(doc["pdf_text"]) > len(doc["html_text"]) else doc["html_text"]
    parts = []
    if doc.get("synth_text"):
        parts.append("=== SYNTHÈSE PUBLIÉE ===\n" + doc["synth_text"][: budget // 4])
    rec_txt = "\n".join(f"{r['kind']} n° {r['num']}{' (' + r['dest'] + ')' if r['dest'] else ''} : {r['texte']}"
                        for r in recs)
    if rec_txt:
        parts.append("=== RECOMMANDATIONS RELEVÉES MOT POUR MOT DANS LE RAPPORT ===\n" + rec_txt[: budget // 3])
    rest = budget - sum(len(p) for p in parts)
    if len(full) <= rest:
        parts.append("=== TEXTE DU RAPPORT ===\n" + full)
    else:
        head, tail = int(rest * 0.55), int(rest * 0.15)
        mid = rest - head - tail
        m0 = len(full) // 2 - mid // 2
        parts.append("=== TEXTE DU RAPPORT (début) ===\n" + full[:head])
        parts.append("=== EXTRAIT (milieu du rapport) ===\n" + full[m0:m0 + mid])
        parts.append("=== TEXTE DU RAPPORT (fin) ===\n" + full[-tail:])
    return "\n\n".join(parts), len(full)


# =====================================================================
# Thèmes
# =====================================================================
def theme_list(cfg):
    return [t["nom"] if isinstance(t, dict) else t for t in cfg.get("themes", [])]


def theme_colors(cfg):
    return {t["nom"]: t.get("couleur", "default") for t in cfg.get("themes", []) if isinstance(t, dict)}


# =====================================================================
# Lien avec la veille législative
# =====================================================================
class LegiLink:
    """Textes suivis par la veille législative (lecture seule de sa mémoire)."""

    STOP = set("loi lois projet proposition visant vise relative relatif portant diverses dispositions mesures "
               "ratifiant ordonnance ordonnances code article articles renforcer ameliorer creation creer cadre "
               "national nationale francaise france etat public publique publics publiques pour dans avec sans "
               "leurs entre afin certaines plusieurs adaptation application organique texte rapport rapports "
               "politique politiques mise oeuvre enjeux".split())

    def __init__(self, cfg):
        self.ok = False
        self.textes, self.dash = {}, None
        try:
            with open(LEGI_STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log.warning("Mémoire de la veille législative absente : pas de rattachement aux lois pour ce passage")
            return
        self.dash = (st.get("notion") or {}).get("dash")
        months = cfg["liens_lois"].get("mois_lois_promulguees", 24)
        for did, t in (st.get("textes") or {}).items():
            stage = t.get("stage", "")
            if stage == P.STAGE_REJET:
                continue
            if stage in (P.STAGES[8], P.STAGE_ORDONNANCE) and days_since(t.get("stage_date")) > months * 30.5:
                continue
            short = (t.get("fiche", {}).get("data") or {}).get("intitule_court") or \
                (t.get("pano_data") or {}).get("title") or t.get("title", "")
            self.textes[did] = {"id": did, "title": t.get("title", ""), "short": short, "stage": stage,
                                "page": t.get("page"), "themes": t.get("themes") or [],
                                "chapo": (t.get("pano_data") or {}).get("chapo", ""),
                                "en_bref": (t.get("fiche", {}).get("data") or {}).get("en_bref", "")}
        self.ok = bool(self.textes)
        idx, df = {}, {}
        for did, t in self.textes.items():
            toks = self.toks(" ".join([t["title"], t["short"], " ".join(t["themes"]), t["chapo"][:400]]))
            idx[did] = toks
            for w in toks:
                df[w] = df.get(w, 0) + 1
        n = max(len(idx), 1)
        self.idf = {w: math.log(1 + n / c) for w, c in df.items()}
        self.idx = idx
        log.info("Veille législative : %d textes disponibles pour les rattachements", len(self.textes))

    def toks(self, text):
        return {w for w in re.findall(r"[a-z]{4,}", P.norm(text)) if w not in self.STOP}

    def candidates(self, text, k=15):
        toks = self.toks(text)
        scored = []
        for did, tt in self.idx.items():
            inter = toks & tt
            if len(inter) < 2:
                continue
            scored.append((sum(self.idf.get(w, 0) for w in inter), did))
        scored.sort(reverse=True)
        return [d for s, d in scored[:k] if s >= 2.5]

    def describe(self, did):
        t = self.textes[did]
        return f"{t['short'][:160]} — {t['stage']}" + (f" — {t['en_bref'][:300] or t['chapo'][:300]}"
                                                       if (t['en_bref'] or t['chapo']) else "")


# =====================================================================
# Notion
# =====================================================================
class RapNotion:
    def __init__(self, cfg):
        self.cfg = cfg
        self.n = V.Notion({})
        self.n.page = V.env("NOTION_PAGE_ID_RAPPORTS")
        self.req = self.n.req
        self.page = self.n.clean_id(self.n.page) if self.n.page else ""
        self.db = None

    @staticmethod
    def is_ours(d):
        p = d.get("properties") or {}
        return not d.get("archived") and not d.get("in_trash") and "Organe" in p and "Recommandations" in p \
            and "Lois liées" in p

    def ensure(self, st, dash_id):
        if not self.n.token:
            raise RuntimeError("NOTION_TOKEN manquant")
        mem = st.setdefault("notion", {})
        if mem.get("db"):
            try:
                d = self.req("GET", f"/databases/{mem['db']}")
                if self.is_ours(d):
                    self.db = d["id"]
            except RuntimeError:
                pass
        if not self.db:
            res = self.req("POST", "/search", {"filter": {"property": "object", "value": "database"},
                                               "page_size": 100})
            for d in res.get("results", []):
                if self.is_ours(d):
                    self.db = d["id"]
                    break
        if not self.db:
            if not self.page:
                raise RuntimeError("NOTION_PAGE_ID_RAPPORTS manquant : impossible de créer la base des rapports")
            if not dash_id:
                raise RuntimeError("Tableau de bord législatif introuvable : lancez d'abord la veille législative")
            self.ensure_summary(st)
            self.db = self.create(dash_id)
        mem["db"] = self.db
        if not mem.get("summary_anchor"):
            self.ensure_summary(st)
        return self.db

    def ensure_summary(self, st):
        if not self.page:
            return
        res = self.req("PATCH", f"/blocks/{self.page}/children", {"children": [
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": V.rt("🆕 Derniers rapports")}},
            {"object": "block", "type": "synced_block", "synced_block": {"synced_from": None, "children": [
                F.B("paragraph", "La liste apparaîtra après le premier passage.", color="gray")]}}]})
        st["notion"]["summary_anchor"] = res["results"][0]["id"]
        st["notion"]["summary"] = res["results"][1]["id"]

    def create(self, dash_id):
        c = self.cfg
        props = {
            "Rapport": {"title": {}},
            "Organe": {"select": {}},
            "Famille": {"select": {"options": [{"name": k, "color": v} for k, v in FAMILLES.items()]}},
            "Type": {"select": {}},
            "Date": {"date": {}},
            "Thèmes": {"multi_select": {"options": [{"name": V.opt(k), "color": v}
                                                     for k, v in theme_colors(c).items()]}},
            "Lien avec la loi": {"select": {"options": [
                {"name": "Texte en cours d'examen", "color": "red"},
                {"name": "Loi récente (application)", "color": "orange"},
                {"name": "Sans texte lié", "color": "gray"}]}},
            "Recommandations": {"number": {}},
            "Résumé": {"rich_text": {}},
            "Lien": {"url": {}},
            "PDF": {"url": {}},
            "Fiche": {"select": {"options": [{"name": "Complète", "color": "green"},
                                             {"name": "En préparation", "color": "default"}]}},
            "Lu": {"checkbox": {}},
            "Ajouté le": {"created_time": {}},
            "Lois liées": {"relation": {"database_id": dash_id, "type": "dual_property", "dual_property": {}}},
        }
        d = self.req("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": self.page}, "is_inline": True,
            "icon": {"type": "emoji", "emoji": "📑"},
            "title": [{"type": "text", "text": {"content": "Rapports publics"}}], "properties": props})
        log.info("Base Notion « Rapports publics » créée")
        # la colonne réciproque apparaît dans le tableau de bord législatif : on lui donne un nom clair
        try:
            dd = self.req("GET", f"/databases/{dash_id}")
            for name, pr in dd.get("properties", {}).items():
                if pr.get("type") == "relation" and pr["relation"].get("database_id", "").replace("-", "") == \
                        d["id"].replace("-", "") and name != "Rapports liés":
                    self.req("PATCH", f"/databases/{dash_id}", {"properties": {name: {"name": "Rapports liés"}}})
        except RuntimeError as e:
            log.warning("Colonne « Rapports liés » non renommée : %s", e)
        return d["id"]


# =====================================================================
# Le programme
# =====================================================================
class Rapports:
    def __init__(self, cfg, st, notion, llm, legi, fetch_vp, dry=False):
        self.cfg, self.st, self.notion, self.llm, self.legi, self.vp, self.dry = cfg, st, notion, llm, legi, \
            fetch_vp, dry
        self.lim = cfg["limites"]
        self.counts = {"nouveaux": 0, "ecartes": 0, "fiches": 0}
        self.themes = theme_list(cfg)

    # ---------------- collecte ----------------
    def src_cfg(self):
        return {"limites": {"liens_max_par_page": self.lim.get("liens_max_par_page", 40)}}

    def collect(self):
        st, lim = self.st, self.lim
        first_src = st.setdefault("sources_init", {})
        new = []
        for src in self.cfg["sources"]:
            if time_left(self.cfg) < 600:
                break
            name = src["nom"]
            try:
                items, report = V.collect_source(src, st.setdefault("vcache", {}), self.src_cfg())
            except Exception as e:  # noqa: BLE001
                log.warning("%s : erreur %s", name, e)
                continue
            first = not first_src.get(name)
            n = 0
            for it in items:
                if not self.keep_title(src, it.get("title", "")):
                    continue
                k = V.url_key(it["url"])
                if k in st["seen"]:
                    continue
                a = days_since(it.get("date"))
                if a is not None and a != 9999 and a > lim["jours_premier_passage"]:
                    st["seen"][k] = TODAY
                    continue
                if first and (a == 9999) and it.get("from_page"):
                    st["seen"][k] = TODAY  # point de départ : la liste existante n'est pas retraitée
                    continue
                st["seen"][k] = TODAY
                new.append({"url": it["url"], "title": it.get("title", ""), "date": it.get("date"),
                            "desc": (it.get("feed_text") or "")[:1500], "organe": name,
                            "famille": src.get("famille", "Autres organes publics"), "source": name})
                n += 1
            first_src[name] = True
            log.info("%-60s %3d nouveaux", name[:60], n)
        new += self.collect_vie_publique()
        # doublons entre organes et vie-publique
        out = []
        for it in new:
            if self.duplicate(it):
                self.counts["ecartes"] += 1
                continue
            self.remember_title(it)
            out.append(it)
        st.setdefault("queue", []).extend(out)
        st["queue"].sort(key=lambda i: i.get("date") or "9999", reverse=True)
        self.counts["nouveaux"] = len(out)
        log.info("Rapports : %d nouveaux, %d en file d'attente", len(out), len(st["queue"]))

    def collect_vie_publique(self):
        st, out = self.st, []
        first = not st.get("vp_init")
        items = P.parse_rss(self.vp.get(f"{P.BASE}/rapports-feeds.xml", "xml") or "")
        pages = 3 if first else 1
        for n in range(pages):
            html = self.vp.get(f"{P.BASE}/rapports" + (f"?page={n}" if n else ""))
            for c in P.parse_cards(html or ""):
                aut = next((d.split(":", 1)[1].strip() for d in c["details"] if d.lower().startswith("auteur")), "")
                items.append({"url": c["url"], "title": c["title"], "date": c["date"], "desc": c["desc"],
                              "autorite": aut})
        for it in items:
            k = V.url_key(it["url"])
            if k in st["seen"]:
                continue
            st["seen"][k] = TODAY
            if days_since(it.get("date")) > self.lim["jours_premier_passage"]:
                continue
            organe, fam = self.organe_of(it.get("autorite") or "", it["title"])
            out.append({"url": it["url"], "title": it["title"], "date": it.get("date"), "desc": it.get("desc", ""),
                        "organe": organe, "famille": fam, "source": "vie-publique.fr"})
        st["vp_init"] = True
        log.info("%-60s %3d nouveaux", "vie-publique.fr (rapports publics)", len(out))
        return out

    def organe_of(self, auteurs, title):
        a = P.norm(auteurs + " " + title)
        for src in self.cfg["sources"]:
            names = [src["nom"]] + re.findall(r"\(([^)]+)\)", src["nom"])
            for nm in names:
                nn = P.norm(re.sub(r"\(.*?\)", "", nm)).strip()
                if nn and len(nn) > 3 and re.search(r"\b" + re.escape(nn) + r"\b", a):
                    return src["nom"], src.get("famille", "Autres organes publics")
        first = re.split(r"\s*;\s*", auteurs)[-1].strip() if auteurs else ""
        return (first[:90] or "Autre organe public"), "Autres organes publics"

    @staticmethod
    def keep_title(src, title):
        if src.get("titre_motif") and not re.search(src["titre_motif"], title or "", re.I):
            return False
        if src.get("titre_exclure") and re.search(src["titre_exclure"], title or "", re.I):
            return False
        return True

    def duplicate(self, it):
        toks = {w for w in re.findall(r"[a-z]{4,}", P.norm(it["title"]))}
        if len(toks) < 3:
            return False
        for prev in self.st.setdefault("titles", [])[-1500:]:
            pt = set(prev["t"])
            if pt and len(toks & pt) / len(toks | pt) >= 0.7:
                return True
        return False

    def remember_title(self, it):
        toks = sorted({w for w in re.findall(r"[a-z]{4,}", P.norm(it["title"]))})
        self.st.setdefault("titles", []).append({"t": toks, "d": TODAY})
        self.st["titles"] = self.st["titles"][-3000:]

    # ---------------- tri : vrai rapport ? portée nationale ? ----------------
    def triage(self):
        q = self.st.get("queue", [])
        todo = [i for i in q if "tri" not in i][: self.lim.get("tri_par_passage", 60)]
        if not todo:
            return
        for i in range(0, len(todo), 8):
            batch = todo[i:i + 8]
            lines = [
                "Tu fais le tri d'une veille de rapports publics pour un haut fonctionnaire. Pour CHAQUE "
                "publication, réponds en JSON.",
                'Champs : "id" ; "rapport" : true si c\'est un rapport, une étude, un avis, une note d\'analyse ou '
                "une évaluation substantielle (false pour une simple actualité, un communiqué, un événement, une "
                'offre, une page de présentation, un rapport d\'activité purement administratif) ; "portee" : '
                '"nationale" si le document concerne une politique publique, une institution ou un secteur à '
                'l\'échelle nationale ou européenne, "locale" s\'il ne concerne qu\'une collectivité, un établissement '
                'ou un organisme local isolé ; "type" : un type court (Rapport public thématique, Rapport '
                "d'information, Rapport de commission d'enquête, Avis, Note, Étude, Rapport annuel, Rapport de "
                "mission, Évaluation…) ; \"titre\" : le titre exact et complet du document.",
                'Réponds UNIQUEMENT avec {"items": [...]}.', ""]
            for j, it in enumerate(batch, 1):
                lines += [f"=== id={j} ===", f"Organe : {it['organe']}", f"Titre : {it['title']}",
                          f"Adresse : {it['url']}", f"Description : {it.get('desc', '')[:800]}", ""]
            try:
                out = L.ask(self.llm, "\n".join(lines), "lite")
            except V.QuotaExhausted as e:
                # sans IA disponible, l'alerte n'attend pas : les publications sont gardées sans tri
                log.warning("Tri : quota IA épuisé (%s) — publications gardées sans tri", e)
                for it in todo[i:]:
                    it["tri"] = {"rapport": True, "portee": "nationale", "type": "Rapport"}
                break
            res = {str(x.get("id")): x for x in (out.get("items") if isinstance(out, dict) else out) or []
                   if isinstance(x, dict)}
            for j, it in enumerate(batch, 1):
                r = res.get(str(j)) or {}
                it["tri"] = {"rapport": bool(r.get("rapport", True)), "portee": r.get("portee", "nationale"),
                             "type": r.get("type") or "Rapport"}
                if r.get("titre") and len(r["titre"]) > len(it["title"]):
                    it["title"] = r["titre"][:400]
        keep_local = self.cfg.get("garder_rapports_locaux", False)
        kept = []
        for it in q:
            t = it.get("tri")
            if t and (not t["rapport"] or (t["portee"] == "locale" and not keep_local)):
                self.counts["ecartes"] += 1
                log.info("  écarté (%s) : %s", "non-rapport" if not t["rapport"] else "portée locale", it["title"][:100])
                continue
            kept.append(it)
        self.st["queue"] = kept

    # ---------------- alerte immédiate ----------------
    def alert_rows(self):
        """Crée tout de suite la ligne Notion (⭐ + lien) des rapports triés, avant même la fiche."""
        if self.dry:
            return
        for it in self.st.get("queue", []):
            if it.get("page") or "tri" not in it or time_left(self.cfg) < 300:
                continue
            props = {
                "Rapport": L.title_prop(F.STAR + it["title"]),
                "Organe": {"select": {"name": V.opt(it["organe"])[:100]}},
                "Famille": {"select": {"name": it["famille"] if it["famille"] in FAMILLES else "Autres organes publics"}},
                "Type": {"select": {"name": V.opt(it["tri"]["type"])[:100]}},
                "Lien": {"url": it["url"]},
                "Fiche": {"select": {"name": "En préparation"}},
            }
            if it.get("date"):
                props["Date"] = {"date": {"start": it["date"][:10]}}
            try:
                page = self.notion.n.req("POST", "/pages", {
                    "parent": {"database_id": self.notion.db}, "properties": props, "children": [
                        F.callout("Nouveau rapport détecté. La fiche détaillée (contexte, chiffres, constats, "
                                  "recommandations mot pour mot, lien avec les lois) est en cours de rédaction.",
                                  "🆕", "yellow_background"),
                        F.B("paragraph", "", [F.link_rt("→ Ouvrir le rapport", it["url"], bold=True)])]})
                it["page"] = page["id"]
                it["star"] = NOW.isoformat()
                log.info("  🔔 %s — %s", it["organe"], it["title"][:110])
            except RuntimeError as e:
                log.warning("Alerte non créée : %s", e)

    # ---------------- fiches ----------------
    def fiches(self):
        done = 0
        q = self.st.get("queue", [])
        for it in list(q):
            if done >= self.cfg["fiches"]["fiches_par_passage"] or time_left(self.cfg) < 420:
                break
            if "tri" not in it or (not self.dry and not it.get("page")):
                continue
            if done:
                time.sleep(float(self.cfg["fiches"].get("pause_entre_fiches_secondes", 20)))
            try:
                self.build(it)
                done += 1
                self.counts["fiches"] += 1
                q.remove(it)
                self.st.setdefault("done", []).append({k: it.get(k) for k in ("url", "title", "organe", "date",
                                                                              "page", "star", "liens")})
                self.st["done"] = self.st["done"][-2000:]
            except V.QuotaExhausted as e:
                log.warning("Fiches : quota IA épuisé (%s) — reprise au prochain passage", e)
                break
            except Exception as e:  # noqa: BLE001
                it["tries"] = it.get("tries", 0) + 1
                log.warning("Fiche « %s » non rédigée (%d) : %s", it["title"][:80], it["tries"], e)
                if it["tries"] >= 3:
                    q.remove(it)
            if not self.dry:
                save_state(self.st)

    def build(self, it):
        fc = self.cfg["fiches"]
        doc = read_report(it["url"], self.vp, fc.get("pages_pdf_max", 400))
        full = doc["pdf_text"] if len(doc["pdf_text"]) > len(doc["html_text"]) else doc["html_text"]
        if len(full) < 800 and it.get("desc"):
            full = it["desc"]
            doc["html_text"] = full
        recs = extract_recommendations(doc["pdf_text"] or doc["html_text"])
        if not recs and doc.get("synth_text"):
            recs = extract_recommendations(doc["synth_text"])
        body, n_chars = compose_for_ai(doc, recs, int(fc.get("caracteres_envoyes_max", 90000)))
        cands = self.legi.candidates(it["title"] + " " + body[:20000]) if self.legi.ok else []
        prompt = self.prompt(it, body, recs, cands, n_chars)
        data = L.ask(self.llm, prompt, "flash", 16384)
        if not isinstance(data, dict):
            raise ValueError("fiche illisible")
        codes = {f"T{n + 1}": did for n, did in enumerate(cands)}
        liens = []
        for x in data.get("liens_lois") or []:
            if isinstance(x, dict) and x.get("code") in codes:
                liens.append(dict(x, did=codes[x["code"]]))
        it["liens"] = [x["did"] for x in liens]
        lecture = ("PDF intégral" if doc["pdf_text"] else "page web") + f", {n_chars:,} caractères".replace(",", " ")
        if self.dry:
            print(json.dumps({"titre": it["title"], "recs_verbatim": len(recs), "liens": liens,
                              "data": data}, ensure_ascii=False, indent=1)[:6000])
            return
        blocks = self.render(it, data, recs, liens, doc, lecture)
        # retire l'encadré provisoire « fiche en préparation » (les sous-pages personnelles sont conservées)
        try:
            res = self.notion.n.req("GET", f"/blocks/{it['page']}/children?page_size=50")
            for blk in res.get("results", []):
                if blk.get("type") in ("callout", "paragraph", "synced_block"):
                    self.notion.n.req("DELETE", f"/blocks/{blk['id']}")
        except RuntimeError as e:
            log.debug("Nettoyage de la page : %s", e)
        t = {"fiche": {}}
        F.write(self.notion.n, it["page"], t, blocks)
        themes = [x for x in (data.get("themes") or []) if x in self.themes][:5]
        stages = [self.legi.textes[x["did"]]["stage"] for x in liens]
        link_kind = "Sans texte lié" if not liens else (
            "Texte en cours d'examen" if any(s not in (P.STAGES[8], P.STAGE_ORDONNANCE) for s in stages)
            else "Loi récente (application)")
        rel = [{"id": self.legi.textes[x["did"]]["page"]} for x in liens if self.legi.textes[x["did"]].get("page")]
        props = {
            "Rapport": L.title_prop((F.STAR if days_since(it.get("star")) <= self.cfg["fiches"]["etoile_jours"]
                                     else "") + (data.get("titre") or it["title"])[:300]),
            "Thèmes": {"multi_select": [{"name": V.opt(x)} for x in themes]},
            "Recommandations": {"number": len(recs) or len(data.get("recommandations") or [])},
            "Résumé": L.text_prop(data.get("en_bref", "")),
            "Lien avec la loi": {"select": {"name": link_kind}},
            "Lois liées": {"relation": rel[:20]},
            "Fiche": {"select": {"name": "Complète"}},
            "PDF": {"url": doc.get("pdf_url")},
        }
        if data.get("type"):
            props["Type"] = {"select": {"name": V.opt(data["type"])[:100]}}
        self.notion.n.req("PATCH", f"/pages/{it['page']}", {"properties": props})
        log.info("  fiche « %s » : %d recommandation(s) relevée(s), %d texte(s) lié(s)", it["title"][:80],
                 len(recs), len(liens))

    def prompt(self, it, body, recs, cands, n_chars):
        lines = [
            "Tu es un rapporteur expert des politiques publiques françaises. Tu rédiges la FICHE DE LECTURE TRÈS "
            "EXHAUSTIVE d'un rapport public, destinée à un haut fonctionnaire qui doit en maîtriser le contenu "
            "sans le lire.",
            F.NEUTRALITE,
            "- Les recommandations sont reproduites MOT POUR MOT, sans reformulation, avec leur numéro, leur "
            "destinataire et leur échéance s'ils sont indiqués. Si une liste relevée mot pour mot est fournie, "
            "reprends-la à l'identique (tu peux seulement corriger les coupures de ligne). S'il n'y a pas de "
            "recommandations formelles, relève les propositions ou orientations formulées, citées exactement.",
            "- Chiffres : toujours avec l'unité, la date, le périmètre et la source mentionnés dans le rapport.",
            "- Sois exhaustif : couvre toutes les parties du rapport, pas seulement l'introduction.",
            f"- Thèmes : 1 à 5 choisis UNIQUEMENT dans {json.dumps(self.themes, ensure_ascii=False)}.",
            "- Rattachement aux lois : parmi les textes législatifs proposés (codes T1, T2…), indique ceux qui "
            "partagent un enjeu avec le rapport ou dont le contenu pourrait accueillir une recommandation "
            "(amendement, mesure d'application, évaluation). N'en retiens aucun si le lien est faible. N'utilise "
            "que les codes proposés.",
            "Réponds UNIQUEMENT avec un objet JSON de cette forme :",
            '{"titre": "titre exact du rapport", "type": "type de document", '
            '"commanditaire": "saisine ou commande (commission, ministre, auto-saisine…), si indiqué", '
            '"en_bref": "6 à 8 phrases : objet, constat principal, principales recommandations, portée", '
            '"contexte": "2 à 4 paragraphes : contexte, cadre juridique et budgétaire, enjeux, état des lieux", '
            '"perimetre_methode": "périmètre, période, méthode, sources mobilisées", '
            '"chiffres_cles": ["10 à 30 données chiffrées précises"], '
            '"constats": [{"titre": "intitulé du constat", "developpement": "3 à 6 phrases : constat, '
            'argumentaire, exemples et données à l\'appui"}], '
            '"recommandations": [{"numero": "1", "texte": "texte exact", "destinataire": "", "echeance": "", '
            '"axe": "partie ou thème du rapport"}], '
            '"reponses": "réponses ou positions des administrations et organismes contrôlés, si publiées", '
            '"enjeux": ["5 à 10 enjeux de politique publique soulevés, une à deux phrases chacun"], '
            '"suites_legislatives": ["recommandations qui supposeraient une loi, une loi de finances ou de '
            'financement de la sécurité sociale, ou un décret, en précisant le vecteur"], '
            '"liens_lois": [{"code": "T1", "nature": "enjeu commun | recommandation transposable | évaluation ou '
            'application", "explication": "2 à 3 phrases précises", "recommandations": ["n° des recommandations '
            'concernées"]}], '
            '"themes": [], "a_suivre": ["prochaines échéances ou suites annoncées"]}',
            "Limites : 25 constats au plus ; toutes les recommandations (aucune limite).",
            "",
            f"=== RAPPORT : {it['title']} ===",
            f"Organe : {it['organe']} — Date : {it.get('date') or 'inconnue'} — Adresse : {it['url']}",
            f"Longueur du document lu : {n_chars} caractères (extraits ci-dessous si le document est long).",
            "",
        ]
        if cands:
            lines.append("=== TEXTES LÉGISLATIFS SUIVIS (en cours d'examen ou promulgués récemment) ===")
            for n, did in enumerate(cands, 1):
                lines.append(f"T{n} = {self.legi.describe(did)}")
            lines.append("")
        else:
            lines.append("(Aucun texte législatif suivi ne paraît proche : laisse \"liens_lois\" vide.)\n")
        lines.append(body)
        return "\n".join(lines)

    # ---------------- rendu de la fiche ----------------
    def render(self, it, d, recs, liens, doc, lecture):
        B, pr, lk = F.B, F.plain_rt, F.link_rt
        out = [F.callout(f"Rapport publié par {it['organe']}" + (f" le {F.fr_date(it['date'])}" if it.get("date")
                                                                 else "") + ".", "📑", "gray_background",
                         [pr("  "), lk("→ Lire le rapport", it["url"], bold=True)] +
                         ([pr("  ·  "), lk("PDF", doc["pdf_url"])] if doc.get("pdf_url") else []))]
        out.append(B("paragraph", f"Fiche rédigée automatiquement le {NOW:%d/%m/%Y} à partir du rapport ({lecture}). "
                                  "Les recommandations sont reproduites telles qu'elles figurent dans le rapport.",
                      color="gray", italic=True))
        meta = [("Organe", it["organe"]), ("Type", d.get("type")), ("Saisine", d.get("commanditaire"))]
        for k, v in meta:
            if v:
                out.append(B("bulleted_list_item", "", [pr(k + " : ", bold=True), pr(str(v)[:1800])]))
        if d.get("en_bref"):
            out += [B("heading_2", "En bref"), B("paragraph", d["en_bref"])]
        # lien avec la loi (en tête : c'est l'information la plus utile)
        out.append(B("heading_2", "Lien avec les textes de loi"))
        if liens:
            for x in liens:
                t = self.legi.textes[x["did"]]
                url = "https://www.notion.so/" + t["page"].replace("-", "") if t.get("page") else None
                recs_s = ", ".join(str(r) for r in (x.get("recommandations") or []))
                out.append(B("bulleted_list_item", "", [
                    lk(t["short"][:200], url, bold=True), pr(f" — {t['stage']}", "gray"),
                    pr(f"\n{x.get('nature', '')} : ", bold=True), pr(x.get("explication", "")[:1500])] +
                    ([pr(f" (recommandation(s) n° {recs_s})", "gray")] if recs_s else [])))
        else:
            out.append(B("paragraph", "Aucun texte législatif en cours ou récent ne se rattache directement à ce "
                                      "rapport. Les enjeux sont détaillés ci-dessous.", color="gray"))
        if d.get("suites_legislatives"):
            out.append(B("heading_3", "Recommandations appelant un vecteur normatif"))
            out += [B("bulleted_list_item", x) for x in d["suites_legislatives"][:15]]
        if d.get("contexte"):
            out.append(B("heading_2", "Contexte"))
            out += [B("paragraph", p) for p in re.split(r"\n\s*\n", d["contexte"]) if p.strip()][:6]
        if d.get("perimetre_methode"):
            out += [B("heading_3", "Périmètre et méthode"), B("paragraph", d["perimetre_methode"])]
        if d.get("chiffres_cles"):
            out.append(B("heading_2", "Chiffres clés"))
            out += [B("bulleted_list_item", str(x)) for x in d["chiffres_cles"][:35]]
        if d.get("constats"):
            out.append(B("heading_2", "Constats et argumentaire"))
            for i, c in enumerate(d["constats"][:25], 1):
                if isinstance(c, dict):
                    out.append(B("heading_3", f"{i}. {c.get('titre', '')}"))
                    out.append(B("paragraph", c.get("developpement", "")))
                else:
                    out.append(B("bulleted_list_item", str(c)))
        # recommandations : relevé mot pour mot prioritaire
        ai_recs = d.get("recommandations") or []
        out.append(B("heading_2", f"Recommandations ({len(recs) or len(ai_recs)})"))
        if recs:
            out.append(B("paragraph", "Texte exact, relevé dans le rapport.", color="gray", italic=True))
            ai_by = {str(r.get("numero")): r for r in ai_recs if isinstance(r, dict)}
            for r in recs:
                extra = ai_by.get(r["num"]) or {}
                dest = r["dest"] or extra.get("destinataire") or ""
                ech = extra.get("echeance") or ""
                out.append(B("numbered_list_item", "", [pr(f"{r['kind']} n° {r['num']} : ", bold=True),
                                                        pr(r["texte"][:1800])] +
                             ([pr(f"  [{dest}{' · ' + ech if ech else ''}]", "gray")] if dest or ech else [])))
        elif ai_recs:
            out.append(B("paragraph", "Recommandations transcrites par l'IA à partir du rapport (pas de liste "
                                      "numérotée détectée automatiquement) : à vérifier sur le document.",
                         color="gray", italic=True))
            for r in ai_recs[:150]:
                if not isinstance(r, dict):
                    out.append(B("numbered_list_item", str(r)))
                    continue
                tail = " · ".join(x for x in (r.get("destinataire"), r.get("echeance")) if x)
                out.append(B("numbered_list_item", "", [pr(f"n° {r.get('numero', '')} : " if r.get("numero") else "",
                                                           bold=True), pr(str(r.get("texte", ""))[:1800])] +
                             ([pr(f"  [{tail}]", "gray")] if tail else [])))
        else:
            out.append(B("paragraph", "Le document ne formule pas de recommandations numérotées.", color="gray"))
        if d.get("reponses"):
            out += [B("heading_2", "Réponses des administrations et organismes"), B("paragraph", d["reponses"])]
        if d.get("enjeux"):
            out.append(B("heading_2", "Enjeux de politique publique"))
            out += [B("bulleted_list_item", str(x)) for x in d["enjeux"][:12]]
        if d.get("a_suivre"):
            out.append(B("heading_2", "À suivre"))
            out += [B("bulleted_list_item", str(x)) for x in d["a_suivre"][:8]]
        return out

    # ---------------- synthèse en haut de page ----------------
    def refresh(self):
        """Retire l'⭐ des rapports de plus de N jours et met à jour la liste « Derniers rapports »."""
        if self.dry:
            return
        days = self.cfg["fiches"]["etoile_jours"]
        for r in self.st.get("done", []):
            if r.get("star") and days_since(r["star"]) > days and r.get("page"):
                try:
                    p = self.notion.n.req("GET", f"/pages/{r['page']}")
                    title = "".join(x.get("plain_text", "") for x in p["properties"]["Rapport"]["title"])
                    if title.startswith(F.STAR):
                        self.notion.n.req("PATCH", f"/pages/{r['page']}", {"properties": {
                            "Rapport": L.title_prop(title[len(F.STAR):])}})
                    r["star"] = None
                except (RuntimeError, KeyError) as e:
                    log.debug("Étoile non retirée : %s", e)
                    r["star"] = None
        mem = self.st["notion"]
        if not mem.get("summary_anchor"):
            return
        recent = sorted(self.st.get("done", []) + [q for q in self.st.get("queue", []) if q.get("page")],
                        key=lambda r: (r.get("date") or "", r.get("star") or ""), reverse=True)[:25]
        blocks = [F.B("paragraph", f"Mis à jour le {NOW:%d/%m/%Y}. Les rapports marqués ⭐ sont arrivés ces "
                                   f"{self.cfg['fiches']['etoile_jours']} derniers jours.", color="gray")]
        for r in recent:
            url = "https://www.notion.so/" + r["page"].replace("-", "") if r.get("page") else r["url"]
            new = r.get("star") and days_since(r["star"]) <= self.cfg["fiches"]["etoile_jours"]
            pending = r in self.st.get("queue", [])
            blocks.append(F.B("bulleted_list_item", "", [
                F.plain_rt(("⭐ " if new else "") + F.fr_date(r.get("date")) + " — ", "gray"),
                F.plain_rt(r["organe"][:80] + " : ", bold=True), F.link_rt(r["title"][:200], url)] +
                ([F.plain_rt(" (fiche en préparation)", "gray")] if pending else []) +
                ([F.plain_rt(f" · lié à {len(r['liens'])} texte(s) de loi", "red")] if r.get("liens") else [])))
        sig = P.sha(json.dumps(blocks, ensure_ascii=False))
        if sig == mem.get("summary_sig"):
            return
        try:
            if mem.get("summary"):
                try:
                    self.notion.n.req("DELETE", f"/blocks/{mem['summary']}")
                except RuntimeError:
                    pass
            res = self.notion.n.req("PATCH", f"/blocks/{self.notion.page}/children", {
                "after": mem["summary_anchor"], "children": [{"object": "block", "type": "synced_block",
                                                             "synced_block": {"synced_from": None,
                                                                              "children": blocks[:100]}}]})
            mem["summary"] = res["results"][0]["id"]
            mem["summary_sig"] = sig
        except RuntimeError as e:
            log.warning("Liste « Derniers rapports » non mise à jour : %s", e)


# =====================================================================
# Mémoire
# =====================================================================
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k, v in (("seen", {}), ("queue", []), ("done", []), ("notion", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    cutoff = (NOW - dt.timedelta(days=500)).strftime("%Y-%m-%d")
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    st["queue"] = st["queue"][-500:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def make_llm(st):
    llm = V.LLM(st, {})
    llm.gkey = V.env("GEMINI_API_KEY_RAPPORTS") or V.env("GEMINI_API_KEY_LEGI") or V.env("GEMINI_API_KEY")
    return llm


# =====================================================================
# Passage normal
# =====================================================================
def run(args):
    cfg = load_config()
    st = load_state()
    legi = LegiLink(cfg)
    notion = RapNotion(cfg)
    if not args.dry_run:
        notion.ensure(st, legi.dash)
    llm = make_llm(st)
    vp = L.Fetcher()
    R = Rapports(cfg, st, notion, llm, legi, vp, args.dry_run)
    try:
        R.collect()
        R.triage()
        R.alert_rows()
        if not args.dry_run:
            save_state(st)
        R.fiches()
        R.refresh()
    finally:
        vp.close()
        if not args.dry_run:
            save_state(st)
    c = R.counts
    msg = (f"Terminé : {c['nouveaux']} nouveau(x) rapport(s), {c['ecartes']} écarté(s) (doublons, hors sujet ou "
           f"portée locale), {c['fiches']} fiche(s) rédigée(s), {len(st['queue'])} en attente. Appels IA : {llm.calls}")
    log.info(msg)
    summ = V.env("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"### Veille des rapports publics\n\n{msg}\n")


# =====================================================================
# Diagnostic
# =====================================================================
def diagnostic(args):
    cfg = load_config()
    st = load_state()
    out = [f"# Diagnostic de la veille des rapports — {NOW:%d/%m/%Y %H:%M} UTC", ""]
    legi = LegiLink(cfg)
    out.append(f"- Veille législative : {len(legi.textes)} textes disponibles pour les rattachements"
               + ("" if legi.ok else " — ⚠️ mémoire introuvable (lancez la veille législative)"))
    out.append(f"- Tableau de bord législatif : {'trouvé' if legi.dash else 'INTROUVABLE'}")
    try:
        n = RapNotion(cfg)
        out.append(f"- Notion : jeton {'présent' if n.n.token else 'MANQUANT'}, page "
                   f"{'renseignée' if n.page else 'MANQUANTE (secret NOTION_PAGE_ID_RAPPORTS)'}")
        if n.page:
            n.req("GET", f"/pages/{n.page}")
            out.append("  - accès à la page « Veille administrative » : OK")
    except Exception as e:  # noqa: BLE001
        out.append(f"- Notion : ÉCHEC — {e}")
    llm = make_llm({})
    try:
        L.ask(llm, 'Réponds {"ok": true} en JSON.', "lite", 50)
        out.append(f"- IA : OK ({'clé GEMINI_API_KEY_RAPPORTS' if V.env('GEMINI_API_KEY_RAPPORTS') else 'clé partagée'})")
    except Exception as e:  # noqa: BLE001
        out.append(f"- IA : ÉCHEC — {e}")
    out += ["", "## Organes suivis", ""]
    ok = 0
    sample = None
    for src in cfg["sources"]:
        try:
            items, report = V.collect_source(src, st.setdefault("vcache", {}),
                                             {"limites": {"liens_max_par_page": 40}})
        except Exception as e:  # noqa: BLE001
            items, report = [], [f"erreur : {e}"]
        kept = [i for i in items if Rapports.keep_title(src, i.get("title", ""))]
        dated = sorted([i for i in kept if i.get("date")], key=lambda i: i["date"], reverse=True)
        status = "✅" if kept else "⚠️"
        ok += bool(kept)
        out.append(f"### {status} {src['nom']} — {len(kept)} publication(s) repérée(s)"
                   + (f", la plus récente le {dated[0]['date'][:10]}" if dated else ""))
        out += [f"- {r}" for r in report]
        for i in (dated or kept)[:3]:
            out.append(f"  - {(i.get('date') or '')[:10]} {i.get('title', '')[:110]} — {i['url']}")
        if kept and sample is None and src["nom"].startswith("Cour"):
            sample = (dated or kept)[0]
        out.append("")
    vp = L.Fetcher()
    try:
        items = P.parse_rss(vp.get(f"{P.BASE}/rapports-feeds.xml", "xml") or "")
        out.append(f"### {'✅' if items else '⚠️'} vie-publique.fr (rapports publics) — {len(items)} élément(s)")
        if sample:
            doc = read_report(sample["url"], vp, 400)
            recs = extract_recommendations(doc["pdf_text"] or doc["html_text"])
            out += ["", "## Test de lecture complète", f"- {sample['title'][:120]}",
                    f"- page : {len(doc['html_text'])} caractères ; PDF : {len(doc['pdf_text'])} caractères "
                    f"({doc.get('pdf_url') or 'aucun PDF trouvé'})",
                    f"- recommandations relevées mot pour mot : {len(recs)}"]
            for r in recs[:3]:
                out.append(f"  - {r['kind']} n° {r['num']} : {r['texte'][:200]}")
    finally:
        vp.close()
    out.insert(2, f"**{ok}/{len(cfg['sources'])} organes répondent.**\n")
    text = "\n".join(out)
    summ = V.env("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


def main():
    ap = argparse.ArgumentParser(description="Veille des rapports publics")
    ap.add_argument("--mode", default="normal", choices=["normal", "diagnostic"])
    ap.add_argument("--dry-run", action="store_true")
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
