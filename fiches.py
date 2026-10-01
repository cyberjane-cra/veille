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
                if b.get("type") == "child_page" and b["child_page"].get("title") == title:
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

    def root(self):
        return self.child_page(self.notion.clean_id(self.notion.page), "Fiches de révision", "📚")

    def countries_root(self):
        return self.child_page(self.notion.clean_id(self.notion.page), "Pays", "🌍")

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
                    out.append(B("bulleted_list_item", ex.get("texte", ""), self.refs_rt(ex.get("refs"), index)))
        if d.get("chiffres"):
            out.append(B("heading_2", "Chiffres clés"))
            out += [B("bulleted_list_item", c.get("texte", ""), self.refs_rt(c.get("refs"), index))
                    for c in d["chiffres"][:15]]
        if d.get("debats"):
            out.append(B("heading_2", "Débats et points de vue"))
            out += [B("bulleted_list_item", c.get("texte", ""), self.refs_rt(c.get("refs"), index))
                    for c in d["debats"][:6]]
        if d.get("chronologie"):
            out.append(B("heading_2", "Chronologie récente"))
            for c in sorted(d["chronologie"], key=lambda x: x.get("date", ""), reverse=True)[:15]:
                dd = c.get("date", "")
                lab = f"{dd[8:10]}/{dd[5:7]}/{dd[:4]} — " if len(dd) == 10 else ""
                out.append(B("bulleted_list_item", lab + c.get("texte", ""), self.refs_rt(c.get("refs"), index)))
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
                out.append(B("bulleted_list_item", lab + c.get("texte", ""), self.refs_rt(c.get("refs"), index)))
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
            out += [B("bulleted_list_item", c.get("texte", ""), self.refs_rt(c.get("refs"), index))
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
        prompt = self.prompt(job, s.get("data"), use)
        data = self.llm.ask_json(prompt)
        if not isinstance(data, dict):
            raise ValueError("fiche illisible")
        index = {a["i"]: a for a in self.st.get("corpus", [])}
        if job["kind"] == "theme":
            fam = job["meta"]
            parent = self.child_page(self.root(), fam["famille"], COLOR_EMOJI.get(fam.get("couleur"), "📁"))
            page = s.get("page") or self.child_page(parent, job["name"], "📘")
            blocks = self.blocks_theme(job, data, index, len(job["arts"]))
        else:
            reg = job["meta"]["region"]
            parent = self.child_page(self.countries_root(), reg,
                                     COLOR_EMOJI.get(self.cfg["region_colors"].get(reg, "gray"), "📁"))
            iso3 = iso3_of(job["name"], self.V.simplify)
            page = s.get("page") or self.child_page(parent, job["name"], flag(iso3) if iso3 else "📄")
            blocks = self.blocks_country(job, data, index, len(job["arts"]), self.world_bank(iso3))
        container = self.write(page, job["key"], blocks)
        self.state[job["key"]] = {"page": page, "container": container, "data": data, "upd": self.V.iso(self.now),
                                  "last_seq": max(int(a["i"][1:]) for a in job["arts"])}


def update(V, st, cfg, notion, llm, time_left):
    fc = cfg.get("fiches") or {}
    if not fc.get("actif", True):
        return 0
    if not st.get("corpus_init"):
        bootstrap_corpus(st, notion, cfg)
        st["corpus_init"] = True
    prune_corpus(st, V.NOW)
    f = Fiches(V, st, cfg, notion, llm)
    jobs = f.due()
    if not jobs:
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
    return done
