# Veille presse automatique : guide d'installation

Ce projet surveille vos 31 sources (think tanks, institutions, podcasts). À chaque nouvelle publication, il :

1. **lit le texte intégral** de la page. Si la page ne présente qu'un rapport, il lit le PDF associé.
2. **transcrit les podcasts** (Le Collimateur, Les Enjeux internationaux, Pascal Boniface, Sources diplomatiques, Carnegie Council).
3. **fait rédiger une fiche par une IA gratuite** : titre traduit, synthèse, points clés, thèmes, pays, région, type.
4. **range tout dans une base Notion**, avec des vues filtrables par thème, pays, source, etc.

Tout fonctionne **gratuitement**, dans le cloud de GitHub, **même quand votre Mac est éteint**. Un passage a lieu toutes les 3 heures.

| Brique | Service gratuit | Rôle |
|---|---|---|
| Automatisation | GitHub Actions (dépôt privé) | lance le programme toutes les 3 h |
| Synthèse et classement | Google Gemini (AI Studio) | IA principale : le programme prend automatiquement les modèles gratuits disponibles |
| Transcription et IA de secours | Groq | transcription audio (Whisper), puis relais si le quota Gemini du jour est épuisé |
| Lecture des pages en JavaScript | Jina Reader | lecture de secours, sans compte |
| Résultat | Notion | votre flux de veille classé |

Comptez environ **20 minutes** pour l'installation, en une seule fois.

---

## Étape 1 : la clé Google Gemini (2 min)

1. Allez sur **https://aistudio.google.com/apikey** et connectez-vous avec un compte Google.
2. Cliquez sur **« Create API key »** (ou « Créer une clé API »). Acceptez la création d'un projet si on vous le propose.
3. Copiez la clé (elle commence par `AIza…`) et gardez-la de côté.

> N'ajoutez **pas** de moyen de paiement : sans carte, vous restez sur l'offre gratuite et rien ne peut vous être facturé.
> Sur l'offre gratuite, Google peut utiliser les textes envoyés pour améliorer ses modèles. Ce n'est pas gênant ici, car il s'agit de publications publiques.

## Étape 2 : la clé Groq (2 min)

1. Allez sur **https://console.groq.com/keys** et créez un compte gratuit (Google ou e-mail).
2. Cliquez sur **« Create API Key »**, donnez-lui un nom (ex. `veille`), puis copiez la clé (`gsk_…`).

## Étape 3 : Notion (5 min)

1. Allez sur **https://www.notion.so/profile/integrations** et cliquez sur **« Nouvelle intégration »**.
   - Nom : `Veille presse`. Espace de travail : le vôtre. Type : **Interne**. Enregistrez.
   - Copiez le **« Secret d'intégration interne »** (`ntn_…`).
2. Dans Notion, créez une page vide nommée **« Veille »**. La base de données y sera créée automatiquement.
3. Sur cette page, cliquez sur **•••** (en haut à droite), puis **Connexions**, puis **Ajouter une connexion**, et choisissez **Veille presse**.
4. Copiez le lien de la page (**•••**, puis **Copier le lien**). Il ressemble à
   `https://www.notion.so/Veille-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d`.
   Les 32 derniers caractères sont l'**identifiant de la page**. Vous pouvez aussi coller le lien entier : le programme sait le lire.

## Étape 4 : GitHub (8 min)

1. Créez un compte gratuit sur **https://github.com/signup** si vous n'en avez pas.
2. Créez un dépôt sur **https://github.com/new** :
   - Nom : `veille-presse`. Cochez **Private**. Cliquez sur **Create repository**.
