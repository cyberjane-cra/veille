"""
Fiches de révision alimentées par la veille
-------------------------------------------
- Un dossier par famille de thèmes (🔴 Sécurité & défense, 🔵 Politique & institutions…),
  avec une fiche par thème : problématique, grandes idées et arguments étayés par les
  articles (exemples, données chiffrées), chiffres clés, débats, chronologie.
- Un dossier Pays (rangé par région), avec une fiche par pays : situation, dernières
  actualités, chiffres tirés de l'actualité et données de référence de la Banque mondiale.

Les fiches sont mises à jour par l'IA au fil des nouveaux articles (mise à jour
incrémentale : la fiche existante est enrichie, pas réécrite de zéro).
"""
import datetime as dt
import gettext
import json
import logging
import re

import requests

log = logging.getLogger("veille")

COLOR_EMOJI = {"red": "🔴", "blue": "🔵", "yellow": "🟡", "purple": "🟣", "green": "🟢", "pink": "💗",
               "orange": "🟠", "brown": "🟤", "gray": "⚪", "default": "⚪"}

WB_INDICATORS = [  # (code Banque mondiale, libellé, format)
    ("SP.POP.TOTL", "Population", "pop"),
    ("NY.GDP.MKTP.CD", "PIB", "usd_big"),
    ("NY.GDP.PCAP.CD", "PIB par habitant", "usd"),
    ("NY.GDP.MKTP.KD.ZG", "Croissance du PIB", "pct"),
    ("FP.CPI.TOTL.ZG", "Inflation", "pct"),
    ("SL.UEM.TOTL.ZS", "Chômage", "pct"),
    ("GC.DOD.TOTL.GD.ZS", "Dette publique (administration centrale, % du PIB)", "pct"),
    ("MS.MIL.XPND.GD.ZS", "Dépenses militaires (% du PIB)", "pct"),
]

COUNTRY_OVERRIDES = {  # nom simplifié → code ISO3
    "etats unis": "USA", "russie": "RUS", "coree du sud": "KOR", "coree du nord": "PRK", "iran": "IRN",
    "syrie": "SYR", "vietnam": "VNM", "viet nam": "VNM", "taiwan": "TWN", "bolivie": "BOL",
    "venezuela": "VEN", "tanzanie": "TZA", "republique democratique du congo": "COD", "rdc": "COD",
    "congo": "COG", "moldavie": "MDA", "laos": "LAO", "palestine": "PSE", "territoires palestiniens": "PSE",
    "gaza": "PSE", "cisjordanie": "PSE", "kosovo": "XKX", "royaume uni": "GBR", "turquie": "TUR",
    "macedoine du nord": "MKD", "tchequie": "CZE", "republique tcheque": "CZE", "birmanie": "MMR",
    "myanmar": "MMR", "cote d ivoire": "CIV", "cap vert": "CPV", "emirats arabes unis": "ARE",
    "arabie saoudite": "SAU", "pays bas": "NLD", "bosnie herzegovine": "BIH", "bielorussie": "BLR",
    "groenland": "GRL", "chine": "CHN", "inde": "IND", "japon": "JPN", "israel": "ISR", "liban": "LBN",
    "libye": "LBY", "soudan du sud": "SSD", "soudan": "SDN", "ukraine": "UKR", "pologne": "POL",
}

_ISO_INDEX = None
STAR = "⭐ "
RENDER_V = 2  # version de la mise en page des fiches


def plain(title):
    return title[len(STAR):] if title.startswith(STAR) else title


STOP = {"dans", "pour", "avec", "sans", "sous", "plus", "moins", "cette", "cette", "leur", "leurs", "elle", "elles",
        "sont", "être", "etre", "avoir", "mais", "comme", "entre", "depuis", "selon", "dont", "ainsi", "aussi",
        "tout", "tous", "toute", "toutes", "fait", "faire", "peut", "doit", "deux", "trois", "notamment", "article",
        "auteur", "auteurs", "analyse", "rapport", "souligne", "explique", "estime", "note", "that", "with", "from",
        "this", "have", "will", "their", "which", "about", "into", "more", "than", "were", "been", "also",
        "pays", "monde", "international", "internationale", "politique", "question", "questions", "enjeux"}


def _toks(text, simplify):
    return {w for w in simplify(text).split() if len(w) > 3 and w not in STOP}


