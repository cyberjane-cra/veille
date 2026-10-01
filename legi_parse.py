"""
Lecture des pages de vie-publique.fr (rubrique « Autour de la loi »)
--------------------------------------------------------------------
Fonctions pures : HTML -> données. Aucune requête réseau ici.

- liste des dossiers législatifs d'une législature
- dossier législatif (étapes de la procédure, documents, débats, liens JORF)
- échéancier d'application (décrets prévus / publiés)
- exposé des motifs
- panorama des lois (frise « Où en est-on ? », état, historique, contenu)
- pages de listes (panoramas, consultations, actualités, rapports)
- consultation publique
- calcul du stade de la procédure
"""
import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup

BASE = "https://www.vie-publique.fr"

MOIS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}


def soup_of(html):
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:  # noqa: BLE001  (lxml absent)
        return BeautifulSoup(html or "", "html.parser")


def norm(s):
    """minuscules, sans accents, espaces simples (pour comparer des libellés)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = s.replace("’", "'").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def txt(el):
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).replace(" ,", ",").replace(" .", ".").strip()


BLOCK_TAGS = ("p", "li", "h2", "h3", "h4", "h5", "td", "th", "blockquote", "dt", "dd")


def block_text(el):
    """Texte d'un bloc HTML, un paragraphe par ligne (les liens et mises en forme ne coupent pas les phrases)."""
    if el is None:
        return ""
    lines = []
    for b in el.find_all(BLOCK_TAGS):
        anc, nested = b.parent, False
        while anc is not None and anc is not el:
            if anc.name in BLOCK_TAGS:
                nested = True
                break
            anc = anc.parent
        if nested:
            continue
        t = txt(b)
        if t:
            lines.append(("- " if b.name == "li" else "") + t)
    return "\n".join(lines) if lines else txt(el)


def absu(href):
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE + href
    return href


def clean_vp_url(u):
    """Retire les paramètres de suivi des liens vie-publique (?egn-publisher=…)."""
    u = absu(u)
    if "vie-publique.fr" in u:
        u = re.sub(r"\?egn-[^#]*$", "", u)
    return u


def date_fr(s):
    """« 3 août 2026 », « 1er octobre 2026 », « 28/09/2026 », « 2026-09-16 » -> 'AAAA-MM-JJ' (ou None)."""
    if not s:
        return None
    s = norm(s)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})(?:er)?\s+(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|"
                  r"novembre|decembre)\s+(\d{4})\b", s)
    if m:
        return f"{int(m.group(3)):04d}-{MOIS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


def first_date_fr(s):
    """Première date d'un libellé (« LOI n° 2026-6 du 7 janvier 2026 … loi du 27 février 2004 » -> 2026-01-07)."""
    m = re.search(r"\b\d{1,2}(?:er)?\s+(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[ûu]t|septembre|"
                  r"octobre|novembre|d[ée]cembre)\s+\d{4}\b", s or "", re.I)
    return date_fr(m.group(0)) if m else None


def last_date_fr(s):
    """Dernière date présente dans un libellé (« … le 10 juin 2025 »)."""
    found = re.findall(r"\b\d{1,2}(?:er)?\s+(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[ûu]t|septembre|"
                       r"octobre|novembre|d[ée]cembre)\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b", s or "", re.I)
    return date_fr(found[-1]) if found else None


def sha(*parts):
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def main_zone(soup):
    return soup.select_one("#block-vp-dsfr-content") or soup.find("main") or soup


def is_vp_page(html):
    """Vraie page du site (et non une page de vérification anti-robots)."""
    return bool(html) and ("vp_dsfr" in html or "vie-publique.fr" in html[:5000] and "<main" in html)


def is_rss(xml):
    return bool(xml) and "<rss" in xml[:2000] and "<item" in xml


