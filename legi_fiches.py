"""
Fiches techniques par texte (veille législative)
------------------------------------------------
Chaque projet / proposition de loi, loi ou ordonnance suivi a sa fiche, qui est
la page même de sa ligne dans le tableau de bord Notion.

- Parties calculées sans IA (toujours exactes) : où en est le texte, procédure
  détaillée, application (échéancier des décrets), liens officiels, journal.
- Parties rédigées par l'IA, à partir de vie-publique.fr et des documents
  officiels (exposé des motifs, avis du Conseil d'État, étude d'impact, rapport de
  commission, décision du Conseil constitutionnel, rapports publics) : enjeux,
  mesures, vecteurs juridiques, points de débat, navette, incidences, application.

La fiche est enrichie à chaque évolution du texte (mise à jour incrémentale) et
n'est jamais supprimée : après promulgation, elle suit la mise en application.
"""
import json
import logging

import legi_parse as P

log = logging.getLogger("veille")

STAR = "⭐ "

NEUTRALITE = (
    "Exigences de rédaction (impératives) :\n"
    "- Registre d'une note administrative destinée à un haut fonctionnaire : précis, technique, dense, neutre.\n"
    "- Aucune appréciation personnelle, aucun adjectif évaluatif ou militant, aucun vocabulaire médiatique ou "
    "polémique (« choc », « polémique », « explosif »…). Les surnoms médiatiques des textes ne sont cités "
    "qu'entre guillemets et attribués.\n"
    "- Toute position, critique ou réserve est ATTRIBUÉE à son auteur institutionnel (Gouvernement, rapporteur, "
    "commission, assemblée, Conseil d'État, Conseil constitutionnel, autorité consultée, groupe parlementaire "
    "nommé) et présentée de façon équilibrée : si le texte expose une position, expose aussi les positions "
    "contraires documentées dans les sources.\n"
    "- N'utilise QUE les sources fournies (vie-publique.fr et documents officiels). N'invente aucun fait, "
    "chiffre, article ou date. Si une information manque, écris-le sobrement ou laisse le champ vide.\n"
    "- Cite les articles du texte et les articles de code concernés lorsque les sources les mentionnent.\n"
    "- Chaque élément factuel indique ses sources dans \"refs\" (identifiants s1, s2…).\n"
    "- Rédige en français."
)

SCHEMA_COMPLET = (
    '{"intitule_court": "intitulé court et neutre du texte (80 caractères au plus)", '
    '"en_bref": "4 à 6 phrases : objet du texte, principales mesures, état de la procédure", '
    '"identite": {"initiative": "Gouvernement ou parlementaire", "auteurs": "ministre(s) ou parlementaire(s) et '
    'groupe, tels que mentionnés", "ministere": "ministère porteur", "rapporteurs": "rapporteurs connus", '
    '"fondement": "vecteur et fondement constitutionnel (art. 34, 38, 39, 45, 47, 47-1, 49 al. 3, 53, 89…)", '
    '"textes_modifies": ["codes et lois principalement modifiés"]}, '
    '"contexte": "contexte juridique et factuel, état du droit antérieur (1 à 2 paragraphes)", '
    '"objectifs": ["objectifs affichés par l\'auteur du texte, attribués"], '
    '"mesures": [{"titre": "intitulé de la mesure", "contenu": "2 à 4 phrases précises, avec les articles", '
    '"statut": "proposée | adoptée | modifiée au cours de la navette | supprimée | censurée | en vigueur", '
    '"refs": ["s1"]}], '
    '"vecteurs": [{"texte": "instrument juridique utilisé : création ou modification d\'articles de code, '
    'habilitation à légiférer par ordonnance, renvoi à des décrets en Conseil d\'État ou simples, arrêtés, '
    'expérimentation, entrée en vigueur différée, rapport au Parlement, transposition…", "refs": ["s2"]}], '
    '"debats": [{"sujet": "point de débat", "positions": "positions exprimées, chacune attribuée (Conseil '
    'd\'État, commissions, assemblées, groupes, organismes consultés…)", "refs": ["s4"]}], '
    '"navette": [{"date": "AAAA-MM-JJ", "etape": "étape de la procédure", "apport": "modifications majeures '
    'apportées à ce stade", "refs": ["s3"]}], '
    '"constitutionnalite": "saisine et décision du Conseil constitutionnel : dispositions censurées, réserves '
    'd\'interprétation, motifs (vide si sans objet)", '
    '"incidences": {"institutions_vie_politique": "", "societe": "", "finances_publiques_economie": "", '
    '"collectivites_territoires": ""}, '
    '"application": {"synthese": "état de la mise en application : textes réglementaires prévus et publiés, '
    'entrées en vigueur, rapports (vide si le texte n\'est pas promulgué)", "territorial": "déclinaison '
    'territoriale et rôle des collectivités, des préfets, des services déconcentrés (si documenté)"}, '
    '"a_suivre": ["prochaines étapes et points de vigilance, factuels"]}'
)

