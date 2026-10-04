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
  5. rattachement aux textes de loi en cours ou récents suivis par la veille législative ;
  6. classement dans les dossiers thématiques et, lorsque plusieurs rapports portent sur le même
     sujet ou se complètent, « super fiche » 🔷 de synthèse (enjeux, textes de loi en cours, éléments
     de langage, tableau de toutes les recommandations mot pour mot avec avantages et limites).
Les fiches reprennent la langue des rapports (formulations, notions, éléments de langage).

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
    # garde-fous contre les faux positifs : une série de recommandations commence à 1 (ou presque)
    # et compte plusieurs éléments (sinon : renvoi à une recommandation d'un autre organisme, etc.)
    nums = [int(re.match(r"\d+", r["num"]).group(0)) for r in recs]
    if min(nums) > 3 or (len(recs) == 1 and nums[0] != 1):
        return []
    recs = [r for r, n in zip(recs, nums) if n <= 300]
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


def split_parts(text, size):
    """Découpe le texte en parties d'environ `size` caractères, sur des fins de paragraphe."""
    parts, i = [], 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):
            k = text.rfind("\n\n", i + int(size * 0.7), j)
            if k == -1:
                k = text.rfind("\n", i + int(size * 0.7), j)
            if k > i:
                j = k
        parts.append(text[i:j])
        i = j
    return parts


def recs_text(recs):
    return "\n".join(f"{r['kind']} n° {r['num']}{' (' + r['dest'] + ')' if r['dest'] else ''} : {r['texte']}"
                     for r in recs)





# =====================================================================
# Thèmes
# =====================================================================
def theme_families(cfg):
    """[{"famille", "icone", "couleur", "themes": [...]}] (ancien format « themes » à plat accepté)."""
    if cfg.get("familles_thematiques"):
        return cfg["familles_thematiques"]
    return [{"famille": "Thèmes", "icone": "📁", "couleur": "default",
             "themes": [t["nom"] if isinstance(t, dict) else t for t in cfg.get("themes", [])]}]


def theme_list(cfg):
    return [t for f in theme_families(cfg) for t in f["themes"]]


def theme_colors(cfg):
    return {t: f.get("couleur", "default") for f in theme_families(cfg) for t in f["themes"]}


def family_of(cfg, theme):
    return next((f for f in theme_families(cfg) if theme in f["themes"]), None)


# =====================================================================
# Restitution fidèle : règles de rédaction et contrôle mot pour mot
# =====================================================================
STYLE = (
    "Exigences de rédaction (impératives) :\n"
    "- RESTITUTION FIDÈLE DU FOND ET DE LA FORME : tu écris avec la langue du rapport. Reprends ses mots, ses "
    "tournures, ses notions, ses intitulés, ses sigles et ses éléments de langage tels qu'il les emploie ; "
    "privilégie la reprise littérale de ses phrases et de ses formules (entre guillemets « » lorsqu'il s'agit de "
    "citations). N'emploie ni synonyme ni paraphrase lorsque le rapport dispose d'une formulation ; ne simplifie "
    "pas et ne « vulgarise » pas le vocabulaire technique ; conserve la terminologie administrative, juridique et "
    "budgétaire exacte (intitulés des dispositifs, des programmes, des instances).\n"
    "- Aucune appréciation de ta part : les appréciations sont celles du rapport, reprises dans ses termes et "
    "attribuées à son auteur (« la Cour relève que… », « la mission estime que… », « selon les rapporteurs… »). "
    "Les positions des administrations et organismes contrôlés sont attribuées de même.\n"
    "- Aucun vocabulaire médiatique ou polémique qui ne figure pas dans le rapport.\n"
    "- N'utilise QUE le document fourni. N'invente aucun fait, chiffre, date ou référence. Si une information "
    "manque, laisse le champ vide.\n"
    "- Rédige en français."
)


def vnorm(s):
    """Normalisation pour comparer une citation au texte (casse, accents, ponctuation, césures)."""
    s = P.norm(s)
    s = re.sub(r"(\w)- (\w)", r"\1\2", s)
    return re.sub(r"[^\w%€$]+", " ", s).strip()


def verbatim_ok(quote, ntext):
    """La citation figure-t-elle (presque) mot pour mot dans le texte normalisé `ntext` ?"""
    q = vnorm(quote)
    if len(q) < 12 or not ntext:
        return False
    if q in ntext:
        return True
    w = q.split()
    if len(w) < 6:
        return False
    sh = [" ".join(w[i:i + 5]) for i in range(len(w) - 4)]
    return sum(1 for x in sh if x in ntext) / len(sh) >= 0.8


# =====================================================================
# Écriture Notion par morceaux (taille et nombre de blocs limités par requête)
# =====================================================================
def _blocks_count(b):
    return 1 + len(b.get(b["type"], {}).get("children") or [])


def chunk_blocks(blocks, max_n=90, max_chars=200000):
    out, cur, n, size = [], [], 0, 0
    for b in blocks:
        c, s = _blocks_count(b), len(json.dumps(b, ensure_ascii=False))
        if cur and (len(cur) >= max_n or n + c > 400 or size + s > max_chars):
            out.append(cur)
            cur, n, size = [], 0, 0
        cur.append(b)
        n += c
        size += s
    if cur:
        out.append(cur)
    return out


def table_as_list(b):
    """Repli si Notion refuse un tableau : une puce par ligne."""
    if b.get("type") != "table":
        return [b]
    rows = b["table"].get("children") or []
    out = []
    for r in rows[1:]:
        rich = []
        for i, cell in enumerate(r["table_row"]["cells"]):
            if not cell:
                continue
            if rich:
                rich.append(F.plain_rt(" | ", "gray"))
            rich += cell
        out.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich[:90]}})
    return out


def put_blocks(n, page_id, holder, blocks):
    """Remplace le contenu généré de la page : un bloc conteneur (synchronisé) supprimé puis recréé.
    Les notes personnelles ajoutées hors du conteneur sont conservées."""
    old = holder.get("container")
    if old:
        try:
            n.req("DELETE", f"/blocks/{old}")
        except RuntimeError:
            pass
    first = blocks[:1] if blocks and blocks[0]["type"] != "table" else [F.B("paragraph", "")]
    rest = blocks[1:] if first == blocks[:1] else blocks
    res = n.req("PATCH", f"/blocks/{page_id}/children", {"children": [{
        "object": "block", "type": "synced_block", "synced_block": {"synced_from": None, "children": first}}]})
    cont = res["results"][0]["id"]
    holder["container"] = cont
    for chunk in chunk_blocks(rest):
        try:
            n.req("PATCH", f"/blocks/{cont}/children", {"children": chunk})
        except RuntimeError as e:
            if not any(b["type"] == "table" for b in chunk):
                raise
            log.warning("Tableau refusé par Notion (%s) : présenté en liste", str(e)[:150])
            flat = [x for b in chunk for x in table_as_list(b)]
            for c2 in chunk_blocks(flat):
                n.req("PATCH", f"/blocks/{cont}/children", {"children": c2})
    return cont


def notion_url(page_id):
    return "https://www.notion.so/" + page_id.replace("-", "") if page_id else None


# =====================================================================
# Données détaillées de chaque fiche (pour les super fiches)
# =====================================================================
FICHE_V = 2  # 2 : éléments de langage, notions, avantages/limites des recommandations


def data_path(k):
    return os.path.join(STATE_DIR, "fiches", f"{k}.json")


