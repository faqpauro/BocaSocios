import asyncio
import ctypes
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.async_api import async_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

START_URL = "https://bocasocios.bocajuniors.com.ar/auth/login"
PROFILE_DIR = Path("profiles")
SCREENSHOT_DIR = Path("screenshots")

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765
POLL_INTERVAL = 2

# ============================================================
# INFORMACIÓN DE HARDWARE DEL SISTEMA (WINDOWS)
# ============================================================

def get_system_specs():
    cores = os.cpu_count() or 4
    total_ram_gb = 8.0
    avail_ram_gb = 4.0
    load_pct = 50

    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total_ram_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
            avail_ram_gb = round(stat.ullAvailPhys / (1024 ** 3), 1)
            load_pct = int(stat.dwMemoryLoad)
    except Exception:
        pass

    return {
        "cores": cores,
        "total_ram_gb": total_ram_gb,
        "avail_ram_gb": avail_ram_gb,
        "load_pct": load_pct,
    }


# ============================================================
# ESTADO GLOBAL DEL MONITOR
# ============================================================

app_lock = threading.Lock()

# Estados posibles: "CONFIGURING", "STARTING", "RUNNING", "STOPPING"
app_state = {
    "status": "CONFIGURING",
    "num_sessions": 5,
    "start_url": START_URL,
    "system": get_system_specs(),
    "sessions": {},
}

active_contexts = []
active_tasks = []
playwright_instance = None
async_loop = None


def update_session(session_id, data):
    with app_lock:
        if session_id in app_state["sessions"]:
            app_state["sessions"][session_id].update(data)
            app_state["sessions"][session_id]["lastMonitorUpdate"] = datetime.now().strftime("%H:%M:%S")


def get_full_state():
    with app_lock:
        app_state["system"] = get_system_specs()
        return json.loads(json.dumps(app_state))


# ============================================================
# CLASIFICACIÓN DE URL
# ============================================================

def classify_url(url):
    if not url:
        return "INICIANDO"
    url_lower = url.lower()
    if "queue-it.net" in url_lower:
        return "QUEUE-IT"
    if "bocasocios-gw.bocajuniors.com.ar" in url_lower:
        return "REDIRECT-BOCA"
    if "bocasocios.bocajuniors.com.ar" in url_lower:
        return "BOCA-SOCIOS"
    if "staging.boca.com" in url_lower:
        return "STAGING"
    return "OTRA"


# ============================================================
# LECTURA DE DATOS DE QUEUE-IT
# ============================================================

async def get_queue_info(page):
    try:
        return await page.evaluate(
            """
            () => {
                const vm = window.queueViewModel;
                if (!vm) return null;
                const ticket = vm.ticket;
                if (!ticket) return null;

                const value = (name) => {
                    try {
                        const item = ticket[name];
                        if (typeof item === "function") return item();
                        return item ?? null;
                    } catch {
                        return null;
                    }
                };

                const vmValue = (name) => {
                    try {
                        const item = vm[name];
                        if (typeof item === "function") return item();
                        return item ?? null;
                    } catch {
                        return null;
                    }
                };

                return {
                    customerId: vmValue("customerId"),
                    eventId: vmValue("eventId"),
                    queueId: vmValue("queueId"),
                    queueState: vmValue("queueState"),
                    queuePaused: value("queuePaused"),
                    progress: value("progress"),
                    expectedServiceTime: value("expectedServiceTime"),
                    expectedServiceTimeUTC: value("expectedServiceTimeUTC"),
                    whichIsIn: value("whichIsIn"),
                    usersInLineAheadOfYou: value("usersInLineAheadOfYou"),
                    usersInQueue: value("usersInQueue"),
                    queueNumber: value("queueNumber"),
                    lastUpdated: value("lastUpdated"),
                    lastUpdatedUTC: value("lastUpdatedUTC"),
                    eventStartTimeFormatted: value("eventStartTimeFormatted"),
                    eventStartTimeUTC: value("eventStartTimeUTC"),
                    windowStartTime: value("windowStartTime"),
                    targetUrl: vmValue("targetUrl"),
                    isBeforeOrIdle: vmValue("isBeforeOrIdle"),
                    isIdle: vmValue("isIdle")
                };
            }
            """
        )
    except Exception:
        return None


# ============================================================
# MONITOR DE UNA SESIÓN HEADLESS
# ============================================================

async def monitor_session(session_number, context, target_url):
    session_id = f"session_{session_number:02d}"

    page = context.pages[0] if context.pages else await context.new_page()

    previous_page_type = None
    previous_url = None
    previous_queue_data = None

    async def response_handler(response):
        try:
            if "queue-it.net" in response.url.lower() and response.status >= 400:
                print(f"[SESIÓN {session_number}] HTTP Error {response.status}: {response.url}")
        except Exception:
            pass

    page.on("response", response_handler)

    try:
        await page.goto(target_url, wait_until="domcontentloaded")
    except Exception as exc:
        update_session(session_id, {"pageType": "ERROR", "error": str(exc)})
        print(f"[SESIÓN {session_number}] Error inicial al navegar: {exc}")

    while True:
        try:
            if page.is_closed():
                update_session(session_id, {"pageType": "ERROR", "error": "Página cerrada"})
                return

            url = page.url
            page_type = classify_url(url)

            if page_type != previous_page_type or url != previous_url:
                print(f"[SESIÓN {session_number}] Estado: {page_type} | URL: {url}")
                update_session(session_id, {
                    "pageType": page_type,
                    "url": url,
                    "error": None,
                })

                try:
                    SCREENSHOT_DIR.mkdir(exist_ok=True)
                    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    shot_path = SCREENSHOT_DIR / f"s{session_number:02d}_{stamp}_{page_type.lower()}.png"
                    await page.screenshot(path=str(shot_path), full_page=True)
                except Exception:
                    pass

                previous_page_type = page_type
                previous_url = url

            if page_type == "QUEUE-IT":
                data = await get_queue_info(page)
                if data:
                    data["pageType"] = page_type
                    data["url"] = url
                    update_session(session_id, data)

                    if previous_queue_data is None:
                        print(f"[SESIÓN {session_number}] Primeros datos de fila recibidos.")
                    else:
                        old_w = previous_queue_data.get("windowStartTime")
                        new_w = data.get("windowStartTime")
                        if not old_w and new_w:
                            print(f"\n🟢 [SESIÓN {session_number}] ¡TURNO COMENZÓ! Hora: {new_w}\n")

                    previous_queue_data = data

        except asyncio.CancelledError:
            return
        except Exception as exc:
            update_session(session_id, {"error": str(exc)})

        await asyncio.sleep(POLL_INTERVAL)


# ============================================================
# GESTIÓN DEL CICLO DE VIDA DE SESIONES (INICIO / PARADA)
# ============================================================