SCHEMA_ALLEGE = (
    '{"intitule_court": "intitulé court et neutre (80 caractères au plus)", '
    '"en_bref": "3 à 4 phrases : objet du texte et état de la procédure", '
    '"identite": {"initiative": "", "auteurs": "", "ministere": "", "rapporteurs": "", "fondement": "", '
    '"textes_modifies": []}, '
    '"objectifs": ["objectifs affichés, attribués"], '
    '"mesures": [{"titre": "", "contenu": "1 à 3 phrases avec les articles", "statut": "", "refs": ["s1"]}], '
    '"vecteurs": [{"texte": "", "refs": []}], '
    '"a_suivre": ["prochaines étapes"]}'
)


# =====================================================================
# Matière première
# =====================================================================
def collect_materials(L, t, fetch, ext_get, cfg):
    """Rassemble les sources d'une fiche. Renvoie (sources, textes) :
    sources = [{"id": "s1", "label", "url"}], textes = {"s1": "…"}."""
    fc = cfg.get("fiches") or {}
    lim = int(fc.get("caracteres_par_source", 8000))
    sources, texts = [], {}

    def add(label, url, text, n=lim):
        if not text:
            return
        sid = f"s{len(sources) + 1}"
        sources.append({"id": sid, "label": label, "url": url})
        texts[sid] = text[:n]

    pano = t.get("pano_data") or {}
    if t.get("pano_url"):
        ptxt = L.fetch_panorama_text(t)
        add("Vie publique — Panorama des lois : " + (pano.get("title") or t["title"])[:150], t["pano_url"], ptxt,
            int(lim * 1.5))
    add("Vie publique — Dossier législatif", t["url"], dossier_summary(t), lim)
    expose = L.fetch_tab(t, "EXPOSE_MOTIFS")
    add("Exposé des motifs", t["url"] + "?detailType=EXPOSE_MOTIFS&detailId=", expose, lim)
    if fc.get("sources_officielles_externes", True):
        for s in t.get("steps", []):
            if s["kind"] == "avis_ce" and s.get("url"):
                add("Avis du Conseil d'État", s["url"], ext_get(s["url"], pdf_pages=25), lim)
            elif s["kind"] == "etude_impact" and s.get("url"):
                add("Étude d'impact", s["url"], ext_get(s["url"], pdf_pages=30), lim)
        rapports = [i for g in t.get("documents", []) for i in g["items"] if P.norm(i["label"]).startswith("rapport")]
        if rapports:
            r = rapports[-1]  # rapport le plus récent (CMP, 2e lecture…, sinon le rapport au fond)
            add("Rapport parlementaire : " + r["label"][:150], r["url"], ext_get(r["url"]), lim)
        cc = [x for x in (pano.get("sources") or []) if "conseil-constitutionnel.fr" in x.get("url", "")]
        if cc:
            dec = ext_get(cc[0]["url"])
            if dec:
                add("Décision du Conseil constitutionnel : " + cc[0]["label"], cc[0]["url"],
                    dec[:3000] + "\n[…]\n" + dec[-5000:] if len(dec) > 8000 else dec, 8000)
    for f in L.linked_items(t["id"])[: int(fc.get("elements_lies_max", 8))]:
        add(f"{f.get('rub', '')} — {f.get('t', '')[:150]} ({f.get('d', '')})", f.get("u", ""),
            f.get("r", ""), 2500)
    total_max = int(fc.get("caracteres_max_par_fiche", 55000))
    total = sum(len(x) for x in texts.values())
    if total > total_max:  # réduction proportionnelle (début et fin de chaque source conservés)
        ratio = total_max / total
        for k, v in texts.items():
            n = max(1500, int(len(v) * ratio))
            texts[k] = v if len(v) <= n else v[:int(n * 0.8)] + "\n[…]\n" + v[-int(n * 0.2):]
    return sources, texts


