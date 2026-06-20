# Guía: MCP de GitHub CLI (`gh`)

## ¿Qué hace el servidor `gh`?

Da a los agentes acceso gobernado a GitHub —ver despliegues, runs de Actions, PRs, checks y releases— envolviendo el binario `gh` por subprocess. Reemplaza el uso de `gh` por Bash crudo, que pedía permiso en cada llamada, derivaba de cuenta entre invocaciones y no tenía barrera para operaciones destructivas.

El mismo `server.py` sirve para múltiples cuentas/orgs: se diferencia por argumentos (una instancia por org). Sigue el patrón de `gcloud` (multi-account) y `redis` (gate de escritura).

## Tools disponibles

### Lectura (siempre permitidos)

| Tool              | Mapea a |
|-------------------|---------|
| `gh`              | comando `gh ...` arbitrario (gateado: lectura libre; mutación → requiere `allow_write`) |
| `api`             | `gh api <endpoint>` raw (GET libre; método ≠ GET → requiere `allow_write`) |
| `run_list`        | `gh run list` — runs de Actions (despliegues/CI) |
| `run_view`        | `gh run view <id> [--log]` — detalle/log de un run |
| `workflow_list`   | `gh workflow list` |
| `pr_list`         | `gh pr list` |
| `pr_view`         | `gh pr view <n>` |
| `pr_checks`       | `gh pr checks <n>` — estado de CI del PR |
| `deployment_list` | `gh api repos/{owner}/{repo}/deployments` |
| `release_list`    | `gh release list` |
| `repo_view`       | `gh repo view` |

### Escritura (gateadas — requieren `allow_write`)

| Tool             | Mapea a |
|------------------|---------|
| `pr_merge`       | `gh pr merge <n> --merge\|--squash\|--rebase` |
| `pr_comment`     | `gh pr comment <n> --body ...` |
| `release_create` | `gh release create <tag> [--title] [--notes]` |
| `workflow_run`   | `gh workflow run <wf>` — dispara CI (`workflow_dispatch`) |

### El agente pasa `repo` en cada llamada

La instancia es **por org** y cubre todos sus repos; no fija ningún repo por defecto (no hay `workdir`). Cada tool recibe `repo` (`owner/repo`, o solo `repo` y asume el `--owner` de la instancia):

```
run_list(repo="lait/lait-ui-kit", limit=5)
pr_checks(number=42, repo="lait/lait-ui-kit")
deployment_list(repo="lait/lait-ui-kit", environment="production")
```

Para que el agente de cada proyecto sepa qué `repo` disparar, deja los parámetros en el `CLAUDE.md` **del proyecto** (ver snippet abajo). `repo` nunca limita el acceso —eso lo decide el token—; solo dirige la llamada.

## Multi-cuenta sin deriva

A diferencia de gcloud —que activa la service account en disco (`~/.config/gcloud`) y necesita `CLOUDSDK_CONFIG` para que la cuenta activa no derive entre instancias— en `gh` basta con pasar **`GH_TOKEN` por instancia**:

- Con `GH_TOKEN` seteado, `gh` es **stateless**: no lee ni escribe `~/.config/gh/hosts.yml`. Cada instancia usa exactamente su token, sin estado compartido en disco → la deriva de cuenta es estructuralmente imposible.
- `add-mcp-to-project.py` inyecta `GH_TOKEN` en el bloque `env` del MCP en `~/.claude.json` (no como flag, para no exponerlo en la línea de comandos).
- `config_dir` (→ `GH_CONFIG_DIR`) es opcional, solo defensa en profundidad para subcomandos que toquen config local.

Una instancia por org: `gh-lait`, `gh-melquiades`, `gh-gz`, cada una con su PAT.

## Operaciones de escritura (gate `allow_write`)

Por defecto las mutaciones están **bloqueadas** (patrón `--allow-flush` de redis). El gate cubre:

- los tools de escritura nombrados (`pr_merge`, `pr_comment`, `release_create`, `workflow_run`),
- el tool genérico `gh` cuando detecta un verbo de escritura (`pr merge`, `release create`, `repo delete`, `workflow run`, `secret set`, `issue close`...),
- el tool `api` cuando el método no es GET.

Un intento bloqueado devuelve:

```
Bloqueado: 'pr merge' es una operación de escritura deshabilitada en gh-lait.
Re-registra la instancia con allow_write:true en secrets.json (flag --allow-write) para permitirlo.
```

Para habilitar: `allow_write: true` en la entrada de `secrets.json` y re-registrar con `--update`.

## Token (PAT): cuál y qué permisos

Un token por org/cuenta. Dos opciones:

