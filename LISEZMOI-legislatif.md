# Veille législative automatique : guide d'installation

Ce programme suit **tous les textes de la 17e législature** recensés par vie-publique.fr (rubrique « Autour de la loi ») : projets et propositions de loi en cours, lois et ordonnances publiées. Il tourne dans le même dépôt GitHub que la veille presse, **gratuitement**, toutes les 3 heures, même quand votre Mac est éteint.

## Ce que vous obtenez dans Notion

Sur une page « Veille législative », le programme crée :

1. **📊 Synthèse du jour** (en haut de page) : nombre de textes par stade, taux d'application des lois, listes dépliables des textes en cours par stade, et les 15 dernières alertes.
2. **📊 Tableau de bord législatif** : une ligne par texte, avec son **stade** (1 · Déposé → 9 · Promulgué, ou « Rejeté ou retiré »), une barre de progression, la dernière étape et sa date, la nature et le vecteur (loi ordinaire, organique, de finances, habilitation, ratification, transposition…), l'assemblée de dépôt, la procédure accélérée, la décision du Conseil constitutionnel, le taux d'application (décrets publiés / attendus), les thèmes et les liens officiels (vie-publique, Légifrance, Assemblée, Sénat).
   **La page de chaque ligne est la fiche technique du texte.**
3. **🔔 Alertes législatives** : une ligne par événement daté (nouveau texte, adoption par une assemblée, rejet, lettre rectificative, 49.3, CMP, saisine et décision du Conseil constitutionnel, promulgation, décret d'application publié, panorama mis à jour). Le texte concerné prend une ⭐ et la colonne « Alerte » pendant 3 jours.
4. **📰 Fil de veille législative** : toutes les productions des encarts de la page « Autour de la loi » (panoramas des lois, dossiers législatifs, débats et consultations, « Comprendre l'élaboration des lois »), ainsi que les actualités et rapports publics de vie-publique qui concernent l'activité législative. Chaque élément est résumé et relié au texte qu'il concerne.

### Les fiches par texte

Les parties **calculées** (toujours exactes, sans IA) : où en est le texte, procédure détaillée avec liens, mise en application (échéancier ligne par ligne : ✅ publié, ⏳ attendu), liens officiels, journal des évolutions.

Les parties **rédigées par l'IA**, à partir de vie-publique.fr et des documents officiels uniquement (exposé des motifs, avis du Conseil d'État, étude d'impact, rapports parlementaires, décision du Conseil constitutionnel, rapports publics) : en bref, carte d'identité, contexte et objectifs, principales mesures (avec leur sort au fil de la navette), vecteurs et instruments juridiques, points de débat (chaque position attribuée à son auteur institutionnel), évolutions au fil de la procédure, contrôle de constitutionnalité, incidences (institutions, société, finances publiques, collectivités), mise en application, à suivre. Chaque élément renvoie à ses sources [s1], [s2]…

Les fiches **ne disparaissent jamais** : après la promulgation, elles suivent l'application (décrets, arrêtés, entrées en vigueur différées). Les textes en sommeil (propositions de loi adoptées par une seule assemblée il y a longtemps, sans panorama) ont une fiche allégée.

⚠️ Ne modifiez pas le contenu généré d'une fiche : il est remplacé à chaque mise à jour. Pour vos notes, créez une sous-page dans la fiche : elle est conservée. La case « Suivi prioritaire » du tableau est à vous : le programme n'y touche pas.

---

## Installation (15 minutes)

### Étape 1 : la clé d'IA (rien à faire)

La veille législative utilise la **même clé Mistral** que la veille presse (secret `MISTRAL_API_KEY`). Une seule clé pour les trois veilles : ne créez pas de clé supplémentaire pour cumuler les quotas gratuits.

### Étape 2 : la page Notion (3 min)

1. Dans Notion, créez une page vide nommée **« Veille législative »**.
2. Sur cette page : **•••** (en haut à droite), puis **Connexions**, puis **Ajouter une connexion**, et choisissez la même intégration que la veille presse (**Veille presse**).
3. Copiez le lien de la page (**•••**, puis **Copier le lien**).

### Étape 3 : GitHub (6 min)

Dans votre dépôt `veille` :

1. **Add file**, puis **Upload files** : glissez `legislatif.py`, `legi_parse.py`, `legi_fiches.py` et `config-legislatif.yaml`. Ils vont **à côté de `veille.py`** (le programme réutilise ses outils). Cliquez sur **Commit changes**.
2. **Add file**, puis **Create new file** : nommez-le exactement `.github/workflows/legislatif.yml`, collez tout le contenu de `workflow-legislatif.yml`, puis **Commit changes**.
3. **Settings**, puis **Secrets and variables**, puis **Actions**, puis **New repository secret** :

| Nom du secret | Valeur |
|---|---|
| `NOTION_PAGE_ID_LEGI` | le lien de la page « Veille législative » |

Les secrets `MISTRAL_API_KEY`, `GROQ_API_KEY` et `NOTION_TOKEN` de la veille presse sont réutilisés.

### Étape 4 : diagnostic, puis premier passage (5 min)

1. Onglet **Actions**, puis **Veille législative** (à gauche), puis **Run workflow**, mode **diagnostic**.
2. Après 2 à 4 minutes, ouvrez l'exécution : le rapport s'affiche en bas (« Summary »). Il vérifie l'accès à vie-publique.fr (le site protège ses pages contre les robots : le programme sait ouvrir un navigateur sans écran pour passer la vérification), la lecture des dossiers, l'IA et Notion. **Envoyez-moi ce rapport.**
3. Relancez **Run workflow** en mode **normal**.

### Ce qui se passe les premiers jours

- **Premier passage** : lecture des 335 dossiers de la législature et des panoramas, création du tableau de bord (les lignes apparaissent au fil de la lecture), rattrapage du fil sur 90 jours. Aucune alerte n'est créée pour l'existant : c'est l'état de référence.
- **Fiches rédigées par l'IA** : 15 par passage, en priorité les textes qui bougent, puis les textes en cours, puis les lois publiées, puis les textes en sommeil. Comptez **3 à 4 jours** pour que toutes les fiches soient rédigées. En attendant, chaque fiche affiche déjà sa procédure et son application.
- **Ensuite** : chaque nouvelle étape crée une alerte et enrichit la fiche dans les 3 heures.

---

## Les vues Notion conseillées (2 min)

L'API de Notion ne permet pas de créer des vues : à faire une fois, à la main, sur le **Tableau de bord législatif** (**+** à côté du nom de la vue) :

- **Tableau** (« Kanban ») groupé par **Stade**, filtré sur *Stade* n'est pas « 9 · Promulgué », « Ordonnance publiée », « Rejeté ou retiré » : **les textes en cours de vote, colonne par colonne.**
- **Tableau** filtré sur *Alerte* n'est pas vide, trié par *Date de l'étape* décroissante : ce qui a bougé ces 3 derniers jours.
- **Tableau** filtré sur *Stade* = « 9 · Promulgué », trié par *Taux d'application* croissant : le suivi de l'application.
- Dans la colonne **Avancement**, cliquez sur l'en-tête, puis **Afficher comme : barre** (idem pour *Taux d'application*).

Dans **Alertes législatives** : une vue filtrée sur *Lu* non coché. Vous pouvez aussi activer, dans Notion, les notifications de la base (••• puis « Notifications » ou automatisations, selon votre offre).

## Personnaliser (`config-legislatif.yaml`)

- `fiches_par_passage` : nombre de fiches rédigées par passage (8 par défaut, rythme adapté à l'offre gratuite Mistral).
- `etoile_jours` : durée de l'⭐ et de l'alerte sur le tableau de bord.
- `heures_entre_lectures_*` : fréquence de relecture des dossiers selon leur activité.
- `jours_historique_fil` : profondeur du rattrapage du fil au premier passage.
- `sources_officielles_externes: false` pour s'en tenir strictement à vie-publique.fr.
- `legislature` : à changer (18…) en cas de nouvelle législature.

## Sources utilisées

- **vie-publique.fr** (DILA) : liste et contenu des dossiers législatifs, échéanciers d'application (repris de Légifrance), panoramas des lois, consultations, actualités, rapports publics.
- **Documents officiels**, uniquement pour enrichir les fiches : avis du Conseil d'État et études d'impact (Légifrance), rapports de commission (Assemblée nationale, Sénat), décisions du Conseil constitutionnel.
- Aucune source de presse.

## Coût

Zéro : GitHub Actions (dépôt public, minutes illimitées), Mistral (offre « Experiment ») et Groq en offre gratuite sans carte bancaire, Notion gratuit.
