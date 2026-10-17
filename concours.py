"""
Fiches concours alimentées par la veille
----------------------------------------
- Un dossier Notion « FICHES CONCOURS » et une sous-page par sujet de concours (liste dans concours.yaml),
  créées toutes d'un coup, dans l'ordre de la liste.
- Chaque article de la veille est rattaché par l'IA à 0 à 3 sujets, d'après le périmètre précis de chaque
  sujet (classement par lots, qui reprend aussi les articles déjà présents dans Notion).
- La fiche d'un sujet suit le format des fiches de révision (problématique, à retenir, arguments étayés
  par les articles, chiffres, débats, chronologie, liste des articles), enrichi pour l'épreuve :
  sujets possibles, notions, proposition de plan, repères de cours, synthèse.
- Mise à jour incrémentale : la fiche existante est enrichie avec les articles pas encore intégrés.
- Une réserve du budget IA quotidien est laissée aux articles : les fiches concours s'arrêtent avant.
"""
import json
import logging
import os

import yaml

from fiches import Fiches, STAR, plain

log = logging.getLogger("veille")

FOLDER = "FICHES CONCOURS"
FOLDER_ICON = "🎓"
PAGE_ICON = "🎯"
PROP = "Fiches concours"  # colonne du tableau de veille
PROP_COLOR = "orange"


def load_subjects(root):
    """Lit concours.yaml : [{code, titre, perimetre}] (codes uniques, ordre conservé)."""
    with open(os.path.join(root, "concours.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out, seen = [], set()
    for s in data.get("sujets") or []:
        if not isinstance(s, dict):
            continue
        code = str(s.get("code") or "").strip().upper()
        titre = " ".join(str(s.get("titre") or "").split())
        if not code or not titre or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "titre": titre[:180], "perimetre": " ".join(str(s.get("perimetre") or "").split())})
    return out


def _list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _items(x):
    """Liste d'éléments {texte, refs} (l'IA renvoie parfois de simples chaînes)."""
    out = []
    for e in _list(x):
        if isinstance(e, dict) and (e.get("texte") or e.get("text")):
            out.append({**e, "texte": e.get("texte") or e.get("text"), "refs": _list(e.get("refs"))})
        elif isinstance(e, str) and e.strip():
            out.append({"texte": e.strip(), "refs": []})
    return out


def normalize(d):
    """Remet en forme la réponse de l'IA (types inattendus, champs manquants)."""
    d = dict(d or {})
    out = {}
    for k in ("problematique", "synthese"):
        v = d.get(k)
        out[k] = " ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")
    out["sujets"] = [str(x) for x in _list(d.get("sujets")) if str(x).strip()]
    out["a_retenir"] = [str(x) for x in _list(d.get("a_retenir")) if str(x).strip()]
    out["notions"] = []
    for n in _list(d.get("notions")):
        if isinstance(n, dict) and n.get("terme"):
            out["notions"].append({"terme": str(n["terme"]), "definition": str(n.get("definition") or "")})
        elif isinstance(n, str) and n.strip():
            t, _, df = n.partition(":")
            out["notions"].append({"terme": t.strip(), "definition": df.strip()})
    out["plan"] = []
    for p in _list(d.get("plan")):
        if isinstance(p, dict) and p.get("partie"):
            out["plan"].append({"partie": str(p["partie"]),
                                "sous_parties": [str(x) for x in _list(p.get("sous_parties")) if str(x).strip()]})
        elif isinstance(p, str) and p.strip():
            out["plan"].append({"partie": p.strip(), "sous_parties": []})
    out["arguments"] = []
    for a in _list(d.get("arguments")):
        if isinstance(a, dict) and (a.get("titre") or a.get("idee")):
            out["arguments"].append({"titre": str(a.get("titre") or ""), "idee": str(a.get("idee") or ""),
                                     "exemples": _items(a.get("exemples"))})
    out["chiffres"] = _items(d.get("chiffres"))
    out["debats"] = _items(d.get("debats"))
    out["chronologie"] = [{**c, "date": str(c.get("date") or "")} for c in _items(d.get("chronologie"))]
    out["reperes"] = [{"date": str(c.get("date") or ""), "texte": c["texte"]} for c in _items(d.get("reperes"))]
    return out