def dossier_summary(t):
    lines = [f"Intitulé : {t['title']}"]
    if t.get("nor"):
        lines.append(f"NOR : {t['nor']}")
    lines.append(f"Stade actuel : {t.get('stage')} — {t.get('stage_label', '')}")
    if t.get("dep"):
        lines.append(f"Assemblée de dépôt : {t['dep']}")
    if t.get("accel"):
        lines.append("Procédure accélérée engagée par le Gouvernement")
    lines.append("Étapes de la procédure :")
    for s in t.get("steps", []):
        lines.append(f"- {s.get('date') or ''} {s['label']}")
    for j in t.get("jorf", []):
        lines.append(f"- {j.get('date') or ''} {j['label']}")
    for g in t.get("documents", []):
        lines.append(f"Documents — {g['groupe']} : " + " ; ".join(i["label"] for i in g["items"][:6]))
    for g in t.get("debats", [])[:8]:
        lines.append(f"Débats — {g['groupe']} : " + " ; ".join(i["label"][:200] for i in g["items"][:4]))
    e = t.get("eche") or {}
    if e.get("rows"):
        lines.append(f"Échéancier d'application : {e.get('pub', 0)} mesure(s) publiée(s) sur {e.get('total', 0)}")
        for r in e["rows"][:25]:
            m = " ; ".join(x["label"] for x in r.get("mesures", [])) or r.get("statut", "")
            lines.append(f"- [{r['etat']}] {r['article']} ({r.get('base', '')}) : {r['objet'][:250]} → {m[:200]}")
    elif t.get("no_decree"):
        lines.append("Loi n'appelant pas de décret d'application.")
    return "\n".join(lines)


def build_prompt(t, level, current, sources, texts):
    schema = SCHEMA_COMPLET if level == "Complète" else SCHEMA_ALLEGE
    limits = ("Limites : 4 à 12 mesures (les plus structurantes), 8 points de débat au plus, une entrée de navette "
              "par étape significative, 8 éléments « à suivre » au plus." if level == "Complète" else
              "Fiche allégée : 5 mesures au plus, 4 éléments « à suivre » au plus.")
    lines = [
        "Tu es un administrateur expert de la procédure législative française. Tu tiens à jour la FICHE TECHNIQUE "
        f"d'un texte législatif : « {t['title']} ».",
        NEUTRALITE,
        "- Mise à jour : conserve ce qui reste exact dans la fiche existante, intègre les nouvelles sources, "
        "corrige ce qui est dépassé (mesures modifiées, supprimées ou censurées au fil de la navette), "
        "et mets à jour l'état de la procédure.",
        limits,
        "Réponds UNIQUEMENT avec un objet JSON de cette forme : " + schema,
        "",
        "=== FICHE EXISTANTE ===",
        json.dumps(current, ensure_ascii=False) if current else "(aucune : crée la fiche)",
        "",
        "=== SOURCES ===",
    ]
    for s in sources:
        lines += [f"[{s['id']}] {s['label']} — {s['url']}", texts.get(s["id"], ""), ""]
    return "\n".join(lines)


# =====================================================================
# Rendu Notion
# =====================================================================
def B(kind, text="", extra=None, color=None, bold=False, italic=False):
    rich = []
    text = str(text or "")
    for i in range(0, min(len(text), 5700), 1900):
        rich.append({"type": "text", "text": {"content": text[i:i + 1900]},
                     "annotations": {"bold": bold, "italic": italic, "color": color or "default"}})
    rich += extra or []
    return {"object": "block", "type": kind, kind: {"rich_text": rich[:90]}}


def link_rt(label, url, color=None, bold=False):
    r = {"type": "text", "text": {"content": str(label)[:1900]}}
    if url and str(url).startswith("http"):
        r["text"]["link"] = {"url": str(url)[:1900]}
    r["annotations"] = {"color": color or "default", "bold": bold}
    return r


def plain_rt(label, color=None, bold=False, italic=False):
    return {"type": "text", "text": {"content": str(label)[:1900]},
            "annotations": {"color": color or "default", "bold": bold, "italic": italic}}


def refs_rt(refs, sources):
    idx = {s["id"]: s for s in sources}
    out = []
    for r in refs or []:
        s = idx.get(str(r))
        if not s:
            continue
        out.append(plain_rt(" · " if out else " — ", "gray"))
        out.append(link_rt(f"[{s['id']}]", s["url"], "gray"))
    return out[:20]