# =====================================================================
# Liste des dossiers législatifs (/liste/dossierslegislatifs/17)
# =====================================================================
def parse_list_dossiers(html):
    """Renvoie [{id, url, title, section, cat, year}] dans l'ordre de la page."""
    s = soup_of(html)
    m = main_zone(s)
    out, cur = [], {"h2": "", "h3": "", "h4": ""}
    for el in m.find_all(["h2", "h3", "h4", "a"]):
        if el.name in ("h2", "h3", "h4"):
            cur[el.name] = txt(el)
            if el.name == "h2":
                cur["h3"] = cur["h4"] = ""
            elif el.name == "h3":
                cur["h4"] = ""
            continue
        href = el.get("href") or ""
        mm = re.search(r"/dossierlegislatif/(JORFDOLE\d+)", href)
        if not mm:
            continue
        out.append({"id": mm.group(1), "url": f"{BASE}/dossierlegislatif/{mm.group(1)}", "title": txt(el),
                    "section": cur["h2"], "cat": cur["h3"], "year": cur["h4"]})
    seen, res = set(), []
    for d in out:
        if d["id"] not in seen:
            seen.add(d["id"])
            res.append(d)
    return res


# =====================================================================
# Dossier législatif
# =====================================================================
def classify_step(label, url=""):
    """Type d'une étape de la procédure, d'après son libellé vie-publique / Légifrance."""
    n = norm(label)
    host = (url or "").lower()
    ch = "AN" if "assemblee nationale" in n or "assemblee-nationale" in host else \
        ("Sénat" if "senat" in n or "senat.fr" in host else "")
    if n.startswith("loi") or n.startswith("ordonnance n") or n.startswith("loi organique"):
        return "promulgation", ch
    if "decision du conseil constitutionnel" in n or "conseil-constitutionnel" in host:
        return "decision_cc", ch
    if "saisine" in n and "conseil constitutionnel" in n:
        return "saisine_cc", ch
    if "lettre rectificative" in n:
        return "lettre_rectificative", ch
    if "etude d'impact" in n:
        return "etude_impact", ch
    if "avis du conseil d'etat" in n:
        return "avis_ce", ch
    if "conseil des ministres" in n:
        return "conseil_ministres", ch
    if re.search(r"\brejet", n) or "n'a pas adopte" in n:
        return "rejet", ch
    if "considere comme adopte" in n or "article 49" in n:
        return "art49_3", ch
    if "retrait" in n or "retire" in n:
        return "retrait", ch
    if "texte adopte" in n or "adopte" in n or "texte modifie" in n:
        if "article 45, alinea 3" in n or "article 45, alineas 2 et 3" in n or "commission mixte" in n:
            return "adoption_cmp", ch
        if "lecture definitive" in n:
            return "lecture_definitive", ch
        if "nouvelle lecture" in n:
            return "nouvelle_lecture", ch
        if re.search(r"\b(2e|2eme|deuxieme|3e|3eme|troisieme)\s+lecture", n):
            return "lecture_2", ch
        if "sans modification" in n or "conforme" in n:
            return "adoption_conforme", ch
        return "adoption_1", ch
    if n.startswith("projet de loi") or n.startswith("proposition de loi") or "texte depose" in n:
        return "depot", ch
    return "autre", ch


STEP_LABELS = {
    "depot": "Dépôt", "conseil_ministres": "Conseil des ministres", "etude_impact": "Étude d'impact",
    "avis_ce": "Avis du Conseil d'État", "lettre_rectificative": "Lettre rectificative du Gouvernement",
    "adoption_1": "Adoption en 1re lecture", "adoption_conforme": "Adoption sans modification",
    "lecture_2": "Adoption en 2e lecture (ou suivante)", "adoption_cmp": "Adoption du texte de la CMP",
    "nouvelle_lecture": "Adoption en nouvelle lecture", "lecture_definitive": "Adoption en lecture définitive",
    "art49_3": "Engagement de la responsabilité du Gouvernement (art. 49, al. 3)", "rejet": "Rejet",
    "retrait": "Retrait", "saisine_cc": "Saisine du Conseil constitutionnel",
    "decision_cc": "Décision du Conseil constitutionnel", "promulgation": "Promulgation / publication au JO",
    "cmp_accord": "Accord en commission mixte paritaire", "cmp_echec": "Échec de la commission mixte paritaire",
    "autre": "Autre étape",
}