**Classic** (rápido; https://github.com/settings/tokens/new) — todo-o-nada:
- `repo` — cubre Actions runs, deployments, PRs, checks y releases (la única casilla imprescindible).
- `read:org` — opcional, para navegar info de la org.
- `workflow` — solo si vas a **disparar** workflows (`workflow_run`).
- Ojo: `repo` da **lectura + escritura**. En classic no hay read-only; tu barrera de escritura es el gate `allow_write` del MCP.

**Fine-grained** (recomendado, menor privilegio; https://github.com/settings/personal-access-tokens/new) — *Resource owner* = la org. Repository permissions (todo *Read* para solo-lectura):

| Permiso | Nivel | Tool |
|---|---|---|
| Metadata | Read | obligatorio (`gh`, `api`, `repo_view`) |
| Actions | Read | `run_list`, `run_view`, `workflow_list` |
| Deployments | Read | `deployment_list` |
| Pull requests | Read | `pr_list`, `pr_view` |
| Commit statuses + Checks | Read | `pr_checks` |
| Contents | Read | `release_list` |

Para escritura (`allow_write: true`): subir a *Read and write* **Pull requests** + **Contents** (`pr_merge`, `release_create`) y **Actions** (`workflow_run`).

Combinación ideal con el gate: instancias normales con token **fine-grained read-only** (no pueden mutar ni por accidente); una instancia aparte con token *write* + `allow_write: true` solo si necesitas merge/release.

Probar: `GH_TOKEN=ghp_xxx gh auth status` y `GH_TOKEN=ghp_xxx gh run list -R org/repo --limit 3`.

## Instalación

```bash
bash "~/Mrgenkko Skills/scripts/install-gh-mcp.sh"
```

Verifica que `gh` esté instalado y copia el `server.py` a `~/.claude/mcp-servers/gh/`. No instala dependencias pip (usa el binario `gh` del sistema; el venv ya tiene `mcp`).

## Registrar en un proyecto

### 1. Agregar entrada en `scripts/secrets.json`

```json
{
  "name": "gh-lait",
  "type": "gh",
  "token": "github_pat_xxx",
  "owner": "lait",
  "allow_write": false
}
```

### 2. Registrar con el script automático

```bash
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto --only gh-lait
```

O con la skill: `/mcp-project add <proyecto> gh-lait`.

### 3. Registro manual en `~/.claude.json`

```json
{
  "projects": {
    "/ruta/al/proyecto": {
      "mcpServers": {
        "gh-lait": {
          "type": "stdio",
          "command": "/home/melquiades/Mrgenkko Skills/.venv/bin/python",
          "args": [
            "/home/melquiades/.claude/mcp-servers/gh/server.py",
            "--owner=lait",
            "--name=lait"
          ],
          "env": { "GH_TOKEN": "github_pat_xxx" }
        }
      }
    }
  }
}
```

Agregar `"--allow-write"` a `args` para habilitar mutaciones.

### 4. Dejar los parámetros en el `CLAUDE.md` del proyecto

Como no hay `workdir`, el agente necesita saber qué `repo` pasar. Pega un bloque así en el `CLAUDE.md` **del proyecto** (no en este repo):

```markdown
## GitHub (MCP `gh-lait`)

Repo de este proyecto: `lait/lait-ui-kit`. Pasar siempre `repo` en las llamadas:

- `run_list(repo="lait/lait-ui-kit", limit=5)` — runs de Actions (despliegues/CI)
- `run_view(run_id="…", repo="lait/lait-ui-kit", log_failed=true)` — por qué falló un run
- `pr_checks(number=42, repo="lait/lait-ui-kit")` — estado de CI de un PR
- `deployment_list(repo="lait/lait-ui-kit", environment="production")` — deployments
- `release_list(repo="lait/lait-ui-kit")` — releases

Mutaciones (`pr_merge`, `release_create`, `workflow_run`) bloqueadas salvo instancia con `allow_write`.
```

## Flujo típico (ver un despliegue)

```
run_list(repo="lait/lait-ui-kit", workflow="deploy.yml", limit=5)  # ¿cuál fue el último deploy?
run_view(run_id="123456789", repo="lait/lait-ui-kit", log_failed=true) # ¿por qué falló?
deployment_list(repo="lait/lait-ui-kit", environment="production") # estado de deployments
pr_checks(number="42", repo="lait/lait-ui-kit")                    # ¿pasó CI el PR?
```

## Troubleshooting

**`[error] 'gh' no está instalado o no está en PATH`**
→ Instalar GitHub CLI (https://cli.github.com) y reiniciar la extensión.

**`[exit 1] ... HTTP 401` / `Bad credentials`**
→ El PAT es inválido o expiró. Regenerarlo y actualizar `GH_TOKEN` en `secrets.json` + `--update`.

**`[exit 1] ... HTTP 403` o falta un scope**
→ El PAT no tiene el scope/permiso necesario (ej. `actions:read` para runs). Ajustar scopes.

**`Bloqueado: ... operación de escritura deshabilitada`**
→ Esperado: la instancia es de solo lectura. Re-registrar con `allow_write: true` si la escritura es intencional.

**El MCP no aparece en VSCode**
→ Los MCPs van en `~/.claude.json`, no en `settings.json`. Reiniciar la extensión.

## Agregar más tools al servidor

Editar `deployed/gh/server.py` (y sincronizar con `~/.claude/mcp-servers/gh/server.py` vía `install-gh-mcp.sh`). Las mutaciones nuevas deben chequear `ALLOW_WRITE` al entrar y, si aplican al tool genérico `gh`, añadir el par `(cmd, sub)` a `WRITE_VERBS`.
