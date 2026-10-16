# Format de `transcription.json`

Chaque transcription produit un fichier `transcription.json` (bouton « JSON structuré »). Ce document décrit
son contrat ; le schéma complet est dans [`transcription.schema.json`](transcription.schema.json).

**Version du format : `schema_version` = `"1.0"`.** Dans une même version majeure (`1.x`), des champs peuvent
être **ajoutés** mais aucun champ existant ne disparaît ni ne change de sens. Un changement incompatible
passerait à `2.0`. Les fichiers créés avant la version 1.11 de l'application n'ont pas de `schema_version` :
ils ont la forme historique (`metadata`, `speakers`, `segments`) et peuvent être lus avec les mêmes règles en
ignorant les champs absents.

## Structure

```json
{
  "schema_version": "1.0",
  "generator": {"name": "youtube-transcriber", "version": "1.11.0", "generated_at": "2026-10-16T20:00:00+00:00"},
  "metadata": {
    "title": "Conseil communal du 16 septembre",
    "source_url": "https://www.youtube.com/watch?v=…",
    "video_id": "…",
    "duration_seconds": 21600.0,
    "transcribed_from": null,
    "transcribed_to": null,
    "model": "gpt-4o-transcribe-diarize",
    "language": "fr",
    "diarized": true,
    "speaker_linking": "light",
    "speaker_labels_need_review": true,
    "session_date": null,
    "publication_date": "2025-09-16",
    "reference_date_used": "2025-09-16",
    "reference_date_source": "publication_youtube",
    "stitch_uncertain_segments": 0
  },
  "speakers": {"Voix 01": "Président du conseil"},
  "segments": [
    {
      "id": 1,
      "start": 12.4, "end": 19.8,
      "text": "Madame Belkattire, vous avez la parole.",
      "speaker": "Voix 01", "speaker_id": "Voix 01", "speaker_name": "Président du conseil",
      "raw_speaker": "P01-A", "chunk_index": 0
    }
  ]
}
```

## Règles à connaître pour l'importer

- **`text` est le texte brut du modèle et n'est jamais modifié.** Les corrections de noms (analyse des noms
  cités, lancée à la demande) sont dans `text_normalized`, avec le détail dans `mentions`.
- **`speaker_id` est stable** : renommer un intervenant ne change que `speaker_name` et `speakers`. Utilisez
  `speaker_id` comme clé, `speaker_name` pour l'affichage. `speaker` est identique à `speaker_id` (compatibilité).
- **`id`** est le numéro d'ordre du segment dans le fichier. Il reste identique quand on renomme des
  intervenants ou qu'on analyse les noms ; une nouvelle transcription (nouveau traitement) repart de 1.
- **Dates** : `session_date` est la date de la séance saisie par l'utilisateur, `publication_date` celle de
  YouTube, `reference_date_used` celle qui a servi à choisir les élus en exercice, et `reference_date_source`
  vaut `saisie`, `publication_youtube` ou `inconnue` (dans ce cas, l'année en cours a été utilisée). Une
  rediffusion peut être publiée longtemps après la séance : ne vous fiez à `publication_date` que si
  `session_date` est `null`.
- **Incertitude** : `stitch_uncertain: true` sur un segment signale un passage à la jonction de deux parties
  audio à vérifier à la main ; `speaker_labels_need_review: true` rappelle que les voix sont des hypothèses du
  modèle de diarisation.
- Les temps (`start`, `end`) sont en secondes depuis le début de la **vidéo** (pas de la plage transcrite).