def _section_links(m, title_regex):
    """Liens situés dans le bloc dont le titre h2 correspond au motif."""
    for h2 in m.find_all("h2"):
        if re.search(title_regex, norm(txt(h2))):
            box = h2.parent
            return box, [a for a in box.find_all("a", href=True)]
    return None, []


def _accordions(box):
    """[{groupe, items:[{label, url}]}] pour les blocs dépliables (documents, débats)."""
    out = []
    if box is None:
        return out
    for sec in box.find_all("section", class_=re.compile("fr-accordion")):
        title = txt(sec.find(["h3", "h4"], class_=re.compile("accordion__title")))
        parent = sec.find_parent("section", class_=re.compile("fr-accordion"))
        if parent is not None:  # sous-bloc (débats par date) : « Assemblée nationale (1ère lecture) — 1ère séance… »
            title = txt(parent.find(["h3", "h4"], class_=re.compile("accordion__title"))) + " — " + title
        items = []
        for li in sec.find_all("li"):
            a = li.find("a", href=True)
            if not a:
                continue
            items.append({"label": txt(li), "url": absu(a["href"])})
        if items:
            out.append({"groupe": title, "items": items})
    # sections imbriquées (débats AN par date) : on évite les doublons d'éléments
    seen, res = set(), []
    for g in out:
        its = [i for i in g["items"] if (g["groupe"], i["url"]) not in seen]
        for i in its:
            seen.add((g["groupe"], i["url"]))
        if its:
            res.append({"groupe": g["groupe"], "items": its})
    return res


def parse_dossier(html, url=""):
    s = soup_of(html)
    m = main_zone(s)
    d = {"url": url, "title": txt(m.find("h1")), "modified": None, "jorf": [], "tabs": [], "panorama": None,
         "steps": [], "parl": {}, "documents": [], "debats": [], "accel": False, "no_decree": False, "nor": None}
    mm = re.search(r"Derni[èe]re modification\s*:\s*(\d{2}/\d{2}/\d{4})", m.get_text(" "))
    if mm:
        d["modified"] = date_fr(mm.group(1))
    nor = re.search(r"\(([A-Z]{4}\d{7}[A-Z])\)", d["title"])
    if nor:
        d["nor"] = nor.group(1)
    for a in m.find_all("a", href=True):
        href, lab = a["href"], txt(a)
        if "legifrance.gouv.fr/jorf/id/" in href and a.find_parent("div", class_=re.compile("callout")) is None:
            # liens du haut de page : loi publiée, décision du Conseil constitutionnel
            h2_before = a.find_previous("h2")
            if h2_before is None or "processus" not in norm(txt(h2_before)):
                kind, _ = classify_step(lab, href)
                d["jorf"].append({"label": lab, "url": href, "kind": kind, "date": first_date_fr(lab)})
                if "n'appelant pas de decret" in norm(lab):
                    d["no_decree"] = True
        if "detailType=" in href:
            d["tabs"].append({"label": lab, "url": href})
        if re.search(r"^/loi/\d+", href) or re.search(r"vie-publique\.fr/loi/\d+", href):
            d["panorama"] = absu(href)
    # dédoublonnage des liens JORF
    seen, jorf = set(), []
    for j in d["jorf"]:
        if j["url"] not in seen:
            seen.add(j["url"])
            jorf.append(j)
    d["jorf"] = jorf
    # Processus législatif
    box, links = _section_links(m, r"processus legislatif")
    if box is not None:
        for li in box.find_all("li"):
            a = li.find("a", href=True)
            label = txt(li)
            href = absu(a["href"]) if a else ""
            kind, ch = classify_step(label, href)
            date = last_date_fr(label)
            d["steps"].append({"label": label, "url": href, "kind": kind, "chamber": ch, "date": date,
                               "k": sha(norm(label))})
    # Dossiers des assemblées
    box, links = _section_links(m, r"dossiers legislatifs")
    for a in links:
        lab = norm(txt(a))
        if "senat" in lab or "senat.fr" in a["href"]:
            d["parl"]["Sénat"] = absu(a["href"])
        elif "assemblee" in lab or "assemblee-nationale" in a["href"]:
            d["parl"]["Assemblée nationale"] = absu(a["href"])
    box, _ = _section_links(m, r"documents preparatoires")
    d["documents"] = _accordions(box)
    box, _ = _section_links(m, r"debats parlementaires")
    if box is not None:
        h2 = box.find("h2")
        if "acceleree" in norm(txt(h2)):
            d["accel"] = True
        d["debats"] = _accordions(box)
    return d


