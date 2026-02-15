# ucm_client.py - Client per l'API Grandstream UCM6202 (click-to-dial)
#
# Rubrica LDAP - Frontend web per la gestione di una rubrica telefonica LDAP
# Copyright (C) 2024
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <https://www.gnu.org/licenses/>.

import hashlib
import ssl
import time

import requests
import requests.adapters
import urllib3
from urllib3.util.ssl_ import create_urllib3_context

# Disabilita i warning SSL per il certificato self-signed del UCM
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LegacySSLAdapter(requests.adapters.HTTPAdapter):
    """Adapter HTTPS che abbassa il livello di sicurezza SSL.

    Il centralino Grandstream UCM6202 usa una chiave Diffie-Hellman
    troppo corta (DH_KEY_TOO_SMALL) che le versioni recenti di OpenSSL
    rifiutano. Questo adapter imposta SECLEVEL=1 per accettarla.
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


class UCMClient:
    """Client per il centralino Grandstream UCM6202.

    Gestisce l'autenticazione challenge/response e il click-to-dial
    tramite l'API HTTPS sulla porta 8089. La sessione viene mantenuta
    in cache e rinnovata automaticamente alla scadenza (5 minuti).
    """

    # Durata massima della sessione UCM (secondi) meno un margine di sicurezza
    SESSION_TIMEOUT = 270  # 4.5 minuti (il cookie scade dopo 5 minuti)

    def __init__(self, host, port, user, password):
        """Inizializza il client UCM.

        Args:
            host: indirizzo IP o hostname del centralino.
            port: porta HTTPS dell'API (di solito 8089).
            user: nome utente API (es. "cdrapi").
            password: password dell'utente API.
        """
        self.base_url = f"https://{host}:{port}/api"
        self.user = user
        self.password = password
        self._cookie = None
        self._cookie_time = 0
        self._session = requests.Session()
        self._session.mount('https://', LegacySSLAdapter())

    def _request(self, payload):
        """Invia una richiesta POST all'API UCM.

        Args:
            payload: dizionario con il corpo della richiesta.

        Returns:
            Dizionario con la risposta JSON.

        Raises:
            UCMError: se la richiesta fallisce o il server risponde con errore.
        """
        try:
            resp = self._session.post(
                self.base_url,
                json=payload,
                verify=False,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise UCMError(f"Errore di connessione al centralino: {e}")

        # L'API UCM restituisce status 0 per successo
        status = data.get("status")
        if status is not None and status != 0:
            msg = data.get("response", {}).get("message", f"Codice errore: {status}")
            raise UCMError(msg)

        return data

    def _authenticate(self):
        """Esegue il flusso challenge/login e salva il cookie di sessione.

        Raises:
            UCMError: se l'autenticazione fallisce.
        """
        # Passo 1: richiede il challenge
        challenge_resp = self._request({
            "request": {
                "action": "challenge",
                "user": self.user,
                "version": "1.0",
            }
        })
        challenge = challenge_resp.get("response", {}).get("challenge")
        if not challenge:
            raise UCMError("Il centralino non ha restituito un challenge valido")

        # Passo 2: calcola il token MD5(challenge + password) e fa login
        token = hashlib.md5((challenge + self.password).encode()).hexdigest()
        login_resp = self._request({
            "request": {
                "action": "login",
                "user": self.user,
                "token": token,
            }
        })
        cookie = login_resp.get("response", {}).get("cookie")
        if not cookie:
            raise UCMError("Autenticazione fallita: nessun cookie ricevuto")

        self._cookie = cookie
        self._cookie_time = time.time()

    def _get_cookie(self):
        """Restituisce un cookie di sessione valido, autenticandosi se necessario."""
        if self._cookie and (time.time() - self._cookie_time) < self.SESSION_TIMEOUT:
            return self._cookie
        self._authenticate()
        return self._cookie

    def dial_outbound(self, extension, external_number):
        """Avvia una chiamata click-to-dial.

        Fa squillare l'interno specificato; quando l'utente risponde,
        il centralino chiama il numero esterno.

        Args:
            extension: interno da far squillare (es. "1001").
            external_number: numero esterno da chiamare.

        Raises:
            UCMError: se la chiamata non puo' essere avviata.
        """
        cookie = self._get_cookie()
        try:
            self._request({
                "request": {
                    "action": "dialOutbound",
                    "cookie": cookie,
                    "caller": str(extension),
                    "outbound": str(external_number),
                }
            })
        except UCMError:
            # Il cookie potrebbe essere scaduto: riprova con una nuova sessione
            self._cookie = None
            cookie = self._get_cookie()
            self._request({
                "request": {
                    "action": "dialOutbound",
                    "cookie": cookie,
                    "caller": str(extension),
                    "outbound": str(external_number),
                }
            })


class UCMError(Exception):
    """Errore durante la comunicazione con il centralino UCM."""