def related_for(st, a, simplify, k=4, now=None, cache=None):
    """Articles les plus proches de `a` (thèmes, pays et vocabulaire communs) parmi le corpus."""
    cache = cache if cache is not None else {}
    th = set(a.get("th") or [])
    py = {n for n, _ in a.get("py") or []}
    tt, tr = _toks(a.get("t", ""), simplify), _toks(a.get("t", "") + " " + a.get("r", "")[:800], simplify)
    limit = (now - dt.timedelta(days=180)).strftime("%Y-%m-%d") if now else ""
    scored = []
    for b in st.get("corpus", []):
        if not b.get("p") or b["p"] == a.get("p") or b["p"].startswith("dry-") or (b["d"] and b["d"] < limit):
            continue
        sth = len(th & set(b.get("th") or []))
        spy = len(py & {n for n, _ in b.get("py") or []})
        if not sth and not spy:
            continue
        if b["i"] not in cache:
            cache[b["i"]] = (_toks(b.get("t", ""), simplify),
                             _toks(b.get("t", "") + " " + b.get("r", "")[:800], simplify))
        bt, br = cache[b["i"]]
        jt = len(tt & bt) / len(tt | bt) if tt and bt else 0
        jr = len(tr & br) / len(tr | br) if tr and br else 0
        if jr < 0.08 and jt < 0.2:
            continue
        score = sth + 1.5 * spy + 8 * jt + 12 * jr
        if score >= 3.5:
            scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    return [b for _, b in scored[:k]]


def link_backfill(st, notion, simplify, now, time_left, limit=150):
    """Relie progressivement les articles déjà présents à leurs articles proches (colonne « Articles liés »)."""
    if "Articles liés" not in getattr(notion, "props", {}):
        return 0
    todo = [a for a in st.get("corpus", []) if not a.get("rl") and a.get("p") and not a["p"].startswith("dry-")]
    cache, n = {}, 0
    for a in todo[:limit]:
        if time_left() < 300:
            break
        rel = related_for(st, a, simplify, now=now, cache=cache)
        a["rel"] = [b["p"] for b in rel]
        a["rl"] = True
        if a["rel"]:
            try:
                notion.set_relation(a["p"], a["rel"])
            except RuntimeError as e:
                log.debug("Liens non posés pour %s : %s", a["p"], e)
        n += 1
    if n:
        log.info("Articles liés : %d article(s) reliés à leurs articles proches (reste %d)", n, len(todo) - n)
    return n


def iso3_of(name, simplify):
    """Code ISO3 d'un pays nommé en français (ou en anglais), ou None (ex. « Union européenne »)."""
    global _ISO_INDEX
    s = simplify(name)
    if s in COUNTRY_OVERRIDES:
        return COUNTRY_OVERRIDES[s]
    if _ISO_INDEX is None:
        _ISO_INDEX = {}
        try:
            import pycountry
            fr = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["fr"])
            for c in pycountry.countries:
                for n in {c.name, getattr(c, "common_name", c.name), getattr(c, "official_name", c.name)}:
                    for variant in (n, fr.gettext(n)):
                        _ISO_INDEX.setdefault(simplify(variant), c.alpha_3)
                        _ISO_INDEX.setdefault(simplify(variant.split(",")[0]), c.alpha_3)
        except Exception as e:  # noqa: BLE001
            log.warning("Table des pays indisponible : %s", e)
    return _ISO_INDEX.get(s)


def flag(iso3):
    try:
        import pycountry
        a2 = pycountry.countries.get(alpha_3=iso3).alpha_2
        return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in a2.upper())
    except Exception:  # noqa: BLE001
        return "📄"


def fmt_num(v, kind):
    def fr(x, dec=1):
        s = f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")
        return s[:-2] if s.endswith(",0") else s
    if v is None:
        return "—"
    if kind == "pop":
        return f"{fr(v / 1e6)} millions" if v >= 1e6 else fr(v, 0)
    if kind == "usd_big":
        return f"{fr(v / 1e12, 2)} billions $" if v >= 1e12 else f"{fr(v / 1e9)} Md$"
    if kind == "usd":
        return f"{fr(v, 0)} $"
    return f"{fr(v)} %"