def parse_echeancier(html):
    """Lignes de l'échéancier : article, base légale, objet, statut, mesures publiées, entrée en vigueur."""
    s = soup_of(html)
    m = main_zone(s)
    rows = []
    for table in m.find_all("table"):
        section = txt(table.find_previous(["h3", "h4"])) if table.find_previous(["h3", "h4"]) else ""
        for tr in table.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 4:
                continue
            if norm(txt(tds[0])) == "article" or tr.find("strong") and norm(txt(tds[0])).startswith("article") \
                    and norm(txt(tds[2])) == "objet":
                continue
            mesures = [{"label": txt(a), "url": absu(a["href"])} for a in tds[3].find_all("a", href=True)]
            statut = txt(tds[3])
            ns = norm(statut)
            if mesures and any("jorf" in x["url"].lower() or "legifrance" in x["url"] for x in mesures):
                etat = "publiée"
            elif "eventuelle" in ns:
                etat = "éventuelle"
            elif "sans objet" in ns or "abroge" in ns:
                etat = "sans objet"
            else:
                etat = "attendue"
            rows.append({"article": txt(tds[0]), "base": txt(tds[1]), "objet": txt(tds[2])[:600],
                         "statut": statut[:300], "mesures": mesures, "etat": etat,
                         "vigueur": txt(tds[4]) if len(tds) > 4 else "", "section": section,
                         "k": sha(norm(txt(tds[0])), norm(txt(tds[2]))[:200])})
    return rows


def echeancier_stats(rows):
    utiles = [r for r in rows if r["etat"] in ("publiée", "attendue")]
    pub = [r for r in utiles if r["etat"] == "publiée"]
    return {"total": len(utiles), "pub": len(pub), "taux": (len(pub) / len(utiles)) if utiles else None}


def parse_detail_text(html, max_chars=60000):
    """Texte d'un onglet de dossier (exposé des motifs, texte du projet)."""
    s = soup_of(html)
    m = main_zone(s)
    for junk in m.find_all(["script", "style", "nav", "button"]):
        junk.decompose()
    t = m.get_text("\n", strip=True)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"^.*?Derni[èe]re modification\s*:\s*\d{2}/\d{2}/\d{4}\s*", "", t, flags=re.S)
    return t.replace("Haut de page", "").strip()[:max_chars]


# =====================================================================
# Panorama des lois (/loi/123456-…)
# =====================================================================
def pano_id(url):
    m = re.search(r"/loi/(\d+)", url or "")
    return m.group(1) if m else None


def vp_id(url):
    m = re.search(r"vie-publique\.fr/[a-z-]+/(\d+)", absu(url) or "") or re.search(r"^/[a-z-]+/(\d+)", url or "")
    return m.group(1) if m else None