def fr_date(d):
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}" if d and len(d) >= 10 else (d or "")


def callout(text, emoji="ℹ️", color="gray_background", extra=None):
    return {"object": "block", "type": "callout", "callout": {
        "rich_text": [plain_rt(text)] + (extra or []), "icon": {"type": "emoji", "emoji": emoji}, "color": color}}


def render(t, data, sources, now, recent_events=None):
    """Blocs Notion de la fiche. `data` = partie rédigée par l'IA (peut être None)."""
    d = data or {}
    out = []
    ev = recent_events or []
    if ev:
        txt_ev = "Nouveau : " + " ; ".join(f"{fr_date(e.get('date'))} — {e['text']}" for e in ev[:5])
        out.append(callout(txt_ev[:1900], "🔔", "yellow_background"))
    note = (f"Fiche mise à jour automatiquement le {now:%d/%m/%Y} à partir de vie-publique.fr et des documents "
            "officiels. Elle ne remplace pas le texte officiel." if data else
            "Partie analytique en cours de rédaction (elle sera ajoutée lors d'un prochain passage). "
            "Les informations de procédure ci-dessous sont à jour.")
    out.append(B("paragraph", note, color="gray", italic=True))
    if d.get("en_bref"):
        out += [B("heading_2", "En bref"), B("paragraph", d["en_bref"])]

    # ---- Où en est le texte ? (calculé) ----
    out.append(B("heading_2", "Où en est le texte ?"))
    out.append(B("paragraph", "", [plain_rt(P.progress_bar(t.get("stage", "")) + "  ", bold=True),
                                   plain_rt(t.get("stage", ""), bold=True),
                                   plain_rt(f" — {t.get('stage_label', '')}"
                                            + (f" ({fr_date(t.get('stage_date'))})" if t.get("stage_date") else ""),
                                            "gray")]))
    pano = t.get("pano_data") or {}
    if pano.get("status"):
        out.append(B("quote", pano["status"]))
    for s in t.get("steps", []):
        lab = s["label"]
        out.append(B("bulleted_list_item", "", [plain_rt((fr_date(s.get("date")) + " — ") if s.get("date") else ""),
                                                link_rt(lab[:300], s.get("url"))]))
    for j in t.get("jorf", []):
        out.append(B("bulleted_list_item", "", [plain_rt((fr_date(j.get("date")) + " — ") if j.get("date") else ""),
                                                link_rt(j["label"][:300], j.get("url"), bold=True)]))
    if pano.get("historique"):
        out.append({"object": "block", "type": "toggle", "toggle": {
            "rich_text": [plain_rt("Historique détaillé (vie-publique)")],
            "children": [B("paragraph", pano["historique"][:5600])]}})

    # ---- Carte d'identité ----
    idt = d.get("identite") or {}
    out.append(B("heading_2", "Carte d'identité"))
    rows = [("Nature", t.get("nature")), ("Vecteur", t.get("vecteur")), ("Fondement", idt.get("fondement")),
            ("Initiative", idt.get("initiative")), ("Auteur(s)", idt.get("auteurs")),
            ("Ministère porteur", idt.get("ministere")), ("Assemblée de dépôt", t.get("dep")),
            ("Procédure accélérée", "oui" if t.get("accel") else None), ("Rapporteur(s)", idt.get("rapporteurs")),
            ("Textes principalement modifiés", ", ".join(idt.get("textes_modifies") or [])),
            ("NOR", t.get("nor"))]
    for k, v in rows:
        if v:
            out.append(B("bulleted_list_item", "", [plain_rt(k + " : ", bold=True), plain_rt(str(v)[:1800])]))

    if d.get("contexte") or d.get("objectifs"):
        out.append(B("heading_2", "Contexte et objectifs"))
        if d.get("contexte"):
            out.append(B("paragraph", d["contexte"]))
        for o in (d.get("objectifs") or [])[:10]:
            out.append(B("bulleted_list_item", o))
    if d.get("mesures"):
        out.append(B("heading_2", "Principales mesures"))
        for i, ms in enumerate(d["mesures"][:14], 1):
            stt = ms.get("statut") or ""
            col = {"supprimée": "red", "censurée": "red", "adoptée": "green", "en vigueur": "green"}.get(
                P.norm(stt).replace("supprimee", "supprimée").replace("censuree", "censurée").replace(
                    "adoptee", "adoptée"), "gray")
            out.append(B("heading_3", "", [plain_rt(f"{i}. {ms.get('titre', '')}"),
                                           plain_rt(f"  [{stt}]" if stt else "", col)]))
            out.append(B("paragraph", ms.get("contenu", ""), refs_rt(ms.get("refs"), sources)))
    if d.get("vecteurs"):
        out.append(B("heading_2", "Vecteurs et instruments juridiques"))
        for v in d["vecteurs"][:12]:
            v = v if isinstance(v, dict) else {"texte": str(v)}
            out.append(B("bulleted_list_item", v.get("texte", ""), refs_rt(v.get("refs"), sources)))
    if d.get("debats"):
        out.append(B("heading_2", "Points de débat"))
        for x in d["debats"][:10]:
            out.append(B("bulleted_list_item", "", [plain_rt(x.get("sujet", "") + " : ", bold=True),
                                                    plain_rt(x.get("positions", ""))] + refs_rt(x.get("refs"), sources)))
    if d.get("navette"):
        out.append(B("heading_2", "Évolutions au fil de la procédure"))
        for x in sorted(d["navette"], key=lambda z: z.get("date") or "")[:15]:
            out.append(B("bulleted_list_item", "", [plain_rt(f"{fr_date(x.get('date'))} — {x.get('etape', '')} : ",
                                                             bold=True), plain_rt(x.get("apport", ""))]
                         + refs_rt(x.get("refs"), sources)))
    if d.get("constitutionnalite"):
        out += [B("heading_2", "Contrôle de constitutionnalité"), B("paragraph", d["constitutionnalite"])]
    inc = d.get("incidences") or {}
    labels = [("institutions_vie_politique", "Institutions et vie politique"), ("societe", "Société"),
              ("finances_publiques_economie", "Finances publiques et économie"),
              ("collectivites_territoires", "Collectivités et territoires")]
    if any(inc.get(k) for k, _ in labels):
        out.append(B("heading_2", "Incidences"))
        for k, lab in labels:
            if inc.get(k):
                out.append(B("bulleted_list_item", "", [plain_rt(lab + " : ", bold=True), plain_rt(inc[k])]))

    # ---- Application (calculé + IA) ----
    e = t.get("eche") or {}
    app = d.get("application") or {}
    if t.get("stage") in (P.STAGES[8], P.STAGE_ORDONNANCE) or e.get("rows") or app.get("synthese"):
        out.append(B("heading_2", "Mise en application"))
        if t.get("no_decree"):
            out.append(B("paragraph", "Loi n'appelant pas de décret d'application (mention Légifrance)."))
        if e.get("rows"):
            taux = f" ({round(100 * e['pub'] / e['total'])} %)" if e.get("total") else ""
            out.append(B("paragraph", "", [plain_rt(f"Échéancier : {e.get('pub', 0)} mesure(s) publiée(s) sur "
                                                    f"{e.get('total', 0)}{taux}", bold=True),
                                           plain_rt(" — source : échéancier Légifrance repris par vie-publique",
                                                    "gray")]))
        if app.get("synthese"):
            out.append(B("paragraph", app["synthese"]))
        if app.get("territorial"):
            out.append(B("paragraph", "", [plain_rt("Déclinaison territoriale : ", bold=True),
                                           plain_rt(app["territorial"])]))
        if e.get("rows"):
            children = []
            for r in e["rows"][:90]:
                icon = {"publiée": "✅", "attendue": "⏳", "éventuelle": "◻️", "sans objet": "—"}.get(r["etat"], "•")
                rt = [plain_rt(f"{icon} {r['article']} — ", bold=True), plain_rt(r["objet"][:600])]
                if r.get("mesures"):
                    for mz in r["mesures"][:3]:
                        rt += [plain_rt(" → "), link_rt(mz["label"][:200], mz.get("url"))]
                elif r.get("statut"):
                    rt.append(plain_rt(f" → {r['statut'][:200]}", "gray"))
                children.append({"object": "block", "type": "bulleted_list_item",
                                  "bulleted_list_item": {"rich_text": rt}})
            out.append({"object": "block", "type": "toggle", "toggle": {
                "rich_text": [plain_rt(f"Détail de l'échéancier ({len(e['rows'])} lignes)")],
                "children": children[:95]}})
    if d.get("a_suivre"):
        out.append(B("heading_2", "À suivre"))
        for x in d["a_suivre"][:8]:
            out.append(B("bulleted_list_item", x))

    # ---- Liens officiels ----
    out.append(B("heading_2", "Liens officiels"))
    links = [("Dossier législatif (vie-publique)", t.get("url")), ("Panorama des lois (vie-publique)",
                                                                   t.get("pano_url"))]
    for k, v in (t.get("parl") or {}).items():
        links.append((f"Dossier législatif — {k}", v))
    for j in t.get("jorf", []):
        links.append((j["label"][:150], j.get("url")))
    for lab, u in links:
        if u:
            out.append(B("bulleted_list_item", "", [link_rt(lab, u)]))
    if sources:
        out.append(B("heading_2", "Sources de la fiche"))
        for s in sources:
            out.append(B("paragraph", "", [plain_rt(f"[{s['id']}] ", "gray", bold=True), link_rt(s["label"], s["url"])]))
    hist = t.get("events") or []
    if hist:
        out.append(B("heading_2", "Journal des évolutions"))
        for e2 in sorted(hist, key=lambda z: (z.get("date") or "", z.get("seen") or ""), reverse=True)[:40]:
            out.append(B("bulleted_list_item", "", [plain_rt(fr_date(e2.get("date")) + " — ", "gray"),
                                                    link_rt(e2["text"][:400], e2.get("url"))]))
    return out