# =====================================================================
# Corpus : mémoire compacte des articles publiés (sert de matière aux fiches)
# =====================================================================
def add_to_corpus(st, page_id, item, fiche):
    st["corpus_seq"] = st.get("corpus_seq", 0) + 1
    st.setdefault("corpus", []).append({
        "i": f"a{st['corpus_seq']}", "p": page_id, "t": fiche.get("titre_fr") or item.get("title") or "",
        "s": item.get("source", ""), "d": (item.get("date") or "")[:10], "th": fiche.get("themes", []),
        "py": [[n, r] for n, r in fiche.get("pays", [])], "r": (fiche.get("resume") or "")[:1500],
        "k": [str(x)[:300] for x in fiche.get("points_cles", [])][:6],
        "c": [str(x)[:300] for x in fiche.get("chiffres", [])][:6],
    })


def prune_corpus(st, now, days=400):
    cutoff = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    st["corpus"] = [a for a in st.get("corpus", []) if not a["d"] or a["d"] >= cutoff]


def bootstrap_corpus(st, notion, cfg):
    """Premier lancement : reconstitue le corpus à partir des fiches déjà présentes dans Notion."""
    color_region = {v: k for k, v in cfg["region_colors"].items()}
    known = {a["p"] for a in st.get("corpus", [])}
    n = 0
    for p in notion.all_pages():
        if p["id"] in known:
            continue
        pr = p["properties"]

        def txt(name):
            x = pr.get(name) or {}
            if x.get("type") in ("title", "rich_text"):
                return "".join(t.get("plain_text", "") for t in x[x["type"]])
            if x.get("type") == "select":
                return (x["select"] or {}).get("name", "")
            if x.get("type") == "date":
                return ((x["date"] or {}).get("start") or "")[:10]
            return ""

        def ms(name):
            x = pr.get(name) or {}
            return x.get("multi_select", []) if x.get("type") == "multi_select" else []

        item = {"title": txt("Titre original"), "source": txt("Source"), "date": txt("Date")}
        fiche = {"titre_fr": txt("Titre"), "themes": [o["name"] for o in ms("Thèmes")],
                 "pays": [(o["name"], color_region.get(o.get("color"))) for o in ms("Pays")],
                 "resume": txt("Résumé")}
        add_to_corpus(st, p["id"], item, fiche)
        st["corpus"][-1]["d"] = st["corpus"][-1]["d"] or p.get("created_time", "")[:10]
        n += 1
    st["corpus"].sort(key=lambda a: a["d"])
    log.info("Fiches de révision : corpus initialisé avec %d articles déjà présents dans Notion", n)