def parse_panorama(html, url=""):
    s = soup_of(html)
    m = main_zone(s)
    p = {"url": url, "id": pano_id(url), "title": txt(m.find("h1")), "chapo": "", "published": None,
         "updated": None, "timeline": [], "status": "", "historique": "", "body": "", "sources": [],
         "keywords": [], "dossier": None}
    ch = m.select_one(".field--name-field-chapo")
    p["chapo"] = txt(ch)
    full = m.get_text(" ", strip=True)
    mm = re.search(r"Publi[ée] le\s+(\d{1,2}(?:er)?\s+\w+\s+\d{4})", full)
    if mm:
        p["published"] = date_fr(mm.group(1))
    mm = re.search(r"(?:Mis à jour|Dernière mise à jour) le\s+(\d{1,2}(?:er)?\s+\w+\s+\d{4})", full)
    if mm:
        p["updated"] = date_fr(mm.group(1))
    ol = m.select_one("ol.timeline-list")
    if ol:
        for li in ol.find_all("li", recursive=False):
            cls = " ".join(li.get("class") or [])
            state = "valid" if "valid" in cls else ("progress" if "progress" in cls else "future")
            ps = [txt(x) for x in li.find_all("p")]
            ps = [x for x in ps if not re.match(r"^[ÉE]tape \d+", x)]
            name = ps[0] if ps else ""
            date = date_fr(ps[1]) if len(ps) > 1 else None
            detail = " ".join(ps[2:]) if len(ps) > 2 else ""
            p["timeline"].append({"name": name, "date": date, "state": state, "detail": detail})
    st = m.select_one(".vp-law--status .fr-callout__title")
    p["status"] = txt(st)
    hist = m.select_one(".field--name-field-where-we-are-")
    p["historique"] = txt(hist)
    body = m.select_one(".law--content .vp-page-content") or m.select_one(".law--content")
    if body is not None:
        b = BeautifulSoup(str(body), "lxml") if body else None
        for junk in b.find_all(["nav", "script", "style"]):
            junk.decompose()
        for el in b.select(".fr-summary"):
            el.decompose()
        t = block_text(b)
        p["body"] = t.split("Cette page propose un résumé explicatif")[0].strip()
    for li in m.select("li"):
        h3 = li.find("h3")
        a = li.find("a", href=True)
        if h3 and a and h3.get_text().strip().endswith(":"):
            lab = txt(h3).rstrip(" :")
            p["sources"].append({"type": lab, "label": txt(a), "url": absu(a["href"])})
    for a in m.find_all("a", href=True):
        mm = re.search(r"/dossierlegislatif/(JORFDOLE\d+)", a["href"])
        if mm:
            p["dossier"] = mm.group(1)
    tags = m.select(".tagsBox a") or []
    p["keywords"] = [txt(a) for a in tags]
    return p


def pano_hash(p):
    return sha(p.get("status"), p.get("historique"), p.get("body", "")[:20000],
               "|".join(f"{t['name']}{t['date']}{t['state']}" for t in p.get("timeline", [])))


# =====================================================================
# Pages de listes (cartes) : panoramas, consultations, actualités, rapports
# =====================================================================
def parse_cards(html):
    s = soup_of(html)
    m = main_zone(s)
    out, seen = [], set()
    for t in m.select(".fr-card__title a[href], h3.fr-card__title a[href]"):
        href = absu(t["href"])
        if href in seen:
            continue
        seen.add(href)
        card = t.find_parent(class_=re.compile(r"fr-card(?!_)|views-row")) or t.find_parent("div")
        tm = card.find("time") if card else None
        desc = card.select_one(".fr-card__desc") if card else None
        details = [txt(x) for x in card.select(".fr-card__detail")] if card else []
        out.append({"url": href, "title": txt(t), "date": date_fr(tm.get("datetime")) if tm and tm.get("datetime")
                    else date_fr(" ".join(details)), "desc": txt(desc), "details": details})
    return out


def parse_rss(xml):
    """Flux RSS de vie-publique, avec les champs propres au site (type, sujets, autorité, dates de consultation)."""
    try:
        s = BeautifulSoup(xml or "", "xml")
    except Exception:  # noqa: BLE001
        s = BeautifulSoup(xml or "", "html.parser")
    out = []
    for it in s.find_all("item"):
        def g(name):
            el = it.find(name)
            return el.get_text(" ", strip=True) if el else ""
        link = clean_vp_url(g("link"))
        if not link:
            continue
        dc = g("dc:date") or g("date") or g("pubDate")
        dcons = g("date-consultation")
        dd = re.findall(r"\d{4}-\d{2}-\d{2}", dcons)
        out.append({"url": link, "title": BeautifulSoup(g("title"), "html.parser").get_text(" ", strip=True),
                    "date": date_fr(dc) or date_fr(g("pubDate")), "desc": BeautifulSoup(
                        g("dc:description") or g("description"), "html.parser").get_text(" ", strip=True),
                    "type": g("dc:type") or g("category"), "subjects": g("dc:subject"),
                    "autorite": g("autorite-pilote"), "cons_start": dd[0] if dd else None,
                    "cons_end": dd[1] if len(dd) > 1 else None})
    return out