def load_data(k):
    try:
        with open(data_path(k), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_data(k, d):
    os.makedirs(os.path.dirname(data_path(k)), exist_ok=True)
    with open(data_path(k), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))


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
                    self.migrate(d)
            except RuntimeError:
                pass
        if not self.db:
            res = self.req("POST", "/search", {"filter": {"property": "object", "value": "database"},
                                               "page_size": 100})
            for d in res.get("results", []):
                if self.is_ours(d):
                    self.db = d["id"]
                    self.migrate(d)
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

    def migrate(self, d):
        """Base créée par une version précédente : ajoute la colonne « Super fiche » et les nouveaux thèmes."""
        props = d.get("properties") or {}
        upd = {}
        if "Super fiche" not in props:
            upd["Super fiche"] = {"url": {}}
        th = (props.get("Thèmes") or {}).get("multi_select")
        if th is not None:
            have = {o.get("name") for o in th.get("options", [])}
            missing = [{"name": V.opt(k), "color": c} for k, c in theme_colors(self.cfg).items() if V.opt(k) not in have]
            if missing:
                upd["Thèmes"] = {"multi_select": {"options": [
                    {k: o[k] for k in ("id", "name", "color") if k in o} for o in th.get("options", [])] + missing}}
        if upd:
            try:
                self.req("PATCH", f"/databases/{d['id']}", {"properties": upd})
                log.info("Base « Rapports publics » mise à jour : %s", ", ".join(upd))
            except RuntimeError as e:
                log.warning("Base « Rapports publics » non mise à jour : %s", e)

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
            "Super fiche": {"url": {}},
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
                if not self.keep_title(src, it.get("title", ""), it.get("url", "")):
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
        self.fix_titles(new)
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

    WEAK_TITLE = re.compile(r"^\W*$|^(?:consulter|en savoir plus|lire la suite|lire|télécharger|voir|accéder|"
                            r"découvrir|le rapport|la note|l'avis)\b|^\d{2}-[A-Z]-\d{2}\b", re.I)

    def fix_titles(self, items, maxn=40):
        """Titres de liens peu parlants (« Consulter le rapport », vide…) : on lit le titre de la page."""
        n = 0
        for it in items:
            t = (it.get("title") or "").strip()
            t2 = re.sub(r"^(?:en savoir plus|lire la suite|consulter)\s*[:\-–]?\s*", "", t, flags=re.I)
            if t2 != t and len(t2) >= 20:
                it["title"] = t2
                continue
            if len(t) >= 25 and not self.WEAK_TITLE.search(t):
                it["title"] = re.sub(r"\s+", " ", t)
                continue
            if n >= maxn:
                continue
            n += 1
            r = V.http_get(it["url"], timeout=30)
            if r is None or r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                continue
            soup = BeautifulSoup(r.text, "lxml")
            cand = [soup.find("meta", property="og:title"), soup.find("h1"), soup.find("title")]
            for c in cand:
                v = (c.get("content") if c is not None and c.name == "meta" else (c.get_text(" ", strip=True)
                                                                                   if c is not None else ""))
                v = re.sub(r"\s+", " ", v or "").strip()
                v = re.split(r"\s+[|–-]\s+(?=[^|–-]{0,60}$)", v)[0].strip() if len(v) > 40 else v
                if len(v) >= 12 and not self.WEAK_TITLE.search(v):
                    it["title"] = v[:400]
                    break

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
    def keep_title(src, title, url=""):
        """Filtres de la source, appliqués au titre ET à l'adresse (ex. « rap-info » dans l'adresse)."""
        hay = (title or "") + " " + (url or "")
        if src.get("titre_motif") and not re.search(src["titre_motif"], hay, re.I):
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
        fc = self.cfg["fiches"]
        q = self.st.get("queue", [])
        todo = [("new", it) for it in q if "tri" in it and (self.dry or it.get("page"))]
        if fc.get("reprendre_anciennes_fiches", True) and not self.dry:
            # fiches rédigées par une version précédente : refaites quand la file des nouveautés est vide
            todo += [("old", r) for r in self.st.get("done", [])
                     if r.get("page") and r.get("v", 1) < FICHE_V and r.get("tries", 0) < 3]
        for kind, it in todo:
            if done >= fc["fiches_par_passage"] or time_left(self.cfg) < 420:
                break
            if done:
                time.sleep(0 if V.env("LEGI_TEST") else float(fc.get("pause_entre_fiches_secondes", 20)))
            try:
                rec = self.build(it)
                done += 1
                self.counts["fiches"] += 1
                if kind == "new":
                    q.remove(it)
                    self.st.setdefault("done", []).append(rec)
                    self.st["done"] = self.st["done"][-2000:]
                else:
                    it.clear()
                    it.update(rec)
                    self.counts["reprises"] = self.counts.get("reprises", 0) + 1
            except V.QuotaExhausted as e:
                log.warning("Fiches : quota IA épuisé (%s) — reprise au prochain passage", e)
                break
            except Exception as e:  # noqa: BLE001
                it["tries"] = it.get("tries", 0) + 1
                log.warning("Fiche « %s » non rédigée (%d) : %s", it["title"][:80], it["tries"], e)
                if it["tries"] >= 3 and kind == "new":
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
        full = doc["pdf_text"] if len(doc["pdf_text"]) > len(doc["html_text"]) else doc["html_text"]
        n_chars = len(full)
        if n_chars <= int(fc.get("lecture_integrale_max_caracteres", 250000)):
            # rapport lu en entier en une seule fois
            body = "\n\n".join(x for x in (
                ("=== SYNTHÈSE PUBLIÉE ===\n" + doc["synth_text"][:60000]) if doc.get("synth_text") else "",
                ("=== RECOMMANDATIONS RELEVÉES MOT POUR MOT DANS LE RAPPORT ===\n" + recs_text(recs)) if recs else "",
                "=== TEXTE INTÉGRAL DU RAPPORT ===\n" + full) if x)
            mode = "lu intégralement"
        else:
            # rapport très long : lecture partie par partie, puis synthèse de l'ensemble
            notes, n_parts = self.read_in_parts(it, full)
            body = "\n\n".join(x for x in (
                ("=== SYNTHÈSE PUBLIÉE ===\n" + doc["synth_text"][:40000]) if doc.get("synth_text") else "",
                ("=== RECOMMANDATIONS RELEVÉES MOT POUR MOT DANS LE RAPPORT ===\n" + recs_text(recs)) if recs else "",
                "=== DÉBUT DU RAPPORT (synthèse, introduction) ===\n" + full[:40000],
                "=== NOTES DE LECTURE DE CHAQUE PARTIE DU RAPPORT (le rapport a été lu en entier) ===\n" + notes) if x)
            mode = f"lu intégralement en {n_parts} parties"
        cands = self.legi.candidates(it["title"] + " " + body[:20000]) if self.legi.ok else []
        prompt = self.prompt(it, body, recs, cands, n_chars)
        data = L.ask(self.llm, prompt, "flash", 32768)
        if not isinstance(data, dict):
            raise ValueError("fiche illisible")
        # éléments de langage : seules les formules retrouvées mot pour mot dans le rapport sont gardées
        ntext = vnorm(full + "\n" + (doc.get("synth_text") or "") + "\n" + (doc.get("html_text") or ""))
        lang = [x for x in (data.get("elements_de_langage") or []) if isinstance(x, dict)
                and verbatim_ok(str(x.get("formule", "")), ntext)]
        log.info("  éléments de langage : %d retrouvés mot pour mot sur %d proposés", len(lang),
                 len(data.get("elements_de_langage") or []))
        data["elements_de_langage"] = lang[:30]
        # avantages / limites (analyse du rapport) rattachés aux recommandations relevées mot pour mot
        ai_by = {str(r.get("numero")).strip(): r for r in (data.get("recommandations") or []) if isinstance(r, dict)}
        for r in recs:
            x = ai_by.get(r["num"]) or {}
            for k in ("avantages", "limites", "echeance", "axe"):
                r[k] = str(x.get(k) or "").strip()
            r["dest"] = r["dest"] or str(x.get("destinataire") or "").strip()
        codes = {f"T{n + 1}": did for n, did in enumerate(cands)}
        liens = []
        for x in data.get("liens_lois") or []:
            if isinstance(x, dict) and x.get("code") in codes:
                liens.append(dict(x, did=codes[x["code"]]))
        it["liens"] = [x["did"] for x in liens]
        pages = max(1, round(n_chars / 2800))
        lecture = ("PDF" if doc["pdf_text"] else "page web") + f" d'environ {pages} pages, {mode}"
        themes = [x for x in (data.get("themes") or []) if x in self.themes][:5]
        k = it.get("k") or V.url_key(it["url"])
        if recs:
            store_recs = recs
        else:  # pas de liste numérotée détectée : transcription de l'IA, signalée comme telle
            store_recs = [{"num": str(r.get("numero") or n), "kind": "Recommandation", "texte": str(r.get("texte", "")),
                           "dest": str(r.get("destinataire") or ""), "echeance": str(r.get("echeance") or ""),
                           "axe": str(r.get("axe") or ""), "avantages": str(r.get("avantages") or ""),
                           "limites": str(r.get("limites") or ""), "ia": True}
                          for n, r in enumerate(data.get("recommandations") or [], 1)
                          if isinstance(r, dict) and r.get("texte")]
        record = {"k": k, "url": it["url"], "title": (data.get("titre") or it["title"])[:400], "organe": it["organe"],
                  "date": it.get("date"), "page": it.get("page"), "star": it.get("star"),
                  "liens": [x["did"] for x in liens], "themes": themes, "type": data.get("type") or "", "v": FICHE_V}
        if self.dry:
            print(json.dumps({"titre": it["title"], "recs_verbatim": len(recs), "liens": liens,
                              "data": data}, ensure_ascii=False, indent=1)[:6000])
            return record
        save_data(k, dict(record, en_bref=data.get("en_bref", ""), contexte=data.get("contexte", ""),
                          constats=[c for c in (data.get("constats") or []) if isinstance(c, dict)][:25],
                          chiffres=[str(c) for c in (data.get("chiffres_cles") or [])][:30],
                          enjeux=[str(c) for c in (data.get("enjeux") or [])][:12],
                          langage=lang[:30], notions=[x for x in (data.get("notions_cles") or [])
                                                      if isinstance(x, dict)][:25],
                          recs=store_recs[:300], pdf_url=doc.get("pdf_url"),
                          liens_detail=[{k2: x.get(k2) for k2 in ("did", "nature", "explication", "recommandations")}
                                        for x in liens]))
        blocks = self.render(it, data, recs, liens, doc, lecture)
        # retire l'encadré provisoire « fiche en préparation » (les sous-pages personnelles sont conservées)
        try:
            res = self.notion.n.req("GET", f"/blocks/{it['page']}/children?page_size=50")
            for blk in res.get("results", []):
                if blk.get("type") in ("callout", "paragraph", "synced_block"):
                    self.notion.n.req("DELETE", f"/blocks/{blk['id']}")
        except RuntimeError as e:
            log.debug("Nettoyage de la page : %s", e)
        put_blocks(self.notion.n, it["page"], {}, blocks)
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
        return record

    def prompt(self, it, body, recs, cands, n_chars):
        if recs:
            rec_schema = ('"recommandations": [{"numero": "numéro, tel que dans la liste relevée", "destinataire": "", '
                          '"echeance": "", "axe": "partie ou orientation du rapport", "avantages": "…", '
                          '"limites": "…"}]')
            rec_rule = ("- Recommandations : leur texte exact est DÉJÀ relevé (liste fournie) ; ne le recopie pas. Pour "
                        "CHACUNE (même numéro), donne seulement le destinataire, l'échéance, l'axe, et l'analyse que le "
                        "rapport en fait.")
        else:
            rec_schema = ('"recommandations": [{"numero": "1", "texte": "texte EXACT, mot pour mot", "destinataire": "", '
                          '"echeance": "", "axe": "partie ou orientation du rapport", "avantages": "…", "limites": "…"}]')
            rec_rule = ("- Recommandations : reproduites MOT POUR MOT, sans reformulation. S'il n'y a pas de "
                        "recommandations formelles, relève les propositions ou orientations formulées, citées exactement.")
        lines = [
            "Tu es un rapporteur expert des politiques publiques françaises. Tu rédiges la FICHE DE LECTURE TRÈS "
            "EXHAUSTIVE d'un rapport public, destinée à un haut fonctionnaire qui doit en maîtriser le contenu "
            "sans le lire et pouvoir en reprendre les éléments de langage.",
            STYLE,
            rec_rule,
            "- Avantages et limites de chaque recommandation : \"avantages\" = effets attendus, bénéfices, "
            "justification, chiffrage ou économies que le rapport donne pour cette recommandation ; \"limites\" = "
            "limites, risques, coûts, conditions de réussite, difficultés de mise en œuvre ou réserves exprimées dans "
            "le rapport (ou dans les réponses des administrations, en les attribuant). Dans les termes du rapport. "
            "Laisse vide si le rapport n'en dit rien : n'invente pas d'analyse.",
            "- Éléments de langage : relève 15 à 30 formulations caractéristiques du rapport (formules-clés, "
            "diagnostics, qualifications, mots d'ordre, expressions techniques propres au sujet), COPIÉES MOT POUR MOT "
            "depuis le texte (elles seront vérifiées automatiquement dans le rapport ; une formule modifiée est rejetée).",
            "- Notions clés : les notions, sigles, dispositifs et indicateurs du rapport, avec la définition qu'il en "
            "donne, dans ses termes.",
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
            '"en_bref": "6 à 8 phrases dans les termes du rapport : objet, constat principal, principales '
            'recommandations, portée", '
            '"elements_de_langage": [{"formule": "citation exacte (5 à 40 mots)", "sens": "ce qu\'elle désigne, '
            'dans les termes du rapport"}], '
            '"notions_cles": [{"terme": "", "definition": ""}], '
            '"contexte": "2 à 4 paragraphes reprenant les formulations du rapport : contexte, cadre juridique et '
            'budgétaire, enjeux, état des lieux", '
            '"perimetre_methode": "périmètre, période, méthode, sources mobilisées", '
            '"chiffres_cles": ["10 à 30 données chiffrées précises"], '
            '"constats": [{"titre": "intitulé du constat, repris du rapport si possible", "developpement": "3 à 6 '
            'phrases restituant l\'argumentaire du rapport avec ses propres formulations, exemples et données à '
            'l\'appui"}], '
            + rec_schema + ', '
            '"reponses": "réponses ou positions des administrations et organismes contrôlés, si publiées", '
            '"enjeux": ["5 à 10 enjeux de politique publique soulevés, dans les termes du rapport"], '
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
            f"Longueur du document : {n_chars} caractères. Le rapport a été lu en entier : tu disposes soit de son "
            "texte intégral, soit, pour les rapports très longs, des notes de lecture exhaustives de chacune de ses "
            "parties. Couvre TOUTES les parties.",
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

    def read_in_parts(self, it, full):
        """Lecture exhaustive d'un très long rapport : notes détaillées partie par partie (IA « lite »)."""
        fc = self.cfg["fiches"]
        parts = split_parts(full, int(fc.get("taille_partie_caracteres", 100000)))
        maxp = int(fc.get("parties_max", 12))
        if len(parts) > maxp:  # au-delà, les dernières parties (annexes) sont regroupées
            parts = parts[:maxp - 1] + ["\n".join(parts[maxp - 1:])[: int(fc.get("taille_partie_caracteres", 100000))]]
        out = []
        for n, part in enumerate(parts, 1):
            prompt = "\n".join([
                f"Tu lis la partie {n}/{len(parts)} du rapport « {it['title']} » ({it['organe']}). Prends des notes "
                "de lecture EXHAUSTIVES et fidèles de CETTE partie, pour qu'un haut fonctionnaire n'ait pas à la lire.",
                STYLE,
                "Réponds UNIQUEMENT en JSON : {\"titres\": [\"titres des chapitres ou sections de la partie\"], "
                "\"resume\": \"5 à 10 phrases reprenant les formulations du texte\", \"constats\": [{\"titre\": \"\", "
                "\"developpement\": \"2 à 5 phrases, argumentaire et exemples, dans les termes du rapport\"}], "
                "\"chiffres\": [\"donnée chiffrée avec unité, date, périmètre\"], "
                "\"elements_de_langage\": [\"formulations caractéristiques copiées MOT POUR MOT (5 à 40 mots)\"], "
                "\"recommandations\": [{\"texte\": \"recommandation ou proposition de la partie, MOT POUR MOT\", "
                "\"avantages\": \"effets attendus, justification, chiffrage donnés par le rapport\", \"limites\": "
                "\"limites, risques, coûts, conditions ou réserves mentionnés\"}], "
                "\"positions\": [\"positions ou réponses d'acteurs, attribuées\"]}",
                "", "=== TEXTE DE LA PARTIE ===", part])
            try:
                d = L.ask(self.llm, prompt, "lite", 8192)
                out.append(f"--- Partie {n}/{len(parts)} ---\n" + json.dumps(d, ensure_ascii=False))
            except (ValueError, KeyError) as e:
                out.append(f"--- Partie {n}/{len(parts)} : notes indisponibles ({e}) ---")
            time.sleep(0 if V.env("LEGI_TEST") else float(fc.get("pause_entre_parties_secondes", 8)))
        log.info("  rapport long : %d parties lues", len(parts))
        return "\n".join(out), len(parts)

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
        if d.get("elements_de_langage"):
            out.append(B("heading_2", "Éléments de langage du rapport"))
            out.append(B("paragraph", "Formulations du rapport, reproduites mot pour mot (vérifiées dans le texte).",
                         color="gray", italic=True))
            for x in d["elements_de_langage"][:30]:
                out.append(B("bulleted_list_item", "", [pr("« " + str(x.get("formule", ""))[:1500] + " »", italic=True)]
                             + ([pr(" — " + str(x["sens"])[:600], "gray")] if x.get("sens") else [])))
        notions = [x for x in (d.get("notions_cles") or []) if isinstance(x, dict) and x.get("terme")]
        if notions:
            out.append(B("heading_3", "Notions clés"))
            for x in notions[:25]:
                out.append(B("bulleted_list_item", "", [pr(str(x["terme"])[:200] + " : ", bold=True),
                                                        pr(str(x.get("definition", ""))[:1500])]))
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
                             ([pr(f"  [{dest}{' · ' + ech if ech else ''}]", "gray")] if dest or ech else []) +
                             self.av_lim(r.get("avantages"), r.get("limites"))))
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
                             ([pr(f"  [{tail}]", "gray")] if tail else []) +
                             self.av_lim(r.get("avantages"), r.get("limites"))))
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

    @staticmethod
    def av_lim(av, lim):
        out = []
        if av:
            out += [F.plain_rt("\n➕ Avantages selon le rapport : ", "green", bold=True), F.plain_rt(str(av)[:1500], "green")]
        if lim:
            out += [F.plain_rt("\n➖ Limites selon le rapport : ", "orange", bold=True),
                    F.plain_rt(str(lim)[:1500], "orange")]
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
# Dossiers thématiques et super fiches
# =====================================================================
NATURES = ["Législative", "Réglementaire", "Budgétaire", "Organisationnelle", "Connaissance et évaluation"]
PRIO_COLOR = {"Prioritaire": "red", "Importante": "orange", "Complémentaire": "gray"}
SUPER = "Super fiche · "


