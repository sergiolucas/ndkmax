import httpx

import state
from config import URL_BASE, APP_KEY, AUTH_STR
from utils.logger import setup_logger

logger = setup_logger(__name__)

class Perfil:
    def __init__(self, credenciales: str):
        self.credenciales = credenciales      # "mail:pass"
        self.username, self.password = credenciales.split(":")
        self.sid = None
        self.profile_id = None  # pr = id del perfil de usuario
        self.valido = False

        self._login()

        self.usage_counter = 0 

    def _login(self):
        login_url = f"{URL_BASE}/get/login/{APP_KEY}"
        data = {"username": self.username, "password": self.password}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        resp = httpx.post(login_url, data=data, headers=headers)

        if resp.status_code != 200:
            return

        try:
            self.sid = resp.json()["result"]["sid"]
            self.valido = True
            self._fetch_profile_id()
        except KeyError:
            self.valido = False

    def _fetch_profile_id(self):
        """Obtiene el ID del perfil principal del usuario (campo 'pr' para v7)."""
        if not self.sid:
            return
        profiles_url = f"{URL_BASE}/get/user_profiles/{APP_KEY}/{self.sid}"
        try:
            resp = httpx.get(profiles_url)
            if resp.status_code == 200:
                data = resp.json()
                profiles = data.get("result", [])
                # Buscar el perfil principal (main_profile == 1), si no, usar el primero
                for p in profiles:
                    if p.get("main_profile") == 1:
                        self.profile_id = p["id"]
                        logger.info(f"[PERFIL] Profile ID (pr) obtenido: {self.profile_id} (perfil: {p.get('name', '?')})")
                        return
                # Si no hay main_profile, usar el primero
                if profiles:
                    self.profile_id = profiles[0]["id"]
                    logger.info(f"[PERFIL] Profile ID (pr) obtenido (primer perfil): {self.profile_id}")
            else:
                logger.warning(f"[PERFIL] Error obteniendo profiles: HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"[PERFIL] Excepción obteniendo profile_id: {e}")


class GestorPerfiles:
    def __init__(self,  instancias: dict):
        # lista de perfiles válidos
        self.instancias = list(instancias.values())
        self.index = 0

    def siguiente(self) -> Perfil:
        if not self.instancias:
            raise RuntimeError("No hay perfiles válidos")
        perfil = self.instancias[self.index]
        self.index = (self.index + 1) % len(self.instancias)
        perfil.usage_counter += 1
        return perfil


async def obtener_enlace(client, media_id: str, is_movie: bool, season=0, episode=0):
    if state.gestor is None:
        logger.error("El gestor de perfiles no está inicializado en state.")
        return []

    perfil = state.gestor.siguiente()
    sid = perfil.sid
    tipo = 0 if is_movie else 1

    url = f"{URL_BASE}/get/tv_hash_link_v7/{APP_KEY}/{sid}/{tipo}/{media_id}"
    data = {"auth": AUTH_STR, "season": season, "episode": episode, "pr": perfil.profile_id}
    headers = {
        "x-device-id": "ndkmax-server-01",
        "x-device-name": "NDKMAX Server",
        "x-device-source": "1",
    }

    logger.debug(f"[DIXMAX] POST {url}")
    logger.debug(f"[DIXMAX] Body: {data}")

    resp = await client.post(url, json=data, headers=headers)
    logger.info(f"[DIXMAX] Status: {resp.status_code} | Response: {resp.text[:500]}")

    if resp.status_code == 200:
        json_data = resp.json()
        result = json_data.get("data", [])
        return result if isinstance(result, list) else [result]
    return []
