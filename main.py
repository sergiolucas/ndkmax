import httpx
import time
from aiocron import crontab
from urllib.parse import unquote, quote
from fastapi import FastAPI, Response 
from fastapi.middleware.cors import CORSMiddleware

import state
from metadata.tmdb import TMDB
from utils.logger import setup_logger
from utils.stremio_parser import parse_manifest_to_qualities
from utils.dixmax import GestorPerfiles, Perfil, obtener_enlace
from utils.hls_proxy import fetch_and_rewrite_manifest, filter_manifest_by_quality, diagnose_manifest
from config import PERFILES, ROOT_PATH, IS_DEV, VERSION, ADDON_URL

logger = setup_logger(__name__)

limits = httpx.Limits(max_keepalive_connections=100, max_connections=500, keepalive_expiry=30)
timeout = httpx.Timeout(15.0, connect=5.0)

http_client = httpx.AsyncClient(
    timeout=timeout,
    limits=limits,
    follow_redirects=True,
    verify=False,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
)

app = FastAPI(root_path=f"/{ROOT_PATH}" if ROOT_PATH and not ROOT_PATH.startswith("/") else ROOT_PATH)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE = {}
LINK_CACHE = {}
MAX_CACHE_SIZE = 2000

async def ensure_cache_space():
    if len(CACHE) >= MAX_CACHE_SIZE:
        items_to_delete = sorted(CACHE.items(), key=lambda x: x[1]['last_access'])[:int(MAX_CACHE_SIZE * 0.1)]
        for key, _ in items_to_delete:
            del CACHE[key]
        logger.info(f"[CACHE CLEANUP] Espacio liberado. Eliminados: {len(items_to_delete)}")

async def get_or_fetch_content(url: str):
    now = time.time()
    if url in CACHE:
        CACHE[url]['last_access'] = now
        logger.info(f"[CACHE HIT] {url[:60]}...")
        return CACHE[url]['content'], 200, CACHE[url]['ctype']

    content, status, ctype = await fetch_and_rewrite_manifest(http_client, url)
    
    if status == 200 and content:
        await ensure_cache_space()
        CACHE[url] = {
            'content': content,
            'ctype': ctype,
            'last_access': now
        }
    
    return content, status, ctype

def actualizar_perfiles_periodicamente():
    state.INSTANCIAS = {} 
    for nombre, cred in PERFILES.items():
        p = Perfil(cred)
        if p.valido:
            state.INSTANCIAS[nombre] = p
    if state.INSTANCIAS:
        state.gestor = GestorPerfiles(state.INSTANCIAS)
        print(f"[INFO] Perfiles activos: {len(state.INSTANCIAS)}")
    else:
        logger.error("PERFILES: Ninguno válido.")

@app.on_event("startup")
async def startup_event():
    actualizar_perfiles_periodicamente()

@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()