def org_short(organe):
    return re.sub(r"\s*\(.*?\)\s*", " ", organe or "").strip()


class Dossiers:
    """Dossiers thématiques (sous-pages de « Veille administrative ») et super fiches 🔷.
    Arborescence : Veille administrative › famille (💶 Finances publiques et fiscalité…) › thème
    (📂 Finances publiques et dette…) › super fiches (🔷). Les fiches simples (📑) restent dans la base
    « Rapports publics » ; chaque dossier de thème en donne la liste."""

    def __init__(self, R):
        self.R, self.cfg, self.st, self.llm, self.legi = R, R.cfg, R.st, R.llm, R.legi
        self.n = R.notion.n if R.notion else None
        self.root = R.notion.page if R.notion else ""
        self.sc = self.cfg.get("super_fiches") or {}
        self.mem = self.st.setdefault("dossiers", {})
        for k in ("pages", "sujets", "themes"):
            self.mem.setdefault(k, {})
        self.counts = {"sujets": 0, "super": 0, "dossiers": 0}

    def reports(self):
        return [r for r in self.st.get("done", []) if r.get("k") and r.get("v", 1) >= FICHE_V]

    def run(self):
        if self.R.dry or not self.sc.get("actif", True) or not self.root or not self.n:
            return
        try:
            self.cluster()
        except V.QuotaExhausted as e:
            log.warning("Regroupement par sujet : quota IA épuisé (%s) — reprise au prochain passage", e)
        except (ValueError, KeyError, TypeError) as e:
            log.warning("Regroupement par sujet impossible pour ce passage : %s", e)
        self.build_supers()
        try:
            self.theme_pages()
        except RuntimeError as e:
            log.warning("Dossiers thématiques non mis à jour : %s", e)
        self.prune()

    # ---------------- regroupement des rapports par sujet ----------------
    def add(self, sid, ks):
        s = self.mem["sujets"][sid]
        for k in ks:
            if k and k not in s["members"]:
                s["members"].append(k)
                s["dirty"] = True

    def cluster(self):
        reps = self.reports()
        by_k = {r["k"]: r for r in reps}
        subj = self.mem["sujets"]
        per = int(self.sc.get("regroupement_par_appel", 20))
        for _ in range(3):
            new = [r for r in reps if not r.get("cl")][:per]
            if not new or time_left(self.cfg) < 600:
                return
            pool = [r for r in reps if r.get("cl")][-250:]
            if not pool and len(new) < 2:
                return  # un seul rapport : rien à regrouper pour l'instant
            codes = {}
            lines = [
                "Tu organises une veille de rapports publics pour un haut fonctionnaire. Tu regroupes les rapports par "
                "SUJET afin de constituer des « super fiches » de synthèse.",
                "Un sujet réunit des rapports qui traitent de la même politique publique, du même dispositif ou du même "
                "problème, ou qui se complètent (par exemple un rapport de la Cour des comptes et un rapport "
                "d'information parlementaire sur le même dispositif ; un avis du HCFP et une note du Trésor sur la "
                "trajectoire des finances publiques). Un sujet est plus précis qu'un thème (« Financement des retraites "
                "des fonctionnaires de l'État », « Prévention de la perte d'autonomie », « Trajectoire des finances "
                "publiques ») mais assez large pour réunir des rapports complémentaires. Le libellé reprend les termes "
                "employés par les rapports eux-mêmes.",
                "Pour CHAQUE nouveau rapport (codes N…), réponds : \"sujets\" = codes des sujets existants (S…) qu'il "
                "traite (0 à 2) ; \"nouveau\" = s'il forme un NOUVEAU sujet avec d'autres rapports (codes P… ou N…) "
                "portant sur le même objet : {\"libelle\": \"intitulé du sujet\", \"avec\": [\"codes de ces "
                "rapports\"]}, sinon null. Ne réunis pas des rapports qui n'ont en commun qu'un thème général. Un rapport "
                "sans objet commun avec un autre reste seul (sujets vides, nouveau null).",
                'Réponds UNIQUEMENT avec {"items": [{"id": "N1", "sujets": [], "nouveau": null}]}.', ""]
            lines.append("=== NOUVEAUX RAPPORTS À RATTACHER ===")
            for i, r in enumerate(new, 1):
                codes[f"N{i}"] = ("R", r["k"])
                d = load_data(r["k"]) or {}
                lines.append(f"N{i} = {org_short(r['organe'])[:50]} — {r['title'][:200]} "
                             f"[{', '.join(r.get('themes') or [])}]\n   {str(d.get('en_bref', ''))[:500]}")
            subs = sorted(subj.items(), key=lambda kv: kv[1].get("upd") or kv[1].get("created") or "", reverse=True)
            lines += ["", "=== SUJETS EXISTANTS ==="]
            for i, (sid, s) in enumerate(subs[:150], 1):
                codes[f"S{i}"] = ("S", sid)
                titles = " ; ".join(by_k[k]["title"][:90] for k in s["members"][:3] if k in by_k)
                lines.append(f"S{i} = {s['label']} ({len(s['members'])} rapports : {titles})")
            lines += ["", "=== RAPPORTS DÉJÀ TRAITÉS ==="]
            for i, r in enumerate(reversed(pool), 1):  # les plus récents d'abord
                codes[f"P{i}"] = ("R", r["k"])
                lines.append(f"P{i} = {org_short(r['organe'])[:50]} — {r['title'][:160]} "
                             f"[{', '.join(r.get('themes') or [])}]")
            out = L.ask(self.llm, "\n".join(lines), "lite", 8192)
            items = out.get("items") if isinstance(out, dict) else out
            created = {P.norm(s["label"]): sid for sid, s in subj.items()}
            for x in items or []:
                if not isinstance(x, dict):
                    continue
                c = codes.get(str(x.get("id")))
                if not c or c[0] != "R":
                    continue
                k = c[1]
                for code in (x.get("sujets") or [])[:2]:
                    t = codes.get(str(code))
                    if t and t[0] == "S":
                        self.add(t[1], [k])
                nv = x.get("nouveau")
                if isinstance(nv, dict) and str(nv.get("libelle") or "").strip():
                    others = []
                    for o in nv.get("avec") or []:
                        t = codes.get(str(o))
                        if t and t[0] == "R" and t[1] != k:
                            others.append(t[1])
                    lab = str(nv["libelle"]).strip()[:150]
                    key = P.norm(lab)
                    if key in created:
                        self.add(created[key], [k] + others)
                    elif others:
                        sid = "s" + P.sha(lab, TODAY, k)[:10]
                        subj[sid] = {"label": lab, "members": [], "created": TODAY}
                        created[key] = sid
                        self.add(sid, [k] + others)
                        self.counts["sujets"] += 1
                        log.info("  nouveau sujet : %s", lab)
            for r in new:
                r["cl"] = True
            save_state(self.st)

    # ---------------- super fiches ----------------
    def build_supers(self):
        by_k = {r["k"]: r for r in self.reports()}
        todo = [(sid, s) for sid, s in self.mem["sujets"].items()
                if s.get("dirty") and s.get("tries", 0) < 3 and sum(k in by_k for k in s["members"]) >= 2]
        todo.sort(key=lambda x: -len(x[1]["members"]))
        done = 0
        for sid, s in todo:
            if done >= int(self.sc.get("super_fiches_par_passage", 2)) or time_left(self.cfg) < 400:
                break
            if done:
                time.sleep(0 if V.env("LEGI_TEST") else float(self.sc.get("pause_secondes", 30)))
            try:
                self.build_super(s, by_k)
                done += 1
                self.counts["super"] += 1
            except V.QuotaExhausted as e:
                log.warning("Super fiches : quota IA épuisé (%s) — reprise au prochain passage", e)
                break
            except Exception as e:  # noqa: BLE001
                s["tries"] = s.get("tries", 0) + 1
                log.warning("Super fiche « %s » non rédigée (%d) : %s", s["label"][:80], s["tries"], e)
            save_state(self.st)

    def main_theme(self, ds):
        cnt = {}
        for i, d in enumerate(ds):
            for j, t in enumerate(d.get("themes") or []):
                cnt[t] = cnt.get(t, 0) + 10 - min(j, 5)  # le premier thème d'un rapport pèse davantage
        if cnt:
            return max(cnt, key=lambda t: cnt[t])
        tl = theme_list(self.cfg)
        return "Institutions et administration" if "Institutions et administration" in tl else tl[0]

    def build_super(self, s, by_k):
        members = sorted([by_k[k] for k in s["members"] if k in by_k], key=lambda r: r.get("date") or "", reverse=True)
        ds = []
        for r in members:
            d = load_data(r["k"])
            if d:
                d.update(page=r.get("page"), title=r["title"], themes=r.get("themes") or d.get("themes") or [])
                ds.append(d)
        if len(ds) < 2:
            s["dirty"] = False
            return
        A = {f"A{i}": d for i, d in enumerate(ds, 1)}
        recs = []
        for ac, d in A.items():
            for r in d.get("recs") or []:
                recs.append((f"R{len(recs) + 1}", ac, d, r))
        langs = []
        for ac, d in A.items():
            for x in (d.get("langage") or [])[:30]:
                langs.append((f"L{len(langs) + 1}", ac, d, x))
        cand = []
        for d in ds:
            for did in d.get("liens") or []:
                if did in self.legi.textes and did not in cand:
                    cand.append(did)
        if self.legi.ok:
            txt = s["label"] + " " + " ".join(d["title"] + " " + " ".join(d.get("enjeux") or [])[:1500] for d in ds[:30])
            for did in self.legi.candidates(txt, k=12):
                if did not in cand:
                    cand.append(did)
        cand = cand[:20]
        data = L.ask(self.llm, self.super_prompt(s, A, recs, langs, cand), "flash", 32768)
        if not isinstance(data, dict):
            raise ValueError("super fiche illisible")
        themes = sorted({t for d in ds for t in d.get("themes") or []})
        theme = s.get("theme") or self.main_theme(ds)
        blocks = self.render_super(s, data, A, recs, langs, cand)
        title = F.STAR + SUPER + s["label"]
        for attempt in (0, 1):
            try:
                if not s.get("page"):
                    s["page"] = self.create_page(self.theme_page(theme, check=bool(attempt)), title, "🔷")
                    s.pop("container", None)
                put_blocks(self.n, s["page"], s, blocks)
                break
            except RuntimeError as e:
                if attempt:
                    raise
                log.warning("Super fiche « %s » : page introuvable (%s), recréée", s["label"][:60], str(e)[:120])
                s["page"] = None
        self.n.req("PATCH", f"/pages/{s['page']}", {"properties": {"title": L.title_prop(title)}})
        s.update(dirty=False, upd=NOW.isoformat(), star=NOW.isoformat(), theme=theme, themes=themes, n=len(ds), tries=0)
        for d in ds:
            if d.get("page"):
                try:
                    self.n.req("PATCH", f"/pages/{d['page']}", {"properties": {
                        "Super fiche": {"url": notion_url(s["page"])}}})
                except RuntimeError as e:
                    log.debug("Colonne « Super fiche » non renseignée : %s", e)
        log.info("  🔷 super fiche « %s » : %d rapports, %d recommandations", s["label"][:80], len(ds), len(recs))

    def super_prompt(self, s, A, recs, langs, cand):
        maxr = int(self.sc.get("rapports_lus_max", 30))
        lines = [
            "Tu rédiges une SUPER FICHE : la synthèse de plusieurs rapports publics qui portent sur le même sujet ou "
            "se complètent, destinée à un haut fonctionnaire qui doit maîtriser le sujet et en reprendre les éléments "
            "de langage.",
            f"SUJET : {s['label']}",
            STYLE,
            "- Ici, « le rapport » désigne chacun des rapports réunis (codes A1, A2…) : chaque diagnostic, formule ou "
            "appréciation reprise est attribuée à son auteur (« la Cour des comptes relève… », « le Sénat "
            "préconise… »). Ne fusionne pas des positions différentes : expose-les chacune dans ses termes.",
            "- Classement des recommandations : classe TOUTES les recommandations (codes R…), chacune une seule fois, "
            "dans 3 à 8 catégories pertinentes pour le sujet (leviers d'action : gouvernance et pilotage, "
            "financement, organisation, droits et prestations, connaissance et évaluation…, à adapter au sujet et "
            "à nommer avec les termes des rapports). Ordonne les catégories de la plus structurante à la plus "
            "secondaire, et dans chaque catégorie les recommandations par priorité. Priorité : « Prioritaire », "
            "« Importante » ou « Complémentaire », d'après ce que disent les rapports (urgence, échéance, importance "
            "soulignée, enjeu budgétaire, convergence de plusieurs rapports). Nature : "
            + " | ".join(NATURES) + " (vecteur de mise en œuvre).",
            "- Recommandations convergentes : signale les recommandations de rapports différents qui se rejoignent "
            "ou se complètent.",
            "- Éléments de langage : choisis parmi les formules relevées mot pour mot (codes L…) les 15 à 30 plus "
            "structurantes pour le sujet, dans un ordre logique. N'en écris pas de nouvelles.",
            "- Textes législatifs : parmi les textes proposés (T1, T2…), retiens ceux qui portent sur le sujet ou "
            "pourraient accueillir une recommandation ; n'en retiens aucun si le lien est faible.",
            "Réponds UNIQUEMENT avec un objet JSON de cette forme :",
            '{"presentation": "2 à 3 paragraphes : ce qu\'examine chaque rapport et comment ils se complètent", '
            '"enjeux": [{"titre": "intitulé repris des rapports", "developpement": "4 à 8 phrases restituant les '
            'diagnostics avec les formulations des rapports, attribuées", "rapports": ["A1"]}], '
            '"convergences": ["constats partagés, attribués"], '
            '"divergences": ["points de divergence ou éclairages différents, attribués"], '
            '"chiffres_cles": [{"donnee": "donnée chiffrée avec unité, date, périmètre", "rapport": "A1"}], '
            '"langage": ["L3", "L1"], '
            '"classement": {"critere": "critère de classement retenu, en une phrase", "categories": [{"nom": "", '
            '"presentation": "1 à 2 phrases dans les termes des rapports", "recommandations": [{"id": "R3", '
            '"nature": "Législative", "priorite": "Prioritaire"}]}]}, '
            '"convergences_recos": [{"ids": ["R1", "R7"], "commentaire": "en quoi elles se rejoignent"}], '
            '"liens_lois": [{"code": "T1", "explication": "2 à 3 phrases", "recommandations": ["R3"]}], '
            '"a_suivre": ["échéances et suites annoncées par les rapports"]}',
            "Limites : 4 à 10 enjeux ; 10 à 25 chiffres clés.", ""]
        budget = max(4000, 240000 // max(1, min(len(A), maxr)))
        lines.append("=== RAPPORTS RÉUNIS ===")
        for i, (ac, d) in enumerate(A.items()):
            head = f"{ac} = {d['organe']} — {F.fr_date(d.get('date'))} — {d.get('type') or 'rapport'} — {d['title']}"
            if i >= maxr:
                lines.append(head + " (rapport plus ancien : seules ses recommandations sont fournies)")
                continue
            parts = [head, "En bref : " + str(d.get("en_bref", "")), "Contexte : " + str(d.get("contexte", ""))[:3000],
                     "Constats : " + " ".join(f"[{c.get('titre', '')}] {c.get('developpement', '')}"
                                               for c in d.get("constats") or []),
                     "Chiffres : " + " ; ".join(d.get("chiffres") or []),
                     "Enjeux : " + " ; ".join(d.get("enjeux") or []),
                     "Notions : " + " ; ".join(f"{x.get('terme')} : {x.get('definition')}" for x in d.get("notions") or [])]
            lines.append("\n".join(parts)[:budget])
            lines.append("")
        lines.append("=== ÉLÉMENTS DE LANGAGE RELEVÉS MOT POUR MOT ===")
        for code, ac, d, x in langs[:300]:
            lines.append(f"{code} [{ac}] « {str(x.get('formule', ''))[:300]} »")
        lines += ["", "=== RECOMMANDATIONS (texte exact) ==="]
        for code, ac, d, r in recs[:450]:
            extra = "".join(f" | {lab} : {str(r.get(k))[:220]}" for k, lab in (("avantages", "avantages"),
                                                                                ("limites", "limites")) if r.get(k))
            lines.append(f"{code} [{ac}, n° {r.get('num')}] {str(r.get('texte', ''))[:380]}{extra}")
        lines.append("")
        if cand:
            lines.append("=== TEXTES LÉGISLATIFS SUIVIS (en cours d'examen ou promulgués récemment) ===")
            for n, did in enumerate(cand, 1):
                lines.append(f"T{n} = {self.legi.describe(did)}")
        else:
            lines.append("(Aucun texte législatif suivi ne paraît proche : laisse \"liens_lois\" vide.)")
        return "\n".join(lines)

    # ---------------- rendu de la super fiche ----------------
    def rec_label(self, d, r):
        return f"{org_short(d['organe'])[:60]} n° {r.get('num')}"

    def rec_tables(self, rows):
        pr, lk = F.plain_rt, F.link_rt
        header = ["Rang", "Recommandation (texte exact)", "Rapport", "Nature · destinataire",
                  "Avantages (selon le rapport)", "Limites (selon le rapport)"]

        def row(cells):
            return {"object": "block", "type": "table_row", "table_row": {"cells": cells}}
        out = []
        for i in range(0, len(rows), 40):
            trs = [row([[pr(h, bold=True)] for h in header])]
            for rank, prio, nat, ac, d, r in rows[i:i + 40]:
                trs.append(row([
                    [pr(str(rank), bold=True)] + ([pr("\n" + prio, PRIO_COLOR.get(prio, "gray"))] if prio else []),
                    [pr(f"{r.get('kind') or 'Recommandation'} n° {r.get('num')} : ", bold=True),
                     pr(str(r.get("texte", ""))[:1800])] +
                    ([pr(" (transcription IA, à vérifier)", "gray", italic=True)] if r.get("ia") else []),
                    [lk(org_short(d["organe"])[:80], notion_url(d.get("page")) or d["url"], bold=True),
                     pr(" (" + F.fr_date(d.get("date")) + ")\n" if d.get("date") else "\n", "gray"),
                     pr(d["title"][:160] + "\n", "gray"), lk("→ rapport", d["url"], "gray")],
                    [pr(nat or "")] + ([pr("\n" + str(r["dest"])[:300], "gray")] if r.get("dest") else []) +
                    ([pr("\n" + str(r["echeance"])[:100], "gray")] if r.get("echeance") else []),
                    [pr(str(r["avantages"])[:1800], "green")] if r.get("avantages") else [],
                    [pr(str(r["limites"])[:1800], "orange")] if r.get("limites") else [],
                ]))
            out.append({"object": "block", "type": "table", "table": {
                "table_width": len(header), "has_column_header": True, "has_row_header": False, "children": trs}})
        return out

    def render_super(self, s, data, A, recs, langs, cand):
        B, pr, lk = F.B, F.plain_rt, F.link_rt
        orgs = sorted({org_short(d["organe"]) for d in A.values()})
        out = [F.callout(f"SUPER FICHE — synthèse de {len(A)} rapports sur « {s['label']} »", "🔷", "blue_background",
                         [pr("\n" + " · ".join(orgs)[:1500], "blue")])]
        out.append(B("paragraph", f"Mise à jour le {NOW:%d/%m/%Y}. Rédigée à partir des rapports, avec leurs propres "
                                  "formulations ; les recommandations sont reproduites mot pour mot, avec l'analyse "
                                  "(avantages, limites) donnée par chaque rapport, et classées par l'IA. Les fiches "
                                  "simples des rapports sont marquées 📑.", color="gray", italic=True))
        out.append(B("heading_2", "📑 Rapports réunis"))
        for ac, d in A.items():
            out.append(B("bulleted_list_item", "", [
                pr(f"{ac} · {F.fr_date(d.get('date'))} — ", "gray"), pr(org_short(d["organe"])[:80] + " : ", bold=True),
                lk(d["title"][:220], notion_url(d.get("page")) or d["url"]), pr("  "), lk("→ rapport", d["url"], "gray")]))
        src = {ac: org_short(d["organe"]) for ac, d in A.items()}

        def who(codes):
            names = []
            for c in codes or []:
                if src.get(str(c)) and src[str(c)] not in names:
                    names.append(src[str(c)])
            return [pr("  (" + ", ".join(names)[:300] + ")", "gray")] if names else []
        if data.get("presentation"):
            out.append(B("heading_2", "Présentation"))
            out += [B("paragraph", p) for p in re.split(r"\n\s*\n", str(data["presentation"])) if p.strip()][:5]
        enj = [e for e in data.get("enjeux") or [] if isinstance(e, dict)]
        if enj:
            out.append(B("heading_2", "Grands enjeux"))
            for i, e in enumerate(enj[:10], 1):
                out.append(B("heading_3", f"{i}. {e.get('titre', '')}"))
                out.append(B("paragraph", "", [pr(str(e.get("developpement", ""))[:5000])] + who(e.get("rapports"))))
        if data.get("convergences") or data.get("divergences"):
            out.append(B("heading_2", "Convergences et différences d'approche"))
            out += [B("bulleted_list_item", "", [pr("Convergence : ", "green", bold=True), pr(str(x)[:1800])])
                    for x in (data.get("convergences") or [])[:10]]
            out += [B("bulleted_list_item", "", [pr("Différence d'approche : ", "orange", bold=True), pr(str(x)[:1800])])
                    for x in (data.get("divergences") or [])[:10]]
        out += self.render_lois(data, A, recs, cand)
        # éléments de langage (formules vérifiées mot pour mot dans les rapports)
        lmap = {code: (ac, d, x) for code, ac, d, x in langs}
        chosen = [str(c) for c in data.get("langage") or [] if str(c) in lmap]
        if not chosen:
            seen_ac = {}
            for code, ac, d, x in langs:
                seen_ac[ac] = seen_ac.get(ac, 0) + 1
                if seen_ac[ac] <= 4:
                    chosen.append(code)
        if chosen:
            out.append(B("heading_2", "🗣️ Éléments de langage"))
            out.append(B("paragraph", "Formulations des rapports, reproduites mot pour mot (vérifiées dans le texte "
                                      "de chaque rapport).", color="gray", italic=True))
            for c in list(dict.fromkeys(chosen))[:35]:
                ac, d, x = lmap[c]
                out.append(B("bulleted_list_item", "", [pr("« " + str(x.get("formule", ""))[:1500] + " »", italic=True),
                                                        pr(" — "), lk(org_short(d["organe"])[:80],
                                                                       notion_url(d.get("page")) or d["url"], "gray")]))
        notions, seen = [], set()
        for ac, d in A.items():
            for x in d.get("notions") or []:
                key = P.norm(str(x.get("terme", "")))
                if key and key not in seen:
                    seen.add(key)
                    notions.append((d, x))
        if notions:
            out.append(B("heading_3", "Notions clés"))
            for d, x in notions[:25]:
                out.append(B("bulleted_list_item", "", [pr(str(x["terme"])[:200] + " : ", bold=True),
                                                        pr(str(x.get("definition", ""))[:1500]),
                                                        pr(f"  ({org_short(d['organe'])[:60]})", "gray")]))
        ch = [c for c in data.get("chiffres_cles") or [] if isinstance(c, dict) and c.get("donnee")]
        if ch:
            out.append(B("heading_2", "Chiffres clés"))
            out += [B("bulleted_list_item", "", [pr(str(c["donnee"])[:1500])] + who([c.get("rapport")])) for c in ch[:25]]
        out += self.render_recs(data, recs)
        conv = [c for c in data.get("convergences_recos") or [] if isinstance(c, dict)]
        rmap = {code: (ac, d, r) for code, ac, d, r in recs}
        if conv:
            out.append(B("heading_2", "🔗 Recommandations convergentes"))
            for c in conv[:20]:
                labs = [self.rec_label(rmap[str(i)][1], rmap[str(i)][2]) for i in c.get("ids") or [] if str(i) in rmap]
                if len(labs) >= 2:
                    out.append(B("bulleted_list_item", "", [pr(" ↔ ".join(labs)[:600] + " : ", bold=True),
                                                            pr(str(c.get("commentaire", ""))[:1500])]))
        if data.get("a_suivre"):
            out.append(B("heading_2", "À suivre"))
            out += [B("bulleted_list_item", str(x)) for x in data["a_suivre"][:10]]
        return out

    def render_lois(self, data, A, recs, cand):
        B, pr, lk = F.B, F.plain_rt, F.link_rt
        codes = {f"T{n}": did for n, did in enumerate(cand, 1)}
        rmap = {code: (ac, d, r) for code, ac, d, r in recs}
        items = {}
        for x in data.get("liens_lois") or []:
            if isinstance(x, dict) and x.get("code") in codes:
                labs = [self.rec_label(rmap[str(i)][1], rmap[str(i)][2]) for i in x.get("recommandations") or []
                        if str(i) in rmap]
                items[codes[x["code"]]] = (str(x.get("explication", "")), labs)
        for d in A.values():  # liens établis par les fiches des rapports
            for x in d.get("liens_detail") or []:
                did = x.get("did")
                if did in self.legi.textes and did not in items:
                    items[did] = (f"{org_short(d['organe'])} : {x.get('explication', '')}", [])
        enacted = (P.STAGES[8], P.STAGE_ORDONNANCE)
        cours = [d for d in items if self.legi.textes[d]["stage"] not in enacted]
        recent = [d for d in items if self.legi.textes[d]["stage"] in enacted]
        out = [B("heading_2", "⚖️ Textes législatifs en cours sur le sujet")]
        if not items:
            out.append(B("paragraph", "Aucun texte législatif en cours d'examen ou récemment promulgué ne porte "
                                      "directement sur ce sujet.", color="gray"))
            return out
        for label, group in (("Textes en cours d'examen", cours), ("Lois récentes (mise en application)", recent)):
            if not group:
                continue
            out.append(B("heading_3", label))
            for did in group:
                t = self.legi.textes[did]
                expl, labs = items[did]
                out.append(B("bulleted_list_item", "", [
                    lk(t["short"][:200], notion_url(t.get("page")), bold=True), pr(f" — {t['stage']}", "gray"),
                    pr("\n" + expl[:1500])] + ([pr("\nRecommandations concernées : " + " ; ".join(labs)[:800], "gray")]
                                               if labs else [])))
        return out

    def render_recs(self, data, recs):
        B, pr = F.B, F.plain_rt
        rmap = {code: (ac, d, r) for code, ac, d, r in recs}
        cl = data.get("classement") if isinstance(data.get("classement"), dict) else {}
        out = [B("heading_2", f"📋 Tableau des recommandations ({len(recs)})")]
        out.append(B("paragraph", "", [pr("Classement retenu : ", bold=True),
                                       pr(str(cl.get("critere") or "par levier d'action et par priorité")[:600]),
                                       pr(". Texte exact des recommandations ; avantages et limites tels que les "
                                          "rapports les présentent (cases vides lorsque le rapport n'en dit rien).",
                                          "gray")]))
        used, rank, cats = set(), 0, []
        for c in cl.get("categories") or []:
            if not isinstance(c, dict):
                continue
            rows = []
            for x in c.get("recommandations") or []:
                code = str(x.get("id") if isinstance(x, dict) else x)
                if code in rmap and code not in used:
                    used.add(code)
                    nat = x.get("nature", "") if isinstance(x, dict) else ""
                    prio = x.get("priorite", "") if isinstance(x, dict) else ""
                    rows.append((prio, nat if nat in NATURES else "", code))
            if rows:
                cats.append((str(c.get("nom") or "Recommandations"), str(c.get("presentation") or ""), rows))
        rest = [("", "", code) for code, ac, d, r in recs if code not in used]
        if rest:
            cats.append(("Autres recommandations" if cats else "Recommandations", "", rest))
        for i, (nom, pres, rows) in enumerate(cats, 1):
            out.append(B("heading_3", f"{i}. {nom[:200]} ({len(rows)})"))
            if pres:
                out.append(B("paragraph", pres[:1500], color="gray"))
            trs = []
            for prio, nat, code in rows:
                rank += 1
                ac, d, r = rmap[code]
                trs.append((rank, prio, nat, ac, d, r))
            out += self.rec_tables(trs)
        return out

    # ---------------- pages Notion ----------------
    def create_page(self, parent, title, icon, children=None):
        body = {"parent": {"type": "page_id", "page_id": parent}, "icon": {"type": "emoji", "emoji": icon},
                "properties": {"title": L.title_prop(title)}}
        if children:
            body["children"] = children
        try:
            return self.n.req("POST", "/pages", body)["id"]
        except RuntimeError as e:
            if "emoji" not in str(e):
                raise
            body["icon"] = {"type": "emoji", "emoji": "📁"}
            return self.n.req("POST", "/pages", body)["id"]

    def child_page(self, parent, title, icon, check=False, children=None):
        key = f"{parent}|{title}"
        pages = self.mem["pages"]
        pid = pages.get(key)
        if pid and check:
            try:
                p = self.n.req("GET", f"/pages/{pid}")
                if p.get("archived") or p.get("in_trash"):
                    pid = None
            except RuntimeError:
                pid = None
            if not pid:
                pages.pop(key, None)
        if pid:
            return pid
        cursor = None
        while True:
            res = self.n.req("GET", f"/blocks/{parent}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
            for b in res.get("results", []) or []:
                if b.get("type") == "child_page" and P.norm(b["child_page"].get("title", "")) == P.norm(title) \
                        and not b.get("archived") and not b.get("in_trash"):
                    pages[key] = b["id"]
                    return b["id"]
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        pages[key] = self.create_page(parent, title, icon, children)
        self.counts["dossiers"] += 1
        return pages[key]

    def theme_page(self, theme, check=False):
        fam = family_of(self.cfg, theme) or {"famille": "Autres thèmes", "icone": "📁", "couleur": "default", "themes": []}
        col = fam.get("couleur", "default")
        intro = [F.callout(f"Dossier « {fam['famille']} » : un sous-dossier par thème. Chaque thème réunit ses super "
                           "fiches 🔷 (synthèse de plusieurs rapports sur un même sujet) et la liste de ses fiches de "
                           "rapport 📑.", fam.get("icone") or "📁",
                           (col + "_background") if col != "default" else "gray_background")]
        fp = self.child_page(self.root, fam["famille"], fam.get("icone") or "📁", check, intro)
        return self.child_page(fp, theme, "📂", check)

    # ---------------- dossiers de thème ----------------
    def theme_pages(self):
        reps = self.reports()
        subj = self.mem["sujets"]
        days = self.cfg["fiches"]["etoile_jours"]
        for s in subj.values():  # ⭐ des super fiches mises à jour il y a plus de N jours
            if s.get("star") and s.get("page") and days_since(s["star"]) > days:
                try:
                    self.n.req("PATCH", f"/pages/{s['page']}", {"properties": {"title": L.title_prop(SUPER + s["label"])}})
                except RuntimeError as e:
                    log.debug("Étoile non retirée : %s", e)
                s["star"] = None
        by_theme, sup_by_theme, k_sub = {}, {}, {}
        for r in reps:
            for t in r.get("themes") or []:
                by_theme.setdefault(t, []).append(r)
        for s in subj.values():
            if not s.get("page"):
                continue
            for t in s.get("themes") or [s.get("theme")]:
                sup_by_theme.setdefault(t, []).append(s)
            for k in s["members"]:
                k_sub.setdefault(k, []).append(s)
        for theme in sorted(set(by_theme) | set(sup_by_theme), key=lambda t: -len(by_theme.get(t, []))):
            if time_left(self.cfg) < 180:
                break
            rs = sorted(by_theme.get(theme, []), key=lambda r: r.get("date") or "", reverse=True)
            sups = sorted(sup_by_theme.get(theme, []), key=lambda s: s.get("upd") or "", reverse=True)
            sig = P.sha(json.dumps([[r["k"], r.get("page"), r["title"], [x["label"] for x in k_sub.get(r["k"], [])]]
                                    for r in rs] + [[x.get("page"), x["label"], x.get("n"), bool(x.get("star"))]
                                                    for x in sups], ensure_ascii=False))
            tm = self.mem["themes"].setdefault(theme, {})
            if tm.get("sig") == sig:
                continue
            blocks = self.render_theme(theme, rs, sups, k_sub)
            for attempt in (0, 1):
                try:
                    put_blocks(self.n, self.theme_page(theme, check=bool(attempt)), tm, blocks)
                    break
                except RuntimeError:
                    if attempt:
                        raise
                    tm.pop("container", None)
            tm["sig"] = sig

    def render_theme(self, theme, rs, sups, k_sub):
        B, pr, lk = F.B, F.plain_rt, F.link_rt
        fam = family_of(self.cfg, theme) or {}
        col = fam.get("couleur", "default")
        out = [F.callout(f"Dossier thématique « {theme} » — {len(rs)} rapport(s), {len(sups)} super fiche(s). "
                         f"Mis à jour le {NOW:%d/%m/%Y}.", fam.get("icone") or "📂",
                         (col + "_background") if col != "default" else "gray_background"),
               B("paragraph", "🔷 Les super fiches réunissent les rapports qui portent sur le même sujet ou se "
                              "complètent : grands enjeux, textes de loi en cours, éléments de langage, tableau de "
                              "toutes les recommandations. 📑 Les fiches simples présentent un rapport.",
                 color="gray", italic=True),
               B("heading_2", "🔷 Super fiches")]
        if sups:
            for s in sups:
                out.append(B("bulleted_list_item", "", [
                    lk(("⭐ " if s.get("star") else "") + SUPER + s["label"][:200], notion_url(s["page"]), "blue", bold=True),
                    pr(f" — {s.get('n') or len(s['members'])} rapports, mise à jour le "
                       f"{F.fr_date((s.get('upd') or '')[:10])}", "gray")]))
        else:
            out.append(B("paragraph", "Aucune pour l'instant : une super fiche est créée dès que deux rapports portent "
                                      "sur le même sujet ou se complètent.", color="gray"))
        out.append(B("heading_2", f"📑 Fiches de rapport ({len(rs)})"))
        for r in rs[:300]:
            out.append(B("bulleted_list_item", "", [
                pr(F.fr_date(r.get("date")) + " — ", "gray"), pr(org_short(r["organe"])[:80] + " : ", bold=True),
                lk(r["title"][:220], notion_url(r.get("page")) or r["url"])] +
                [pr("  · 🔷 " + s["label"][:80], "blue") for s in k_sub.get(r["k"], []) if s.get("page")][:2]))
        return out

    def prune(self):
        """Supprime les données détaillées des rapports sortis de la mémoire."""
        keep = {r.get("k") for r in self.st.get("done", [])}
        folder = os.path.join(STATE_DIR, "fiches")
        try:
            for fn in os.listdir(folder):
                if fn.endswith(".json") and fn[:-5] not in keep:
                    os.remove(os.path.join(folder, fn))
        except FileNotFoundError:
            pass


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
    os.makedirs(os.path.join(STATE_DIR, "fiches"), exist_ok=True)  # toujours présent (cache GitHub)
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
    D = None
    try:
        R.collect()
        R.triage()
        R.alert_rows()
        if not args.dry_run:
            save_state(st)
        R.fiches()
        D = Dossiers(R)
        D.run()
        R.refresh()
    finally:
        vp.close()
        if not args.dry_run:
            save_state(st)
    c = R.counts
    dc = D.counts if D else {"sujets": 0, "super": 0}
    old = sum(1 for r in st.get("done", []) if r.get("v", 1) < FICHE_V)
    msg = (f"Terminé : {c['nouveaux']} nouveau(x) rapport(s), {c['ecartes']} écarté(s) (doublons, hors sujet ou "
           f"portée locale), {c['fiches']} fiche(s) rédigée(s) (dont {c.get('reprises', 0)} refaite(s)), "
           f"{len(st['queue'])} en attente, {old} ancienne(s) fiche(s) à refaire. Super fiches : {dc['sujets']} "
           f"nouveau(x) sujet(s), {dc['super']} super fiche(s) rédigée(s) ou mise(s) à jour "
           f"({len(st.get('dossiers', {}).get('sujets', {}))} sujet(s) au total). Appels IA : {llm.calls}")
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
        kept = [i for i in items if Rapports.keep_title(src, i.get("title", ""), i.get("url", ""))]
        dated = sorted([i for i in kept if i.get("date")], key=lambda i: i["date"], reverse=True)
        status = "✅" if kept else "⚠️"
        ok += bool(kept)
        out.append(f"### {status} {src['nom']} — {len(kept)} publication(s) repérée(s)"
                   + (f", la plus récente le {dated[0]['date'][:10]}" if dated else ""))
        out += [f"- {r}" for r in report]
        for i in (dated or kept)[:3]:
            out.append(f"  - {(i.get('date') or '')[:10]} {i.get('title', '')[:110]} — {i['url']}")
        if dated and sample is None and src["nom"] in ("Cour des comptes", "Sénat"):
            sample = dated[0]
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