async def start_monitoring_async(num_sessions, target_url):
    global active_contexts, active_tasks, playwright_instance

    with app_lock:
        app_state["status"] = "STARTING"
        app_state["num_sessions"] = num_sessions
        app_state["start_url"] = target_url
        app_state["sessions"] = {
            f"session_{i:02d}": {
                "session": i,
                "pageType": "INICIANDO",
                "url": target_url,
                "customerId": None,
                "eventId": None,
                "queueId": None,
                "queueState": None,
                "queuePaused": None,
                "progress": None,
                "expectedServiceTime": None,
                "expectedServiceTimeUTC": None,
                "whichIsIn": None,
                "usersInLineAheadOfYou": None,
                "usersInQueue": None,
                "queueNumber": None,
                "lastUpdated": None,
                "lastUpdatedUTC": None,
                "eventStartTimeFormatted": None,
                "eventStartTimeUTC": None,
                "windowStartTime": None,
                "targetUrl": None,
                "isBeforeOrIdle": None,
                "isIdle": None,
                "lastMonitorUpdate": datetime.now().strftime("%H:%M:%S"),
                "error": None,
            }
            for i in range(1, num_sessions + 1)
        }

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n[MONITOR] Iniciando {num_sessions} sesiones en segundo plano (Headless=True)...")

    try:
        playwright_instance = await async_playwright().start()

        active_contexts.clear()
        active_tasks.clear()

        for i in range(1, num_sessions + 1):
            profile = PROFILE_DIR / f"session_{i:02d}"
            profile.mkdir(parents=True, exist_ok=True)

            context = await playwright_instance.chromium.launch_persistent_context(
                str(profile),
                headless=True,
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            active_contexts.append(context)

            task = async_loop.create_task(monitor_session(i, context, target_url))
            active_tasks.append(task)

        with app_lock:
            app_state["status"] = "RUNNING"

        print(f"[MONITOR] {num_sessions} sesiones activas en segundo plano. Monitoreando desde el dashboard.\n")

    except Exception as exc:
        print(f"[ERROR] Error al iniciar sesiones: {exc}")
        await stop_monitoring_async()


async def stop_monitoring_async():
    global active_contexts, active_tasks, playwright_instance

    with app_lock:
        app_state["status"] = "STOPPING"

    print("\n[MONITOR] Deteniendo sesiones activas...")

    for task in active_tasks:
        task.cancel()

    if active_tasks:
        await asyncio.gather(*active_tasks, return_exceptions=True)
    active_tasks.clear()

    for ctx in active_contexts:
        try:
            await ctx.close()
        except Exception:
            pass
    active_contexts.clear()

    if playwright_instance:
        try:
            await playwright_instance.stop()
        except Exception:
            pass
        playwright_instance = None

    with app_lock:
        app_state["status"] = "CONFIGURING"
        app_state["sessions"] = {}

    print("[MONITOR] Todas las sesiones detenidas. Monitor listo para nueva configuración.\n")


# ============================================================
# DASHBOARD HTML (FRONTEND COMPLETO: CONFIGURADOR + FILAS)
# ============================================================
HTML = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Boca Socios — Monitor de Fila Virtual</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

<style>
:root {
    --bg-main: #050811;
    --bg-header: #081020;
    --bg-card: #0b1528;
    --bg-row: #0d1930;
    --bg-row-hover: #132345;
    --border-color: #172847;
    --border-highlight: #254073;
    
    --boca-blue: #002b66;
    --boca-blue-light: #0d4bb5;
    --boca-gold: #ffb800;
    --boca-gold-glow: rgba(255, 184, 0, 0.4);
    
    --text-primary: #f0f4fc;
    --text-secondary: #8ca0c3;
    --text-muted: #576d91;

    --status-queue-bg: rgba(245, 158, 11, 0.12);
    --status-queue-text: #fbbf24;
    --status-queue-border: rgba(245, 158, 11, 0.3);

    --status-boca-bg: rgba(16, 185, 129, 0.14);
    --status-boca-text: #34d399;
    --status-boca-border: rgba(16, 185, 129, 0.35);

    --status-staging-bg: rgba(56, 189, 248, 0.12);
    --status-staging-text: #38bdf8;
    --status-staging-border: rgba(56, 189, 248, 0.3);

    --status-error-bg: rgba(239, 68, 68, 0.14);
    --status-error-text: #f87171;
    --status-error-border: rgba(239, 68, 68, 0.35);

    --status-other-bg: rgba(148, 163, 184, 0.1);
    --status-other-text: #94a3b8;
    --status-other-border: rgba(148, 163, 184, 0.2);
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

/* Pantalla 100% fija en viewport (CERO SCROLL DE PÁGINA) */
html, body {
    height: 100vh;
    max-height: 100vh;
    overflow: hidden;
    background-color: var(--bg-main);
    color: var(--text-primary);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    display: flex;
    flex-direction: column;
}

/* Scrollbar interna estilizada */
::-webkit-scrollbar {
    width: 5px;
    height: 5px;
}
::-webkit-scrollbar-track {
    background: #08101e;
}
::-webkit-scrollbar-thumb {
    background: #1c2e4f;
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
    background: var(--boca-gold);
}

/* Header compacto (46px) */
header {
    height: 46px;
    flex-shrink: 0;
    background: linear-gradient(180deg, var(--bg-header) 0%, rgba(8, 16, 32, 0.98) 100%);
    border-bottom: 1px solid var(--border-color);
    padding: 0 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 50;
}

.brand {
    display: flex;
    align-items: center;
    gap: 10px;
}

.boca-crest {
    width: 26px;
    height: 30px;
    background: linear-gradient(135deg, #001f4d 0%, #003380 48%, #ffb800 48%, #ffb800 100%);
    border-radius: 4px 4px 12px 12px;
    border: 1.5px solid var(--boca-gold);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 10px var(--boca-gold-glow);
    font-weight: 800;
    font-size: 8.5px;
    letter-spacing: 0.5px;
    color: #001f4d;
}

.brand-info h1 {
    font-size: 14px;
    font-weight: 800;
    letter-spacing: -0.3px;
    background: linear-gradient(90deg, #ffffff 0%, #ffcf55 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    line-height: 1.1;
}

.brand-info .tagline {
    color: var(--text-secondary);
    font-size: 10.5px;
}

.header-controls {
    display: flex;
    align-items: center;
    gap: 8px;
}

.clock-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    color: var(--boca-gold);
    background: rgba(255, 184, 0, 0.08);
    border: 1px solid rgba(255, 184, 0, 0.25);
    padding: 3px 8px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    gap: 6px;
}

.pulse-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background-color: #10b981;
    box-shadow: 0 0 6px #10b981;
    animation: pulse 1.8s infinite;
}

@keyframes pulse {
    0% { transform: scale(0.95); opacity: 0.8; }
    50% { transform: scale(1.3); opacity: 1; }
    100% { transform: scale(0.95); opacity: 0.8; }
}

.btn-header {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    padding: 4px 9px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 5px;
    transition: all 0.2s;
}

.btn-header:hover {
    background: rgba(255, 255, 255, 0.1);
    border-color: var(--border-highlight);
}

.btn-header.danger {
    background: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.4);
    color: #fca5a5;
}

.btn-header.danger:hover {
    background: rgba(239, 68, 68, 0.25);
    border-color: #ef4444;
}

/* Contenedor central 100% flexible y sin overflow exterior */
.main-container {
    flex: 1;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding: 8px 14px;
}

/* ============================================================ */
/* VISTA 1: CONFIGURADOR EN PANTALLA ÚNICA (ZERO SCROLL)       */
/* ============================================================ */

#view-config {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    width: 100%;
}

