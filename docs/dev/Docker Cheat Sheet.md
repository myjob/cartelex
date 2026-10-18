 `2026-08-26

see also
[docs/dev/Docker Compose auto rebuild-deploy-restart](<docs/dev/Docker Compose auto rebuild-deploy-restart>)
[docs/infra/Native Docker Setup](<docs/infra/Native Docker Setup>)
# rebuild
```bash
docker compose build --no-cache celex-facts-backend-oi_dspy
```

if  sha256 in the IMAGE column is Docker telling you the container is still running the **old** image.

```
docker compose up -d --force-recreate celex-facts-backend-oi_dspy
```

Plain `docker compose up -d` would also work — Compose notices the image changed and recreates the container — but `--force-recreate` removes any doubt.


## Docker Timezone
```bash
date; date -u
docker exec <container> date
```


## Image size & build

```
docker image ls
docker image ls --format '{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}' | column -t
docker image ls --format 'table {{.Repository}}\t{{.Tag}}\t{{.CreatedAt}}\t{{.Size}}'
```