# =====================================================================
# Pages de contenu (consultation, en bref, rapport…)
# =====================================================================
def parse_consultation(html, url=""):
    s = soup_of(html)
    m = main_zone(s)
    c = {"url": url, "title": txt(m.find("h1")), "start": None, "end": None, "online": None, "type": "",
         "fondement": "", "autorite": "", "statut": "", "acces": "", "body": ""}
    for cls, key in (("field--name-field-start-date", "start"), ("field--name-field-end-date", "end"),
                     ("field--name-field-consultation-online-date", "online")):
        el = m.select_one("." + cls + " time")
        if el is not None:
            c[key] = date_fr(el.get("datetime") or txt(el))
    tf = m.select_one(".type-fondement-consultation")
    if tf is not None:
        t = txt(tf)
        mm = re.search(r"Type\s*:\s*(.*?)(?:\||$)", t)
        c["type"] = mm.group(1).strip() if mm else ""
        mm = re.search(r"Fondement\(s\) juridique\(s\)\s*:\s*(.*)$", t)
        c["fondement"] = mm.group(1).strip() if mm else ""
    au = m.select_one(".autoriteBox a") or m.select_one(".autoriteBox")
    c["autorite"] = txt(au).replace("Autorité administrative pilote :", "").strip()
    stt = m.select_one(".vp-consultation-status")
    c["statut"] = txt(stt)
    acc = m.select_one(".consultation--infos a[href]")
    c["acces"] = absu(acc["href"]) if acc else ""
    body = m.select_one(".field--name-body")
    c["body"] = block_text(body) if body is not None else ""
    return c


def parse_article(html, url=""):
    """Texte principal d'une page « en bref », « questions-réponses », « rapport », etc."""
    s = soup_of(html)
    m = main_zone(s)
    title = txt(m.find("h1"))
    ch = txt(m.select_one(".field--name-field-chapo"))
    for junk in m.find_all(["script", "style", "nav", "button", "form"]):
        junk.decompose()
    for el in m.select(".fr-share, .tagsBox, .vp-related, .fr-summary"):
        el.decompose()
    body = m.select_one(".vp-page-content") or m.select_one(".field--name-body") or m
    t = block_text(body)
    full = m.get_text(" ", strip=True)
    mm = re.search(r"Publi[ée] le\s+(\d{1,2}(?:er)?\s+\w+\s+\d{4})", full)
    tags = [txt(a) for a in m.select(".vp-tags-list a")]
    pdfs = [absu(a["href"]) for a in m.find_all("a", href=True) if re.search(r"\.pdf(\?|$)", a["href"], re.I)]
    return {"url": url, "title": title, "chapo": ch, "text": t.replace("Haut de page", "").strip(),
            "date": date_fr(mm.group(1)) if mm else None, "tags": tags, "pdfs": pdfs}


def parse_autour_de_la_loi(html):
    """Tous les liens de contenu de la page « Autour de la loi », par encart."""
    s = soup_of(html)
    m = main_zone(s)
    out, cur = [], ""
    for el in m.find_all(["h2", "a"]):
        if el.name == "h2":
            cur = txt(el)
            continue
        href = re.sub(r"^https?://(?:www\.)?vie-publique\.fr", "", el.get("href") or "")
        typed = re.search(r"^/(loi|dossierlegislatif|consultations|en-bref|fiches|podcast|infographie|eclairage|"
                          r"questions-reponses|rapport|parole-d-expert|dossier|video|discours)/", href)
        # encart « Comprendre » : toutes ses pages internes (articles de fond comme /procedure-legislative)
        loose = "comprendre" in norm(cur) and re.match(r"^/[a-z0-9-]{6,}(/|$)", href) and "#" not in href
        if not (typed or loose) or not txt(el) or txt(el).lower() in ("haut de page",):
            continue
        out.append({"encart": cur, "url": absu(href), "title": txt(el)})
    return out


# =====================================================================
# Stade de la procédure
# =====================================================================
STAGES = [
    "1 · Déposé",
    "2 · Adopté par une assemblée",
    "3 · Adopté par les deux assemblées",
    "4 · 2e lecture",
    "5 · Commission mixte paritaire",
    "6 · Nouvelle lecture",
    "7 · Adopté définitivement",
    "8 · Conseil constitutionnel",
    "9 · Promulgué",
]
STAGE_ORDONNANCE = "Ordonnance publiée"
STAGE_REJET = "✖ Rejeté ou retiré"
ALL_STAGES = STAGES + [STAGE_ORDONNANCE, STAGE_REJET]


