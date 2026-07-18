# Pathé IMAX 70mm Watcher

Surveille les séances **IMAX 70mm** de *L'Odyssée* au Pathé Odysseum et envoie une push iPhone (ntfy) dès qu'une **nouvelle séance** apparaît.

## Coolify

1. New Resource → **Docker Compose** (ou Dockerfile)
2. Repo Git → ce projet
3. Variables d'environnement :

| Variable | Requis | Défaut |
|----------|--------|--------|
| `NTFY_TOPIC` | oui | — |
| `NTFY_SERVER` | non | `https://ntfy.sh` |
| `CHECK_INTERVAL_SECONDS` | non | `300` |
| `COOLDOWN_SECONDS` | non | `1800` |
| `ERROR_COOLDOWN_SECONDS` | non | `3600` |
| `STATE_FILE` | non | `/data/state.json` |
| `LOOP` | non | `1` |

4. Volume persistant : monter un volume sur `/data` (pour `state.json`)
5. Deploy

## Local

```bash
cp .env.example .env   # renseigner NTFY_TOPIC
docker compose up -d --build
docker compose logs -f
```

Test push :

```bash
docker compose run --rm imax-watcher python -u watch_imax.py --test-push
```

## Notifications

| Événement | Push |
|-----------|------|
| Nouvelle séance | oui |
| Rate-limit API (429) | oui (cooldown erreurs) |
| Erreur script / API | oui (cooldown erreurs) |
| Premier run / rien de nouveau | silence |