def write(notion, page_id, t, blocks):
    """Remplace le contenu généré de la page (bloc conteneur supprimé puis recréé)."""
    req = notion.req
    fs = t.setdefault("fiche", {})
    old = fs.get("container")
    if old:
        try:
            req("DELETE", f"/blocks/{old}")
        except RuntimeError:
            pass
    res = req("PATCH", f"/blocks/{page_id}/children", {"children": [{
        "object": "block", "type": "synced_block", "synced_block": {"synced_from": None, "children": blocks[:100]}}]})
    container = res["results"][0]["id"]
    for i in range(100, len(blocks), 100):
        req("PATCH", f"/blocks/{container}/children", {"children": blocks[i:i + 100]})
    fs["container"] = container
    return container


def as_text(v):
    """Texte attendu : les modèles renvoient parfois une liste ou un objet à la place."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n\n".join(as_text(x) for x in v if x)
    if isinstance(v, dict):
        return " ; ".join(f"{k} : {as_text(x)}" for k, x in v.items() if x)
    return str(v)


def clean_data(data, level):
    """Contrôle de la réponse de l'IA : chaque champ reçoit le type attendu (texte, liste, objet)."""
    if not isinstance(data, dict):
        raise ValueError("fiche illisible")
    for k in ("mesures", "vecteurs", "debats", "navette", "objectifs", "a_suivre"):
        v = data.get(k)
        if v is not None and not isinstance(v, list):
            data[k] = [v] if v else []
    for k in ("identite", "incidences", "application"):
        if data.get(k) is not None and not isinstance(data.get(k), dict):
            data[k] = {}
    for k in ("en_bref", "contexte", "constitutionnalite"):
        if data.get(k) is not None:
            data[k] = as_text(data[k])
    for k in ("objectifs", "a_suivre"):
        if data.get(k):
            data[k] = [as_text(x) for x in data[k] if x]
    shapes = {"mesures": ("contenu", ("titre", "contenu", "statut")), "debats": ("positions", ("sujet", "positions")),
              "navette": ("etape", ("date", "etape", "apport")), "vecteurs": ("texte", ("texte",))}
    for k, (main, fields) in shapes.items():
        items = []
        for x in data.get(k) or []:
            if not isinstance(x, dict):
                x = {main: as_text(x)}
            for f in fields:
                x[f] = as_text(x.get(f))
            if not isinstance(x.get("refs"), list):
                x["refs"] = [x["refs"]] if x.get("refs") else []
            items.append(x)
        if k in data:
            data[k] = items
    idt = data.get("identite")
    if isinstance(idt, dict):
        for f, v in list(idt.items()):
            if f == "textes_modifies":
                idt[f] = [as_text(x) for x in v] if isinstance(v, list) else ([as_text(v)] if v else [])
            else:
                idt[f] = as_text(v)
    for k in ("incidences", "application"):
        if isinstance(data.get(k), dict):
            data[k] = {f: as_text(v) for f, v in data[k].items()}
    data["intitule_court"] = as_text(data.get("intitule_court"))[:120]
    return data
