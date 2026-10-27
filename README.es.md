# app-console-mcp

**Google Play Console, App Store Connect y AdMob desde la terminal o desde Claude, sin navegador.**

[English](README.md)

Manejar las consolas de las tiendas con un agente de navegador gasta tokens en capturas y copias de página, y se
rompe cada vez que cambia la interfaz. Este proyecto usa las API oficiales y responde en unas pocas líneas de texto.
Cada herramienta existe dos veces, con la misma salida:

- como **herramienta MCP** para Claude Code, Claude Desktop o cualquier cliente MCP, y
- como **comando** (`appconsole ...`) para ti, tus scripts y tu CI.

Se instala como tres plugins de Claude Code (Google Play, App Store Connect y AdMob), cada uno con su servidor
MCP y una skill que le enseña a Claude los flujos seguros.

## Qué hace

- **Google Play**: fichas (título, descripción breve y completa, idiomas nuevos), subir .aab/.apk a cualquier pista,
  promover entre pistas, lanzamientos graduales (cambiar %, completar, detener), reseñas y respuestas, Android vitals
  (cierres y ANR con las cifras de 28 días de Google), suscripciones y precios, y posición en la búsqueda.
- **App Store Connect**: versiones y estado de revisión, textos de la ficha (nombre, subtítulo, keywords,
  descripción, texto promocional, novedades), crear versión, builds, adjuntar build y enviar a App Review, publicar
  una versión aprobada, reseñas y respuestas, ventas (descargas, compras, ingresos) y posición en la búsqueda.
- **AdMob**: ganancias, eCPM, tasa de coincidencia y de visualización por app, bloque, país, formato, plataforma,
  fecha, mes o fuente de anuncios, comparadas con el periodo anterior; y la lista de apps y bloques.

**Seguro por defecto**: todo lo que cambia algo es un ensayo si no pasas `confirm=true` (`--confirm` en la CLI).
En Google Play el ensayo arma el cambio en un borrador y Google lo valida antes de descartarlo.

## Instalar

Necesitas [uv](https://docs.astral.sh/uv/getting-started/installation/).

En Claude Code:

```text
/plugin marketplace add Alekla0126/app-console-mcp
/plugin install google-play@app-console
/plugin install app-store-connect@app-console
/plugin install admob@app-console
```

Solo la CLI: `uv tool install git+https://github.com/Alekla0126/app-console-mcp` y luego `appconsole --help`.

Para otros clientes MCP y todos los detalles, mira el [README en inglés](README.md#install).

## Credenciales (resumen)

- **Google Play**: cuenta de servicio de Google Cloud con las API *Google Play Android Developer* y *Play Developer
  Reporting* activadas, invitada en Play Console > Usuarios y permisos.
  `appconsole config set GOOGLE_PLAY_SERVICE_ACCOUNT /ruta/llave.json`
- **App Store Connect**: llave de la API (Usuarios y acceso > Integraciones). `ASC_KEY_ID`, `ASC_ISSUER_ID` y
  `ASC_PRIVATE_KEY_PATH` (o `ASC_API_KEY_JSON` si ya usas fastlane). Para ventas, `ASC_VENDOR_NUMBER`.
- **AdMob**: no acepta cuentas de servicio. Crea un cliente OAuth tipo *App de escritorio* (pantalla de
  consentimiento en *Producción* para que el token no caduque a los 7 días) y corre una sola vez
  `appconsole admob login --client-secrets client_secret.json`.

`appconsole doctor` revisa los tres servicios.

## Licencia

MIT. Sin relación con Google ni Apple.
