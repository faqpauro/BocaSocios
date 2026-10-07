# Boca Queue Monitor

Monitor de una sesión normal de navegador para la fila virtual de Boca/Queue-it.

## Qué hace

- Abre Chromium con un perfil persistente.
- Mantiene cookies/local storage de ESA única sesión.
- Detecta cambios de URL.
- Intenta clasificar la pantalla como:
  - PRE-COLA
  - COLA
  - CAPTCHA
  - LIBERADO
  - BOCA / DESTINO
- Registra errores HTTP de requests XHR/fetch de Queue-it.
- Guarda una captura cuando cambia de estado.
- Registra todo en `queue_monitor.log`.

## Qué NO hace

- No crea múltiples identidades.
- No rota IPs/proxies.
- No falsifica cookies o tokens.
- No intenta alterar la posición.
- No automatiza CAPTCHA.
- No hace requests paralelos para conseguir múltiples lugares.

## Instalación

Windows:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
```

## Uso

```powershell
py monitor.py
```

Se abrirá Chromium. Para la venta de un evento concreto, pegá en `START_URL` la URL oficial que corresponda al evento.

El primer arranque crea la carpeta `boca_profile`. Esa carpeta contiene la sesión persistente del navegador.

## Capturas

Las capturas quedan en:

`screenshots/`

## Próximo paso

Durante una prueba real podemos agregar un detector específico de los endpoints XHR/fetch que utilice el evento de Boca, una vez que se capture una sesión legítima desde DevTools/Network.
