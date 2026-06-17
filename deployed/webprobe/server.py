#!/usr/bin/env python3
"""MCP server para diagnóstico de landings vía Playwright.

A diferencia de ssh/redis (conexión nueva por llamada), aquí el browser es caro
y stateful, así que se mantiene un singleton module-level vivo entre llamadas.
El ciclo de vida es invisible para el agente (Capa 1: _ensure_page arranca-o-reutiliza),
con control explícito (Capa 2: open/switch/close tab + close_browser) y una red de
seguridad automática (Capa 3: _reaper_loop cierra tabs/browser idle y purga artefactos).
"""

import argparse
import asyncio
import base64
import json
import os
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from playwright.async_api import async_playwright

try:
    from playwright.async_api import TimeoutError as PWTimeout, Error as PWError
except Exception:  # pragma: no cover
    PWTimeout = PWError = Exception

parser = argparse.ArgumentParser()
parser.add_argument("--name", default=None)
parser.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
parser.add_argument("--headless", dest="headless", action="store_true", default=True)
parser.add_argument("--headed", dest="headless", action="store_false")
parser.add_argument("--allow-headed", dest="allow_headed", action="store_true", default=True)
parser.add_argument("--forbid-headed", dest="allow_headed", action="store_false")
parser.add_argument("--base-url", default=None)
parser.add_argument("--persistent-profile", default=None)
parser.add_argument("--viewport-w", type=int, default=1440)
parser.add_argument("--viewport-h", type=int, default=900)
parser.add_argument("--dpr", type=float, default=1.0)
parser.add_argument("--reduced-motion", default=None, choices=["reduce", "no-preference"])
parser.add_argument("--nav-timeout-ms", type=int, default=15000)
# ciclo de vida / multi-tab
parser.add_argument("--max-tabs", type=int, default=8)
parser.add_argument("--tab-idle-timeout", type=int, default=600)       # seg; 0 = off
parser.add_argument("--browser-idle-timeout", type=int, default=1800)  # seg; 0 = off
parser.add_argument("--reaper-interval", type=int, default=30)
# artefactos
parser.add_argument("--artifact-dir", default=None)
parser.add_argument("--artifact-ttl", type=int, default=3600)          # seg; 0 = off
parser.add_argument("--max-artifacts", type=int, default=50)
args, _ = parser.parse_known_args()

SERVER_LABEL = args.name or "webprobe"
# Bump en cada cambio de comportamiento. El agente lo ve en status() para saber si el
# proceso MCP está stale (un MCP es de vida larga; editar server.py NO recarga el proceso
# vivo — hay que reiniciar Claude Code / recargar la ventana de VSCode).
WEBPROBE_VERSION = "0.3.2"
app = Server(f"webprobe-{SERVER_LABEL}")


# ──────────────────────────────────────────────────────────────────────────
# Estado singleton + helpers de ciclo de vida
# ──────────────────────────────────────────────────────────────────────────
class _State:
    pw = None
    browser = None
    context = None
    tabs: dict = {}          # tab_id -> {"page": Page, "label": str, "last_used": float}
    active = None            # tab_id activo
    last_activity = 0.0      # time.monotonic() del último tool call
    headless_current = args.headless
    tab_counter = 0
    artifact_counter = 0


_state = _State()
_lock = asyncio.Lock()


def _resolve_url(url: str) -> str:
    if url and "://" not in url and args.base_url:
        return args.base_url.rstrip("/") + "/" + url.lstrip("/")
    return url


def _artifact_dir() -> str:
    d = args.artifact_dir or os.path.expanduser(f"~/.cache/webprobe/{SERVER_LABEL}")
    os.makedirs(d, exist_ok=True)
    return d


def _new_tab_id() -> str:
    _state.tab_counter += 1
    return f"t{_state.tab_counter}"


async def _browser_alive() -> bool:
    return (
        _state.browser is not None
        and _state.browser.is_connected()
        and _state.context is not None
    )


async def _teardown_quiet_locked():
    """Cierra todo tragando errores; deja el estado limpio para relanzar."""
    try:
        if _state.context is not None:
            await _state.context.close()
    except Exception:
        pass
    try:
        if _state.browser is not None and not args.persistent_profile:
            await _state.browser.close()
    except Exception:
        pass
    try:
        if _state.pw is not None:
            await _state.pw.stop()
    except Exception:
        pass
    _state.pw = _state.browser = _state.context = None
    _state.tabs = {}
    _state.active = None


async def _ensure_browser_locked():
    """Arranca el browser+context si no hay uno vivo. Llamar bajo _lock."""
    if await _browser_alive():
        return
    await _teardown_quiet_locked()
    _state.pw = await async_playwright().start()
    launcher = getattr(_state.pw, args.browser)
    launch_kwargs = {"headless": _state.headless_current}
    if args.browser == "chromium":
        # flags anti-throttling: el render no se ralentiza en background → medición fiel.
        launch_kwargs["args"] = [
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ]
    ctx_kwargs = {
        "viewport": {"width": args.viewport_w, "height": args.viewport_h},
        "device_scale_factor": args.dpr,
    }
    if args.reduced_motion:
        ctx_kwargs["reduced_motion"] = args.reduced_motion

    if args.persistent_profile:
        _state.context = await launcher.launch_persistent_context(
            args.persistent_profile, **launch_kwargs, **ctx_kwargs
        )
        _state.browser = _state.context.browser
    else:
        _state.browser = await launcher.launch(**launch_kwargs)
        _state.context = await _state.browser.new_context(**ctx_kwargs)

    _state.context.set_default_navigation_timeout(args.nav_timeout_ms)
    _state.tabs = {}
    _state.active = None
    # Un persistent_profile suele traer una page inicial: adoptarla como tab.
    for pg in (_state.context.pages or []):
        tid = _new_tab_id()
        _state.tabs[tid] = {"page": pg, "label": "", "last_used": time.monotonic()}
        if _state.active is None:
            _state.active = tid


async def _new_tab_locked(label: str = None) -> str:
    page = await _state.context.new_page()
    tid = _new_tab_id()
    _state.tabs[tid] = {"page": page, "label": label or "", "last_used": time.monotonic()}
    _state.active = tid
    return tid


async def _safe_close_page(tab_id: str):
    info = _state.tabs.pop(tab_id, None)
    if info is not None:
        try:
            await info["page"].close()
        except Exception:
            pass
    if _state.active == tab_id:
        _state.active = next(iter(_state.tabs), None)


async def _ensure_page(tab: str = None):
    """Capa 1: garantiza una page viva (arranca/reutiliza/reconstruye). Invisible al agente."""
    async with _lock:
        await _ensure_browser_locked()
        tab_id = tab or _state.active
        if not tab_id or tab_id not in _state.tabs:
            tab_id = await _new_tab_locked()
        page = _state.tabs[tab_id]["page"]
        _state.active = tab_id
        _state.tabs[tab_id]["last_used"] = time.monotonic()
        _state.last_activity = time.monotonic()
    # liveness real fuera del lock (no bloquea el reaper si la page cuelga)
    try:
        await page.evaluate("1")
    except Exception:
        async with _lock:
            await _safe_close_page(tab_id)
            tab_id = await _new_tab_locked()
            page = _state.tabs[tab_id]["page"]
            _state.active = tab_id
            _state.last_activity = time.monotonic()
    return page


async def _handle(page, selector: str, nth: int = 0, timeout: int = 4000):
    """Resuelve un selector a un ElementHandle (nth). Soporta CSS, `text=...`,
    `role=button[name=...]`, `:has-text(...)` — vía Playwright locator, no querySelector."""
    try:
        return await page.locator(selector).nth(nth).element_handle(timeout=timeout)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Capa 3: reaper (tabs idle, browser idle, artefactos viejos)