.config-card-compact {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 14px 20px;
    box-shadow: 0 6px 25px rgba(0, 0, 0, 0.45);
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 960px;
    width: 100%;
}

.config-top-banner {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border-color);
}

.config-top-title h2 {
    font-size: 17px;
    font-weight: 800;
    letter-spacing: -0.3px;
    color: #ffffff;
}

.config-top-title p {
    color: var(--text-secondary);
    font-size: 11px;
    margin-top: 2px;
}

.config-badge {
    background: rgba(255, 184, 0, 0.12);
    border: 1px solid rgba(255, 184, 0, 0.3);
    color: var(--boca-gold);
    font-size: 10.5px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 16px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
}

/* Selector Compacto con Presets Rápidos */
.selector-compact {
    display: flex;
    flex-direction: column;
    background: #08101e;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 8px 14px;
    gap: 8px;
}

.selector-top-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
}

.selector-left {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 160px;
}

.selector-label {
    font-size: 10.5px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.selector-slider-box {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 12px;
}

.btn-stepper-compact {
    width: 30px;
    height: 30px;
    border-radius: 6px;
    background: #111d38;
    border: 1px solid var(--border-color);
    color: var(--boca-gold);
    font-size: 16px;
    font-weight: 800;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
    flex-shrink: 0;
}

.btn-stepper-compact:hover {
    background: #182c56;
    border-color: var(--boca-gold);
}

.sessions-number-badge {
    font-size: 26px;
    font-weight: 800;
    font-family: 'JetBrains Mono', monospace;
    color: #ffffff;
    min-width: 38px;
    text-align: center;
    text-shadow: 0 0 10px rgba(255, 184, 0, 0.4);
    flex-shrink: 0;
}

.range-slider-compact {
    flex: 1;
    height: 6px;
    border-radius: 3px;
    background: #14223d;
    outline: none;
    -webkit-appearance: none;
    cursor: pointer;
}

.range-slider-compact::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--boca-gold);
    box-shadow: 0 0 8px var(--boca-gold);
    cursor: pointer;
}

/* Presets rápidos de 1 clic */
.presets-bar {
    display: flex;
    align-items: center;
    gap: 6px;
}

.presets-label {
    font-size: 10px;
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    margin-right: 4px;
}

.preset-pill {
    background: #0e1a32;
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: 10.5px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.15s;
}

.preset-pill:hover {
    background: #142447;
    color: var(--text-primary);
    border-color: var(--border-highlight);
}

.preset-pill.active {
    background: rgba(255, 184, 0, 0.15);
    border-color: var(--boca-gold);
    color: var(--boca-gold);
    font-weight: 700;
}

/* Grid dual compacta: Probabilidades vs Hardware */
.analytics-grid-compact {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}