class Concours(Fiches):
    def __init__(self, V, st, cfg, notion, llm, subjects):
        super().__init__(V, st, cfg, notion, llm)
        self.fc = cfg.get("fiches_concours") or {}
        self.state = st.setdefault("concours", {})
        self.subjects = subjects
        self.by_code = {s["code"]: s for s in subjects}

    # ---------- budget : une part du quota du jour reste réservée aux articles ----------
    def budget_ok(self):
        day = getattr(self.llm, "budget_day", 0)
        if not day:
            return True
        reserve = float(self.fc.get("reserve_budget_articles", 0.3))
        return self.llm.budget.get("utilise", 0) < day * (1 - reserve)

    # ---------- arborescence Notion ----------
    def folder(self):
        return self.top_folder(FOLDER, FOLDER_ICON)

    def _children(self, parent):
        found, cursor = {}, None
        while True:
            res = self.notion.req("GET", f"/blocks/{parent}/children?page_size=100"
                                  + (f"&start_cursor={cursor}" if cursor else ""))
            for b in res.get("results", []):
                if b.get("type") == "child_page":
                    found.setdefault(plain(b["child_page"].get("title", "")), b["id"])
            if not res.get("has_more"):
                return found
            cursor = res.get("next_cursor")

    def _create(self, parent, title, icon):
        body = {"parent": {"type": "page_id", "page_id": parent}, "icon": {"type": "emoji", "emoji": icon},
                "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}}}
        return self.notion.req("POST", "/pages", body)["id"]

    def placeholder(self, s):
        n = self.fc.get("min_articles", 2)
        return [
            {"object": "block", "type": "callout", "callout": {
                "rich_text": [{"type": "text", "text": {"content":
                    f"Fiche en attente : elle sera rédigée automatiquement dès que {n} articles de la veille "
                    "porteront sur ce sujet, puis enrichie au fil des nouveaux articles."}}],
                "icon": {"type": "emoji", "emoji": "⏳"}, "color": "gray_background"}},
            self.B("paragraph", "Périmètre suivi : " + s["perimetre"], color="gray"),
        ]

    def ensure_pages(self, time_left):
        """Crée le dossier et toutes les sous-pages manquantes (dans l'ordre de concours.yaml) ;
        renomme les pages dont le titre a changé dans concours.yaml."""
        missing = [s for s in self.subjects if not self.state.get(s["code"], {}).get("page")]
        if missing:
            folder = self.folder()
            existing = self._children(folder)
            made = 0
            for s in missing:
                if time_left() < 300:
                    break
                st_ = self.state.setdefault(s["code"], {})
                pid = existing.get(s["titre"])
                st_.update(name=s["titre"], parent=folder)
                if pid:  # page déjà présente (mémoire perdue) : on la reprend telle quelle
                    st_["page"] = pid
                    continue
                st_["page"] = self._create(folder, s["titre"], PAGE_ICON)
                try:
                    st_["container"] = self.write(st_["page"], s["code"], self.placeholder(s))
                except RuntimeError as e:
                    log.debug("Page d'attente non écrite (%s) : %s", s["titre"], e)
                made += 1
            if made:
                log.info("Fiches concours : %d sous-page(s) créée(s) dans « %s »", made, FOLDER)
        for s in self.subjects:
            st_ = self.state.get(s["code"])
            if st_ and st_.get("page") and st_.get("name") != s["titre"]:
                try:
                    self.set_title(st_["page"], (STAR if st_.get("star") else "") + s["titre"])
                    st_["name"] = s["titre"]
                except RuntimeError as e:
                    log.debug("Page non renommée (%s) : %s", s["titre"], e)

    def ensure_column(self):
        """Colonne « Fiches concours » du tableau de veille (filtre par sujet)."""
        props = getattr(self.notion, "props", None)
        if props is None:
            return
        opt = self.V.opt
        if PROP not in props:
            self.notion.req("PATCH", f"/databases/{self.notion.db}", {"properties": {PROP: {"multi_select": {
                "options": [{"name": opt(s["titre"]), "color": PROP_COLOR} for s in self.subjects][:100]}}}})
            self.notion.load_schema()
        else:
            self.notion.ensure_options(PROP, [(opt(s["titre"]), PROP_COLOR) for s in self.subjects])

    # ---------- classement des articles ----------
    def classify_prompt(self, batch):
        lines = [
            "Tu classes les articles d'une revue de presse internationale (think tanks, médias spécialisés) dans les "
            "FICHES CONCOURS d'une préparation aux concours (INSP, Sciences Po, relations internationales).",
            "Pour chaque article, choisis de 0 à 3 sujets dans la liste ci-dessous, d'après leur périmètre.",
            "Règles :",
            "- Rattache un article à un sujet seulement s'il apporte une matière directement utilisable dans une "
            "dissertation sur ce sujet (fait, argument, exemple, chiffre, analyse). Une simple mention ne suffit pas.",
            "- Préfère le sujet le plus spécifique ; n'ajoute un sujet plus large que si l'article l'éclaire vraiment.",
            "- Respecte les exclusions indiquées par « Pas : … » dans les périmètres.",
            "- Beaucoup d'articles relèvent d'un seul sujet ; certains d'aucun : renvoie alors une liste vide.",
            "- Utilise uniquement les codes de la liste (C01, C02…).",
            'Réponds UNIQUEMENT avec un objet JSON : {"classement": [{"id": "a12", "sujets": ["C04", "C09"]}]}, '
            "avec une entrée par article.",
            "",
            "=== SUJETS ===",
        ]
        lines += [f"{s['code']} — {s['titre']} : {s['perimetre']}" for s in self.subjects]
        lines += ["", f"=== ARTICLES ({len(batch)}) ==="]
        for a in batch:
            meta = []
            if a.get("th"):
                meta.append("Thèmes : " + ", ".join(a["th"]))
            if a.get("py"):
                meta.append("Pays : " + ", ".join(n for n, _ in a["py"]))
            lines += [f"[{a['i']}] {a.get('t', '')} — {a.get('s', '')} — {a.get('d') or 'date inconnue'}",
                      " ; ".join(meta), "Résumé : " + (a.get("r") or "")[:350], ""]
        return "\n".join(lines)

    @staticmethod
    def parse_classes(res):
        """{"classement": [{"id", "sujets"}]} ou {"a12": [...]} → {id: [codes]}."""
        rows = res.get("classement") if isinstance(res, dict) else res
        out = {}
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("id") is not None:
                    out[str(r["id"]).strip("[] ")] = _list(r.get("sujets"))
        elif isinstance(res, dict):
            out = {str(k): _list(v) for k, v in res.items() if str(k).startswith("a")}
        return out

    def pending(self):
        return [a for a in self.st.get("corpus", []) if "cc" not in a and a.get("p") and not a["p"].startswith("dry-")]

    def classify(self, time_left):
        todo = sorted(self.pending(), key=lambda a: (a.get("d") or "", int(a["i"][1:])), reverse=True)
        size = int(self.fc.get("classement_par_requete", 60))
        max_req = int(self.fc.get("classement_requetes_par_passage", 6))
        names = {s["code"]: self.V.opt(s["titre"]) for s in self.subjects}
        can_tag = PROP in (getattr(self.notion, "props", None) or {})
        done = 0
        for k in range(0, min(len(todo), size * max_req), size):
            if time_left() < 300 or not self.budget_ok():
                break
            batch = todo[k:k + size]
            try:
                res = self.parse_classes(self.llm.ask_json(self.classify_prompt(batch), 4096))
            except self.V.QuotaExhausted as e:
                log.warning("Fiches concours : quota IA épuisé pendant le classement (%s)", e)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("Fiches concours : classement impossible (%s)", e)
                for a in batch:
                    a["cct"] = a.get("cct", 0) + 1
                    if a["cct"] >= 3:
                        a["cc"] = []
                continue
            for a in batch:
                got = res.get(a["i"])
                if got is None:
                    a["cct"] = a.get("cct", 0) + 1
                    if a["cct"] >= 2:  # article oublié deux fois par l'IA : rangé sans sujet
                        a["cc"] = []
                    continue
                codes = []
                for c in got:
                    c = str(c).strip().upper()
                    if c in self.by_code and c not in codes:
                        codes.append(c)
                a["cc"] = codes[:3]
                a.pop("cct", None)
                done += 1
                if can_tag and a["cc"]:
                    try:
                        self.notion.req("PATCH", f"/pages/{a['p']}", {"properties": {PROP: {"multi_select": [
                            {"name": names[c]} for c in a["cc"]]}}})
                    except RuntimeError as e:
                        log.debug("Colonne « %s » non remplie pour %s : %s", PROP, a["p"], e)
            self.V.save_state(self.st)
        if done:
            log.info("Fiches concours : %d article(s) classé(s), %d restant(s)", done, len(self.pending()))
        return done

    # ---------- sélection des fiches à (re)construire ----------
    def due(self):
        corpus = self.st.get("corpus", [])
        min_n = int(self.fc.get("min_articles", 2))
        days = float(self.fc.get("maj_apres_jours", 4))
        quick = int(self.fc.get("nouveaux_articles_pour_maj_immediate", 8))
        jobs = []
        for s in self.subjects:
            arts = [a for a in corpus if s["code"] in (a.get("cc") or ())]
            st_ = self.state.get(s["code"], {})
            done = set(st_.get("done") or [])
            new = [a for a in arts if a["i"] not in done]
            if not st_.get("data"):
                if len(arts) < min_n:
                    continue
            else:
                if not new:
                    continue
                age = (self.now - self.V.parse_date(st_["upd"])).total_seconds() / 86400 if st_.get("upd") else 99
                if age < days and len(new) < quick:
                    continue
            jobs.append({"kind": "concours", "code": s["code"], "name": s["titre"], "key": s["code"],
                         "subject": s, "arts": arts, "meta": {}, "new": len(new), "exists": bool(st_.get("data"))})
        jobs.sort(key=lambda j: (j["exists"], -j["new"]))
        return jobs

    # ---------- prompt ----------
    def prompt(self, job, current, arts):
        s = job["subject"]
        schema = (
            '{"problematique": "5 à 7 phrases : définition et délimitation des termes du sujet, enjeux, tensions ou '
            'paradoxes, puis la question centrale formulée comme une problématique de dissertation", '
            '"sujets": ["3 à 5 intitulés de sujets de dissertation ou d\'oral possibles"], '
            '"notions": [{"terme": "notion ou concept clé", "definition": "définition précise en une ou deux phrases"}], '
            '"a_retenir": ["5 à 8 idées essentielles à mémoriser, une phrase chacune"], '
            '"plan": [{"partie": "I. intitulé argumentatif", "sous_parties": ["A. intitulé", "B. intitulé"]}], '
            '"arguments": [{"titre": "intitulé court de la grande idée", '
            '"idee": "2 à 4 phrases qui développent l\'argument, avec ses limites", '
            '"exemples": [{"texte": "exemple concret ou donnée chiffrée précise (unité, date, acteur)", "refs": ["a12"]}]}], '
            '"chiffres": [{"texte": "donnée chiffrée clé et son contexte", "refs": ["a3"]}], '
            '"debats": [{"texte": "controverse ou position, en nommant qui défend quoi (chercheurs, think tanks, États)", '
            '"refs": ["a7"]}], '
            '"reperes": [{"date": "AAAA ou AAAA-MM-JJ", "texte": "repère historique, juridique ou théorique de cours"}], '
            '"chronologie": [{"date": "AAAA-MM-JJ", "texte": "événement récent", "refs": ["a9"]}], '
            '"synthese": "4 à 6 phrases de synthèse nuancée qui répondent à la problématique, avec une ouverture"}')
        lines = [
            "Tu es professeur de préparation aux concours (INSP, Sciences Po, concours d'Orient, questions "
            f"internationales). Tu tiens à jour la FICHE CONCOURS « {s['titre']} ».",
            f"Périmètre du sujet : {s['perimetre']}",
            "La fiche est alimentée par une revue de presse de think tanks et de médias spécialisés. Elle doit "
            "permettre de traiter une dissertation ou un oral sur ce sujet : analyse, problématisation, "
            "arguments étayés, synthèse.",
            "Règles impératives :",
            "- Faits d'actualité, exemples, chiffres, débats et chronologie : UNIQUEMENT tirés de la fiche existante et "
            "des articles fournis ; n'invente aucun fait ni chiffre. Chacun cite ses sources dans \"refs\" "
            "(ex. [\"a12\", \"a40\"]).",
            "- \"notions\", \"reperes\", \"plan\" et \"sujets\" peuvent mobiliser des connaissances de cours sûres et "
            "incontestées (définitions établies, traités, dates historiques, auteurs classiques), sans \"refs\" ; "
            "jamais de fait d'actualité ni de chiffre récent absent des articles dans ces rubriques.",
            "- N'écris JAMAIS les identifiants (a12, a40…) dans les textes eux-mêmes : uniquement dans les champs \"refs\".",
            "- Problématise : fais apparaître les tensions, les limites et les contre-arguments ; ne te contente pas "
            "de résumer l'actualité.",
            "- Le plan répond à la problématique (2 ou 3 parties, 2 ou 3 sous-parties chacune) et s'appuie sur les "
            "arguments de la fiche.",
            "- Reste strictement dans le périmètre du sujet : laisse de côté ce qui, dans un article, relève d'un "
            "autre sujet.",
            "- Mise à jour : conserve ce qui reste pertinent dans la fiche existante, intègre les nouveaux articles, "
            "fusionne les doublons, remplace ce qui est dépassé, retire le secondaire pour respecter les limites.",
            "- Rédige en français, dans un style clair et dense de fiche de concours.",
            "Limites : 8 notions au plus, 4 à 8 grandes idées avec 2 à 5 exemples chacune, 12 chiffres au plus, "
            "6 débats au plus, 10 repères au plus, 15 événements de chronologie au plus (les plus importants et récents).",
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
    def blocks_concours(self, job, d, index, n_total):
        B, S = self.B, self.S
        out = [B("paragraph", f"Fiche mise à jour automatiquement le {self.now:%d/%m/%Y} à partir de {n_total} "
                              "articles de la veille. Les liens gris renvoient aux fiches-articles.", color="gray")]
        if d.get("problematique"):
            out += [B("heading_2", "Problématique"), B("paragraph", d["problematique"])]
        if d.get("sujets"):
            out.append(B("heading_2", "Sujets possibles"))
            out += [B("bulleted_list_item", x) for x in d["sujets"][:5]]
        if d.get("notions"):
            out.append(B("heading_2", "Notions clés"))
            out += [B("bulleted_list_item", n["terme"] + (" : " if n["definition"] else ""), bold=True,
                      extra=[{"type": "text", "text": {"content": n["definition"][:1900]}}] if n["definition"] else None)
                    for n in d["notions"][:8]]
        if d.get("a_retenir"):
            out.append(B("heading_2", "À retenir"))
            out += [B("bulleted_list_item", x) for x in d["a_retenir"][:8]]
        if d.get("plan"):
            out.append(B("heading_2", "Proposition de plan"))
            for p in d["plan"][:3]:
                out.append(B("paragraph", p["partie"], bold=True))
                out += [B("bulleted_list_item", x) for x in p["sous_parties"][:3]]
        if d.get("arguments"):
            out.append(B("heading_2", "Grandes idées et arguments"))
            for i, ar in enumerate(d["arguments"][:8], 1):
                out.append(B("heading_3", f"{i}. {ar['titre']}"))
                if ar["idee"]:
                    out.append(B("paragraph", ar["idee"]))
                for ex in ar["exemples"][:5]:
                    out.append(B("bulleted_list_item", S(ex), self.refs_rt(ex.get("refs"), index)))
        if d.get("chiffres"):
            out.append(B("heading_2", "Chiffres clés"))
            out += [B("bulleted_list_item", S(c), self.refs_rt(c.get("refs"), index)) for c in d["chiffres"][:12]]
        if d.get("debats"):
            out.append(B("heading_2", "Débats et points de vue"))
            out += [B("bulleted_list_item", S(c), self.refs_rt(c.get("refs"), index)) for c in d["debats"][:6]]
        if d.get("reperes"):
            out.append(B("heading_2", "Repères de cours"))
            out.append(B("paragraph", "Connaissances générales rappelées par l'IA (non tirées de la veille) : "
                                      "à vérifier dans vos manuels.", color="gray"))
            for r in sorted(d["reperes"], key=lambda x: x["date"])[:10]:
                out.append(B("bulleted_list_item", (r["date"] + " — " if r["date"] else "") + r["texte"]))
        if d.get("chronologie"):
            out.append(B("heading_2", "Chronologie récente"))
            for c in sorted(d["chronologie"], key=lambda x: x["date"], reverse=True)[:15]:
                dd = c["date"]
                lab = f"{dd[8:10]}/{dd[5:7]}/{dd[:4]} — " if len(dd) == 10 else (dd + " — " if dd else "")
                out.append(B("bulleted_list_item", lab + S(c), self.refs_rt(c.get("refs"), index)))
        if d.get("synthese"):
            out += [B("heading_2", "Synthèse"), B("paragraph", d["synthese"])]
        return out

    # ---------- exécution ----------
    def build(self, job):
        maxa = int(self.fc.get("articles_par_fiche_max", 25))
        code = job["code"]
        s = self.state.setdefault(code, {})
        index = {a["i"]: a for a in self.st.get("corpus", [])}
        done = {i for i in (s.get("done") or []) if i in index}
        arts = sorted(job["arts"], key=lambda a: (a["d"], int(a["i"][1:])), reverse=True)
        use = [a for a in arts if a["i"] not in done][:maxa]
        created = not s.get("data")
        self.new_ids = set() if created else {a["i"] for a in use}
        data = self.llm.ask_json(self.prompt(job, s.get("data"), use))
        if not isinstance(data, dict):
            raise ValueError("fiche illisible")
        data = normalize(data)
        if not (data["problematique"] or data["arguments"]):
            raise ValueError("fiche vide")
        self._index = index
        page = s.get("page")
        if not page:
            page = self._create(self.folder(), job["name"], PAGE_ICON)
            s.pop("container", None)
        blocks = [self.banner(index, self.new_ids, created, len(use) if created else len(job["arts"]))]
        blocks += self.blocks_concours(job, data, index, len(job["arts"])) + self.all_articles(job, index)
        container = self.write(page, code, blocks)
        self.set_title(page, STAR + job["name"])
        s.update(page=page, container=container, data=data, upd=self.V.iso(self.now), star=self.V.iso(self.now),
                 name=job["name"], done=sorted(done | {a["i"] for a in use}, key=lambda i: int(i[1:])))

    def refresh_stars(self):
        """Retire l'étoile des fiches mises à jour il y a plus de N jours ; étoile le dossier s'il en contient."""
        days = self.fc.get("etoile_jours", 3)
        for code, s in self.state.items():
            if s.get("star") and (self.V.age_days(s["star"]) or 0) > days:
                try:
                    self.set_title(s["page"], s.get("name") or code)
                    s.pop("star", None)
                except RuntimeError as e:
                    log.debug("Étoile non retirée (%s) : %s", code, e)
        want = any(s.get("star") for s in self.state.values())
        if self.st.get("concours_folder_star", False) != want:
            try:
                self.set_title(self.folder(), (STAR if want else "") + FOLDER)
                self.st["concours_folder_star"] = want
            except RuntimeError as e:
                log.debug("Dossier %s : %s", FOLDER, e)


def update(V, st, cfg, notion, llm, time_left):
    fc = cfg.get("fiches_concours") or {}
    if not fc.get("actif", True):
        return 0
    try:
        subjects = load_subjects(V.ROOT)
    except Exception as e:  # noqa: BLE001
        log.warning("Fiches concours : concours.yaml illisible (%s)", e)
        return 0
    if not subjects:
        return 0
    c = Concours(V, st, cfg, notion, llm, subjects)
    try:
        c.ensure_column()
    except RuntimeError as e:
        log.warning("Colonne « %s » non créée : %s", PROP, e)
    c.ensure_pages(time_left)
    V.save_state(st)
    c.classify(time_left)
    left = len(c.pending())
    if not st.get("concours_init"):
        if left:  # premier classement des articles déjà publiés : les fiches attendent qu'il soit terminé
            log.info("Fiches concours : classement initial en cours (%d article(s) restant(s))", left)
            c.refresh_stars()
            return 0
        st["concours_init"] = True
    jobs = c.due()
    if not jobs:
        c.refresh_stars()
        return 0
    log.info("Fiches concours : %d fiche(s) à créer ou mettre à jour", len(jobs))
    done = 0
    for job in jobs[: int(fc.get("fiches_par_passage", 3))]:
        if time_left() < 240:
            break
        if not c.budget_ok():
            log.info("Fiches concours : part du budget IA réservée aux articles atteinte — reprise demain")
            break
        try:
            c.build(job)
            done += 1
            log.info("  fiche concours « %s » à jour (%d nouveaux articles)", job["name"], job["new"])
        except V.QuotaExhausted as e:
            log.warning("Fiches concours : quota IA épuisé (%s) — reprise au prochain passage", e)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Fiche concours « %s » non mise à jour : %s", job["name"], e)
        V.save_state(st)
    c.refresh_stars()
    return done
