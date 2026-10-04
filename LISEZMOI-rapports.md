# Veille des rapports publics : guide

Ce programme surveille **toutes les heures** les rapports des grands organes publics et, pour chaque nouveau rapport :

1. **vous alerte aussitôt** : une ligne ⭐ apparaît dans Notion avec le lien vers le rapport, et il s'affiche en tête de la liste « 🆕 Derniers rapports » ;
2. **lit le rapport intégral** (le PDF, jusqu'à 400 pages) ;
3. **relève les recommandations mot pour mot**, directement dans le texte du rapport (pas de reformulation par l'IA) ;
4. **rédige une fiche très détaillée** : en bref, contexte, périmètre et méthode, chiffres clés, constats et argumentaire, recommandations, réponses des administrations, enjeux, recommandations appelant une loi ou un décret ;
5. **le relie aux textes de loi** en cours d'examen ou promulgués depuis moins de 2 ans (suivis par la veille législative), en expliquant le lien et les recommandations concernées. S'il ne se rattache à aucun texte, la fiche reste complète ;
6. **le range dans les dossiers thématiques** et, s'il porte sur le même sujet qu'un autre rapport ou le complète, l'intègre à une **super fiche 🔷**.

## Restitution fidèle de la langue des rapports

Les fiches sont rédigées **avec les mots, les tournures et les notions des rapports** : pas de paraphrase, pas de vulgarisation, appréciations reprises telles quelles et attribuées (« la Cour relève que… »). Chaque fiche comporte en outre :

- une rubrique **Éléments de langage** : 15 à 30 formules caractéristiques du rapport, **vérifiées automatiquement dans le texte** (une formule que l'IA aurait modifiée est rejetée) ;
- les **Notions clés**, avec la définition qu'en donne le rapport ;
- sous chaque recommandation, ses **avantages** et ses **limites** tels que le rapport les présente (rien si le rapport n'en dit rien).

## Dossiers thématiques et super fiches

Sous la page **Veille administrative** apparaissent des dossiers par grande famille (💶 Finances publiques et fiscalité, 🩺 Protection sociale et santé, 🤝 Société et solidarités, 🏭 Économie, travail et entreprises, 🌿 Environnement, climat et énergie, 💻 Numérique et information, 🎓 Éducation, recherche et culture, 🏘️ Territoires, logement et mobilités, ⚖️ Institutions, justice et sécurité, 🌍 Europe, défense et international). Chaque famille contient un sous-dossier 📂 par thème, qui présente :

- les **super fiches 🔷** du thème ;
- la liste des **fiches de rapport 📑** (fiches simples, dans la base « Rapports publics »).

Une **super fiche 🔷** (titre « Super fiche · … », bandeau bleu) réunit les rapports qui portent sur le même sujet ou se complètent. Elle contient :

- les rapports réunis (liens vers leurs fiches et vers les rapports) ;
- les grands enjeux, les convergences et les différences d'approche, dans les termes des rapports et attribués ;
- les **textes législatifs en cours** sur le sujet (et les lois récentes en cours d'application), avec les recommandations concernées ;
- les **éléments de langage** et notions clés ;
- un **tableau de toutes les recommandations**, mot pour mot, avec le lien vers le rapport, leur nature (législative, réglementaire, budgétaire…), leurs avantages et leurs limites selon le rapport. L'IA les **classe** par catégorie (leviers d'action) et par priorité (prioritaire, importante, complémentaire) ;
- les recommandations convergentes entre rapports.

Elle est mise à jour (⭐) dès qu'un nouveau rapport rejoint le sujet. Dans la base, la colonne **Super fiche** donne le lien depuis chaque rapport.

Les fiches rédigées avant cette version sont refaites automatiquement, une fois la file des nouveaux rapports vidée, pour alimenter les super fiches.

Dans le **Tableau de bord législatif**, une colonne « Rapports liés » apparaît : chaque loi affiche les rapports qui la concernent.

## Organes suivis (fichier `sources-rapports.yaml`)

- **Juridictions financières** : Cour des comptes, Conseil des prélèvements obligatoires, Haut Conseil des finances publiques.
- **Parlement** : rapports d'information et commissions d'enquête du Sénat et de l'Assemblée nationale (les rapports sur les projets et propositions de loi sont déjà dans la veille législative).
- **Hauts conseils et organes d'expertise** : Haut-commissariat à la Stratégie et au Plan, DG Trésor (Trésor-Éco, documents de travail), CEPII, HCFiPS, HCAAM, HCFEA, Haut Conseil pour le climat, COR, CAE, CESE, HCE, Conseil de l'IA et du numérique, Conseil d'État (études et avis), Haut Conseil de la santé publique.
- **Inspections générales** : IGF, IGAS, IGEDD (l'IGA et l'IGÉSR via vie-publique.fr).
- **Autorités indépendantes** : Autorité de la concurrence, CNIL, Défenseur des droits, CNCDH, CRE.
- **Filet de sécurité** : la rubrique « Rapports publics » de vie-publique.fr, qui recense les rapports de nombreux autres organes. Les doublons sont écartés.

Un tri automatique écarte les simples actualités et les rapports de portée purement locale (par exemple le contrôle d'une commune). Pour les garder, mettez `garder_rapports_locaux: true` dans `config-rapports.yaml`.

## Installation (10 minutes)

1. **Clé Gemini n° 3** (recommandé) : sur https://aistudio.google.com/apikey, cliquez sur « Create API key », puis « Create API key in new project ». Les fiches de rapport sont volumineuses : une clé dédiée évite d'entamer le quota des deux autres veilles.
2. **Page Notion** : créez une page **Veille administrative**. Avec **•••**, puis **Connexions**, ajoutez **Veille presse**. Copiez ensuite son lien.
3. **GitHub** (dépôt `veille`) :
   - **Add file**, puis **Upload files** : déposez `rapports.py`, `config-rapports.yaml` et `sources-rapports.yaml`, puis **Commit changes** ;
   - **Add file**, puis **Create new file**, avec le nom `.github/workflows/rapports.yml` : collez le contenu de `workflow-rapports.yml`, puis **Commit changes**.
4. **Secrets** (Settings, puis Secrets and variables, puis Actions) :
   - `NOTION_PAGE_ID_RAPPORTS` : le lien de la page « Veille administrative » ;
   - `GEMINI_API_KEY_RAPPORTS` : la clé de l'étape 1 (facultatif).
5. **Diagnostic** : onglet **Actions**, puis **Veille rapports publics**, puis **Run workflow** en mode **diagnostic**. Le rapport indique, organe par organe, ce qui a été trouvé : envoyez-le-moi pour que je corrige les organes qui ne répondent pas. Relancez ensuite en mode **normal**.

Au premier passage, le programme reprend les rapports des 3 dernières semaines (`jours_premier_passage`). Ensuite, il ne traite que les nouveautés.

## Vues Notion conseillées (base « Rapports publics »)

- **Tableau** trié par *Date* décroissante, avec le filtre *Lu* = non coché : vos rapports à lire.
- **Kanban** groupé par **Famille** ou par **Organe**.
- **Tableau** filtré sur *Lien avec la loi* = « Texte en cours d'examen » : les rapports à mobiliser dans la procédure législative.

## Réglages (`config-rapports.yaml`)

- `fiches_par_passage` : fiches rédigées par passage horaire (5 par défaut).
- `super_fiches_par_passage` : super fiches créées ou mises à jour par passage (2 par défaut).
- `jours_premier_passage` : profondeur du rattrapage au premier lancement.
- `mois_lois_promulguees` : ancienneté maximale des lois auxquelles un rapport peut être rattaché.
- `familles_thematiques` : familles et thèmes (dossiers Notion et colonne « Thèmes »), modifiables.
- Ajouter un organe : copiez un bloc de `sources-rapports.yaml` (flux RSS, ou page de publications avec le motif de leurs adresses), puis relancez un diagnostic.
