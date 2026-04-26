# Guía: MCP de Bases de Datos

## Opción A — MCP oficial de PostgreSQL (npx)

La forma más rápida. No requiere código propio.

### Instalación

No necesita instalación. Se ejecuta con `npx` en el momento.

### Configuración en `~/.claude.json`

```json
{
  "projects": {
    "/ruta/al/proyecto": {
      "mcpServers": {
        "postgres-mi-bd": {
          "type": "stdio",
          "command": "npx",
          "args": [
            "-y",
            "@modelcontextprotocol/server-postgres",
            "postgresql://usuario:contraseña@host:5432/nombre_bd"
          ],
          "env": {}
        }
      }
    }
  }
}
```

### Caracteres especiales en la contraseña

Usar URL encoding. Tabla de los más comunes:

| Carácter | Encoded |
|----------|---------|
| `*`      | `%2A`   |
| `@`      | `%40`   |
| `#`      | `%23`   |
| `$`      | `%24`   |
| `!`      | `%21`   |
| `%`      | `%25`   |

Ejemplo con contraseña `pass*2025`: `postgresql://user:pass%2A2025@host:5432/db`

### Tools que expone

El servidor oficial expone una sola herramienta:

- `query(sql)` — ejecuta cualquier SQL: SELECT, INSERT, UPDATE, DELETE, DDL

---

## Opción B — MCP custom en Python

Útil cuando se necesita lógica adicional: validaciones, múltiples schemas, caché, etc.

### Dependencias

```bash
pip install mcp psycopg2-binary
# o asyncpg para queries asíncronas
```

### Estructura mínima

Ver `examples/mcp-database/server.py` para el código completo.

### Configuración en `~/.claude.json`

```json
{
  "projects": {
    "/ruta/al/proyecto": {
      "mcpServers": {
        "mi-bd-custom": {
          "type": "stdio",
          "command": "/ruta/al/venv/bin/python",
          "args": [
            "/home/melquiades/.claude/mcp-servers/mi-bd/server.py",
            "--host=TU_HOST_BD",
            "--port=5432",
            "--db=mi_base",
            "--user=mi_usuario",
            "--password=mi_pass"
          ],
          "env": {}
        }
      }
    }
  }
}
```

---

## Agregar a un proyecto con el script

Editar `scripts/add-mcp-to-project.py` para incluir el nuevo MCP en `MCP_SERVERS`, luego:

```bash
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto
```

---

## Convenciones de nomenclatura

| Patrón                      | Ejemplo                          |
|-----------------------------|----------------------------------|
| `postgres-<proyecto>`       | `postgres-melquiades-general`    |
| `postgres-<proyecto>-<id>`  | `postgres-melquiades-901982139`  |

---

## Troubleshooting

**El MCP no aparece en Claude Code (VSCode)**  
→ Verificar que el proyecto está registrado en `~/.claude.json` (no en `settings.json`).  
→ Reiniciar la extensión VSCode.

**Error de autenticación**  
→ Verificar URL encoding de la contraseña.  
→ Probar la conexión directamente: `psql "postgresql://user:pass@host:5432/db"`

**npx tarda en arrancar**  
→ Normal la primera vez (descarga el paquete). Las siguientes es inmediato.