.metric-panel-compact {
    background: #08101e;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}

.metric-header-compact {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
}

.metric-title-compact {
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 5px;
}

.chance-badge-compact {
    font-size: 9.5px;
    font-weight: 800;
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'JetBrains Mono', monospace;
}

.chance-badge-compact.high { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
.chance-badge-compact.medium { background: rgba(255, 184, 0, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }
.chance-badge-compact.low { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }

.big-percentage-compact {
    font-size: 26px;
    font-weight: 800;
    font-family: 'JetBrains Mono', monospace;
    color: #ffffff;
    line-height: 1.1;
    margin: 2px 0;
}

.progress-track-compact {
    height: 5px;
    background: #14213d;
    border-radius: 4px;
    overflow: hidden;
    margin: 4px 0 6px 0;
}

.progress-fill-compact {
    height: 100%;
    border-radius: 4px;
    background: linear-gradient(90deg, #f59e0b 0%, #10b981 100%);
    transition: width 0.2s ease;
}

.metric-desc-compact {
    font-size: 10.5px;
    color: var(--text-muted);
    line-height: 1.35;
}

.hw-row-compact {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    font-size: 11px;
}

.hw-row-compact:last-child {
    border-bottom: none;
}

.hw-val-compact {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    color: var(--text-primary);
}

.specs-bar-compact {
    display: flex;
    gap: 6px;
    margin-top: 6px;
    background: #0c172d;
    padding: 4px 8px;
    border-radius: 5px;
    font-size: 10px;
    color: var(--text-secondary);
    justify-content: space-around;
}

/* Botón de Inicio Compacto */
.config-action-compact {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-top: 6px;
    border-top: 1px solid var(--border-color);
    gap: 12px;
}

.btn-launch-compact {
    background: linear-gradient(135deg, #0d4bb5 0%, #002b66 100%);
    border: 1.5px solid var(--boca-gold);
    color: #ffffff;
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 0.3px;
    padding: 10px 24px;
    border-radius: 8px;
    cursor: pointer;
    box-shadow: 0 0 16px rgba(255, 184, 0, 0.3);
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    white-space: nowrap;
}

.btn-launch-compact:hover {
    transform: translateY(-1px);
    box-shadow: 0 0 24px rgba(255, 184, 0, 0.5);
    background: linear-gradient(135deg, #135ec4 0%, #003780 100%);
}

.headless-notice-compact {
    font-size: 11px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 6px;
}

/* ============================================================ */
/* VISTA 2: MONITOR DE FILAS EN PANTALLA ÚNICA (ZERO SCROLL)    */
/* ============================================================ */

#view-monitor {
    flex: 1;
    min-height: 0;
    display: none;
    flex-direction: column;
    overflow: hidden;
    gap: 6px;
}

/* Tira superior compacta de KPIs (34px de alto) */
.kpi-strip {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-shrink: 0;
}

.kpi-pill {
    flex: 1;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 4px 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 10.5px;
}

.kpi-pill-title {
    color: var(--text-secondary);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    display: flex;
    align-items: center;
    gap: 5px;
}

.kpi-pill-val {
    font-size: 14px;
    font-weight: 800;
    font-family: 'JetBrains Mono', monospace;
    color: #ffffff;
}

.kpi-pill.ready-turn {
    background: rgba(16, 185, 129, 0.15);
    border-color: #10b981;
}

.kpi-pill.best-choice {
    background: rgba(255, 184, 0, 0.12);
    border-color: var(--boca-gold);
}

/* Banner de Turno Compacto */
.turn-banner-compact {
    display: none;
    background: linear-gradient(90deg, rgba(16, 185, 129, 0.25) 0%, rgba(255, 184, 0, 0.2) 100%);
    border: 1.5px solid #10b981;
    border-radius: 6px;
    padding: 6px 12px;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
    animation: flashBorder 2s infinite ease-in-out;
}

@keyframes flashBorder {
    0%, 100% { border-color: #10b981; box-shadow: 0 0 10px rgba(16, 185, 129, 0.3); }
    50% { border-color: #ffb800; box-shadow: 0 0 16px rgba(255, 184, 0, 0.4); }
}

.turn-banner-compact.active {
    display: flex;
}

.turn-banner-title {
    font-size: 12px;
    font-weight: 800;
    color: #4ade80;
    display: flex;
    align-items: center;
    gap: 6px;
}

.btn-enter-compact {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    color: white;
    font-weight: 700;
    font-size: 11px;
    padding: 4px 12px;
    border-radius: 5px;
    border: none;
    cursor: pointer;
    box-shadow: 0 2px 8px rgba(16, 185, 129, 0.4);
    animation: pulse 1.5s infinite;
}

/* Barra de Filtros Compacta */
.table-toolbar-compact {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
    padding: 1px 0;
}

.filter-group-compact {
    display: flex;
    gap: 4px;
}

.filter-btn-compact {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: 10px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.15s;
}

.filter-btn-compact:hover {
    color: var(--text-primary);
    border-color: var(--border-highlight);
}

.filter-btn-compact.active {
    background: var(--boca-blue);
    border-color: var(--boca-gold);
    color: var(--boca-gold);
}

/* Contenedor de la Tabla que llena exactamente el espacio sobrante sin scroll exterior */
.table-viewport {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-card);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}

.sessions-table {
    width: 100%;
    border-collapse: collapse;
    text-align: left;
    font-size: 11px;
}

.sessions-table th {
    background: #08101e;
    color: var(--text-secondary);
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border-color);
    position: sticky;
    top: 0;
    z-index: 10;
}

.session-row {
    background: var(--bg-row);
    border-bottom: 1px solid var(--border-color);
    transition: background 0.15s;
    cursor: pointer;
}

.session-row:hover {
    background: var(--bg-row-hover);
}

.session-row.has-turn {
    background: rgba(16, 185, 129, 0.1);
    box-shadow: inset 3px 0 0 #10b981;
}

.session-row.is-best {
    background: rgba(255, 184, 0, 0.08);
    box-shadow: inset 3px 0 0 var(--boca-gold);
}

.sessions-table td {
    padding: 5px 10px;
    vertical-align: middle;
    white-space: nowrap;
}

.session-badge {
    background: rgba(255, 184, 0, 0.12);
    border: 1px solid rgba(255, 184, 0, 0.3);
    color: var(--boca-gold);
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 4px;
}

.best-badge {
    background: linear-gradient(135deg, rgba(255, 184, 0, 0.25) 0%, rgba(255, 184, 0, 0.1) 100%);
    border: 1px solid var(--boca-gold);
    color: var(--boca-gold);
    font-size: 9px;
    font-weight: 800;
    padding: 1px 4px;
    border-radius: 3px;
    letter-spacing: 0.3px;
}

.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 6px;
    border-radius: 12px;
    font-size: 9.5px;
    font-weight: 700;
    border: 1px solid transparent;
}

.status-pill.queue { background: var(--status-queue-bg); color: var(--status-queue-text); border-color: var(--status-queue-border); }
.status-pill.boca { background: var(--status-boca-bg); color: var(--status-boca-text); border-color: var(--status-boca-border); }
.status-pill.staging { background: var(--status-staging-bg); color: var(--status-staging-text); border-color: var(--status-staging-border); }
.status-pill.error { background: var(--status-error-bg); color: var(--status-error-text); border-color: var(--status-error-border); }
.status-pill.other { background: var(--status-other-bg); color: var(--status-other-text); border-color: var(--status-other-border); }

.cell-time {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    font-size: 11.5px;
    color: #ffffff;
}

.progress-box-compact {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 90px;
}

.progress-track-compact-row {
    flex: 1;
    height: 4px;
    background: #14213a;
    border-radius: 4px;
    overflow: hidden;
}

.progress-fill-compact-row {
    height: 100%;
    background: linear-gradient(90deg, #10b981 0%, var(--boca-gold) 100%);
    border-radius: 4px;
}

.btn-open-session {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    color: white;
    font-weight: 700;
    font-size: 10px;
    padding: 3px 8px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(16, 185, 129, 0.3);
    display: inline-flex;
    align-items: center;
    gap: 3px;
}

.btn-open-session:hover {
    background: linear-gradient(135deg, #34d399 0%, #059669 100%);
}

.btn-open-tab {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 4px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    transition: all 0.15s;
}

.btn-open-tab:hover {
    background: rgba(255, 255, 255, 0.1);
    color: var(--text-primary);
    border-color: var(--border-highlight);
}

/* Fila de detalles expandible */
.drawer-row {
    background: #070e1c;
    border-bottom: 1px solid var(--border-color);
    display: none;
}

.drawer-row.active {
    display: table-row;
}

.drawer-content {
    padding: 8px 12px;
}

.drawer-grid-compact {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 6px;
}

.drawer-card-compact {
    background: #0b1427;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    padding: 5px 8px;
}

.drawer-card-label {
    font-size: 9px;
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
}

.drawer-card-val {
    font-size: 10.5px;
    color: var(--text-primary);
    margin-top: 1px;
    word-break: break-all;
    font-family: 'JetBrains Mono', monospace;
}

.drawer-url-box-compact {
    margin-top: 6px;
    background: #0b1427;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    padding: 5px 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
}

.btn-mini-action-compact {
    background: rgba(255, 184, 0, 0.12);
    border: 1px solid rgba(255, 184, 0, 0.3);
    color: var(--boca-gold);
    font-size: 9.5px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 4px;
    cursor: pointer;
    flex-shrink: 0;
}
</style>
</head>

<body>

<header>
    <div class="brand">
        <div class="boca-crest">CABJ</div>
        <div class="brand-info">
            <h1>BOCA SOCIOS · MONITOR DE FILA</h1>
            <div class="tagline">Control multi-sesión silencioso (Headless) · Detección de turno en tiempo real</div>
        </div>
    </div>
    
    <div class="header-controls">
        <div class="clock-badge">
            <span class="pulse-dot"></span>
            <span id="live-clock">--:--:--</span>
        </div>

        <button id="btn-sound-toggle" class="btn-header" onclick="toggleSound()">
            🔔 Alarma: ON
        </button>

        <button id="btn-reconfigure" class="btn-header danger" style="display:none;" onclick="stopSessions()">
            ⚙️ Reconfigurar / Detener
        </button>
    </div>
</header>

<div class="main-container">

    <!-- ============================================================ -->
    <!-- PANTALLA 1: CONFIGURADOR COMPACTO (100% EN PANTALLA)         -->
    <!-- ============================================================ -->
    <div id="view-config">
        <div class="config-card-compact">
            <div class="config-top-banner">
                <div class="config-top-title">
                    <h2>Configuración de Sesiones de Espera</h2>
                    <p>Monitoreá los turnos sin abrir navegadores en tu pantalla. Ingresá a Boca con 1 clic en la que tenga menos tiempo.</p>
                </div>
                <div class="config-badge">
                    <span>🛡️</span> 100% HEADLESS (SILENCIOSO)
                </div>
            </div>

            <!-- Selector Compacto con Presets -->
            <div class="selector-compact">
                <div class="selector-top-row">
                    <div class="selector-left">
                        <span class="selector-label">Navegadores simultáneos</span>
                        <span style="font-size:10.5px; color:var(--text-secondary);">Seleccioná la cantidad a abrir</span>
                    </div>
                    <div class="selector-slider-box">
                        <button class="btn-stepper-compact" onclick="adjustSessions(-1)">−</button>
                        <span class="sessions-number-badge" id="num-sessions-val">5</span>
                        <button class="btn-stepper-compact" onclick="adjustSessions(1)">+</button>
                        <input type="range" class="range-slider-compact" id="session-slider" min="1" max="20" value="5" oninput="onSliderChange(this.value)">
                    </div>
                </div>

                <div class="presets-bar">
                    <span class="presets-label">Atajos rápidos:</span>
                    <button type="button" class="preset-pill" onclick="updateConfigStats(3)">3 sesiones</button>
                    <button type="button" class="preset-pill active" onclick="updateConfigStats(5)">5 (Recomendado)</button>
                    <button type="button" class="preset-pill" onclick="updateConfigStats(8)">8 sesiones</button>
                    <button type="button" class="preset-pill" onclick="updateConfigStats(10)">10 sesiones</button>
                    <button type="button" class="preset-pill" onclick="updateConfigStats(15)">15 sesiones</button>
                    <button type="button" class="preset-pill" onclick="updateConfigStats(20)">20 sesiones</button>
                </div>
            </div>

            <!-- Análisis en 2 Columnas -->
            <div class="analytics-grid-compact">
                <!-- Probabilidad -->
                <div class="metric-panel-compact">
                    <div>
                        <div class="metric-header-compact">
                            <span class="metric-title-compact">
                                <span>🎯</span> Probabilidad de Entrada
                            </span>
                            <span class="chance-badge-compact" id="chance-badge">BUENA</span>
                        </div>
                        <div class="big-percentage-compact" id="chance-pct">63.2%</div>
                        <div class="progress-track-compact">
                            <div class="progress-fill-compact" id="chance-bar" style="width: 63.2%;"></div>
                        </div>
                    </div>
                    <div class="metric-desc-compact" id="chance-desc">
                        En Queue-it, cada sesión independiente actúa como un número adicional en el sorteo inicial de la sala previa.
                    </div>
                </div>

                <!-- Hardware -->
                <div class="metric-panel-compact">
                    <div>
                        <div class="metric-header-compact">
                            <span class="metric-title-compact">
                                <span>⚡</span> Consumo de PC Estimado
                            </span>
                            <span class="chance-badge-compact high" id="resource-badge">🟢 LIGERO</span>
                        </div>

                        <div class="hw-row-compact">
                            <span style="color:var(--text-secondary);">RAM Proyectada:</span>
                            <span class="hw-val-compact" id="ram-est">~375 MB</span>
                        </div>
                        <div class="hw-row-compact">
                            <span style="color:var(--text-secondary);">Carga de CPU:</span>
                            <span class="hw-val-compact" id="cpu-est">~15% inicio / ~3% reposo</span>
                        </div>
                    </div>

                    <div class="specs-bar-compact">
                        <span>CPU: <strong id="sys-cores">8 Cores</strong></span>
                        <span>•</span>
                        <span>RAM Total: <strong id="sys-total-ram">8.0 GB</strong></span>
                        <span>•</span>
                        <span>RAM Libre: <strong id="sys-avail-ram">3.5 GB</strong></span>
                    </div>
                </div>
            </div>

            <!-- Botón de Lanzamiento -->
            <div class="config-action-compact">
                <div class="headless-notice-compact">
                    <span>💡</span>
                    <span>No te tapa la pantalla ni molesta: cuando toque tu turno hacés clic en entrar.</span>
                </div>
                <button class="btn-launch-compact" id="btn-start" onclick="startSessions()">
                    <span>🚀 INICIAR MONITOREO (5 SESIONES)</span>
                </button>
            </div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!-- PANTALLA 2: TABLERO EN PANTALLA ÚNICA (ZERO SCROLL)          -->
    <!-- ============================================================ -->
    <div id="view-monitor">
        <!-- Tira de KPIs compacta -->
        <div class="kpi-strip">
            <div class="kpi-pill">
                <span class="kpi-pill-title"><span>🖥️</span> Sesiones</span>
                <span class="kpi-pill-val" id="kpi-total">5</span>
            </div>
            <div class="kpi-pill">
                <span class="kpi-pill-title"><span>⏳</span> En Cola</span>
                <span class="kpi-pill-val" id="kpi-in-queue">0</span>
            </div>
            <div class="kpi-pill ready-turn">
                <span class="kpi-pill-title" style="color:#10b981;"><span>🎟️</span> Turnos Listos</span>
                <span class="kpi-pill-val" id="kpi-turns-ready" style="color:#10b981;">0</span>
            </div>
            <div class="kpi-pill best-choice">
                <span class="kpi-pill-title" style="color:var(--boca-gold);"><span>⭐</span> Mejor Sesión</span>
                <span class="kpi-pill-val" id="kpi-best-position" style="font-size:12px; color:var(--boca-gold);">—</span>
            </div>
            <div class="kpi-pill">
                <span class="kpi-pill-title"><span>🛡️</span> En Boca</span>
                <span class="kpi-pill-val" id="kpi-in-boca">0</span>
            </div>
        </div>

        <!-- Banner Compacto de Turno Listo -->
        <div id="turn-banner" class="turn-banner-compact">
            <div class="turn-banner-title">
                <span>🟢</span>
                <span id="turn-banner-title">¡TU TURNO COMENZÓ!</span>
            </div>
            <button class="btn-enter-compact" onclick="openTargetInBrowser()">
                INGRESAR A BOCA SOCIOS ➔
            </button>
        </div>

        <!-- Barra de Filtros Compacta -->
        <div class="table-toolbar-compact">
            <div class="filter-group-compact">
                <button class="filter-btn-compact active" onclick="setFilter('all', this)">Todas (<span id="count-all">0</span>)</button>
                <button class="filter-btn-compact" onclick="setFilter('queue', this)">En Cola (<span id="count-queue">0</span>)</button>
                <button class="filter-btn-compact" onclick="setFilter('ready', this)">Turno Listo (<span id="count-ready">0</span>)</button>
                <button class="filter-btn-compact" onclick="setFilter('boca', this)">En Boca (<span id="count-boca">0</span>)</button>
                <button class="filter-btn-compact" onclick="setFilter('error', this)">Errores (<span id="count-error">0</span>)</button>
            </div>

            <div style="display:flex; gap:4px;">
                <button class="filter-btn-compact" onclick="toggleAllDrawers(true)">Expandir Todos</button>
                <button class="filter-btn-compact" onclick="toggleAllDrawers(false)">Colapsar Todos</button>
            </div>
        </div>

        <!-- Tabla de Filas con Viewport Flexible (Zero scroll de pantalla) -->
        <div class="table-viewport">
            <table class="sessions-table">
                <thead>
                    <tr>
                        <th>Sesión</th>
                        <th>Estado Actual</th>
                        <th>Tiempo Estimado</th>
                        <th>Progreso</th>
                        <th>Gente Delante</th>
                        <th>N° de Cola</th>
                        <th>Estado Fila</th>
                        <th>Última Act.</th>
                        <th>Acción / Entrar</th>
                        <th>Detalles</th>
                    </tr>
                </thead>
                <tbody id="sessions-tbody">
                    <!-- Filas renderizadas -->
                </tbody>
            </table>
        </div>
    </div>

</div>

<script>
let selectedSessions = 5;
let activeFilter = "all";
const expandedSessions = new Set();
let soundEnabled = true;
const notifiedTurns = new Set();
let bannerTargetUrl = null;
let audioCtx = null;

function getAudioContext() {
    if (!audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (AudioContext) audioCtx = new AudioContext();
    }
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
    return audioCtx;
}

function playTurnChime() {
    if (!soundEnabled) return;
    try {
        const ctx = getAudioContext();
        if (!ctx) return;
        const now = ctx.currentTime;
        const notes = [523.25, 659.25, 783.99, 1046.50];
        notes.forEach((freq, idx) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'triangle';
            osc.frequency.setValueAtTime(freq, now + idx * 0.12);
            gain.gain.setValueAtTime(0.001, now + idx * 0.12);
            gain.gain.exponentialRampToValueAtTime(0.3, now + idx * 0.12 + 0.03);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.12 + 0.4);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(now + idx * 0.12);
            osc.stop(now + idx * 0.12 + 0.45);
        });
    } catch (e) {}
}

function toggleSound() {
    soundEnabled = !soundEnabled;
    const btn = document.getElementById('btn-sound-toggle');
    if (soundEnabled) {
        btn.innerHTML = '🔔 Alarma: ON';
        getAudioContext();
    } else {
        btn.innerHTML = '🔕 Alarma: OFF';
    }
}

function updateClock() {
    const clock = document.getElementById('live-clock');
    if (clock) clock.textContent = new Date().toTimeString().split(' ')[0];
}
setInterval(updateClock, 1000);
updateClock();

// ============================================================
// LÓGICA DEL CONFIGURADOR
// ============================================================

function calculateChances(n) {
    const pBase = 0.18;
    const pSuccess = 1 - Math.pow(1 - pBase, n);
    return Math.min(99.4, (pSuccess * 100));
}

function updateConfigStats(n) {
    selectedSessions = parseInt(n, 10);
    document.getElementById("num-sessions-val").textContent = selectedSessions;
    document.getElementById("session-slider").value = selectedSessions;
    document.getElementById("btn-start").innerHTML = `<span>🚀 INICIAR MONITOREO (${selectedSessions} SESIONES)</span>`;

    // Actualizar pills de presets
    document.querySelectorAll('.preset-pill').forEach(btn => {
        const text = btn.textContent;
        if (text.startsWith(String(selectedSessions) + " ") || text === String(selectedSessions)) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    const pct = calculateChances(selectedSessions);
    document.getElementById("chance-pct").textContent = pct.toFixed(1) + "%";
    document.getElementById("chance-bar").style.width = pct + "%";

    const chanceBadge = document.getElementById("chance-badge");
    const chanceDesc = document.getElementById("chance-desc");

    if (selectedSessions <= 2) {
        chanceBadge.className = "chance-badge-compact low";
        chanceBadge.textContent = "BAJA";
        chanceDesc.textContent = `Con ${selectedSessions} sola sesión dependés de un único número aleatorio en la sala previa.`;
    } else if (selectedSessions <= 5) {
        chanceBadge.className = "chance-badge-compact medium";
        chanceBadge.textContent = "BUENA / RECOMENDADA";
        chanceDesc.textContent = `Con ${selectedSessions} sesiones independientes en la sala previa, triplicás estadísticamente tus chances.`;
    } else if (selectedSessions <= 10) {
        chanceBadge.className = "chance-badge-compact high";
        chanceBadge.textContent = "MUY ALTA";
        chanceDesc.textContent = `Con ${selectedSessions} sesiones tenés una enorme ventaja para quedar en el primer lote de compra.`;
    } else {
        chanceBadge.className = "chance-badge-compact high";
        chanceBadge.textContent = "MÁXIMA VENTAJA";
        chanceDesc.textContent = `Con ${selectedSessions} sesiones maximizás casi al límite matemático tus opciones de entrada.`;
    }

    const ramMb = selectedSessions * 75;
    document.getElementById("ram-est").textContent = `~${ramMb} MB RAM`;
    document.getElementById("cpu-est").textContent = `~${selectedSessions * 3}% inicio / ~${Math.max(1, Math.round(selectedSessions * 0.6))}% reposo`;

    const resBadge = document.getElementById("resource-badge");
    if (selectedSessions <= 4) {
        resBadge.className = "chance-badge-compact high";
        resBadge.textContent = "🟢 LIGERO";
    } else if (selectedSessions <= 8) {
        resBadge.className = "chance-badge-compact high";
        resBadge.textContent = "🟢 MODERADO - ÓPTIMO";
    } else if (selectedSessions <= 14) {
        resBadge.className = "chance-badge-compact medium";
        resBadge.textContent = "🟡 INTENSO";
    } else {
        resBadge.className = "chance-badge-compact low";
        resBadge.textContent = "🔴 PESADO";
    }
}

function adjustSessions(delta) {
    let nextVal = selectedSessions + delta;
    if (nextVal < 1) nextVal = 1;
    if (nextVal > 20) nextVal = 20;
    updateConfigStats(nextVal);
}

function onSliderChange(val) {
    updateConfigStats(val);
}

async function startSessions() {
    const btn = document.getElementById("btn-start");
    btn.innerHTML = `<span>⏳ INICIANDO HEADLESS...</span>`;
    btn.style.opacity = "0.7";
    btn.disabled = true;

    try {
        await fetch("/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ num_sessions: selectedSessions })
        });
    } catch (e) {
        alert("Error al iniciar sesiones: " + e);
        btn.disabled = false;
        btn.style.opacity = "1";
        updateConfigStats(selectedSessions);
    }
}

async function stopSessions() {
    if (!confirm("¿Deseas detener el monitoreo y volver a la configuración?")) return;
    try {
        await fetch("/stop", { method: "POST" });
    } catch (e) {
        console.error(e);
    }
}

async function openInBrowser(url) {
    if (!url) return;
    try {
        await fetch("/open-browser", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: url })
        });
    } catch (e) {
        window.open(url, "_blank");
    }
}

function openTargetInBrowser() {
    if (bannerTargetUrl) openInBrowser(bannerTargetUrl);
}

// ============================================================
// LÓGICA DE LA TABLA DE MONITOREO
// ============================================================

function esc(value) {
    if (value === null || value === undefined || value === "") return "—";
    return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    const num = Number(value);
    if (isNaN(num)) return esc(value);
    return num.toLocaleString('es-AR');
}

function formatPercent(value) {
    if (value === null || value === undefined || value === "") return "—";
    const num = Number(value);
    if (isNaN(num)) return "—";
    return (num * 100).toFixed(1) + "%";
}

function getStatusClass(pageType) {
    if (!pageType) return "other";
    const pt = pageType.toUpperCase();
    if (pt.includes("QUEUE")) return "queue";
    if (pt.includes("BOCA")) return "boca";
    if (pt.includes("STAGING")) return "staging";
    if (pt.includes("ERROR")) return "error";
    return "other";
}

function setFilter(filter, btn) {
    activeFilter = filter;
    document.querySelectorAll('.filter-btn-compact').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    if (window.lastData) renderMonitor(window.lastData);
}

function toggleDrawer(sessionId, event) {
    if (event) event.stopPropagation();
    if (expandedSessions.has(sessionId)) expandedSessions.delete(sessionId);
    else expandedSessions.add(sessionId);
    if (window.lastData) renderMonitor(window.lastData);
}

function toggleAllDrawers(expand) {
    if (!window.lastData) return;
    Object.keys(window.lastData.sessions || {}).forEach(sid => {
        if (expand) expandedSessions.add(sid);
        else expandedSessions.delete(sid);
    });
    renderMonitor(window.lastData);
}

function renderMonitor(data) {
    const tbody = document.getElementById("sessions-tbody");
    if (!tbody) return;

    const sessions = Object.entries(data.sessions || {});
    let totalCount = sessions.length;
    let queueCount = 0;
    let turnsReadyCount = 0;
    let bocaCount = 0;
    let errorCount = 0;
    let bestAhead = Infinity;
    let bestSessionId = null;
    let bestSessionNumber = null;
    let turnBannerSession = null;

    sessions.forEach(([id, s]) => {
        const pt = (s.pageType || "").toUpperCase();
        const hasTurn = !!(s.windowStartTime || s.targetUrl);

        if (pt.includes("QUEUE")) queueCount++;
        if (pt.includes("BOCA")) bocaCount++;
        if (pt.includes("ERROR") || s.error) errorCount++;
        if (hasTurn) {
            turnsReadyCount++;
            if (!turnBannerSession) turnBannerSession = s;

            if (!notifiedTurns.has(id)) {
                notifiedTurns.add(id);
                playTurnChime();
            }
        }

        if (s.usersInLineAheadOfYou !== null && s.usersInLineAheadOfYou !== undefined) {
            const aheadNum = Number(s.usersInLineAheadOfYou);
            if (!isNaN(aheadNum) && aheadNum < bestAhead) {
                bestAhead = aheadNum;
                bestSessionId = id;
                bestSessionNumber = s.session;
            }
        }
    });

    document.getElementById("kpi-total").textContent = totalCount;
    document.getElementById("kpi-in-queue").textContent = queueCount;
    document.getElementById("kpi-turns-ready").textContent = turnsReadyCount;
    document.getElementById("kpi-in-boca").textContent = bocaCount;

    const bestPosEl = document.getElementById("kpi-best-position");
    if (turnsReadyCount > 0 && turnBannerSession) {
        bestPosEl.textContent = `¡S${String(turnBannerSession.session).padStart(2, '0')} LISTA!`;
    } else if (bestAhead !== Infinity) {
        bestPosEl.textContent = `S${String(bestSessionNumber).padStart(2, '0')} (${bestAhead.toLocaleString('es-AR')} pers.)`;
    } else {
        bestPosEl.textContent = "—";
    }

    document.getElementById("count-all").textContent = totalCount;
    document.getElementById("count-queue").textContent = queueCount;
    document.getElementById("count-ready").textContent = turnsReadyCount;
    document.getElementById("count-boca").textContent = bocaCount;
    document.getElementById("count-error").textContent = errorCount;

    const turnBanner = document.getElementById("turn-banner");
    if (turnBannerSession) {
        bannerTargetUrl = turnBannerSession.targetUrl || turnBannerSession.url;
        turnBanner.classList.add("active");
        document.getElementById("turn-banner-title").textContent = `🟢 ¡TURNO LISTO EN SESIÓN ${String(turnBannerSession.session).padStart(2, "0")}!`;
    } else {
        turnBanner.classList.remove("active");
        bannerTargetUrl = null;
    }

    const filtered = sessions.filter(([id, s]) => {
        const pt = (s.pageType || "").toUpperCase();
        const hasTurn = !!(s.windowStartTime || s.targetUrl);
        if (activeFilter === 'queue') return pt.includes('QUEUE');
        if (activeFilter === 'ready') return hasTurn;
        if (activeFilter === 'boca') return pt.includes('BOCA');
        if (activeFilter === 'error') return pt.includes('ERROR') || s.error;
        return true;
    });

    let rowsHtml = "";

    filtered.forEach(([id, s]) => {
        const isExpanded = expandedSessions.has(id);
        const pt = s.pageType || "INICIANDO";
        const hasTurn = !!(s.windowStartTime || s.targetUrl);
        const isBest = (id === bestSessionId) || (hasTurn && turnBannerSession && turnBannerSession.session === s.session);
        const progressVal = Number(s.progress || 0);
        const progressPct = Math.min(100, Math.max(0, progressVal * 100));

        let actionHtml = "";
        if (s.targetUrl) {
            actionHtml = `
                <button class="btn-open-session" onclick="event.stopPropagation(); openInBrowser('${esc(s.targetUrl)}')">
                    INGRESAR A BOCA ➔
                </button>
            `;
        } else {
            actionHtml = `
                <button class="btn-open-tab" onclick="event.stopPropagation(); openInBrowser('${esc(s.url)}')">
                    🌐 Abrir
                </button>
            `;
        }

        const queueStateText = s.queuePaused === true || s.queuePaused === "true" 
            ? `<span style="color:#f87171; font-weight:700;">⏸️ Pausada</span>`
            : (s.queueState ? esc(s.queueState) : "Normal");

        const bestTag = isBest ? `<span class="best-badge">⭐ MEJOR</span>` : "";

        rowsHtml += `
            <tr class="session-row ${hasTurn ? 'has-turn' : ''} ${isBest ? 'is-best' : ''}" onclick="toggleDrawer('${id}')">
                <td>
                    <div style="display:flex; align-items:center; gap:5px;">
                        <span class="session-badge">#${String(s.session).padStart(2, "0")}</span>
                        <strong style="color:#f0f4fc;">S${s.session}</strong>
                        ${bestTag}
                    </div>
                </td>
                <td>
                    <span class="status-pill ${getStatusClass(pt)}">
                        <span>●</span> ${esc(pt)}
                    </span>
                </td>
                <td><span class="cell-time">${esc(s.expectedServiceTime)}</span></td>
                <td>
                    <div class="progress-box-compact">
                        <div class="progress-track-compact-row">
                            <div class="progress-fill-compact-row" style="width: ${progressPct}%;"></div>
                        </div>
                        <span style="font-family:'JetBrains Mono',monospace; font-size:10px; color:#8ca0c3;">${formatPercent(s.progress)}</span>
                    </div>
                </td>
                <td><strong style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#ffffff;">${formatNumber(s.usersInLineAheadOfYou)}</strong></td>
                <td><span style="background:#08101e; border:1px solid #172847; padding:1px 5px; border-radius:4px; font-family:'JetBrains Mono',monospace; color:#cbd5e1; font-size:10px;">${esc(s.queueNumber)}</span></td>
                <td>${queueStateText}</td>
                <td><span style="font-family:'JetBrains Mono',monospace; font-size:10px; color:#8ca0c3;">${esc(s.lastMonitorUpdate || s.lastUpdated)}</span></td>
                <td>${actionHtml}</td>
                <td><span style="color:#8ca0c3; font-size:9px;">${isExpanded ? '▲' : '▼'}</span></td>
            </tr>

            <!-- Drawer desplegable -->
            <tr class="drawer-row ${isExpanded ? 'active' : ''}">
                <td colspan="10">
                    <div class="drawer-content">
                        <div class="drawer-grid-compact">
                            <div class="drawer-card-compact">
                                <div class="drawer-card-label">Event ID</div>
                                <div class="drawer-card-val">${esc(s.eventId)}</div>
                            </div>
                            <div class="drawer-card-compact">
                                <div class="drawer-card-label">Queue ID</div>
                                <div class="drawer-card-val">${esc(s.queueId)}</div>
                            </div>
                            <div class="drawer-card-compact">
                                <div class="drawer-card-label">Customer ID</div>
                                <div class="drawer-card-val">${esc(s.customerId)}</div>
                            </div>
                            <div class="drawer-card-compact">
                                <div class="drawer-card-label">Turno Comenzó</div>
                                <div class="drawer-card-val" style="color:#10b981; font-weight:700;">${esc(s.windowStartTime)}</div>
                            </div>
                        </div>

                        ${s.targetUrl ? `
                            <div class="drawer-url-box-compact">
                                <div style="overflow:hidden;">
                                    <div class="drawer-card-label" style="color:#10b981;">Target URL con Token de Compra</div>
                                    <div class="drawer-card-val" style="text-overflow:ellipsis; overflow:hidden;">${esc(s.targetUrl)}</div>
                                </div>
                                <button class="btn-mini-action-compact" onclick="openInBrowser('${esc(s.targetUrl)}')">Abrir</button>
                            </div>
                        ` : ''}

                        <div class="drawer-url-box-compact">
                            <div style="overflow:hidden;">
                                <div class="drawer-card-label">URL Actual</div>
                                <div class="drawer-card-val" style="text-overflow:ellipsis; overflow:hidden;">${esc(s.url)}</div>
                            </div>
                            <button class="btn-mini-action-compact" onclick="openInBrowser('${esc(s.url)}')">Abrir</button>
                        </div>

                        ${s.error ? `
                            <div style="margin-top:5px; padding:5px; background:rgba(239,68,68,0.15); border:1px solid #ef4444; border-radius:4px; color:#fca5a5; font-size:10.5px;">
                                <strong>Error:</strong> ${esc(s.error)}
                            </div>
                        ` : ''}
                    </div>
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = rowsHtml;
}

// Bucle de refresco
async function refresh() {
    try {
        const response = await fetch("/data");
        const data = await response.json();
        window.lastData = data;

        if (data.system) {
            document.getElementById("sys-cores").textContent = `${data.system.cores} Cores`;
            document.getElementById("sys-total-ram").textContent = `${data.system.total_ram_gb} GB`;
            document.getElementById("sys-avail-ram").textContent = `${data.system.avail_ram_gb} GB`;
        }

        if (data.status === "CONFIGURING") {
            document.getElementById("view-config").style.display = "flex";
            document.getElementById("view-monitor").style.display = "none";
            document.getElementById("btn-reconfigure").style.display = "none";
        } else {
            document.getElementById("view-config").style.display = "none";
            document.getElementById("view-monitor").style.display = "flex";
            document.getElementById("btn-reconfigure").style.display = "inline-flex";
            renderMonitor(data);
        }
    } catch (e) {
        console.error("Error al actualizar estado:", e);
    }
}

// Inicialización
updateConfigStats(5);
setInterval(refresh, 1000);
refresh();
</script>

</body>
</html>
"""

# ============================================================
# SERVIDOR HTTP REST & DASHBOARD
# ============================================================

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/":
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/data":
            payload = json.dumps(get_full_state(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req_data = {}

        if self.path == "/start":
            num_sessions = int(req_data.get("num_sessions", 5))
            target_url = req_data.get("start_url", START_URL)

            if async_loop:
                asyncio.run_coroutine_threadsafe(
                    start_monitoring_async(num_sessions, target_url),
                    async_loop
                )

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"starting"}')
            return

        if self.path == "/stop":
            if async_loop:
                asyncio.run_coroutine_threadsafe(
                    stop_monitoring_async(),
                    async_loop
                )

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"stopped"}')
            return

        if self.path == "/open-browser":
            target_url = req_data.get("url", "")
            if target_url:
                webbrowser.open(target_url)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"opened"}')
            return

        self.send_response(404)
        self.end_headers()


def start_dashboard():
    server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ============================================================
# BUCLE DE EVENTOS ASINCRÓNICO EN HILO
# ============================================================

def run_async_event_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


# ============================================================
# ENTRADA PRINCIPAL
# ============================================================

def main():
    global async_loop

    async_loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=run_async_event_loop, args=(async_loop,), daemon=True)
    loop_thread.start()

    dashboard_server = start_dashboard()
    dashboard_url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"

    print("\n" + "=" * 50)
    print(" [CABJ] BOCA SOCIOS - MONITOR DE FILA VIRTUAL [CABJ]")
    print("=" * 50)
    print(f"\nDashboard listo: {dashboard_url}")
    print("Configuración interactiva activa en el navegador.")
    print("Presiona Ctrl+C en esta consola para cerrar el monitor.\n")

    # Abrir automáticamente el dashboard en el navegador del usuario
    try:
        webbrowser.open(dashboard_url)
    except Exception:
        pass

    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("\nCerrando monitor y sesiones activas...")
        if async_loop:
            fut = asyncio.run_coroutine_threadsafe(stop_monitoring_async(), async_loop)
            try:
                fut.result(timeout=8)
            except Exception:
                pass
            async_loop.call_soon_threadsafe(async_loop.stop)

        try:
            dashboard_server.shutdown()
        except Exception:
            pass
        print("Monitor cerrado correctamente.")


if __name__ == "__main__":
    main()