def nature_of(title, cat=""):
    """(nature, vecteur) d'après l'intitulé."""
    n = norm(title)
    c = norm(cat)
    if n.startswith("ordonnance") or c.startswith("ordonnance"):
        nature = "Ordonnance"
    elif n.startswith("loi"):
        nature = "Loi"
    elif n.startswith("projet de loi"):
        nature = "Projet de loi"
    elif n.startswith("proposition de loi"):
        nature = "Proposition de loi"
    elif "projet" in c:
        nature = "Projet de loi"
    elif "proposition" in c:
        nature = "Proposition de loi"
    else:
        nature = "Loi" if "loi" in c else "Autre"
    if "constitutionnel" in n:
        vect = "Révision constitutionnelle"
    elif "organique" in n:
        vect = "Loi organique"
    elif "de finances rectificative" in n or "de finances de fin de gestion" in n:
        vect = "Loi de finances rectificative"
    elif "de finances pour" in n or "loi de finances" in n and "speciale" not in n:
        vect = "Loi de finances"
    elif "loi speciale" in n:
        vect = "Loi spéciale (art. 45 LOLF)"
    elif "financement de la securite sociale" in n:
        vect = "Loi de financement de la sécurité sociale"
    elif "approbation des comptes" in n or "reglement du budget" in n or "resultats de la gestion" in n:
        vect = "Loi d'approbation des comptes"
    elif "ratifiant l" in n or "ratification de l'ordonnance" in n or "portant ratification" in n and "ordonnance" in n:
        vect = "Ratification d'ordonnance"
    elif "habilitation" in n or "habilitant" in n:
        vect = "Habilitation à légiférer par ordonnance (art. 38)"
    elif "autorisant la ratification" in n or "autorisant l'approbation" in n:
        vect = "Autorisation de ratification d'un traité (art. 53)"
    elif "programmation" in n:
        vect = "Loi de programmation"
    elif "transposition" in n or "adaptation au droit de l'union" in n or "diverses dispositions d'adaptation" in n:
        vect = "Transposition / adaptation au droit de l'UE"
    elif nature == "Ordonnance":
        vect = "Ordonnance (art. 38)"
    else:
        vect = "Loi ordinaire"
    return nature, vect


