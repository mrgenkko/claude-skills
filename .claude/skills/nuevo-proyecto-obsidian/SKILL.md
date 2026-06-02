---
name: nuevo-proyecto-obsidian
description: Crea la estructura base de un proyecto nuevo en el vault de Obsidian (index, arquitectura, decisiones/, documentacion/). Uso: /nuevo-proyecto-obsidian <org> <nombre-proyecto>
argument-hint: <org> <nombre-proyecto>
allowed-tools: mcp__obsidian__get_context, mcp__obsidian__write_note, mcp__obsidian__list_notes
---

# nuevo-proyecto-obsidian · Scaffold de proyecto en Obsidian

Crea los archivos base de un proyecto en `<org>/proyectos/<nombre>/` siguiendo las convenciones del vault.

## Argumentos

`$ARGUMENTS` contiene la organización y el nombre del proyecto:

| Forma | Descripción |
|---|---|
| `lait mi-proyecto` | Crea el proyecto en `lait/proyectos/mi-proyecto/` |
| `melquiades mi-servicio` | Crea el proyecto en `melquiades/proyectos/mi-servicio/` |

## Organizaciones válidas

- `lait` → `lait/proyectos/`
- `melquiades` → `melquiades/proyectos/`

Si `<org>` no es válida, reportar el error y detenerse.

## Flujo de ejecución

1. Parsear `$ARGUMENTS`: extraer `<org>` y `<nombre-proyecto>`.
2. Llamar `mcp__obsidian__get_context` para leer las convenciones del vault.
3. Verificar que el proyecto no exista ya con `mcp__obsidian__list_notes` en `<org>/proyectos/<nombre-proyecto>/`. Si hay archivos, avisar al usuario y detenerse.
4. Crear los 4 archivos con `mcp__obsidian__write_note` (pueden hacerse en paralelo):
   - `<org>/proyectos/<nombre>/index.md`
   - `<org>/proyectos/<nombre>/arquitectura.md`
   - `<org>/proyectos/<nombre>/decisiones/.placeholder.md`
   - `<org>/proyectos/<nombre>/documentacion/index.md`
5. Reportar los archivos creados y recordar al usuario que el agente del proyecto completará el contenido.

## Contenido de cada archivo

### index.md

```markdown
# <nombre-proyecto>

<!-- Descripción de una línea del proyecto. Completar. -->

## Documentación

- [[<org>/proyectos/<nombre>/arquitectura|Arquitectura]] — stack, acceso, dependencias
- [[<org>/proyectos/<nombre>/documentacion/index|Documentación]]

## Decisiones

| # | Decisión | Estado |
|---|---|---|
| — | _sin decisiones aún_ | — |

## Ver también

- [[<org>/ecosistema/infraestructura|Infraestructura compartida]]
- [[<org>/ecosistema/comunicacion|Comunicación]]
- [[<org>/index|Proyectos <Org>]]
```

### arquitectura.md

```markdown
# <nombre-proyecto>

## ¿Qué hace?

<!-- Descripción del propósito del proyecto. Completar. -->

## Stack

| Capa | Tecnología |
|---|---|
| | |

## Acceso y puertos

| Entorno | URL / Puerto |
|---|---|
| Desarrollo | |
| Producción | |

## Base de datos

<!-- Si aplica. Si no, eliminar esta sección. -->

## Dependencias

<!-- Servicios externos, APIs, integraciones. -->

## Estructura de carpetas clave

```
<nombre-proyecto>/
└── ...
```

## Ver también

- [[<org>/proyectos/<nombre>/index|Index del proyecto]]
- [[<org>/ecosistema/infraestructura|Infraestructura compartida]]
```

### decisiones/.placeholder.md

```markdown
<!-- Carpeta de Architecture Decision Records. Copiar templates/adr.md al crear el primero y numerar: 001-titulo.md -->
```

### documentacion/index.md

```markdown
# Documentación — <nombre-proyecto>

<!-- Índice de tutoriales, guías de uso y referencia de API del proyecto. -->

## Contenido

_Sin documentación aún._
```

## Sustituciones

En todos los archivos:
- `<nombre-proyecto>` → el nombre exacto pasado en `$ARGUMENTS`
- `<nombre>` → igual que `<nombre-proyecto>` (nombre del directorio)
- `<org>` → `lait` o `melquiades`
- `<Org>` → versión capitalizada para texto legible (`Lait` / `Melquiades`)

## Reglas de topología (del vault)

- Los archivos del proyecto **no deben enlazar a otros proyectos** con `[[...]]`.
- `index.md` es el único archivo desde donde se puede enlazar a `wiki/*`.
- Las dependencias con otros proyectos se documentan en `<org>/ecosistema/comunicacion.md`, no aquí.
