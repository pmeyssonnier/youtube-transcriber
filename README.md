# Transcripteur vidéo YouTube — Windows

Application locale pour télécharger l’audio d’une vidéo YouTube autorisée, le découper, l’envoyer à l’API OpenAI pour transcription et produire des fichiers JSON, Markdown, texte, SRT et VTT.

**Version : 1.8.0**

[![Tests](https://github.com/pmeyssonnier/youtube-transcriber/actions/workflows/tests.yml/badge.svg)](https://github.com/pmeyssonnier/youtube-transcriber/actions/workflows/tests.yml)

> Dépôt : https://github.com/pmeyssonnier/youtube-transcriber — code source de la version 1.7.1 (Windows, Python 3.11+).

## Ce qui est local — et ce qui ne l’est pas

- l’interface, le téléchargement, FFmpeg, les fichiers de travail et les exports fonctionnent sur votre PC ;
- les parties audio sont envoyées à l’API OpenAI pour être transcrites ;
- l’utilisation de l’API est facturée directement sur votre compte OpenAI ;
- l’application n’annonce pas de montant exact, car il dépend du modèle et des tarifs API en vigueur.

Ce n’est donc pas une transcription entièrement hors ligne. N’utilisez l’outil que pour des contenus que vous êtes autorisé à télécharger et à traiter, en respectant les conditions de YouTube et les droits applicables.

## Fonctionnalités

- accepte une vidéo YouTube précise (`watch`, `youtu.be`, `live` terminé, `shorts` ou `embed`) ;
- refuse les chaînes, recherches, playlists et directs encore en cours ;
- analyse le titre et la durée avant le lancement ;
- demande une confirmation pour une vidéo de 2 heures ou plus, ou de durée inconnue ;
- permet de ne transcrire qu'une plage de la vidéo (champs « Début » et « Fin », ou aperçu YouTube avec curseurs) pour réduire le coût et la durée ; les horodatages restent ceux de la vidéo d'origine ;
- télécharge la meilleure piste audio en M4A avec `yt-dlp` ;
- accélère le téléchargement par fragments simultanés ;
- découpe l’audio avec FFmpeg, par défaut en parties de 10 minutes ;
- transcrit jusqu’à 4 parties en parallèle par défaut, réglable de 1 à 6 ;
- distingue les intervenants avec `gpt-4o-transcribe-diarize`, en option ;
- utilise `whisper-1` lorsque la distinction des intervenants est désactivée ;
- sauvegarde chaque partie terminée : après une erreur ou un redémarrage, elle n’est pas refacturée ;
- affiche le pourcentage, le temps écoulé et une estimation du temps restant ;
- permet de reprendre un traitement en erreur, supprimer un traitement terminé et télécharger le M4A ;
- exporte en JSON, Markdown, TXT, SRT et VTT avec des noms dérivés du titre ;
- conserve la saisie des noms d’intervenants pendant l’actualisation de la liste ;
- classe les intervenants par part de parole avec un extrait et l’heure de première intervention, fusionne les identifiants qui reçoivent le même nom ;
- relie les intervenants d'une partie à l'autre grâce à un recouvrement entre parties (option, voir ci-dessous) ;
- propose des noms d’intervenants par IA (bouton facultatif) : seuls de courts extraits de texte sont envoyés, chaque suggestion est justifiée par une citation et doit être validée avant enregistrement ;
- empêche deux instances et deux traitements actifs pour la même vidéo.

L’interface écoute uniquement sur `http://127.0.0.1:8765`. Fermer l’onglet ne coupe pas le travail ; fermer la fenêtre noire de l’application l’interrompt. Au redémarrage, les étapes et parties déjà sauvegardées sont réutilisées.

## Installation Windows

Prérequis : Windows 10 ou 11, connexion Internet et, si une dépendance manque, `winget` fourni par **App Installer**.

1. Décompressez complètement le ZIP dans un dossier durable, par exemple `Documents\youtube-transcriber`.
2. Fermez toute ancienne instance de l’application.
3. Double-cliquez sur `INSTALLER.bat`.
4. Attendez `Installation terminee (version 1.8.0)`.
5. Double-cliquez sur `demarrer.bat`.

L’installateur cherche un Python 3.11 ou plus récent, installe Python 3.12 si nécessaire, puis FFmpeg, Deno et les dépendances Python. Il actualise aussi le `PATH` de la fenêtre en cours. Si Windows ne voit pas encore une commande fraîchement installée, fermez la fenêtre et relancez `INSTALLER.bat`.

Ne lancez pas l’application directement depuis le contenu du ZIP.

## Première transcription

1. Créez une clé sur <https://platform.openai.com/api-keys>.
2. Ouvrez l’application avec `demarrer.bat`.
3. Enregistrez la clé API dans la section Configuration.
4. Collez le lien d’une vidéo unique.
5. Choisissez les options puis cliquez sur **Analyser la vidéo**.
6. Vérifiez le titre, la durée et le nombre de parties.
7. Cliquez sur **Confirmer et démarrer**.

Pour une vidéo nécessitant une connexion YouTube, les options avancées permettent de lire les cookies de Chrome, Edge, Firefox ou Brave. Fermez le navigateur concerné si Windows empêche l’accès à sa base de cookies.

## Vitesse et fiabilité

Le réglage **Parties simultanées** concerne les appels OpenAI :

| Réglage | Usage conseillé |
| --- | --- |
| 1 | diagnostic ou limite API faible |
| 2 | compte API modeste |
| 4 | valeur recommandée |
| 6 | compte autorisant davantage de requêtes simultanées |

Une valeur élevée peut rencontrer une limite de débit. Le client réessaie automatiquement les erreurs transitoires. Si le traitement finit en erreur, cliquez sur **Reprendre** : les fichiers `chunk-results\chunk_XXX.json` valides sont relus et seules les parties manquantes repartent vers l’API.

Les parties audio sont contrôlées sous 24 Mio afin de rester sous la limite annoncée de 25 Mo de l’API. Des parties de 10 minutes à 64 kbit/s restent très en dessous de cette limite.

## Fichiers créés

```text
data\jobs\IDENTIFIANT\
├── job.json
├── source\
│   └── source.m4a
├── chunks\
│   ├── manifest.json
│   ├── chunk_000.mp3
│   └── ...
├── chunk-results\
│   ├── chunk_000.json
│   └── ...
└── outputs\
    ├── transcription.json
    ├── transcription.md
    ├── transcription.txt
    ├── transcription.srt
    └── transcription.vtt
```

| Export | Contenu |
| --- | --- |
| JSON | métadonnées et segments structurés |
| Markdown | transcription lisible avec intervenants |
| TXT | texte simple horodaté |
| SRT | sous-titres standards |
| VTT | sous-titres WebVTT |
| M4A | piste audio extraite |

## Clé API et confidentialité

La clé est conservée dans `.env` sous la forme `OPENAI_API_KEY="…"`. Elle persiste donc après un rafraîchissement de la page ou un redémarrage. Elle n’est jamais renvoyée au navigateur après enregistrement.

Ne partagez jamais `.env`. Les fichiers de travail et l’historique restent dans `data\jobs`, mais les parties audio partent chez OpenAI pour la transcription. Le bouton **Supprimer** efface définitivement le dossier local du traitement concerné.

## Mise à jour

Attendez la fin du traitement en cours avant d’installer une nouvelle version.

1. Conservez `.env` et le dossier `data\jobs`.
2. Remplacez les fichiers de l’application par ceux de la nouvelle archive.
3. Relancez `INSTALLER.bat` : il réinstalle les versions applicatives testées et met `yt-dlp` à jour.

Une sauvegarde de `data\jobs` est recommandée avant une mise à jour importante.

## Dépannage

### `py.exe : No suitable Python runtime found`

Utilisez cette version de l’installateur. Elle ne considère plus la présence de `py.exe` comme la preuve qu’un runtime Python est installé. Si l’installation automatique échoue :

```powershell
winget install --source winget --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
```

Fermez ensuite la fenêtre et relancez `INSTALLER.bat`.

### `winget est introuvable`

Installez ou mettez à jour **App Installer** depuis le Microsoft Store, puis rouvrez l’installateur.

### FFmpeg ou Deno est introuvable

Fermez toutes les anciennes fenêtres PowerShell et relancez `INSTALLER.bat`. Le programme actualise le `PATH` et vérifie `ffmpeg`, `ffprobe` et `deno` avant de continuer.

### La barre semble immobile

Pendant un appel OpenAI, le pourcentage ne progresse qu’à la fin d’une partie. L’animation, le temps écoulé et le message restent actifs. Avec le mode parallèle, plusieurs parties peuvent terminer presque en même temps et faire avancer la barre par bonds.

### YouTube demande de confirmer que vous n’êtes pas un robot

Analysez à nouveau la vidéo en choisissant votre navigateur dans **Options avancées > Cookies du navigateur**. Cette fonction suppose que vous avez légitimement accès à la vidéo.

### Une transcription a échoué

Vérifiez la clé, le crédit API, la connexion et les éventuelles limites de débit, puis utilisez **Reprendre**. Les parties déjà sauvegardées sont conservées.

## Tests

Après l’installation, depuis PowerShell :

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -v
```

Les tests couvrent notamment les URL, les doublons, le stockage, les exports, l’ordre des résultats parallèles, les points de reprise et le verrou d’instance.

## Limites connues

- les étiquettes de voix sont propres à chaque partie (`P01-A`, `P02-A`, etc.) et peuvent désigner la même personne ;
- il n’y a pas de chevauchement entre les parties : un mot placé exactement à une frontière peut être imparfait ;
- le nombre d’appels parallèles accélère l’attente réseau, mais le gain réel dépend des limites et de la charge de l’API ;
- les vidéos privées, protégées ou soumises à des restrictions peuvent rester inaccessibles même avec les cookies ;
- YouTube change régulièrement : relancer `INSTALLER.bat` met `yt-dlp` à jour ;
- la transcription continue seulement tant que le processus local reste ouvert.

## Carte du code

| Emplacement | Rôle |
| --- | --- |
| `INSTALLER.bat` / `installer_windows.ps1` | installation Windows |
| `requirements.txt` / `constraints.txt` | dépendances et versions applicatives testées ; `yt-dlp` reste actualisable |
| `demarrer.bat` / `run.py` | instance unique, serveur et navigateur |
| `app/main.py` | API locale, sécurité et analyse préalable |
| `app/pipeline.py` | téléchargement, découpage, parallélisme et reprise |
| `app/store.py` | état JSON atomique des traitements |
| `app/exporters.py` | JSON, Markdown, TXT, SRT et VTT |
| `app/static/` | interface HTML, CSS et JavaScript |
| `tests/` | tests automatisés |

## Relier les intervenants entre les parties

La diarisation d'OpenAI recalcule ses étiquettes (A, B, C…) à chaque partie audio : sans aide, une même personne apparaît sous autant d'identifiants que de parties. L'option **Relier les intervenants entre les parties** (Options avancées, active en mode « Léger » par défaut) fait déborder chaque partie sur la suivante (45 s en mode léger, 3 min en mode renforcé). Les personnes qui parlent dans ce passage commun sont reconnues d'une partie à l'autre et regroupées sous un même nom (« Voix 01 », « Voix 02 »…). La transcription bascule d'une partie à l'autre à un moment de silence, sans phrase perdue ni répétée.

Limites à connaître :

- seules les personnes qui **parlent dans un passage commun** sont reliées : quelqu'un qui n'intervient jamais à la jonction de deux parties garde un identifiant séparé. Le gain est réel mais partiel, surtout avec beaucoup d'intervenants ;
- le recouvrement ajoute de l'audio à transcrire (environ +4 % en mode léger, +15 % en mode renforcé) ;
- les règles de liaison sont volontairement prudentes (recouvrement net, mutuel et majoritaire) : mieux vaut deux identifiants séparés qu'une fusion de deux personnes ;
- le résultat est indiqué en fin de traitement (« Intervenants regroupés : 103 → 41 »). Vérifiez-le dans « Vérifier les intervenants », complétez avec la suggestion de noms par IA, et donnez le même nom aux identifiants qui restent à fusionner ;
- les traitements déjà terminés ne sont pas modifiés.

## Mettre à jour sans tout réinstaller

**Dossier cloné avec git** : `demarrer.bat` récupère lui-même la dernière version (`git pull --ff-only`) et vérifie les composants Python à chaque démarrage. En cas d'échec (modifications locales, pas de réseau), l'application démarre avec la version actuelle. Le reste de cette section concerne les copies issues d'un zip.

1. Fermez la fenêtre noire de l'application.
2. Double-cliquez sur `METTRE_A_JOUR.bat` : il télécharge la dernière version depuis GitHub, sauvegarde l'ancien code dans `.sauvegarde_code\` et remplace le code.
3. Relancez `demarrer.bat`, puis faites `Ctrl+F5` dans le navigateur.

Ne sont jamais touchés : `.venv`, `.env` (clé API) et `data` (historique et fichiers de transcription). Les composants Python ne sont réinstallés que si `requirements.txt` ou `constraints.txt` changent réellement.

Si le téléchargement échoue (dépôt privé, réseau filtré), téléchargez le zip depuis GitHub (*Code → Download ZIP*) et glissez-le sur `METTRE_A_JOUR.bat`.

## Licence

Ce projet est publié sous licence MIT — voir le fichier [LICENSE](LICENSE). © 2026 Consultora sprl.

L'utilisation de l'API OpenAI est soumise aux conditions d'OpenAI, et le téléchargement de vidéos YouTube aux conditions de YouTube : la licence du code ne vous donne aucun droit sur les contenus que vous traitez.

## Tests automatiques

À chaque envoi sur `main` et à chaque pull request, GitHub Actions (`.github/workflows/tests.yml`) lance les tests sous Ubuntu et Windows avec Python 3.11 et 3.12, vérifie la syntaxe du JavaScript et celle des scripts PowerShell (Windows PowerShell 5.1). En local : `python -m pip install pytest` puis `python -m pytest tests`.

## Historique

- **1.8.0** : option « Relier les intervenants entre les parties » (recouvrement de 45 s ou 3 min entre parties, regroupement des voix vues ensemble dans le passage commun, « Voix 01 »…) ;
- **1.7.1** : `demarrer.bat` met à jour automatiquement un dossier cloné avec git (`git pull --ff-only` puis vérification des composants Python) avant de démarrer ; sans git, rien ne change ;
- **1.7.0** : yt-dlp stable par défaut (les versions de développement ne s'installent que sur demande, case dédiée dans les Paramètres) ; l'option 30 minutes est désactivée avec la distinction des intervenants (20 minutes au plus) et l'estimation des parties en tient compte ; test de mise à jour insensible aux dossiers d'outils (.ruff_cache, .vscode…) ;
- **1.6.0** : script `METTRE_A_JOUR.bat` (mise à jour du code sans réinstaller, données conservées) ;
- **1.5.0** : clé API et mise à jour de yt-dlp déplacées dans une fenêtre « Paramètres » (écran principal allégé, ouverture automatique tant que la clé n'est pas configurée) ;
- **1.4.0** : plage de transcription (début/fin) avec aperçu vidéo et curseurs facultatifs ; l'aperçu charge le lecteur YouTube depuis le navigateur, uniquement à la demande ;
- **1.3.0** : vérification des intervenants enrichie (part du texte, première intervention, extrait, tri, fusion par nom, masquage des voix secondaires) et suggestion de noms par IA à valider ;
- **1.2.0** : bouton « Mettre à jour yt-dlp » dans l'interface ;
- **1.1.1** : parties limitées à 20 min avec la diarisation, relance immédiate d'un traitement en erreur, réponse allégée du renommage des intervenants ;
- **1.1.0** : appels parallèles, points de reprise par partie, analyse et confirmation avant lancement, URL vidéo stricte, sécurité locale renforcée, interface légère, reprise/suppression, cookies facultatifs, noms de téléchargement, instance unique et installateur amélioré ;
- **1.0.3** : pourcentage, compteurs, estimation du temps restant et protection initiale contre les doublons ;
- **1.0.2** : correction de la détection de `py.exe` sans runtime Python ;
- **1.0.1** : amélioration de la recherche Python et des chemins de l’environnement virtuel ;
- **1.0.0** : première version.

## Publication GitHub

Le code n’est pas encore fusionné sur GitHub, car aucun dépôt de destination n’a été fourni. Pour le publier ensuite, il faudra choisir le dépôt et la licence, puis pousser le projet en conservant `.env`, `.venv`, `data\jobs` et les caches hors de Git.
