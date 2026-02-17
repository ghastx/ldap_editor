# pbx_monitor.py - Monitor chiamate in tempo reale via WebSocket
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

"""Monitor WebSocket per il centralino Grandstream UCM6202.

Si connette al centralino via WebSocket (wss://host:8089/websockify),
autentica con challenge/response MD5, si sottoscrive agli eventi
ExtensionStatus e ActiveCallStatus, e mantiene in memoria lo stato
corrente delle chiamate attive.
"""

import asyncio
import hashlib
import json
import logging
import ssl
import threading
import uuid
from collections import OrderedDict

import websockets

logger = logging.getLogger(__name__)

# Intervalli in secondi
HEARTBEAT_INTERVAL = 30
RECONNECT_DELAY = 10


def _make_ssl_context():
    """Crea un contesto SSL che accetta certificati self-signed."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    return ctx


def _transaction_id():
    """Genera un ID di transazione univoco."""
    return uuid.uuid4().hex[:16]


class PBXMonitor:
    """Monitor WebSocket per eventi chiamate del centralino UCM6202.

    Attributi pubblici (thread-safe tramite lock):
        active_calls: dict delle chiamate attive, indicizzato per uniqueid.
        extension_status: dict stato interni, indicizzato per extension.
    """

    def __init__(self, host, port, user, password):
        """Inizializza il monitor.

        Args:
            host: indirizzo IP o hostname del centralino.
            port: porta WebSocket (di solito 8089).
            user: nome utente API (es. "cdrapi").
            password: password dell'utente API.
        """
        self.ws_url = f"wss://{host}:{port}/websockify"
        self.user = user
        self.password = password
        self._ssl_ctx = _make_ssl_context()

        # Stato condiviso (protetto da lock per accesso da altri thread)
        self._lock = threading.Lock()
        self.active_calls = OrderedDict()
        self.extension_status = {}

        # Coda per notificare eventi ai consumer (es. SSE endpoint)
        self._event_queues = []
        self._queues_lock = threading.Lock()

        # Stato interno asyncio
        self._ws = None
        self._running = False
        self._loop = None
        self._thread = None

    # ------------------------------------------------------------------
    # API pubblica (thread-safe)
    # ------------------------------------------------------------------

    def get_active_calls(self):
        """Restituisce una copia delle chiamate attive correnti."""
        with self._lock:
            return dict(self.active_calls)

    def get_extension_status(self):
        """Restituisce una copia dello stato degli interni."""
        with self._lock:
            return dict(self.extension_status)

    def subscribe_events(self, queue):
        """Registra una coda per ricevere eventi (usato dall'endpoint SSE).

        Args:
            queue: oggetto queue.Queue su cui verranno messi i dict degli eventi.
        """
        with self._queues_lock:
            self._event_queues.append(queue)

    def unsubscribe_events(self, queue):
        """Rimuove una coda dalla lista dei subscriber."""
        with self._queues_lock:
            try:
                self._event_queues.remove(queue)
            except ValueError:
                pass

    def start(self):
        """Avvia il monitor in un thread background dedicato."""
        if self._thread and self._thread.is_alive():
            logger.warning("PBX monitor gia' in esecuzione")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="pbx-monitor", daemon=True
        )
        self._thread.start()
        logger.info("PBX monitor avviato (thread background)")

    def stop(self):
        """Ferma il monitor e attende la chiusura del thread."""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("PBX monitor fermato")

    # ------------------------------------------------------------------
    # Loop asyncio (eseguito nel thread dedicato)
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Crea un event loop asyncio e avvia il ciclo di connessione."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connection_loop())
        except Exception:
            logger.exception("Errore fatale nel loop PBX monitor")
        finally:
            self._loop.close()

    async def _connection_loop(self):
        """Ciclo di connessione con riconnessione automatica."""
        while self._running:
            try:
                await self._connect_and_run()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                logger.warning("Connessione PBX persa: %s", e)
            except Exception:
                logger.exception("Errore imprevisto nella connessione PBX")
            finally:
                # Pulisce lo stato quando la connessione cade per evitare
                # dati stale (le chiamate attive non sono piu' verificabili)
                with self._lock:
                    self.active_calls.clear()
                    self.extension_status.clear()

            if self._running:
                logger.info(
                    "Riconnessione al PBX tra %d secondi...", RECONNECT_DELAY
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_run(self):
        """Connessione, autenticazione, sottoscrizione e ricezione eventi."""
        logger.info("Connessione a %s ...", self.ws_url)

        async with websockets.connect(
            self.ws_url,
            ssl=self._ssl_ctx,
            ping_interval=None,  # heartbeat gestito manualmente
            open_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connesso a %s", self.ws_url)

            # Autenticazione challenge/response
            await self._authenticate(ws)

            # Sottoscrizione eventi
            await self._subscribe(ws)

            # Avvia heartbeat in background
            heartbeat_task = asyncio.ensure_future(self._heartbeat_loop(ws))
            try:
                await self._receive_loop(ws)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

    # ------------------------------------------------------------------
    # Protocollo WebSocket Grandstream
    # ------------------------------------------------------------------

    async def _send(self, ws, message_body):
        """Invia un messaggio JSON al PBX con transactionid automatico.

        Args:
            ws: connessione WebSocket.
            message_body: dict con action e parametri.

        Returns:
            Il transactionid usato.
        """
        tid = _transaction_id()
        message_body["transactionid"] = tid
        payload = {"type": "request", "message": message_body}
        raw = json.dumps(payload)
        logger.debug("TX: %s", raw)
        await ws.send(raw)
        return tid

    async def _recv_response(self, ws, timeout=10):
        """Riceve e decodifica un messaggio JSON dal PBX.

        Args:
            ws: connessione WebSocket.
            timeout: secondi massimi di attesa.

        Returns:
            Il dict decodificato.
        """
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        logger.debug("RX: %s", raw)
        return json.loads(raw)

    async def _authenticate(self, ws):
        """Esegue il flusso challenge/login.

        Raises:
            RuntimeError: se l'autenticazione fallisce.
        """
        # Passo 1: richiesta challenge
        await self._send(ws, {
            "action": "challenge",
            "username": self.user,
            "version": "1",
        })
        resp = await self._recv_response(ws)
        challenge = resp.get("response", {}).get("challenge")
        if not challenge:
            raise RuntimeError(
                f"Challenge non ricevuto dal PBX: {resp}"
            )
        logger.info("Challenge ricevuto: %s", challenge)

        # Passo 2: login con token MD5(challenge + password)
        token = hashlib.md5(
            (challenge + self.password).encode()
        ).hexdigest()
        await self._send(ws, {
            "action": "login",
            "token": token,
            "url": self.ws_url,
        })
        resp = await self._recv_response(ws)
        status = resp.get("status")
        if status != 0:
            raise RuntimeError(
                f"Login PBX fallito (status={status}): {resp}"
            )
        logger.info("Autenticazione PBX riuscita")

    async def _subscribe(self, ws):
        """Si sottoscrive agli eventi ExtensionStatus e ActiveCallStatus."""
        await self._send(ws, {
            "action": "subscribe",
            "eventnames": ["ExtensionStatus", "ActiveCallStatus"],
        })
        resp = await self._recv_response(ws)
        status = resp.get("status")
        if status != 0:
            logger.warning("Subscribe PBX status=%s: %s", status, resp)
        else:
            logger.info("Sottoscritto a ExtensionStatus e ActiveCallStatus")

    async def _heartbeat_loop(self, ws):
        """Invia heartbeat periodici per mantenere la sessione aperta."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._send(ws, {"action": "heartbeat"})
                logger.debug("Heartbeat inviato")
            except Exception:
                logger.warning("Errore invio heartbeat")
                break

    # ------------------------------------------------------------------
    # Ricezione e gestione eventi
    # ------------------------------------------------------------------

    async def _receive_loop(self, ws):
        """Riceve messaggi dal PBX e li smista al gestore appropriato."""
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Messaggio non JSON dal PBX: %s", raw[:200])
                continue

            logger.debug("RX: %s", json.dumps(data, ensure_ascii=False)[:500])

            msg = data.get("message", {})
            action = msg.get("action", "")

            if action == "notify":
                eventname = msg.get("eventname", "")
                if eventname == "ExtensionStatus":
                    self._handle_extension_status(msg)
                elif eventname == "ActiveCallStatus":
                    self._handle_active_call_status(msg)
                else:
                    logger.debug("Evento notify sconosciuto: %s", eventname)

    def _handle_extension_status(self, msg):
        """Gestisce un evento ExtensionStatus.

        Aggiorna lo stato degli interni e notifica i subscriber.
        """
        eventbody = msg.get("eventbody", [])
        for entry in eventbody:
            ext = entry.get("extension", "")
            status = entry.get("status", "")
            if ext:
                with self._lock:
                    self.extension_status[ext] = status
                logger.info(
                    "ExtensionStatus: interno %s -> %s", ext, status
                )
                self._broadcast_event({
                    "event": "extension_status",
                    "extension": ext,
                    "status": status,
                })

    def _handle_active_call_status(self, msg):
        """Gestisce un evento ActiveCallStatus.

        Gestisce i sotto-eventi:
        - chantype=bridge (chiamata connessa): action add/update/delete
        - chantype=unbridge (squillo/hangup): action add/update/delete
        """
        eventbody = msg.get("eventbody", [])
        for entry in eventbody:
            chantype = entry.get("chantype", "")
            action = entry.get("action", "")
            uniqueid = entry.get("uniqueid", "")

            if chantype == "unbridge":
                self._handle_unbridge(action, entry, uniqueid)
            elif chantype == "bridge":
                self._handle_bridge(action, entry, uniqueid)
            else:
                logger.debug(
                    "ActiveCallStatus chantype sconosciuto: %s", chantype
                )

    def _handle_unbridge(self, action, entry, uniqueid):
        """Gestisce eventi unbridge (squillo, riaggancio)."""
        state = entry.get("state", "")
        callernum = entry.get("callernum", "")
        connectednum = entry.get("connectednum", "")
        callername = entry.get("callername", "")

        if action in ("add", "update"):
            if state in ("Ring", "Ringing"):
                call_info = {
                    "uniqueid": uniqueid,
                    "state": "ringing",
                    "callernum": callernum,
                    "connectednum": connectednum,
                    "callername": callername,
                }
                with self._lock:
                    self.active_calls[uniqueid] = call_info

                logger.info(
                    "Squillo: %s (%s) -> %s",
                    callernum, callername, connectednum,
                )
                self._broadcast_event({
                    "event": "call_ring",
                    "uniqueid": uniqueid,
                    "callernum": callernum,
                    "connectednum": connectednum,
                    "callername": callername,
                })

            elif state == "Down":
                with self._lock:
                    removed = self.active_calls.pop(uniqueid, None)
                if removed:
                    logger.info("Chiamata terminata (down): %s", uniqueid)
                    self._broadcast_event({
                        "event": "call_hangup",
                        "uniqueid": uniqueid,
                    })

        elif action == "delete":
            with self._lock:
                removed = self.active_calls.pop(uniqueid, None)
            if removed:
                logger.info("Chiamata rimossa (unbridge delete): %s", uniqueid)
                self._broadcast_event({
                    "event": "call_hangup",
                    "uniqueid": uniqueid,
                })

    def _handle_bridge(self, action, entry, uniqueid):
        """Gestisce eventi bridge (chiamata connessa)."""
        if action in ("add", "update"):
            callerid1 = entry.get("callerid1", "")
            callerid2 = entry.get("callerid2", "")
            name1 = entry.get("name1", "")
            name2 = entry.get("name2", "")
            bridge_time = entry.get("bridge_time", "")

            call_info = {
                "uniqueid": uniqueid,
                "state": "connected",
                "callerid1": callerid1,
                "callerid2": callerid2,
                "name1": name1,
                "name2": name2,
                "bridge_time": bridge_time,
            }
            with self._lock:
                self.active_calls[uniqueid] = call_info

            logger.info(
                "Chiamata connessa: %s (%s) <-> %s (%s)",
                callerid1, name1, callerid2, name2,
            )
            self._broadcast_event({
                "event": "call_connect",
                "uniqueid": uniqueid,
                "callerid1": callerid1,
                "callerid2": callerid2,
                "name1": name1,
                "name2": name2,
                "bridge_time": bridge_time,
            })

        elif action == "delete":
            with self._lock:
                removed = self.active_calls.pop(uniqueid, None)
            if removed:
                logger.info("Chiamata terminata (bridge delete): %s", uniqueid)
                self._broadcast_event({
                    "event": "call_hangup",
                    "uniqueid": uniqueid,
                })

    # ------------------------------------------------------------------
    # Broadcasting eventi ai subscriber (code thread-safe)
    # ------------------------------------------------------------------

    def _broadcast_event(self, event):
        """Invia un evento a tutte le code registrate.

        Le code piene vengono svuotate per evitare blocchi.
        """
        with self._queues_lock:
            for q in self._event_queues:
                try:
                    q.put_nowait(event)
                except Exception:
                    pass