3. Sur la page du dépôt, cliquez sur le lien **« uploading an existing file »**.
   Glissez-y ces fichiers depuis le dossier `Veille-automatique` : `veille.py`, `config.yaml`, `sources.yaml`, `requirements.txt`, `LISEZMOI.md`. Cliquez ensuite sur **Commit changes**.
   (Ne glissez **pas** le fichier `workflow-veille.yml` : il sert à l'étape suivante.)
4. Créez le fichier qui planifie les passages : cliquez sur **Add file**, puis **Create new file**.
   - Dans le champ du nom, tapez exactement : `.github/workflows/veille.yml`. Les `/` créent les dossiers au fur et à mesure.
   - Ouvrez `workflow-veille.yml` (dans le dossier `Veille-automatique`) avec TextEdit, copiez **tout** son contenu et collez-le dans la zone de texte de GitHub.
   - Cliquez sur **Commit changes**.
5. Enregistrez les clés secrètes : dans le dépôt, allez dans **Settings**, puis **Secrets and variables**, puis **Actions**, puis **New repository secret**. Créez ces 4 secrets (nom exact à gauche, valeur à droite) :

| Nom du secret | Valeur |
|---|---|
| `GEMINI_API_KEY` | la clé de l'étape 1 |
| `GROQ_API_KEY` | la clé de l'étape 2 |
| `NOTION_TOKEN` | le secret d'intégration de l'étape 3 |
| `NOTION_PAGE_ID` | l'identifiant (ou le lien) de la page « Veille » |

Les secrets sont chiffrés : personne, même vous, ne peut les relire ensuite.

## Étape 5 : premier lancement (3 min)

1. Ouvrez l'onglet **Actions** du dépôt et activez les workflows si GitHub vous le demande.
2. Cliquez sur **Veille presse** (à gauche), puis **Run workflow**. Choisissez le mode **diagnostic** et cliquez sur **Run workflow**.
3. Après 5 à 10 minutes, cliquez sur l'exécution terminée. Le **rapport de diagnostic** s'affiche en bas de la page (« Summary ») :
   - il vérifie les clés Gemini, Groq et Notion, et crée la base Notion ;
   - il teste chaque source : flux RSS trouvés, liens lus, et un test de lecture intégrale.
   **Envoyez-moi ce rapport** (copier-coller) : je corrigerai les sources qui ne fonctionnent pas.
4. Relancez ensuite **Run workflow** en mode **normal**. C'est le premier vrai passage : il ne traite que les publications des 2 derniers jours, pour ne pas crouler sous l'historique.

Ensuite, la veille tourne **seule toutes les 3 heures**. Vous n'avez plus rien à faire.

---

## Utiliser la base Notion

Chaque fiche contient : **Titre** (traduit en français), **Source**, **Date**, **Type**, **Thèmes**, **Pays**, **Région**, **Résumé**, **Lien**, **Lecture** et une case **Lu**.

La propriété **Lecture** indique ce que l'IA a pu lire :
- *Texte intégral* : l'article ou le PDF a été lu en entier.
- *Transcription audio* : l'épisode de podcast a été transcrit. La transcription complète se trouve dans la page, sous un bloc à déplier.
- *Extrait seulement* : le site bloque la lecture (souvent un paywall, comme Foreign Affairs). La fiche s'appuie alors sur le chapeau.

Vues conseillées, à créer dans Notion avec **+ Ajouter une vue** :
- **Tableau** trié par *Date* décroissante, avec le filtre *Lu* = non coché : votre flux du jour.
- **Tableau** regroupé par *Thèmes* : votre veille classée par thème.
- **Galerie** ou **Tableau** regroupé par *Pays* ou *Région*.
- Un filtre par thème, par exemple « Thèmes contient Défense & sécurité ».

## Personnaliser

- **Thèmes, régions et types** : modifiez `config.yaml` directement sur GitHub (ouvrez le fichier, cliquez sur le crayon, modifiez, puis *Commit changes*). Les nouvelles étiquettes s'utilisent dès le passage suivant.
- **Ajouter ou retirer une source** : modifiez `sources.yaml`. Un simple `site:` suffit, le programme cherche le flux RSS tout seul. Relancez un diagnostic pour vérifier.
- **Fréquence** : dans `.github/workflows/veille.yml`, la ligne `cron: "7 */3 * * *"` signifie « toutes les 3 heures ». Dans un dépôt privé, gardez 3 heures pour rester dans les 2 000 minutes gratuites par mois. Aucun article n'est perdu entre deux passages, grâce à la file d'attente. Avec un dépôt **public**, les minutes sont illimitées et vous pouvez passer à `"7 * * * *"` (toutes les heures). Vos clés restent secrètes et votre base Notion reste privée, mais la liste de vos sources devient visible.

## Comment le programme maximise le nombre d'articles lus gratuitement

- Les articles sont **envoyés à l'IA par groupes de 4**, ce qui fait 4 fois moins de requêtes.
- Le texte envoyé est limité à environ 16 000 caractères par article (le début et la conclusion). C'est suffisant pour une bonne synthèse et ça économise le quota.
- **Enchaînement des modèles** : quand le quota gratuit d'un modèle Gemini est épuisé pour la journée, le programme passe au modèle Gemini suivant (chacun a son propre quota), puis aux modèles Groq.
- **File d'attente** : si tous les quotas sont épuisés, les articles restants attendent le passage suivant. Rien n'est perdu, même quand un article disparaît du flux RSS entre-temps.
- **Pas de doublons** : un article déjà présent dans Notion n'est jamais retraité.

## En cas de problème

- **Onglet Actions, croix rouge** : cliquez sur l'exécution pour voir le message d'erreur (en français), puis envoyez-le-moi.
- **Une source ne remonte plus rien** : lancez un diagnostic. Les sites changent parfois l'adresse de leur flux.
- **Les publications ralentissent** : le quota gratuit du jour est peut-être atteint. Le rattrapage se fait automatiquement le lendemain.
- **GitHub suspend les tâches planifiées** d'un dépôt public sans activité depuis 60 jours. Cela n'arrive pas avec un dépôt privé. Au besoin, réactivez-les dans l'onglet Actions.

---

## Fiches de révision (dossiers « Fiches de révision » et « Pays »)

À chaque passage, après les articles, le programme met à jour quelques fiches de révision dans Notion, sous votre page « Veille » :

- **📚 Fiches de révision** : un dossier par famille (🔴 Sécurité & défense, 🔵 Politique & institutions…), une fiche par thème. Chaque fiche contient : problématique, « à retenir », grandes idées et arguments, chacun étayé par des exemples et des données chiffrées tirés des articles (avec des liens vers les fiches-articles), chiffres clés, débats, chronologie récente.
- **🌍 Pays** : un dossier par région, une fiche par pays (dès 4 articles sur le pays). Elle contient : situation, dernières actualités, chiffres tirés de l'actualité, données de référence de la Banque mondiale (population, PIB, croissance, inflation, chômage, dette, dépenses militaires), enjeux.

Les fiches s'enrichissent au fil de l'eau. L'IA reprend la fiche existante, y intègre les nouveaux articles, fusionne les doublons et retire ce qui est dépassé. Les réglages (seuils, fréquence, nombre de fiches par passage) sont dans `config.yaml`, section `fiches`.

⚠️ Ne modifiez pas le contenu des fiches à la main : il est remplacé à chaque mise à jour. Pour vos notes personnelles, créez une sous-page dans la fiche : elle sera conservée.