# =====================================================================
# Constructeur de fiches
# =====================================================================
class Fiches:
    def __init__(self, V, st, cfg, notion, llm):
        self.V, self.st, self.cfg, self.notion, self.llm = V, st, cfg, notion, llm
        self.fc = cfg.get("fiches") or {}
        self.pages = st.setdefault("fiche_pages", {})
        self.state = st.setdefault("fiches", {})
        self.now = V.NOW

    # ---------- arborescence Notion ----------
    def child_page(self, parent, title, icon):
        key = f"{parent}|{title}"
        if key in self.pages:
            return self.pages[key]
        cursor = None
        while True:
            res = self.notion.req("GET", f"/blocks/{parent}/children?page_size=100"
                                  + (f"&start_cursor={cursor}" if cursor else ""))
            for b in res.get("results", []):
                if b.get("type") == "child_page" and plain(b["child_page"].get("title", "")) == plain(title):
                    self.pages[key] = b["id"]
                    return b["id"]
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        body = {"parent": {"type": "page_id", "page_id": parent}, "icon": {"type": "emoji", "emoji": icon},
                "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}}}
        try:
            p = self.notion.req("POST", "/pages", body)
        except RuntimeError as e:
            if "emoji" not in str(e):
                raise
            body["icon"] = {"type": "emoji", "emoji": "📄"}  # emoji refusé par Notion (certains drapeaux…)
            p = self.notion.req("POST", "/pages", body)
        self.pages[key] = p["id"]
        return p["id"]

    def set_title(self, page_id, title):
        self.notion.req("PATCH", f"/pages/{page_id}", {"properties": {"title": {"title": [
            {"type": "text", "text": {"content": title}}]}}})

    def banner(self, index, new_ids, created, n_total):
        when = f"{self.now:%d/%m/%Y}"
        if created:
            text = f"Nouvelle fiche créée le {when} à partir de {n_total} articles."
            extra = []
        else:
            text = f"Mise à jour du {when} : {len(new_ids)} nouvel(s) article(s) intégré(s). Les ⭐ signalent les apports"
            extra = self.refs_rt(sorted(new_ids, key=lambda i: int(i[1:]), reverse=True)[:8], index)
        return {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": text}}] + extra,
            "icon": {"type": "emoji", "emoji": "⭐"}, "color": "yellow_background"}}

    def top_folder(self, title, icon):
        """Dossier principal : retrouvé où qu'il soit (vous pouvez le déplacer dans Notion), sinon créé."""
        key = "top|" + title
        pid = self.pages.get(key)
        if pid:
            try:
                p = self.notion.req("GET", f"/pages/{pid}")
                if not p.get("archived") and not p.get("in_trash"):
                    return pid
            except RuntimeError:
                pass
        res = self.notion.req("POST", "/search", {"query": title, "filter": {"property": "object", "value": "page"},
                                                  "page_size": 50})
        for p in res.get("results", []):
            t = "".join(x.get("plain_text", "") for x in
                        (p.get("properties", {}).get("title", {}) or {}).get("title", []))
            if plain(t) == title and not p.get("archived") and not p.get("in_trash") \
                    and p.get("parent", {}).get("type") != "database_id":
                self.pages[key] = p["id"]
                return p["id"]
        pid = self.child_page(self.notion.clean_id(self.notion.page), title, icon)
        self.pages[key] = pid
        return pid

    def root(self):
        return self.top_folder("Fiches de révision", "📚")

    def countries_root(self):
        return self.top_folder("Pays", "🌍")

    def all_articles(self, job, index):
        """Fin de fiche : liens vers tous les articles de la veille rattachés à la fiche."""
        arts = sorted(job["arts"], key=lambda a: (a["d"], int(a["i"][1:])), reverse=True)
        cap = self.fc.get("liens_articles_max", 400)
        out = [self.B("heading_2", f"Tous les articles de la veille ({len(arts)})")]
        for a in arts[:cap]:
            d = a["d"]
            rich = []
            if a["i"] in getattr(self, "new_ids", set()):
                rich.append({"type": "text", "text": {"content": "⭐ "}})
            if len(d) == 10:
                rich.append({"type": "text", "text": {"content": f"{d[8:10]}/{d[5:7]}/{d[:4]} · "},
                             "annotations": {"color": "gray"}})
            rich.append({"type": "text", "text": {"content": (a.get("t") or "(sans titre)")[:300],
                                                  "link": {"url": "https://www.notion.so/" + a["p"].replace("-", "")}}})
            rich.append({"type": "text", "text": {"content": f" — {a['s']}"}, "annotations": {"color": "gray"}})
            out.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich}})
        if len(arts) > cap:
            out.append(self.B("paragraph", f"… et {len(arts) - cap} articles plus anciens (filtrez le tableau de veille "
                                           f"sur ce thème ou ce pays pour les voir tous).", color="gray"))
        return out

    def write(self, page_id, key, blocks):
        """Remplace le contenu de la fiche (regroupé dans un bloc unique, supprimé puis recréé)."""
        req = self.notion.req
        old = self.state.get(key, {}).get("container")
        if old:
            try:
                req("DELETE", f"/blocks/{old}")
            except RuntimeError:
                pass
        else:  # contenu d'une version précédente sans référence : on nettoie les blocs regroupés
            res = req("GET", f"/blocks/{page_id}/children?page_size=100")
            for b in res.get("results", []):
                if b.get("type") == "synced_block":
                    req("DELETE", f"/blocks/{b['id']}")
        res = req("PATCH", f"/blocks/{page_id}/children", {"children": [{
            "object": "block", "type": "synced_block",
            "synced_block": {"synced_from": None, "children": blocks[:100]}}]})
        container = res["results"][0]["id"]
        for i in range(100, len(blocks), 100):
            req("PATCH", f"/blocks/{container}/children", {"children": blocks[i:i + 100]})
        return container

    # ---------- briques de mise en page ----------
    def refs_rt(self, refs, index):
        out = []
        for r in refs or []:
            a = index.get(str(r))
            if not a:
                continue
            label = a["s"] + (f", {a['d'][8:10]}/{a['d'][5:7]}/{a['d'][2:4]}" if len(a["d"]) == 10 else "")
            url = "https://www.notion.so/" + a["p"].replace("-", "")
            out += [{"type": "text", "text": {"content": " · " if out else " — "},
                     "annotations": {"color": "gray"}},
                    {"type": "text", "text": {"content": label, "link": {"url": url}},
                     "annotations": {"color": "gray"}}]
        return out[:40]

    def S(self, item):
        """Préfixe ⭐ si l'élément s'appuie sur un article arrivé lors de cette mise à jour."""
        refs = set(str(r) for r in (item.get("refs") or []))
        return ("⭐ " if refs & getattr(self, "new_ids", set()) else "") + str(item.get("texte", ""))

    def B(self, kind, text="", extra=None, color=None, bold=False):
        rich = [{"type": "text", "text": {"content": str(text)[:1900]},
                 "annotations": {"bold": bold, "color": color or "default"}}] if text else []
        rich += extra or []
        return {"object": "block", "type": kind, kind: {"rich_text": rich}}

    # ---------- sélection des fiches à (re)construire ----------
    def due(self):
        corpus = self.st.get("corpus", [])
        fc = self.fc
        jobs = []
        for fam in self.cfg.get("theme_families", []):
            for th in fam["etiquettes"]:
                arts = [a for a in corpus if th in a["th"]]
                jobs.append(self._job("theme", th, arts, fam, fc.get("themes_min_articles", 3),
                                      fc.get("maj_theme_apres_jours", 2)))
        by_country = {}
        for a in corpus:
            for n, r in a.get("py", []):
                by_country.setdefault(n, {"arts": [], "regions": {}})
                by_country[n]["arts"].append(a)
                if r:
                    by_country[n]["regions"][r] = by_country[n]["regions"].get(r, 0) + 1
        for n, d in by_country.items():
            reg = max(d["regions"], key=d["regions"].get) if d["regions"] else "Monde"
            jobs.append(self._job("pays", n, d["arts"], {"region": reg}, fc.get("pays_min_articles", 4),
                                  fc.get("maj_pays_apres_jours", 4)))
        jobs = [j for j in jobs if j]
        jobs.sort(key=lambda j: (j["new"] == 0 and j["exists"], -j["new"]))
        return jobs

    def _job(self, kind, name, arts, meta, min_n, days):
        key = f"{kind}:{name}"
        s = self.state.get(key)
        if not s:
            if len(arts) < min_n:
                return None
            return {"kind": kind, "name": name, "arts": arts, "meta": meta, "key": key, "new": len(arts),
                    "exists": False}
        last = s.get("last_seq", 0)
        new = [a for a in arts if int(a["i"][1:]) > last]
        if not new:
            return None
        age = (self.now - self.V.parse_date(s["upd"])).total_seconds() / 86400 if s.get("upd") else 99
        if age < days and len(new) < self.fc.get("nouveaux_articles_pour_maj_immediate", 10):
            return None
        return {"kind": kind, "name": name, "arts": arts, "meta": meta, "key": key, "new": len(new),
                "exists": True}

    # ---------- prompts ----------
    @staticmethod
    def art_text(a):
        lines = [f"[{a['i']}] {a['d'] or 'date inconnue'} — {a['s']} — {a['t']}", f"Résumé : {a['r'][:1000]}"]
        if a.get("k"):
            lines.append("Points clés : " + " | ".join(a["k"]))
        if a.get("c"):
            lines.append("Chiffres : " + " | ".join(a["c"]))
        return "\n".join(lines)

    def prompt(self, job, current, arts):
        if job["kind"] == "theme":
            fam = job["meta"]["famille"]
            schema = ('{"problematique": "3 à 5 phrases : définition, enjeux, question centrale", '
                      '"a_retenir": ["5 à 8 idées essentielles à mémoriser, une phrase chacune"], '
                      '"arguments": [{"titre": "intitulé court de la grande idée", '
                      '"idee": "2 à 4 phrases qui développent l\'argument", '
                      '"exemples": [{"texte": "exemple concret ou donnée chiffrée précise (avec unité, date, acteur)", '
                      '"refs": ["a12"]}]}], '
                      '"chiffres": [{"texte": "donnée chiffrée clé et son contexte", "refs": ["a3"]}], '
                      '"debats": [{"texte": "point de vue ou controverse, en nommant qui défend quoi", "refs": ["a7"]}], '
                      '"chronologie": [{"date": "AAAA-MM-JJ", "texte": "événement", "refs": ["a9"]}]}')
            intro = (f"Tu es professeur de préparation aux concours (Sciences Po, INSP, relations internationales, "
                     f"questions contemporaines). Tu tiens à jour une FICHE DE RÉVISION sur le thème « {job['name']} » "
                     f"(dossier « {fam} »), alimentée par une revue de presse de think tanks et de médias spécialisés.")
            limits = ("Limites : 4 à 8 grandes idées (arguments), 2 à 6 exemples par idée, 15 chiffres au plus, "
                      "6 débats au plus, 15 événements de chronologie au plus (les plus importants et récents).")
        else:
            schema = ('{"situation": "3 à 5 phrases sur la situation actuelle du pays (politique, sécurité, économie)", '
                      '"actualites": [{"date": "AAAA-MM-JJ", "texte": "fait d\'actualité en une phrase", "refs": ["a4"]}], '
                      '"chiffres": [{"texte": "donnée chiffrée clé tirée des articles", "refs": ["a2"]}], '
                      '"enjeux": ["3 à 6 enjeux majeurs pour le pays, une phrase chacun"]}')
            intro = (f"Tu tiens à jour une FICHE PAYS de révision, courte et factuelle, sur « {job['name']} », "
                     "alimentée par une revue de presse de think tanks et de médias spécialisés.")
            limits = "Limites : 10 actualités au plus (les plus récentes et importantes d'abord), 10 chiffres au plus."
        lines = [
            intro,
            "Règles impératives :",
            "- Utilise UNIQUEMENT les informations de la fiche existante et des articles fournis ; n'invente aucun fait ni chiffre.",
            "- Chaque exemple, chiffre, débat ou événement cite ses sources par leurs identifiants dans \"refs\" (ex. [\"a12\", \"a40\"]).",
            "- Privilégie les arguments structurants et les données chiffrées précises (unité, date, acteur).",
            "- Mise à jour : conserve ce qui reste pertinent dans la fiche existante, intègre les nouveaux articles, "
            "fusionne les doublons, remplace ce qui est dépassé, retire le secondaire pour respecter les limites.",
            "- Rédige en français, dans un style clair et dense de fiche de révision.",
            limits,
            "Réponds UNIQUEMENT avec un objet JSON de cette forme : " + schema,
            "",
            "=== FICHE EXISTANTE ===",
            json.dumps(current, ensure_ascii=False) if current else "(aucune : crée la fiche)",
            "",
            f"=== ARTICLES ({len(arts)}) ===",
        ]
        lines += [self.art_text(a) + "\n" for a in arts]
        return "\n".join(lines)

    # ---------- rendu Notion ----------
    def blocks_theme(self, job, d, index, n_total):
        B = self.B
        meta = (f"Fiche mise à jour automatiquement le {self.now:%d/%m/%Y} à partir de {n_total} articles de la veille. "
                "Les liens gris renvoient aux fiches-articles.")
        out = [B("paragraph", meta, color="gray")]
        if d.get("problematique"):
            out += [B("heading_2", "Problématique"), B("paragraph", d["problematique"])]
        if d.get("a_retenir"):
            out.append(B("heading_2", "À retenir"))
            out += [B("bulleted_list_item", x) for x in d["a_retenir"][:8]]
        if d.get("arguments"):
            out.append(B("heading_2", "Grandes idées et arguments"))
            for i, ar in enumerate(d["arguments"][:8], 1):
                out.append(B("heading_3", f"{i}. {ar.get('titre', '')}"))
                if ar.get("idee"):
                    out.append(B("paragraph", ar["idee"]))
                for ex in (ar.get("exemples") or [])[:6]:
                    out.append(B("bulleted_list_item", self.S(ex), self.refs_rt(ex.get("refs"), index)))
        if d.get("chiffres"):
            out.append(B("heading_2", "Chiffres clés"))
            out += [B("bulleted_list_item", self.S(c), self.refs_rt(c.get("refs"), index))
                    for c in d["chiffres"][:15]]
        if d.get("debats"):
            out.append(B("heading_2", "Débats et points de vue"))
            out += [B("bulleted_list_item", self.S(c), self.refs_rt(c.get("refs"), index))
                    for c in d["debats"][:6]]
        if d.get("chronologie"):
            out.append(B("heading_2", "Chronologie récente"))
            for c in sorted(d["chronologie"], key=lambda x: x.get("date", ""), reverse=True)[:15]:
                dd = c.get("date", "")
                lab = f"{dd[8:10]}/{dd[5:7]}/{dd[:4]} — " if len(dd) == 10 else ""
                out.append(B("bulleted_list_item", lab + self.S(c), self.refs_rt(c.get("refs"), index)))
        return out

    def blocks_country(self, job, d, index, n_total, wb):
        B = self.B
        out = [B("paragraph", f"Fiche mise à jour automatiquement le {self.now:%d/%m/%Y} à partir de "
                              f"{n_total} articles de la veille.", color="gray")]
        if d.get("situation"):
            out += [B("heading_2", "Situation"), B("paragraph", d["situation"])]
        if d.get("actualites"):
            out.append(B("heading_2", "Dernières actualités"))
            for c in sorted(d["actualites"], key=lambda x: x.get("date", ""), reverse=True)[:10]:
                dd = c.get("date", "")
                lab = f"{dd[8:10]}/{dd[5:7]}/{dd[:4]} — " if len(dd) == 10 else ""
                out.append(B("bulleted_list_item", lab + self.S(c), self.refs_rt(c.get("refs"), index)))
        if wb:
            out.append(B("heading_2", "Données de référence (Banque mondiale)"))
            for code, label, kind in WB_INDICATORS:
                if code in wb:
                    v, year = wb[code]
                    out.append(B("bulleted_list_item", f"{label} : ", [
                        {"type": "text", "text": {"content": fmt_num(v, kind)}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": f" ({year})"}, "annotations": {"color": "gray"}}]))
        if d.get("chiffres"):
            out.append(B("heading_2", "Chiffres tirés de l'actualité"))
            out += [B("bulleted_list_item", self.S(c), self.refs_rt(c.get("refs"), index))
                    for c in d["chiffres"][:10]]
        if d.get("enjeux"):
            out.append(B("heading_2", "Enjeux"))
            out += [B("bulleted_list_item", x) for x in d["enjeux"][:6]]
        return out

    # ---------- Banque mondiale ----------
    def world_bank(self, iso3):
        if not iso3:
            return None
        cache = self.st.setdefault("wb", {})
        c = cache.get(iso3)
        if c and (self.V.age_days(c["date"]) or 99) < 30:
            return c["data"]
        data = {}
        for code, _, _ in WB_INDICATORS:
            try:
                r = requests.get(f"https://api.worldbank.org/v2/country/{iso3}/indicator/{code}",
                                 params={"format": "json", "mrnev": 1}, timeout=30)
                j = r.json()
                if isinstance(j, list) and len(j) > 1 and j[1]:
                    row = j[1][0]
                    if row.get("value") is not None:
                        data[code] = [row["value"], row.get("date")]
            except Exception as e:  # noqa: BLE001
                log.debug("Banque mondiale %s %s : %s", iso3, code, e)
        cache[iso3] = {"date": self.V.iso(self.now), "data": data}
        return data

    # ---------- exécution ----------
    def build(self, job):
        maxa = self.fc.get("articles_par_fiche_max", 50)
        s = self.state.get(job["key"], {})
        arts = sorted(job["arts"], key=lambda a: a["d"], reverse=True)
        if s.get("data"):
            last = s.get("last_seq", 0)
            use = [a for a in arts if int(a["i"][1:]) > last][:maxa]
        else:
            use = arts[:maxa]
        created = not s.get("data")
        self.new_ids = set() if created else {a["i"] for a in use}
        prompt = self.prompt(job, s.get("data"), use)
        data = self.llm.ask_json(prompt)
        if not isinstance(data, dict):
            raise ValueError("fiche illisible")
        index = {a["i"]: a for a in self.st.get("corpus", [])}
        if job["kind"] == "theme":
            fam = job["meta"]
            parent = self.child_page(self.root(), fam["famille"], COLOR_EMOJI.get(fam.get("couleur"), "📁"))
            page = s.get("page") or self.child_page(parent, job["name"], "📘")
            blocks = self.blocks_theme(job, data, index, len(job["arts"])) + self.all_articles(job, index)
        else:
            reg = job["meta"]["region"]
            parent = self.child_page(self.countries_root(), reg,
                                     COLOR_EMOJI.get(self.cfg["region_colors"].get(reg, "gray"), "📁"))
            iso3 = iso3_of(job["name"], self.V.simplify)
            page = s.get("page") or self.child_page(parent, job["name"], flag(iso3) if iso3 else "📄")
            blocks = self.blocks_country(job, data, index, len(job["arts"]), self.world_bank(iso3)) + \
                self.all_articles(job, index)
        blocks.insert(0, self.banner(index, self.new_ids, created, len(job["arts"])))
        self.state.setdefault(job["key"], {})["v"] = RENDER_V
        container = self.write(page, job["key"], blocks)
        self.set_title(page, STAR + job["name"])
        self.st.setdefault("folders", {})[parent] = job["meta"].get("famille") or job["meta"].get("region")
        self.state[job["key"]] = {"page": page, "container": container, "data": data, "upd": self.V.iso(self.now),
                                  "last_seq": max(int(a["i"][1:]) for a in job["arts"]),
                                  "star": self.V.iso(self.now), "name": job["name"], "parent": parent,
                                  "v": RENDER_V}

    def rerender(self, key):
        """Remet en forme une fiche existante (nouvelle présentation) sans rappeler l'IA."""
        s = self.state[key]
        kind, name = key.split(":", 1)
        corpus = self.st.get("corpus", [])
        if kind == "theme":
            fam = next((f for f in self.cfg.get("theme_families", []) if name in f["etiquettes"]), {"famille": ""})
            job = {"kind": kind, "name": name, "meta": fam, "arts": [a for a in corpus if name in a["th"]]}
        else:
            job = {"kind": kind, "name": name, "meta": {}, "arts": [a for a in corpus
                                                                     if name in [n for n, _ in a.get("py", [])]]}
        if not job["arts"]:
            s["v"] = RENDER_V
            return
        index = {a["i"]: a for a in corpus}
        self.new_ids = set()
        if kind == "theme":
            blocks = self.blocks_theme(job, s["data"], index, len(job["arts"]))
        else:
            blocks = self.blocks_country(job, s["data"], index, len(job["arts"]),
                                         self.world_bank(iso3_of(name, self.V.simplify)))
        s["container"] = self.write(s["page"], key, blocks + self.all_articles(job, index))
        s["v"] = RENDER_V

    def refresh_stars(self):
        """Retire l'étoile des fiches mises à jour il y a plus de N jours ; étoile les dossiers concernés."""
        days = self.fc.get("etoile_jours", 3)
        for key, s in self.state.items():
            if s.get("star") and (self.V.age_days(s["star"]) or 0) > days:
                try:
                    self.set_title(s["page"], s.get("name") or key.split(":", 1)[1])
                    s.pop("star", None)
                except RuntimeError as e:
                    log.debug("Étoile non retirée (%s) : %s", key, e)
        starred = {s.get("parent") for s in self.state.values() if s.get("star")}
        shown = self.st.setdefault("folders_star", {})
        for fid, name in self.st.get("folders", {}).items():
            want = fid in starred
            if shown.get(fid) != want:
                try:
                    self.set_title(fid, (STAR if want else "") + name)
                    shown[fid] = want
                except RuntimeError as e:
                    log.debug("Dossier %s : %s", name, e)


def update(V, st, cfg, notion, llm, time_left):
    fc = cfg.get("fiches") or {}
    if not fc.get("actif", True):
        return 0
    if not st.get("corpus_init"):
        bootstrap_corpus(st, notion, cfg)
        st["corpus_init"] = True
    prune_corpus(st, V.NOW)
    link_backfill(st, notion, V.simplify, V.NOW, time_left)
    f = Fiches(V, st, cfg, notion, llm)
    # Fiches existantes : application de la nouvelle mise en page (liste de tous les articles…)
    for key in [k for k, s in st.get("fiches", {}).items() if s.get("data") and s.get("page")
                and s.get("v") != RENDER_V][:25]:
        if time_left() < 400:
            break
        try:
            f.rerender(key)
            log.info("  fiche « %s » remise en forme", key.split(":", 1)[1])
        except Exception as e:  # noqa: BLE001
            log.warning("Fiche « %s » non remise en forme : %s", key, e)
    jobs = f.due()
    if not jobs:
        f.refresh_stars()
        return 0
    log.info("Fiches de révision : %d fiche(s) à créer ou mettre à jour", len(jobs))
    done = 0
    for job in jobs[: fc.get("fiches_par_passage", 6)]:
        if time_left() < 240:
            break
        try:
            f.build(job)
            done += 1
            log.info("  fiche %s « %s » à jour (%d nouveaux articles)",
                     "thème" if job["kind"] == "theme" else "pays", job["name"], job["new"])
        except V.QuotaExhausted as e:
            log.warning("Fiches : quota IA épuisé (%s) — reprise au prochain passage", e)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Fiche « %s » non mise à jour : %s", job["name"], e)
        V.save_state(st)
    f.refresh_stars()
    return done