# ──────────────────────────────────────────────────────────────────────────
async def _reaper_loop():
    interval = max(5, args.reaper_interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await _reap_once()
        except Exception:
            pass


async def _reap_once():
    now = time.monotonic()
    async with _lock:
        if args.tab_idle_timeout > 0 and _state.tabs:
            for tab_id in list(_state.tabs.keys()):
                if tab_id == _state.active:
                    continue  # la activa vive hasta el browser-idle
                if now - _state.tabs[tab_id]["last_used"] > args.tab_idle_timeout:
                    await _safe_close_page(tab_id)
        if (
            args.browser_idle_timeout > 0
            and _state.browser is not None
            and (now - _state.last_activity) > args.browser_idle_timeout
        ):
            await _teardown_quiet_locked()
    _reap_artifacts()


def _reap_artifacts():
    d = args.artifact_dir or os.path.expanduser(f"~/.cache/webprobe/{SERVER_LABEL}")
    if not os.path.isdir(d):
        return
    now = time.time()
    try:
        files = [os.path.join(d, f) for f in os.listdir(d)]
        files = [f for f in files if os.path.isfile(f)]
    except Exception:
        return
    if args.artifact_ttl > 0:
        for f in files:
            try:
                if now - os.path.getmtime(f) > args.artifact_ttl:
                    os.remove(f)
            except Exception:
                pass
    try:
        files = [os.path.join(d, f) for f in os.listdir(d)]
        files = [f for f in files if os.path.isfile(f)]
        if len(files) > args.max_artifacts:
            files.sort(key=os.path.getmtime)
            for f in files[: len(files) - args.max_artifacts]:
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# JS inyectado (mide y reduce server-side; nunca vuelca DOM crudo)
# ──────────────────────────────────────────────────────────────────────────
# Heurística compartida: ¿el nodo lo gestiona Motion/Framer (anima transform inline)?
_JS_IS_MOTION = r"""
  const isMotion = (el) => {
    if (el.getAttribute && el.getAttribute('data-projection-id') !== null) return true;
    if (el.style && el.style.transform) return true;
    const wc = getComputedStyle(el).willChange || '';
    if (wc.indexOf('transform') !== -1) return true;
    try {
      const anims = el.getAnimations ? el.getAnimations() : [];
      for (const a of anims) {
        const kf = (a.effect && a.effect.getKeyframes) ? a.effect.getKeyframes() : [];
        if (kf.some((k) => k.transform !== undefined)) return true;
      }
    } catch (e) {}
    return false;
  };
"""

_JS_CSS_PATH = r"""
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).map(CSS.escape).join('.') : '';
    return el.tagName.toLowerCase() + cls;
  };
"""

_JS_INSPECT_BUTTONS = r"""
(args) => {
  const root = document.querySelector(args.scope) || document.body;
  const sel = 'button, a[role="button"], [role="button"], input[type="submit"], input[type="button"]';
  const allEls = Array.from(root.querySelectorAll(sel));
""" + _JS_CSS_PATH + _JS_IS_MOTION + r"""
  const sigMap = new Map();
  const offenders = [];
  const buttons = [];
  let warns = 0;
  allEls.forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const tp = cs.transitionProperty;
    const props = tp.split(',').map((s) => s.trim());
    const durs = cs.transitionDuration.split(',').map((s) => s.trim());
    const hasDur = durs.some((d) => parseFloat(d) > 0);   // 'all 0s' (default del browser) NO cuenta
    const transitionsAll = props.indexOf('all') !== -1;
    const transitionsTransform = transitionsAll || props.indexOf('transform') !== -1;
    let transition;
    if (!hasDur) transition = 'none';
    else if (transitionsAll) transition = `all ${cs.transitionDuration} ${cs.transitionTimingFunction}`;
    else transition = `${tp} ${cs.transitionDuration}`;
    const motion = isMotion(el);
    const o = {
      i,
      txt: (el.innerText || el.value || '').trim().slice(0, 40),
      sel: cssPath(el),
      vis: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none',
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      cursor: cs.cursor,
      transition,
      willChange: cs.willChange,
    };
    if (motion) o.motion = true;
    if (motion && transitionsTransform && hasDur) {
      o.warn = 'motion+transition:transform (Motion anima transform; el CSS no debe transicionarlo)';
      o.severity = 'high';
    } else if (transitionsAll && hasDur) {
      o.warn = 'transition:all';
      o.severity = 'high';
    }
    if (o.warn) { warns++; if (offenders.length < (args.offenders_limit || 20)) offenders.push(o); }  // cap: las firmas ya dan el conteo total
    if (args.include_all) buttons.push(o);
    // firma = agrupa botones idénticos para no devolver clones
    const sig = transition + '|' + o.willChange + '|' + (motion ? 'm' : '') + '|' + o.cursor + '|' + (o.disabled ? 'd' : '');
    if (!sigMap.has(sig)) sigMap.set(sig, { count: 0, transition, willChange: o.willChange, motion, cursor: o.cursor, disabled: o.disabled, sample: o.sel });
    sigMap.get(sig).count++;
  });
  const signatures = Array.from(sigMap.values()).sort((a, b) => b.count - a.count);
  const out = { total: allEls.length, warns, signatures, offenders, offenders_truncated: warns > offenders.length };
  if (args.include_all) out.buttons = buttons.slice(0, args.limit || 200);
  return out;
}
"""

_JS_QUERY = r"""
(args) => {
  let all;
  try { all = Array.from(document.querySelectorAll(args.selector)); }
  catch (e) { return null; }
  const els = all.slice(0, args.limit);
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).map(CSS.escape).join('.') : '';
    return el.tagName.toLowerCase() + cls;
  };
  const items = els.map((el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const out = {};
    for (const f of args.fields) {
      if (f === 'tag') out.tag = el.tagName.toLowerCase();
      else if (f === 'txt') out.txt = (el.innerText || el.value || '').trim().slice(0, 60);
      else if (f === 'vis') out.vis = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
      else if (f === 'box') out.box = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
      else if (f === 'classes') out.classes = (typeof el.className === 'string' ? el.className : '').trim().slice(0, 240);
      else if (f === 'sel') out.sel = cssPath(el);
      else if (f === 'href') out.href = el.getAttribute('href');
    }
    return out;
  });
  return { total: all.length, returned: items.length, truncated: all.length > items.length, items };
}
"""

_JS_OUTER_HTML = r"""
(el, args) => {
  const s = el.outerHTML;
  return s.length > args.max_len ? s.slice(0, args.max_len) + ' …[+' + (s.length - args.max_len) + ' chars]' : s;
}
"""

_JS_AUDIT_MOTION = r"""
(args) => {
  const root = document.querySelector(args.scope) || document.body;
  const all = Array.from(root.querySelectorAll('*'));
""" + _JS_CSS_PATH + _JS_IS_MOTION + r"""
  const offenders = [];
  for (const el of all) {
    if (!isMotion(el)) continue;
    const cs = getComputedStyle(el);
    const props = cs.transitionProperty.split(',').map((s) => s.trim());
    const durs = cs.transitionDuration.split(',').map((s) => s.trim());
    const hasDur = durs.some((d) => parseFloat(d) > 0);   // 'all 0s' default NO es ofensor
    const isAll = props.indexOf('all') !== -1;
    const isTf = props.indexOf('transform') !== -1;
    if ((isAll || isTf) && hasDur) {
      offenders.push({ sel: cssPath(el), kind: isAll ? 'all' : 'transform',
        transition: `${cs.transitionProperty} ${cs.transitionDuration}` });
      if (offenders.length >= args.limit) break;
    }
  }
  return { scanned: all.length, offenders };
}
"""

_JS_INTERACTION_ANIM = r"""
(trigger, args) => new Promise((resolve) => {
  const start = performance.now();
  const samples = [];
  // si el selector resolvió a un hijo (ej. <span> dentro del <button>), anclar al
  // ancestro clickable/motion: es quien recibe whileHover/onClick (no el span interno).
  const clickable = trigger.closest('button, a, [role="button"], [data-projection-id]') || trigger;
  const qTarget = () => {
    if (!args.target_selector) return clickable;   // sin target → mide el ancestro clickable (hover sobre sí mismo)
    try { return document.querySelectorAll(args.target_selector)[args.target_nth || 0] || null; }
    catch (e) { return null; }                   // selector no-CSS en target → tratado como "no encontrado"
  };
  let target = qTarget();
  let stableSince = null, last = null;
  const STABLE_MS = 120;
  // transform completo: translateX, translateY, scale (no solo Y).
  const parseTf = (cs) => {
    const tr = cs.transform;
    if (!tr || tr === 'none') return { tx: 0, ty: 0, sc: 1 };
    const m = tr.match(/matrix3?d?\(([^)]+)\)/);
    if (!m) return { tx: 0, ty: 0, sc: 1 };
    const p = m[1].split(',').map(parseFloat);
    if (p.length === 6) return { tx: p[4], ty: p[5], sc: Math.hypot(p[0], p[1]) };
    return { tx: p[12], ty: p[13], sc: Math.hypot(p[0], p[1], p[2]) };
  };
  if (args.event === 'hover') {
    // Motion/Framer whileHover escucha PointerEvent y filtra pointerType==='mouse';
    // un MouseEvent no tiene pointerType y lo ignora → hay que disparar PointerEvent.
    const pe = (t, b) => clickable.dispatchEvent(new PointerEvent(t, { bubbles: b, pointerType: 'mouse', isPrimary: true }));
    pe('pointerover', true); pe('pointerenter', false);
    clickable.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    clickable.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
  } else {
    clickable.click();
  }
  const finish = (now) => {
    if (!samples.length) { resolve({ missing: 'target', waited_ms: Math.round(now - start) }); return; }
    const f = samples[samples.length - 1];
    const opFinal = f.op;
    const rect = target.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) || 1;   // para normalizar scale a px visuales
    // per-eje: delta, final y overshoot (% sobre el step). Solo ejes que se movieron.
    const defs = [
      { axis: 'translateX', key: 'tx', thr: 2, norm: 1 },
      { axis: 'translateY', key: 'ty', thr: 2, norm: 1 },
      { axis: 'scale', key: 'sc', thr: 0.01, norm: size },
    ];
    const axes = [];
    for (const d of defs) {
      const vals = samples.map((s) => s[d.key]);
      const mx = Math.max(...vals), mn = Math.min(...vals);
      const delta = mx - mn;
      if (delta < d.thr) continue;
      const finalV = f[d.key], initV = samples[0][d.key];
      const dir = Math.sign(finalV - initV) || 1;
      const peak = dir >= 0 ? mx : mn;
      const overAbs = Math.max(0, dir >= 0 ? peak - finalV : finalV - peak);
      const step = Math.abs(finalV - initV);
      axes.push({
        axis: d.axis, delta: +delta.toFixed(2), final: +finalV.toFixed(2),
        overshoot_abs: +overAbs.toFixed(2),
        overshoot_pct: step > 0.001 ? +(100 * overAbs / step).toFixed(1) : 0,
        eff: delta * d.norm,
      });
    }
    axes.sort((a, b) => b.eff - a.eff);   // dominante visual primero (scale normalizado por tamaño)
    // settle full (opacity+transform) y settle solo-opacity (para comparar con ADRs basados en opacity)
    let settle = 0, opSettle = 0;
    for (let i = samples.length - 1; i > 0; i--) {
      const s = samples[i];
      if (Math.abs(s.op - opFinal) > 0.01 || Math.abs(s.tx - f.tx) > 1 || Math.abs(s.ty - f.ty) > 1 || Math.abs(s.sc - f.sc) > 0.005) { settle = s.t; break; }
    }
    for (let i = samples.length - 1; i > 0; i--) {
      if (Math.abs(samples[i].op - opFinal) > 0.01) { opSettle = samples[i].t; break; }
    }
    // limpiar el estado hover sintético (disparamos enter sin leave) para no contaminar
    // la próxima medición del mismo/relacionado elemento; reset:'none' lo conserva.
    if (args.event === 'hover' && args.reset !== 'none') {
      clickable.dispatchEvent(new PointerEvent('pointerout', { bubbles: true, pointerType: 'mouse' }));
      clickable.dispatchEvent(new PointerEvent('pointerleave', { bubbles: false, pointerType: 'mouse' }));
      clickable.dispatchEvent(new MouseEvent('mouseout', { bubbles: true }));
      clickable.dispatchEvent(new MouseEvent('mouseleave', { bubbles: false }));
    }
    resolve({
      settle_ms: Math.round(settle),
      opacity_settle_ms: Math.round(opSettle),
      opacity_final: +opFinal.toFixed(3),
      opacity_reached_1: opFinal >= 0.99,
      axes: axes.map(({ eff, ...rest }) => rest),
      max_overshoot_pct: axes.reduce((mx, a) => Math.max(mx, a.overshoot_pct), 0),
      samples: samples.length,
    });
  };
  const tick = (now) => {
    if (!target) target = qTarget();
    if (target) {
      const cs = getComputedStyle(target);
      const tf = parseTf(cs);
      const cur = { t: now - start, op: parseFloat(cs.opacity), tx: tf.tx, ty: tf.ty, sc: tf.sc };
      samples.push(cur);
      if (last && Math.abs(cur.op - last.op) < 0.005 && Math.abs(cur.tx - last.tx) < 0.5
          && Math.abs(cur.ty - last.ty) < 0.5 && Math.abs(cur.sc - last.sc) < 0.003) {
        if (stableSince === null) stableSince = now;
      } else {
        stableSince = null;
      }
      last = cur;
      if (stableSince !== null && now - stableSince >= STABLE_MS) return finish(now);
    }
    if (now - start < args.timeout_ms) requestAnimationFrame(tick);
    else return finish(now);
  };
  requestAnimationFrame(tick);
});
"""

_JS_COMPUTED_STYLE = r"""
(el, args) => {
  const cs = getComputedStyle(el);
  const out = {};
  for (const p of args.props) out[p] = cs.getPropertyValue(p) || cs[p] || '';
  return out;
}
"""

_JS_MEASURE_FPS = r"""
(args) => new Promise((resolve) => {
  const frames = [];
  let last = performance.now();
  const start = last;
  let rafId;
  if (args.action === 'scroll') {
    const steps = 30, dist = args.scroll_px / steps;
    let n = 0;
    const drive = () => { if (n++ < steps) { window.scrollBy(0, dist); requestAnimationFrame(drive); } };
    requestAnimationFrame(drive);
  } else if (args.action === 'hover' && args.selector) {
    const el = document.querySelector(args.selector);
    if (el) for (const t of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter'])
      el.dispatchEvent(new MouseEvent(t, { bubbles: true }));
  }
  const loop = (t) => {
    frames.push(t - last); last = t;
    if (t - start < args.duration_ms) rafId = requestAnimationFrame(loop);
    else {
      cancelAnimationFrame(rafId);
      const durs = frames.slice(1);
      const n = durs.length || 1;
      const elapsed = (last - start) / 1000;
      const sorted = [...durs].sort((a, b) => a - b);
      const p95 = sorted[Math.min(n - 1, Math.floor(n * 0.95))] || 0;
      resolve({
        avg_fps: +(durs.length / (elapsed || 1)).toFixed(1),
        total: durs.length,
        dropped: durs.filter((d) => d > 1000 / 55).length,
        jank: durs.filter((d) => d > 50).length,
        worst_ms: +Math.max(0, ...durs).toFixed(1),
        p95_ms: +p95.toFixed(1),
      });
    }
  };
  requestAnimationFrame(loop);
});
"""

_JS_ARM_CLICK_LATENCY = r"""
(el) => {
  window.__wp_lat = null;
  const onClick = (e) => {
    el.removeEventListener('click', onClick, true);
    const evt = e.timeStamp;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      window.__wp_lat = performance.now() - evt;
    }));
  };
  el.addEventListener('click', onClick, true);
  return true;
}
"""

_JS_READ_CLICK_LATENCY = "() => window.__wp_lat"

_JS_LONG_TASKS = r"""
(args) => new Promise((resolve) => {
  const tasks = [];
  const loaf = [];
  let obsLT, obsLoaf;
  try {
    obsLT = new PerformanceObserver((l) => {
      for (const e of l.getEntries())
        tasks.push({ dur: Math.round(e.duration), name: (e.attribution && e.attribution[0] && e.attribution[0].name) || 'script' });
    });
    obsLT.observe({ type: 'longtask', buffered: true });
  } catch (e) {}
  try {
    obsLoaf = new PerformanceObserver((l) => {
      for (const e of l.getEntries()) loaf.push(Math.round(e.duration));
    });
    obsLoaf.observe({ type: 'long-animation-frame', buffered: true });
  } catch (e) {}
  if (args.action === 'scroll') {
    const steps = 30, dist = args.scroll_px / steps;
    let n = 0;
    const drive = () => { if (n++ < steps) { window.scrollBy(0, dist); requestAnimationFrame(drive); } };
    requestAnimationFrame(drive);
  }
  setTimeout(() => {
    try { obsLT && obsLT.disconnect(); } catch (e) {}
    try { obsLoaf && obsLoaf.disconnect(); } catch (e) {}
    tasks.sort((a, b) => b.dur - a.dur);
    const loafBig = loaf.filter((d) => d > 50);
    resolve({
      long_tasks: tasks.length,
      total_blocked_ms: tasks.reduce((s, t) => s + t.dur, 0),
      loaf: loafBig.length,
      worst_loaf_ms: loaf.length ? Math.max(...loaf) : 0,
      top: tasks.slice(0, 3),
    });
  }, args.duration_ms);
});
"""

_JS_ENTRANCE = r"""
(el, args) => new Promise((resolve) => {
  const rm = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const cs0 = getComputedStyle(el);
  const op0 = parseFloat(cs0.opacity);
  const tf0 = cs0.transform;
  let opMin = op0, opMax = op0, tfChanged = false, lastChangeT = 0;
  const start = performance.now();
  const tick = (t) => {
    const cs = getComputedStyle(el);
    const op = parseFloat(cs.opacity);
    const tf = cs.transform;
    if (op < opMin) opMin = op;
    if (op > opMax) opMax = op;
    if (tf !== tf0 && tf !== 'none') tfChanged = true;
    if (Math.abs(op - op0) > 0.01 || tf !== tf0) lastChangeT = t - start;
    if (t - start < args.settle_ms) requestAnimationFrame(tick);
    else {
      const anims = (el.getAnimations ? el.getAnimations() : []).length;
      const opacityAnimated = (opMax - opMin) > 0.05;
      const animated = opacityAnimated || tfChanged || anims > 0 || lastChangeT > 0;
      resolve({
        animated,
        op_from: +opMin.toFixed(2),
        op_to: +opMax.toFixed(2),
        tf_from: tf0 === 'none' ? 'none' : 'transform',
        tf_to: tfChanged ? 'changed' : (tf0 === 'none' ? 'none' : 'transform'),
        duration_ms: Math.round(lastChangeT),
        reduced_motion: rm,
      });
    }
  };
  requestAnimationFrame(tick);
});
"""

_JS_WEB_VITALS = r"""
(args) => new Promise((resolve) => {
  let lcp = null, cls = 0, tbt = 0, inp = 0;
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) lcp = Math.round(e.startTime); })
      .observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) if (!e.hadRecentInput) cls += e.value; })
      .observe({ type: 'layout-shift', buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) tbt += Math.max(0, e.duration - 50); })
      .observe({ type: 'longtask', buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.duration > inp) inp = e.duration; })
      .observe({ type: 'event', buffered: true, durationThreshold: 16 });
  } catch (e) {}
  setTimeout(() => resolve({
    lcp,
    cls: +cls.toFixed(3),
    tbt: Math.round(tbt),
    inp: inp ? Math.round(inp) : null,
  }), args.settle_ms);
});
"""


# ──────────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────────
def _status_line() -> str:
    if _state.browser is None or not _state.browser.is_connected():
        return f"webprobe v{WEBPROBE_VERSION} | running=false"
    parts = [f"webprobe v{WEBPROBE_VERSION}", "running=true", f"tabs={len(_state.tabs)}", f"active={_state.active}"]
    if _state.active and _state.active in _state.tabs:
        try:
            parts.append(f"url={_state.tabs[_state.active]['page'].url}")
        except Exception:
            pass
    parts.append(f"viewport={args.viewport_w}x{args.viewport_h}")
    parts.append(f"headless={str(_state.headless_current).lower()}")
    return " ".join(parts)


async def _tool_goto(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    url = _resolve_url(arguments["url"])
    resp = await page.goto(url, wait_until=arguments.get("wait_until", "load"))
    status = resp.status if resp else "?"
    title = await page.title()
    return f"ok {status} {page.url}\ntitle: {title}\nviewport: {args.viewport_w}x{args.viewport_h} dpr={args.dpr} tab={_state.active}"


async def _tool_reload(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    resp = await page.reload(wait_until=arguments.get("wait_until", "load"))
    return f"reloaded {resp.status if resp else '?'} {page.url}"


async def _tool_set_viewport(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    w, h = int(arguments["w"]), int(arguments["h"])
    await page.set_viewport_size({"width": w, "height": h})
    return f"viewport: {w}x{h} dpr={args.dpr}"


async def _tool_set_mode(arguments: dict) -> str:
    headed = bool(arguments["headed"])
    if headed and not args.allow_headed:
        return ("set_mode(headed) deshabilitado en esta instancia (allow_headed=false). "
                "Re-registrá sin --forbid-headed (allow_headed:true en secrets.json) para permitirlo.")
    new_headless = not headed
    async with _lock:
        if (_state.headless_current == new_headless
                and _state.browser is not None and _state.browser.is_connected()):
            return f"sin cambios: ya está {'headed' if headed else 'headless'}"
        _state.headless_current = new_headless
        await _teardown_quiet_locked()
    await _ensure_page()  # relanza ya, en el nuevo modo
    return f"modo: {'headed' if headed else 'headless'} (browser relanzado)"


async def _tool_open_tab(arguments: dict) -> str:
    evicted = None
    async with _lock:
        await _ensure_browser_locked()
        if len(_state.tabs) >= args.max_tabs:
            candidates = [(tid, info["last_used"]) for tid, info in _state.tabs.items() if tid != _state.active]
            if candidates:
                evicted = min(candidates, key=lambda x: x[1])[0]
                await _safe_close_page(evicted)
        tab_id = await _new_tab_locked(label=arguments.get("label"))
        _state.last_activity = time.monotonic()
    page = _state.tabs[tab_id]["page"]
    msg = f"tab abierta: {tab_id}"
    if arguments.get("label"):
        msg += f" ({arguments['label']})"
    if arguments.get("url"):
        url = _resolve_url(arguments["url"])
        resp = await page.goto(url, wait_until="load")
        msg += f" → {resp.status if resp else '?'} {page.url}"
    if evicted:
        msg += f"\n(LRU-evict de {evicted} por max-tabs={args.max_tabs})"
    return msg


async def _tool_list_tabs(arguments: dict) -> str:
    if not _state.tabs:
        return "(sin tabs)"
    now = time.monotonic()
    rows = []
    for tid, info in _state.tabs.items():
        try:
            url = info["page"].url
        except Exception:
            url = "?"
        rows.append({
            "id": tid, "label": info["label"], "url": url,
            "active": tid == _state.active, "idle_s": round(now - info["last_used"], 1),
        })
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


async def _tool_switch_tab(arguments: dict) -> str:
    tid = arguments["tab_id"]
    async with _lock:
        if tid not in _state.tabs:
            return f"tab desconocida: {tid}. Activas: {', '.join(_state.tabs) or '(ninguna)'}"
        _state.active = tid
        _state.tabs[tid]["last_used"] = time.monotonic()
        _state.last_activity = time.monotonic()
    try:
        await _state.tabs[tid]["page"].bring_to_front()
    except Exception:
        pass
    return f"activa: {tid}"


async def _tool_close_tab(arguments: dict) -> str:
    tid = arguments["tab_id"]
    async with _lock:
        if tid not in _state.tabs:
            return f"tab desconocida: {tid}"
        await _safe_close_page(tid)
    return f"tab cerrada: {tid} (quedan {len(_state.tabs)})"


async def _tool_close_browser(arguments: dict) -> str:
    async with _lock:
        had = _state.browser is not None
        await _teardown_quiet_locked()
    return "browser cerrado" if had else "no había browser activo"


async def _tool_inspect_buttons(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    data = await page.evaluate(_JS_INSPECT_BUTTONS, {
        "scope": arguments.get("scope", "body"),
        "include_all": bool(arguments.get("include_all", False)),
        "limit": int(arguments.get("limit", 200)),
        "offenders_limit": int(arguments.get("offenders_limit", 20)),
    })
    if not data or data.get("total", 0) == 0:
        return f"(sin botones en scope '{arguments.get('scope', 'body')}')"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def _tool_query(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    data = await page.evaluate(_JS_QUERY, {
        "selector": arguments["selector"],
        "limit": int(arguments.get("limit", 20)),
        "fields": arguments.get("fields") or ["tag", "txt", "vis", "box", "classes"],
    })
    if data is None:
        return f"(selector inválido: {arguments['selector']})"
    if not data.get("items"):
        return f"(sin elementos para '{arguments['selector']}')"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def _tool_outer_html(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    h = await _handle(page, arguments["selector"], int(arguments.get("nth", 0)))
    if h is None:
        return f"(sin elemento [{arguments.get('nth', 0)}] para '{arguments['selector']}')"
    data = await h.evaluate(_JS_OUTER_HTML, {"max_len": int(arguments.get("max_len", 2000))})
    await h.dispose()
    return data


async def _tool_audit_feel(arguments: dict) -> str:
    """CI de feel multi-ruta: por cada ruta, ofensores motion-transform + warns de botones + INP/CLS/LCP."""
    page = await _ensure_page(arguments.get("tab"))
    routes = arguments["routes"]
    base = arguments.get("base_url") or args.base_url
    settle = int(arguments.get("settle_ms", 2500))
    lines = ["ruta | motion_offenders | btn_warns | INP | CLS | LCP"]
    tot_off, tot_warn, errors = 0, 0, 0
    for route in routes:
        if "://" in route:
            url = route
        elif base:
            url = base.rstrip("/") + "/" + route.lstrip("/")
        else:
            lines.append(f"{route} | ERROR: ruta relativa sin base_url (pasá URL completa o el param base_url)")
            errors += 1
            continue
        try:
            resp = await page.goto(url, wait_until="load")
            if resp is not None and resp.status >= 400:
                raise Exception(f"HTTP {resp.status}")
        except Exception as e:
            lines.append(f"{route} | ERROR: {e}")
            errors += 1
            continue
        mo = await page.evaluate(_JS_AUDIT_MOTION, {"scope": "body", "limit": 999})
        ib = await page.evaluate(_JS_INSPECT_BUTTONS, {"scope": "body", "include_all": False})
        v = await page.evaluate(_JS_WEB_VITALS, {"settle_ms": settle})
        n_off = len(mo.get("offenders", []))
        n_warn = ib.get("warns", 0)
        tot_off += n_off
        tot_warn += n_warn
        inp = v.get("inp")
        lcp = v.get("lcp")
        lines.append(f"{route} | {n_off} | {n_warn} | {inp if inp is not None else 'n/a'} | {v.get('cls')} | {lcp if lcp is not None else 'n/a'}")
    ok_routes = len(routes) - errors
    if errors:
        verdict = f"error ({errors}/{len(routes)} rutas fallaron)"
    elif tot_off == 0 and tot_warn == 0:
        verdict = "ok"
    else:
        verdict = "revisar"
    lines.append(f"TOTAL: {tot_off} motion_offenders, {tot_warn} btn_warns, {errors} errores ({ok_routes}/{len(routes)} rutas ok) — verdict: {verdict}")
    return "\n".join(lines)


async def _tool_audit_motion_transform(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    data = await page.evaluate(_JS_AUDIT_MOTION, {
        "scope": arguments.get("scope", "body"),
        "limit": int(arguments.get("limit", 30)),
    })
    offs = data.get("offenders", [])
    if not offs:
        return f"motion-vs-css-transform: OK — 0 ofensores ({data.get('scanned', 0)} nodos escaneados)"
    lines = [f"motion-vs-css-transform: {len(offs)} ofensor(es) — nodo Motion cuyo CSS transiciona transform/all:"]
    for o in offs:
        lines.append(f"  [{o['kind']}] {o['sel']} → transition-property: {o['transition']}")
    lines.append("verdict: fix_css_transition")
    return "\n".join(lines)


async def _tool_get_computed_style(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    h = await _handle(page, arguments["selector"], int(arguments.get("nth", 0)))
    if h is None:
        return f"(sin elemento [{arguments.get('nth', 0)}] para '{arguments['selector']}')"
    data = await h.evaluate(_JS_COMPUTED_STYLE, {"props": arguments["props"]})
    await h.dispose()
    return "\n".join(f"{k}: {v}" for k, v in data.items())


async def _tool_measure_fps(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    r = await page.evaluate(_JS_MEASURE_FPS, {
        "action": arguments.get("action", "scroll"),
        "selector": arguments.get("selector"),
        "duration_ms": int(arguments.get("duration_ms", 2000)),
        "scroll_px": int(arguments.get("scroll_px", 2000)),
    })
    total = r.get("total", 0) or 0
    dropped = r.get("dropped", 0)
    pct = round(100 * dropped / total) if total else 0
    avg, jank, worst = r.get("avg_fps", 0), r.get("jank", 0), r.get("worst_ms", 0)
    if avg >= 55 and jank == 0:
        verdict = "smooth"
    elif jank >= 3 or worst > 80:
        verdict = "janky"
    else:
        verdict = "degraded"
    return (f"avg_fps: {avg}\ndropped_frames: {dropped}/{total} ({pct}%)\n"
            f"jank_count: {jank}\nworst_frame_ms: {worst}\np95_frame_ms: {r.get('p95_ms', 0)}\n"
            f"verdict: {verdict}")


async def _tool_button_latency(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    selector = arguments["selector"]
    nth = int(arguments.get("nth", 0))
    runs = int(arguments.get("runs", 5))
    base = page.locator(selector)
    try:
        cnt = await base.count()
    except Exception as e:
        return f"(selector inválido '{selector}': {e})"
    if cnt <= nth:
        return f"(sin elemento [{nth}] para '{selector}'; hay {cnt})"
    loc = base.nth(nth)
    url0 = page.url.split("#", 1)[0]   # ignora cambios de #hash (mismo doc, latencia sí medible)
    samples = []
    for _ in range(runs):
        try:
            h = await loc.element_handle(timeout=2000)
        except Exception:
            h = None
        if h is None:
            break
        await h.evaluate(_JS_ARM_CLICK_LATENCY)
        await h.dispose()
        try:
            await loc.click(timeout=5000)
        except Exception as e:
            return f"(no se pudo click en '{selector}'[{nth}]: {e})"
        await page.wait_for_timeout(60)
        if page.url.split("#", 1)[0] != url0:
            return (f"selector: {selector}[{nth}]\n"
                    f"[aviso] el click NAVEGÓ a {page.url} — latencia no medible (mediría otra página). "
                    f"Usá un botón que cambie estado sin navegar.")
        val = await page.evaluate(_JS_READ_CLICK_LATENCY)
        if isinstance(val, (int, float)) and val >= 0:
            samples.append(val)
        await page.wait_for_timeout(120)
    if not samples:
        return f"(no se capturó latencia para '{selector}'[{nth}]; ¿el click no dispara repaint?)"
    samples.sort()
    n = len(samples)
    avg = round(sum(samples) / n, 1)
    p95 = round(samples[min(n - 1, int(n * 0.95))], 1)
    worst = round(samples[-1], 1)
    verdict = "good" if avg < 100 else ("ok" if avg < 200 else "slow")
    return (f"selector: {selector}[{nth}]\n"
            f"click_to_paint_ms: avg={avg} p95={p95} worst={worst} ({n} runs)\n"
            f"verdict: {verdict}")


async def _tool_interaction_animation(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    trig = arguments["trigger_selector"]
    tgt = arguments.get("target_selector")
    tgt_label = tgt if tgt else "(self)"
    tnth = int(arguments.get("trigger_nth", 0))
    gnth = int(arguments.get("target_nth", 0))
    ev = arguments.get("event", "click")
    reset = arguments.get("reset", "escape")
    trigH = await _handle(page, trig, tnth, timeout=3000)
    if trigH is None:
        return f"(sin trigger '{trig}'[{tnth}])"
    r = await trigH.evaluate(_JS_INTERACTION_ANIM, {
        "target_selector": tgt or "", "target_nth": gnth,
        "event": ev, "reset": reset,
        "timeout_ms": int(arguments.get("timeout_ms", 2500)),
    })
    await trigH.dispose()
    if reset == "escape":
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    elif reset == "reload":
        try:
            await page.reload(wait_until="load")
        except Exception:
            pass
    if r.get("missing") == "target":
        return f"(el target '{tgt_label}'[{gnth}] no apareció tras {r.get('waited_ms')}ms; ¿el {ev} abre ese nodo? target debe ser CSS)"
    reached = r["opacity_reached_1"]
    mo = r["max_overshoot_pct"]
    axes = r.get("axes", [])
    if not reached:
        verdict = "opacity_incompleta"
    elif mo > 15:
        verdict = "overshoot_fuerte"
    elif mo > 5:
        verdict = "overshoot_leve"
    else:
        verdict = "ok"
    if axes:
        parts = []
        for a in axes:
            unit = "" if a["axis"] == "scale" else "px"
            seg = f"{a['axis']} Δ{a['delta']}{unit} final={a['final']}"
            if a["overshoot_pct"]:
                seg += f" over={a['overshoot_pct']}%({a['overshoot_abs']}{unit})"
            parts.append(seg)
        axes_str = " | ".join(parts)
    else:
        axes_str = "(sin movimiento de transform; solo opacity)"
    return (f"trigger: {trig}[{tnth}] → target: {tgt_label}[{gnth}]\n"
            f"settle_ms: {r['settle_ms']} (opacity_settle: {r['opacity_settle_ms']}ms)\n"
            f"opacity_final: {r['opacity_final']} (reached_1: {str(reached).lower()})\n"
            f"axes: {axes_str}\n"
            f"max_overshoot: {mo}%\n"
            f"verdict: {verdict}")


async def _tool_long_tasks(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    r = await page.evaluate(_JS_LONG_TASKS, {
        "duration_ms": int(arguments.get("duration_ms", 3000)),
        "action": arguments.get("action", "none"),
        "scroll_px": int(arguments.get("scroll_px", 2000)),
    })
    lt = r.get("long_tasks", 0)
    total_blocked = r.get("total_blocked_ms", 0)
    lines = [f"long_tasks: {lt} (total {total_blocked}ms blocked)",
             f"loaf: {r.get('loaf', 0)} frames >50ms (worst {r.get('worst_loaf_ms', 0)}ms)"]
    if r.get("top"):
        lines.append("top_blockers:")
        for t in r["top"]:
            lines.append(f"  {t['dur']}ms @ {t['name']}")
    verdict = "main_thread_contention" if (total_blocked > 200 or lt >= 3) else "ok"
    lines.append(f"verdict: {verdict}")
    return "\n".join(lines)


async def _tool_entrance_animation_check(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    selector = arguments["selector"]
    if arguments.get("reload_first", True):
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
    h = await _handle(page, selector, int(arguments.get("nth", 0)), timeout=3000)
    if h is None:
        return f"(sin elemento para '{selector}')"
    r = await h.evaluate(_JS_ENTRANCE, {"settle_ms": int(arguments.get("settle_ms", 1200))})
    await h.dispose()
    if r is None:
        return f"(sin elemento para '{selector}')"
    animated = bool(r.get("animated", False))
    return (f"selector: {selector}\n"
            f"animated: {str(animated).lower()}\n"
            f"opacity: {r.get('op_from')}→{r.get('op_to')}\n"
            f"transform: {r.get('tf_from')}→{r.get('tf_to')}\n"
            f"duration_observed_ms: {r.get('duration_ms', 0)}\n"
            f"reduced_motion_active: {str(r.get('reduced_motion', False)).lower()}\n"
            f"verdict: {'ok' if animated else 'no_entrance_animation'}")


async def _tool_web_vitals(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    if arguments.get("url"):
        await page.goto(_resolve_url(arguments["url"]), wait_until="load")
    r = await page.evaluate(_JS_WEB_VITALS, {"settle_ms": int(arguments.get("settle_ms", 4000))})
    lcp, cls, inp, tbt = r.get("lcp"), r.get("cls"), r.get("inp"), r.get("tbt")
    lines = [f"url: {page.url}",
             f"LCP: {lcp if lcp is not None else 'n/a'}ms (good<2500)",
             f"CLS: {cls} (good<0.1)",
             (f"INP: {inp}ms (good<200)" if inp is not None else "INP: n/a (sin interacción)"),
             f"TBT: {tbt}ms"]
    bad = []
    if isinstance(lcp, (int, float)) and lcp > 2500:
        bad.append("LCP")
    if isinstance(cls, (int, float)) and cls > 0.1:
        bad.append("CLS")
    if isinstance(inp, (int, float)) and inp > 200:
        bad.append("INP")
    lines.append(f"verdict: {'ok' if not bad else '+'.join(bad) + '_poor'}")
    return "\n".join(lines)


async def _tool_screenshot(arguments: dict):
    page = await _ensure_page(arguments.get("tab"))
    ret = arguments.get("return", "path")
    full_page = bool(arguments.get("full_page", False))
    selector = arguments.get("selector")
    clip_el = None
    if selector:
        clip_el = await page.query_selector(selector)
        if clip_el is None:
            return f"(sin elemento para '{selector}')"
    if ret == "inline":
        data = await (clip_el.screenshot(type="png") if clip_el is not None
                      else page.screenshot(full_page=full_page, type="png"))
        return types.ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png")
    _state.artifact_counter += 1
    path = os.path.join(_artifact_dir(), f"shot-{int(time.time())}-{_state.artifact_counter}.png")
    if clip_el is not None:
        await clip_el.screenshot(path=path, type="png")
    else:
        await page.screenshot(path=path, full_page=full_page, type="png")
    size = os.path.getsize(path)
    return f"saved: {path} ({args.viewport_w}x{args.viewport_h}, {size // 1024}KB)\n(para verla: Read sobre la ruta)"


async def _tool_record_trace(arguments: dict) -> str:
    page = await _ensure_page(arguments.get("tab"))
    _state.artifact_counter += 1
    path = os.path.join(_artifact_dir(), f"trace-{int(time.time())}-{_state.artifact_counter}.zip")
    try:
        await _state.context.tracing.start(screenshots=True, snapshots=True)
    except Exception:
        await _state.context.tracing.stop()
        await _state.context.tracing.start(screenshots=True, snapshots=True)
    if arguments.get("action") == "scroll":
        await page.evaluate("(px)=>window.scrollBy(0,px)", int(arguments.get("scroll_px", 2000)))
    await page.wait_for_timeout(int(arguments.get("duration_ms", 4000)))
    await _state.context.tracing.stop(path=path)
    size = os.path.getsize(path)
    return f"trace saved: {path} ({size // 1024}KB)\nabrir con: npx playwright show-trace {path}"


_DISPATCH = {
    "goto": _tool_goto,
    "reload": _tool_reload,
    "set_viewport": _tool_set_viewport,
    "set_mode": _tool_set_mode,
    "open_tab": _tool_open_tab,
    "list_tabs": _tool_list_tabs,
    "switch_tab": _tool_switch_tab,
    "close_tab": _tool_close_tab,
    "close_browser": _tool_close_browser,
    "inspect_buttons": _tool_inspect_buttons,
    "query": _tool_query,
    "get_computed_style": _tool_get_computed_style,
    "outer_html": _tool_outer_html,
    "audit_motion_transform": _tool_audit_motion_transform,
    "audit_feel": _tool_audit_feel,
    "measure_fps": _tool_measure_fps,
    "button_latency": _tool_button_latency,
    "interaction_animation": _tool_interaction_animation,
    "long_tasks": _tool_long_tasks,
    "entrance_animation_check": _tool_entrance_animation_check,
    "web_vitals": _tool_web_vitals,
    "screenshot": _tool_screenshot,
    "record_trace": _tool_record_trace,
}


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    tab = {"tab": {"type": "string", "description": "tab_id objetivo (default: la activa)"}}
    return [
        types.Tool(
            name="status",
            description=f"Estado de {SERVER_LABEL}: versión del server (para detectar si el proceso MCP quedó stale tras actualizar), running, tabs, url, modo. Barato: NO arranca el browser.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="goto",
            description=f"Navega a una URL en {SERVER_LABEL}. URL relativa se resuelve contra --base-url si está; si no, pasá la URL completa. El browser arranca solo si no estaba activo.",
            inputSchema={"type": "object", "properties": {
                "url": {"type": "string", "description": "URL completa o ruta relativa a --base-url"},
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "load"},
                **tab,
            }, "required": ["url"]},
        ),
        types.Tool(
            name="reload",
            description="Recarga la page activa (o la tab indicada).",
            inputSchema={"type": "object", "properties": {
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "load"},
                **tab,
            }},
        ),
        types.Tool(
            name="set_viewport",
            description="Cambia el tamaño del viewport (px) de la page activa.",
            inputSchema={"type": "object", "properties": {
                "w": {"type": "integer"}, "h": {"type": "integer"}, **tab,
            }, "required": ["w", "h"]},
        ),
        types.Tool(
            name="set_mode",
            description="Cambia headless↔headed en runtime (teardown + relaunch). Gobernado por allow_headed: si está deshabilitado, rechaza el cambio a headed.",
            inputSchema={"type": "object", "properties": {
                "headed": {"type": "boolean", "description": "true=ventana visible (WSLg), false=headless"},
            }, "required": ["headed"]},
        ),
        types.Tool(
            name="open_tab",
            description=f"Abre una pestaña nueva (opcionalmente navega). Respeta --max-tabs={args.max_tabs} con LRU-evict. Retorna su tab_id.",
            inputSchema={"type": "object", "properties": {
                "url": {"type": "string", "description": "URL/ruta a abrir (opcional)"},
                "label": {"type": "string", "description": "etiqueta legible (opcional)"},
            }},
        ),
        types.Tool(
            name="list_tabs",
            description="Lista las pestañas abiertas: id, label, url, cuál es la activa e idle_s.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="switch_tab",
            description="Marca otra pestaña como activa.",
            inputSchema={"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]},
        ),
        types.Tool(
            name="close_tab",
            description="Cierra una pestaña por tab_id.",
            inputSchema={"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]},
        ),
        types.Tool(
            name="close_browser",
            description="Cierra todo el browser (libera RAM). La próxima llamada lo relanza solo.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="inspect_buttons",
            description="Resumen de botones/links de un DS: `{total, warns, signatures[], offenders[], offenders_truncated}`. NO devuelve clones — agrupa por firma (transition+willChange+motion+cursor+disabled) con conteo. `offenders` lista ejemplos de ofensores (warn high: motion+transiciona transform, o `transition:all` con duración>0) **acotado a `offenders_limit`** (el conteo total está en `warns`); `offenders_truncated:true` si se capó. `include_all=true` agrega el array completo `buttons`.",
            inputSchema={"type": "object", "properties": {
                "scope": {"type": "string", "default": "body", "description": "selector contenedor"},
                "offenders_limit": {"type": "integer", "default": 20, "description": "máx de ejemplos de ofensores (el total va en warns)"},
                "include_all": {"type": "boolean", "default": False, "description": "incluir el array completo de botones (no solo resumen+ofensores)"},
                "limit": {"type": "integer", "default": 200, "description": "cap del array completo si include_all"},
                **tab,
            }},
        ),
        types.Tool(
            name="query",
            description="Props clave de los elementos de un selector (sin volcar el DOM). fields: tag,txt,vis,box,classes,sel,href (classes hasta 240 chars). Devuelve {total, returned, truncated, items}.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "fields": {"type": "array", "items": {"type": "string"}},
                **tab,
            }, "required": ["selector"]},
        ),
        types.Tool(
            name="get_computed_style",
            description="Estilos computados acotados a props explícitas (props requerido → no vuelca el CSSOM). Selector soporta CSS, `text=...`, `role=button[name=...]`, `:has-text(...)`.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "props": {"type": "array", "items": {"type": "string"}},
                "nth": {"type": "integer", "default": 0},
                **tab,
            }, "required": ["selector", "props"]},
        ),
        types.Tool(
            name="outer_html",
            description="Devuelve el outerHTML de un elemento (truncado a max_len), para depurar qué clase/atributo ganó sin que se trunque como en query. Selector soporta CSS, `text=...`, `role=...`, `:has-text(...)`.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "nth": {"type": "integer", "default": 0},
                "max_len": {"type": "integer", "default": 2000},
                **tab,
            }, "required": ["selector"]},
        ),
        types.Tool(
            name="audit_motion_transform",
            description="Audit de design system: escanea nodos gestionados por Motion (animan transform) y marca los que ADEMÁS lo transicionan por CSS (`transition-property: transform` o `all`) — el smell que rompe el feel (el CSS pelea con la animación de Motion). Output: lista de ofensores con selector + transición.",
            inputSchema={"type": "object", "properties": {
                "scope": {"type": "string", "default": "body", "description": "selector contenedor"},
                "limit": {"type": "integer", "default": 30},
                **tab,
            }},
        ),
        types.Tool(
            name="audit_feel",
            description="CI de feel multi-ruta: recibe una lista de rutas (relativas a --base-url o URLs completas), navega cada una y devuelve una tabla consolidada: motion_offenders (audit_motion_transform), btn_warns (inspect_buttons), INP/CLS/LCP. Valida el feel del design system entero de una.",
            inputSchema={"type": "object", "properties": {
                "routes": {"type": "array", "items": {"type": "string"}, "description": "rutas o URLs a auditar"},
                "base_url": {"type": "string", "description": "base para rutas relativas (ej. http://localhost:7220); si se omite usa --base-url. Sin ninguno, las relativas dan ERROR (no verde)"},
                "settle_ms": {"type": "integer", "default": 2500, "description": "ventana de medición de vitals por ruta"},
                **tab,
            }, "required": ["routes"]},
        ),
        types.Tool(
            name="measure_fps",
            description="Mide FPS/jank durante una acción (scroll programático / hover / idle) con requestAnimationFrame. Output: avg_fps, dropped, jank, worst/p95, verdict smooth|degraded|janky.",
            inputSchema={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["scroll", "hover", "none"], "default": "scroll"},
                "selector": {"type": "string", "description": "para action=hover"},
                "duration_ms": {"type": "integer", "default": 2000},
                "scroll_px": {"type": "integer", "default": 2000},
                **tab,
            }},
        ),
        types.Tool(
            name="button_latency",
            description="Latencia click→repaint (INP-like) de un botón: tiempo del evento al siguiente frame pintado, promediado en N runs. Selector soporta CSS, `text=\"Abrir\"`, `role=button[name=...]`, `:has-text(...)` (apuntar 'el botón que dice X' es estable ante cambios de layout); `nth` desambigua. Avisa si el click navega (rompe la medición). Output: avg/p95/worst + verdict good|ok|slow.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "nth": {"type": "integer", "default": 0, "description": "índice si el selector matchea varios"},
                "runs": {"type": "integer", "default": 5},
                **tab,
            }, "required": ["selector"]},
        ),
        types.Tool(
            name="interaction_animation",
            description="Mide la animación disparada por una INTERACCIÓN (modal/drawer/overlay por click/hover): muestrea el target (translateX/Y + scale + opacity) desde el mismo frame. Reporta TODOS los ejes que se movieron (no solo Y → capta drawers left/right y el scale del pop), cada uno con delta + overshoot con MAGNITUD (% sobre el step, scale normalizado por tamaño del elemento). Reporta settle_ms (opacity+transform estables) y opacity_settle_ms (solo opacity, para comparar con ADRs basados en opacity), opacity_final/reached_1, max_overshoot y verdict (ok | overshoot_leve >5% | overshoot_fuerte >15% | opacity_incompleta). Cubre lo que entrance_animation_check (solo on-load) no.",
            inputSchema={"type": "object", "properties": {
                "trigger_selector": {"type": "string", "description": "elemento que se clickea/hover. Soporta CSS, `text=...`, `role=...`, `:has-text(...)`"},
                "trigger_nth": {"type": "integer", "default": 0, "description": "índice si el trigger matchea varios"},
                "target_selector": {"type": "string", "description": "elemento animado a medir (CSS; se sondea en vivo porque puede montarse tras el evento). OMITIR para medir el propio trigger (ej. hover sobre un botón)"},
                "target_nth": {"type": "integer", "default": 0},
                "event": {"type": "string", "enum": ["click", "hover"], "default": "click"},
                "reset": {"type": "string", "enum": ["escape", "reload", "none"], "default": "escape", "description": "cierra el overlay tras medir para destapar el próximo trigger (escape rápido pero app-dependiente; reload siempre funciona)"},
                "timeout_ms": {"type": "integer", "default": 2500},
                **tab,
            }, "required": ["trigger_selector"]},
        ),
        types.Tool(
            name="long_tasks",
            description="Detecta long tasks / long-animation-frames que bloquean el render durante una ventana. Output: total bloqueado, LoAF, top-3 ofensores, verdict.",
            inputSchema={"type": "object", "properties": {
                "duration_ms": {"type": "integer", "default": 3000},
                "action": {"type": "string", "enum": ["scroll", "none"], "default": "none"},
                "scroll_px": {"type": "integer", "default": 2000},
                **tab,
            }},
        ),
        types.Tool(
            name="entrance_animation_check",
            description="Verifica si la animación de ENTRADA on-load de un elemento realmente dispara (muestrea opacity/transform tras recargar) o si aparece estático ('no se siente'). Para animaciones disparadas por click/hover usá interaction_animation. Selector soporta CSS, `text=...`, `role=...`, `:has-text(...)`. Reporta prefers-reduced-motion.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "nth": {"type": "integer", "default": 0},
                "settle_ms": {"type": "integer", "default": 1200},
                "reload_first": {"type": "boolean", "default": True},
                **tab,
            }, "required": ["selector"]},
        ),
        types.Tool(
            name="web_vitals",
            description="Core Web Vitals de la page (LCP, CLS, INP, TBT) vía PerformanceObserver. INP correlaciona con 'botones lentos'.",
            inputSchema={"type": "object", "properties": {
                "url": {"type": "string", "description": "navega antes de medir (opcional)"},
                "settle_ms": {"type": "integer", "default": 4000},
                **tab,
            }},
        ),
        types.Tool(
            name="screenshot",
            description="Captura PNG. return='path' (default) guarda en --artifact-dir y devuelve la ruta (token-cheap, cliente local); return='inline' devuelve la imagen base64 por el protocolo (cliente remoto sin disco).",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string", "description": "recorta a un elemento (opcional)"},
                "full_page": {"type": "boolean", "default": False},
                "return": {"type": "string", "enum": ["path", "inline"], "default": "path"},
                **tab,
            }},
        ),
        types.Tool(
            name="record_trace",
            description="Graba un Playwright trace (pesado, opt-in) a --artifact-dir. Abrir con 'npx playwright show-trace'.",
            inputSchema={"type": "object", "properties": {
                "duration_ms": {"type": "integer", "default": 4000},
                "action": {"type": "string", "enum": ["scroll", "none"], "default": "none"},
                "scroll_px": {"type": "integer", "default": 2000},
                **tab,
            }},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "status":
            result = _status_line()
        elif name in _DISPATCH:
            result = await _DISPATCH[name](arguments or {})
        else:
            result = f"Tool desconocido: {name}"
    except PWTimeout as e:
        result = f"[timeout] {name}: {e}"
    except PWError as e:
        msg = str(e)
        if any(s in msg for s in ("Target closed", "has been closed", "disconnected", "browser has been closed")):
            try:
                async with _lock:
                    await _teardown_quiet_locked()
            except Exception:
                pass
            result = f"[browser-reset] {name}: el browser se cerró; reintentá, se relanza solo."
        else:
            result = f"Error webprobe ({name}): {msg}"
    except Exception as e:
        result = f"Error ({name}): {e}"

    if isinstance(result, (types.ImageContent, types.TextContent)):
        return [result]
    return [types.TextContent(type="text", text=str(result))]


async def main():
    asyncio.create_task(_reaper_loop())
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