def compute_stage(dos, pano=None, published=False):
    """Stade actuel + date + dernière étape, à partir du dossier (et du panorama s'il existe)."""
    steps = dos.get("steps", [])
    jorf = dos.get("jorf", [])
    kinds = [s["kind"] for s in steps] + [j["kind"] for j in jorf]
    title = dos.get("title", "")
    nature, _ = nature_of(title)
    pst = norm((pano or {}).get("status", ""))
    dates = [s.get("date") for s in steps if s.get("date")] + [j.get("date") for j in jorf if j.get("date")]
    last_date = max(dates) if dates else None
    groups = [norm(g["groupe"]) for g in dos.get("documents", [])] + [norm(g["groupe"]) for g in dos.get("debats", [])]

    def res(stage, label):
        return {"stage": stage, "date": last_date, "label": label}

    if nature == "Ordonnance":
        return res(STAGE_ORDONNANCE, "Ordonnance publiée au Journal officiel")
    promul = [j for j in jorf if j["kind"] == "promulgation"]
    if promul or (published and nature == "Loi") or re.search(r"\ba ete promulguee?\b", pst):
        return res(STAGES[8], promul[0]["label"] if promul else "Loi promulguée")
    if re.search(r"\ba ete (?:definitivement )?(?:rejetee?|retiree?)\b", pst):
        return res(STAGE_REJET, (pano or {}).get("status") or "Texte rejeté ou retiré")
    step_kinds = [s["kind"] for s in steps if s["kind"] not in ("autre", "etude_impact", "avis_ce")]
    if step_kinds and step_kinds[-1] in ("rejet", "retrait") and (nature == "Proposition de loi"
                                                                    or step_kinds[-1] == "retrait"):
        return res(STAGE_REJET, steps[[s["kind"] for s in steps].index(step_kinds[-1])]["label"][:200])
    if "decision_cc" in kinds or "saisine_cc" in kinds or "conseil constitutionnel a ete saisi" in pst:
        return res(STAGES[7], "Saisine ou décision du Conseil constitutionnel")
    adopt_an = [s for s in steps if s["kind"] in ("adoption_1", "adoption_conforme", "lecture_2") and s["chamber"] == "AN"]
    adopt_se = [s for s in steps if s["kind"] in ("adoption_1", "adoption_conforme", "lecture_2") and s["chamber"] == "Sénat"]
    cmp_both = [s for s in steps if s["kind"] == "adoption_cmp"]
    if "lecture_definitive" in kinds or "adoption_conforme" in kinds or len({s["chamber"] for s in cmp_both}) >= 2 \
            or "adopte definitivement" in pst or "adoption definitive" in pst:
        return res(STAGES[6], "Adoption définitive par le Parlement")
    if "nouvelle_lecture" in kinds or "echec" in " ".join(groups) or "desaccord" in " ".join(groups):
        return res(STAGES[5], "Nouvelle lecture (après échec ou absence de CMP)")
    if cmp_both or any("commission mixte" in g for g in groups) or "commission mixte paritaire" in pst:
        return res(STAGES[4], "Commission mixte paritaire")
    if "lecture_2" in kinds:
        return res(STAGES[3], "Deuxième lecture")
    if adopt_an and adopt_se:
        return res(STAGES[2], "Adopté en 1re lecture par les deux assemblées")
    if adopt_an or adopt_se:
        ch = "l'Assemblée nationale" if adopt_an else "le Sénat"
        return res(STAGES[1], f"Adopté en 1re lecture par {ch}")
    rej = [s for s in steps if s["kind"] == "rejet"]
    if rej:  # projet de loi rejeté par une assemblée : la navette se poursuit
        return res(STAGES[1], "Rejeté en 1re lecture par " + ("l'Assemblée nationale" if rej[-1]["chamber"] == "AN"
                                                             else "le Sénat") + " (la navette se poursuit)")
    return res(STAGES[0], "Dépôt au Parlement")


def progress_of(stage):
    if stage in (STAGE_ORDONNANCE, STAGES[8]):
        return 1.0
    if stage == STAGE_REJET:
        return 0.0
    try:
        return (STAGES.index(stage) + 1) / len(STAGES)
    except ValueError:
        return 0.0


def progress_bar(stage):
    if stage == STAGE_REJET:
        return "✖ procédure interrompue"
    n = round(progress_of(stage) * len(STAGES))
    return "▰" * n + "▱" * (len(STAGES) - n) + f" {n}/{len(STAGES)}"


def deposit_info(dos, pano=None):
    """Assemblée de dépôt et procédure accélérée."""
    st = norm((pano or {}).get("status", "") + " " + (pano or {}).get("historique", ""))
    chamber = ""
    m = re.search(r"depose[e]? (?:au|a l'|sur le bureau de l'|sur le bureau du) ?(senat|assemblee nationale)", st)
    if m:
        chamber = "Sénat" if m.group(1) == "senat" else "Assemblée nationale"
    else:
        for s in dos.get("steps", []):
            if s["kind"] in ("depot", "adoption_1", "rejet") and s["chamber"]:
                chamber = "Sénat" if s["chamber"] == "Sénat" else "Assemblée nationale"
                break
        if not chamber and len(dos.get("parl") or {}) == 1:
            chamber = list(dos["parl"])[0]
    accel = bool(dos.get("accel")) or "procedure acceleree" in st
    return chamber, accel


def cc_outcome(dos):
    for j in dos.get("jorf", []):
        if j["kind"] == "decision_cc":
            n = norm(j["label"])
            if "non conforme" in n and "partiellement" not in n:
                return "Non conforme"
            if "partiellement" in n:
                return "Partiellement conforme"
            if "conforme" in n:
                return "Conforme"
            return "Décision rendue"
    return ""