@app.get("/proxy/filter")
async def proxy_filter_endpoint(url: str, bw: int):
    if not url: return Response(status_code=400)
    target_url = unquote(url)
    
    content, status, _ = await get_or_fetch_content(target_url)

    if status != 200 or not content:
        return Response(status_code=status or 404)
        
    filtered_content = filter_manifest_by_quality(content, bw)
    
    return Response(
        content=filtered_content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.get("/manifest.json")
async def get_manifest():
    return {
        "id": "ndk.ndkmax.ndk",
        "version": VERSION,
        "resources": ["stream"],
        "types": ["movie", "series"],
        "name": "NDKMAX",
        "catalogs": [],
        "behaviorHints": {"configurable": False},
    }

@app.get("/stream/{stream_type}/{stream_id}")
async def get_results(stream_type: str, stream_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    try:
        stream_id_clean = stream_id.replace(".json", "")
        metadata_provider = TMDB(http_client)
        media = await metadata_provider.get_metadata(stream_id_clean, stream_type)

        if not media: return {"streams": []}

        is_movie = (media.type == "movie")
        season = getattr(media, 'season', 0)
        episode = getattr(media, 'episode', 0)
        
        cache_key = f"{media.id}_{is_movie}_{season}_{episode}"

        if is_movie:
            titulo = media.titles[0]
            duracion = await metadata_provider.get_duration(media.id, media.type)
        else:
            titulo = f"{media.titles[0]} S{media.season}E{media.episode}"
            duracion = await metadata_provider.get_duration(media.id, media.type, media.season, media.episode)

        if cache_key in LINK_CACHE:
            logger.info(f"[LINK CACHE HIT] Recuperando enlace para {cache_key}")
            search_results = LINK_CACHE[cache_key]
        else:
            search_results = await obtener_enlace(http_client, media.id, is_movie=is_movie, season=season, episode=episode)
            if search_results:
                LINK_CACHE[cache_key] = search_results

        logger.info(f"[STREAM] search_results for {cache_key}: {search_results}")
        if not search_results: return {"streams": []}

        final_streams = []

        for result_url in search_results:
            logger.info(f"[STREAM] Fetching manifest from: {result_url}")
            content, status, _ = await get_or_fetch_content(result_url)
            logger.info(f"[STREAM] Manifest status={status}, content_len={len(content) if content else 0}, first_200={content[:200] if content else 'None'}")
            
            if status == 200 and content:
                streams = parse_manifest_to_qualities(result_url, titulo, duracion, content)
                logger.info(f"[STREAM] Parsed {len(streams)} streams from manifest")
                final_streams.extend(streams)

        return {"streams": final_streams}
    except Exception as e:
        logger.error(f"Error en streams: {e}")
        return {"streams": []}

@app.get("/debug/check/{stream_type}/{stream_id}")
async def debug_check(stream_type: str, stream_id: str):
    try:
        stream_id_clean = stream_id.replace(".json", "")
        metadata_provider = TMDB(http_client)
        media = await metadata_provider.get_metadata(stream_id_clean, stream_type)

        if not media:
            return {"error": "No metadata found"}

        is_movie = (media.type == "movie")
        season = getattr(media, 'season', 0)
        episode = getattr(media, 'episode', 0)
        titulo = media.titles[0] if is_movie else f"{media.titles[0]} S{media.season}E{media.episode}"

        # Raw Dixmax API call to see exactly what it returns
        from config import URL_BASE, APP_KEY, AUTH_STR
        perfil = state.gestor.siguiente()
        sid = perfil.sid
        tipo = 0 if is_movie else 1
        dixmax_url = f"{URL_BASE}/get/tv_hash_link_v7/{APP_KEY}/{sid}/{tipo}/{media.id}"
        dixmax_body = {"auth": AUTH_STR, "season": season, "episode": episode, "pr": perfil.profile_id}

        dixmax_resp = await http_client.post(dixmax_url, json=dixmax_body)
        dixmax_raw = dixmax_resp.json()

        search_results = dixmax_raw.get("data", [])
        if not isinstance(search_results, list):
            search_results = [search_results]

        # Classify URLs
        url_analysis = []
        for url in search_results:
            is_hls = url.endswith(".m3u8") or ".m3u8" in url
            is_forbidden = "forbidden" in url.lower()
            url_analysis.append({
                "url": url,
                "type": "hls" if is_hls else "direct",
                "is_forbidden": is_forbidden,
            })

        # Only run HLS diagnosis on actual .m3u8 URLs
        hls_diagnostics = []
        for url in search_results:
            if ".m3u8" in url:
                diag = await diagnose_manifest(http_client, url)
                hls_diagnostics.append(diag)

        # Diagnosis
        if all(u["is_forbidden"] for u in url_analysis):
            diagnosis = "FORBIDDEN - Dixmax devuelve forbidden.mp4 para todas las URLs. La API detecta acceso fuera de la app."
        elif any(u["is_forbidden"] for u in url_analysis):
            diagnosis = "PARTIAL_FORBIDDEN - Algunas URLs son forbidden, otras no."
        elif not hls_diagnostics:
            diagnosis = "NO_HLS - No hay URLs .m3u8, solo enlaces directos."
        else:
            any_blocked = any(
                s.get("status") in (401, 403)
                for d in hls_diagnostics
                for v in d.get("variants", [])
                for s in v.get("segments", [])
            )
            if any_blocked:
                diagnosis = "SEGMENTS_BLOCKED - Restricción de IP en segmentos."
            else:
                diagnosis = "ALL_OK - Todo accesible desde el servidor."

        return {
            "media": titulo,
            "dixmax_api_response": dixmax_raw,
            "urls": url_analysis,
            "hls_diagnostics": hls_diagnostics,
            "diagnosis": diagnosis,
            "perfil_usado": perfil.username,
        }
    except Exception as e:
        logger.error(f"Error en debug check: {e}")
        return {"error": str(e)}

@crontab("0 */4 * * *", start=not IS_DEV)
async def actualizar_perfiles():
    actualizar_perfiles_periodicamente()

@crontab("0 3 * * *", start=True)
async def validar_enlaces_diario():
    logger.info("[VALIDATOR] Iniciando comprobación diaria de enlaces...")
    keys_to_remove = []
    
    for key, urls in list(LINK_CACHE.items()):
        all_dead = True
        for url in urls:
            try:
                resp = await http_client.head(url, timeout=5.0)
                if resp.status_code == 200:
                    all_dead = False
                    break
            except Exception:
                continue
        
        if all_dead:
            keys_to_remove.append(key)

    for key in keys_to_remove:
        dead_urls = LINK_CACHE.pop(key, [])
        for url in dead_urls:
            if url in CACHE:
                del CACHE[url]
                
    if keys_to_remove:
        logger.info(f"[VALIDATOR] Eliminadas {len(keys_to_remove)} entradas caducadas.")
    else:
        logger.info("[VALIDATOR] Todos los enlaces en caché siguen activos.")

@crontab("* * * * *", start=not IS_DEV)
async def ping_service():
    try:
        async with httpx.AsyncClient() as client:
            await client.get(ADDON_URL)
    except httpx.RequestError as e:
        logger.error(f"Fallo en el ping al servicio: {e}")
