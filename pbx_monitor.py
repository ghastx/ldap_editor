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
import sqlite3
import ssl
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from logging.handlers import RotatingFileHandler

import websockets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Logger dedicato per i messaggi raw del PBX.
# Scrive su file pbx_raw.log TUTTI i messaggi in arrivo e in uscita,
# senza troncamenti, per analisi e debug del protocollo.
# Il file ruota a 10 MB con 3 backup (totale max ~40 MB).
# ---------------------------------------------------------------------------
pbx_raw_logger = logging.getLogger("pbx_raw")
pbx_raw_logger.setLevel(logging.DEBUG)
pbx_raw_logger.propagate = False  # non inquina il log principale
_raw_handler = RotatingFileHandler(
    "pbx_raw.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_raw_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s.%(msecs)03d  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
)
pbx_raw_logger.addHandler(_raw_handler)

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


def _pretty_json(obj):
    """Formatta un oggetto come JSON indentato per il log raw."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


class PBXMonitor:
    """Monitor WebSocket per eventi chiamate del centralino UCM6202.

    Attributi pubblici (thread-safe tramite lock):
        active_calls: dict delle chiamate attive, indicizzato per linkedid.
        extension_status: dict stato interni, indicizzato per extension.

    Le chiamate sono raggruppate per linkedid (il PBX assegna lo stesso
    linkedid a tutti i canali di una stessa chiamata, inclusi ring group).
    """

    def __init__(self, host, port, user, password, db_path=None):
        """Inizializza il monitor.

        Args:
            host: indirizzo IP o hostname del centralino.
            port: porta WebSocket (di solito 8089).
            user: nome utente di accesso al PBX (es. "adminpbx").
            password: password dell'utente PBX.
            db_path: percorso del database SQLite per il registro chiamate.
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

        # Mapping interni per tracciare i canali di ogni chiamata.
        # Acceduti solo dal thread asyncio, non serve lock.
        # channel_map: channel_name → linkedid
        # call_channels: linkedid → set(channel_name)
        self._channel_map = {}
        self._call_channels = {}
        # Set di linkedid di chiamate in ingresso esterne.
        # Popolato quando si riceve un evento trunk con inbound_trunk_name.
        # Accesso solo dal thread asyncio, non serve lock.
        self._incoming_linkedids = set()

        # Database per il registro chiamate (connessione creata nel thread)
        self._db_path = db_path
        self._db = None
        # Metadata temporanea per le chiamate in corso (linkedid → dict)
        # Usato per calcolare la durata alla fine della chiamata.
        self._call_log_meta = {}

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
        self._init_call_log_db()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connection_loop())
        except Exception:
            logger.exception("Errore fatale nel loop PBX monitor")
        finally:
            if self._db:
                self._db.close()
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
                self._channel_map.clear()
                self._call_channels.clear()
                self._incoming_linkedids.clear()
                self._call_log_meta.clear()

            if self._running:
                logger.info(
                    "Riconnessione al PBX tra %d secondi...", RECONNECT_DELAY
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_run(self):
        """Connessione, autenticazione, sottoscrizione e ricezione eventi."""
        logger.info("Connessione a %s ...", self.ws_url)
        pbx_raw_logger.info("=" * 60)
        pbx_raw_logger.info("NUOVA SESSIONE - Connessione a %s", self.ws_url)
        pbx_raw_logger.info("=" * 60)

        async with websockets.connect(
            self.ws_url,
            ssl=self._ssl_ctx,
            ping_interval=None,  # heartbeat gestito manualmente
            open_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connesso a %s", self.ws_url)
            pbx_raw_logger.info("WebSocket connesso")

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
                pbx_raw_logger.info("Sessione chiusa")

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
        pbx_raw_logger.debug(">>> TX >>>\n%s", _pretty_json(payload))
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
        try:
            parsed = json.loads(raw)
            pbx_raw_logger.debug("<<< RX <<<\n%s", _pretty_json(parsed))
        except json.JSONDecodeError:
            pbx_raw_logger.debug("<<< RX (non-JSON) <<<\n%s", raw)
            parsed = json.loads(raw)  # rilancia l'eccezione
        return parsed

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
        challenge = (
            resp.get("message", {}).get("challenge")
            or resp.get("response", {}).get("challenge")
        )
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
            "username": self.user,
        })
        resp = await self._recv_response(ws)
        msg = resp.get("message", {})
        status = msg.get("status") if isinstance(msg, dict) else resp.get("status")
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
        msg = resp.get("message", {})
        status = msg.get("status") if isinstance(msg, dict) else resp.get("status")
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
        """Riceve messaggi dal PBX e li smista al gestore appropriato.

        Il PBX puo' inviare "message" sia come singolo oggetto che come
        array di oggetti (notifiche multiple nello stesso frame).
        """
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Messaggio non JSON dal PBX: %s", raw[:200])
                pbx_raw_logger.warning(
                    "<<< RX (non-JSON) <<<\n%s", raw
                )
                continue

            logger.debug("RX: %s", json.dumps(data, ensure_ascii=False)[:500])

            # ---- LOG RAW COMPLETO (senza troncamenti) ----
            pbx_raw_logger.info(
                "<<< RX <<<\n%s", _pretty_json(data)
            )

            raw_msg = data.get("message", {})
            # Il PBX puo' mandare message come dict o come lista di dict
            messages = raw_msg if isinstance(raw_msg, list) else [raw_msg]

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                action = msg.get("action", "")
                if action == "notify":
                    eventname = msg.get("eventname", "")

                    # ---- LOG DETTAGLIATO PER EVENTO ----
                    eventbody = msg.get("eventbody", [])
                    pbx_raw_logger.info(
                        "--- EVENTO: %s  (%d entries) ---",
                        eventname, len(eventbody) if isinstance(eventbody, list) else 1,
                    )
                    for i, entry in enumerate(
                        eventbody if isinstance(eventbody, list) else [eventbody]
                    ):
                        pbx_raw_logger.info(
                            "  entry[%d]:\n%s", i, _pretty_json(entry)
                        )

                    if eventname == "ExtensionStatus":
                        self._handle_extension_status(msg)
                    elif eventname == "ActiveCallStatus":
                        self._handle_active_call_status(msg)
                    else:
                        logger.debug("Evento notify sconosciuto: %s", eventname)
                        pbx_raw_logger.info(
                            "Evento notify NON gestito: %s", eventname
                        )
                elif action:
                    # Log anche di azioni non-notify (heartbeat response, ecc.)
                    pbx_raw_logger.debug(
                        "--- AZIONE: %s ---\n%s", action, _pretty_json(msg)
                    )

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
        # Ordina: prima i canali trunk (con inbound_trunk_name) cosi' il
        # loro linkedid viene registrato in _incoming_linkedids prima di
        # processare i canali extension corrispondenti.
        eventbody = sorted(
            eventbody,
            key=lambda e: (0 if e.get("inbound_trunk_name") else 1),
        )
        for entry in eventbody:
            chantype = entry.get("chantype", "")
            action = entry.get("action", "")
            uniqueid = entry.get("uniqueid", "")

            # ---- LOG DECISIONALE ----
            pbx_raw_logger.info(
                "  PROCESS: chantype=%s action=%s uniqueid=%s linkedid=%s channel=%s ch1=%s ch2=%s",
                chantype, action, uniqueid,
                entry.get("linkedid", ""),
                entry.get("channel", ""),
                entry.get("channel1", ""),
                entry.get("channel2", ""),
            )

            if chantype == "unbridge":
                self._handle_unbridge(action, entry, uniqueid)
            elif chantype == "bridge":
                self._handle_bridge(action, entry, uniqueid)
            else:
                logger.debug(
                    "ActiveCallStatus chantype sconosciuto: %s", chantype
                )

    def _handle_unbridge(self, action, entry, uniqueid):
        """Gestisce eventi unbridge (squillo, riaggancio).

        Le chiamate sono raggruppate per linkedid. Per un ring group,
        il PBX invia un evento per ogni interno che squilla, tutti con
        lo stesso linkedid. Mostriamo UNA sola entry per chiamata.

        Nei canali extension il PBX inverte la prospettiva:
        - callernum = interno che squilla (es. "1000")
        - connectednum = chiamante esterno (es. "3283259080")
        Per il frontend serviamo il chiamante esterno come callernum.

        Gli eventi delete contengono solo "channel" (no uniqueid),
        quindi usiamo _channel_map per risalire al linkedid.
        """
        channel = entry.get("channel", "")
        linkedid = entry.get("linkedid", uniqueid or channel)

        if action in ("add", "update"):
            # Traccia il mapping channel → linkedid
            if channel and linkedid:
                self._channel_map[channel] = linkedid
                self._call_channels.setdefault(linkedid, set()).add(channel)

            state = entry.get("state", "")
            if state in ("Ring", "Ringing"):
                # Canale trunk di una chiamata in entrata: registra il
                # linkedid come chiamata esterna e salta (le notifiche
                # si basano sui canali extension, non trunk)
                if entry.get("inbound_trunk_name"):
                    if linkedid:  # ignora linkedid vuoti
                        self._incoming_linkedids.add(linkedid)
                    pbx_raw_logger.info(
                        "    -> Trunk inbound rilevato, linkedid=%s registrato come incoming",
                        linkedid,
                    )
                    return

                # Ignora chiamate non in ingresso (interne o in uscita):
                # solo i linkedid associati a un trunk inbound vengono
                # notificati. Ignora anche linkedid vuoti (eventi post-hangup).
                if not linkedid or linkedid not in self._incoming_linkedids:
                    pbx_raw_logger.info(
                        "    -> IGNORATO: linkedid=%s non in incoming_linkedids",
                        linkedid,
                    )
                    return

                callernum = entry.get("callernum", "")
                connectednum = entry.get("connectednum", "")
                connectedname = entry.get("connectedname", "") or ""

                with self._lock:
                    existing = self.active_calls.get(linkedid)
                    if existing:
                        if existing["state"] == "ringing":
                            # Altro interno dello stesso ring group: aggiorna la lista
                            exts = existing.get("extensions", [])
                            if callernum and callernum not in exts:
                                exts.append(callernum)
                                pbx_raw_logger.info(
                                    "    -> Ring group: aggiunto interno %s a linkedid=%s (interni: %s)",
                                    callernum, linkedid, exts,
                                )
                        # Chiamata gia' tracciata (ringing o connected):
                        # non generare una nuova notifica ring
                        pbx_raw_logger.info(
                            "    -> Chiamata gia' tracciata (state=%s), skip notifica",
                            existing.get("state"),
                        )
                        return

                    # Nuova chiamata in arrivo: il chiamante esterno e'
                    # in connectednum, l'interno che squilla e' in callernum
                    call_info = {
                        "uniqueid": linkedid,
                        "state": "ringing",
                        "callernum": connectednum,
                        "callername": connectedname,
                        "connectednum": callernum,
                        "extensions": [callernum] if callernum else [],
                    }
                    self.active_calls[linkedid] = call_info

                pbx_raw_logger.info(
                    "    -> NUOVA CHIAMATA RINGING: da=%s verso=%s linkedid=%s",
                    connectednum, callernum, linkedid,
                )
                logger.info(
                    "Squillo: %s -> interni %s",
                    connectednum, callernum,
                )
                self._broadcast_event({
                    "event": "call_ring",
                    "uniqueid": linkedid,
                    "callernum": connectednum,
                    "connectednum": callernum,
                    "callername": connectedname,
                })

                # Registro chiamate: inserisci record inbound
                self._log_inbound_ring(linkedid, connectednum)
            else:
                pbx_raw_logger.info(
                    "    -> Unbridge add/update con state=%s (non Ring/Ringing), ignorato",
                    state,
                )

        elif action == "delete":
            # I delete hanno solo "channel", non "uniqueid"
            linked = self._channel_map.pop(channel, None) if channel else None
            pbx_raw_logger.info(
                "    -> Unbridge DELETE: channel=%s -> linkedid=%s",
                channel, linked,
            )
            if linked:
                channels = self._call_channels.get(linked, set())
                channels.discard(channel)
                pbx_raw_logger.info(
                    "    -> Canali rimanenti per linkedid=%s: %s",
                    linked, channels,
                )
                if not channels:
                    # Tutti i canali rimossi → chiamata terminata
                    self._call_channels.pop(linked, None)
                    self._incoming_linkedids.discard(linked)
                    self._finalize_call_log(linked)
                    with self._lock:
                        removed = self.active_calls.pop(linked, None)
                    if removed:
                        pbx_raw_logger.info(
                            "    -> CHIAMATA TERMINATA (unbridge): linkedid=%s",
                            linked,
                        )
                        logger.info("Chiamata terminata: %s", linked)
                        self._broadcast_event({
                            "event": "call_hangup",
                            "uniqueid": linked,
                        })

    def _resolve_bridge_linkedid(self, entry):
        """Risolve il linkedid per un evento bridge.

        Gli eventi bridge del PBX hanno spesso linkedid vuoto e usano
        channel1/channel2 invece di channel. Risaliamo al linkedid
        cercando i canali in _channel_map.

        Returns:
            Il linkedid risolto, oppure stringa vuota se non trovato.
        """
        # Prima prova il linkedid diretto (se il PBX lo fornisce)
        linkedid = entry.get("linkedid", "")
        if linkedid:
            return linkedid

        # Altrimenti cerca tramite channel1/channel2 in _channel_map
        for key in ("channel1", "channel2", "channel"):
            ch = entry.get(key, "")
            if ch and ch in self._channel_map:
                resolved = self._channel_map[ch]
                pbx_raw_logger.info(
                    "    -> linkedid risolto da %s=%s -> %s",
                    key, ch, resolved,
                )
                return resolved

        return ""

    def _handle_bridge(self, action, entry, uniqueid):
        """Gestisce eventi bridge (chiamata connessa).

        Quando un interno risponde, il PBX crea un bridge con entrambe
        le parti. Usiamo linkedid per sostituire l'entry "ringing"
        con una "connected", cosi' il badge passa da squillo a connesso.

        Gli eventi bridge usano channel1/channel2 (non channel) e spesso
        hanno linkedid vuoto. Il linkedid viene risolto tramite
        _channel_map dai canali gia' tracciati in fase unbridge.
        """
        # Estrai tutti i canali coinvolti nel bridge
        channel = entry.get("channel", "")
        channel1 = entry.get("channel1", "")
        channel2 = entry.get("channel2", "")
        bridge_channels = [ch for ch in (channel, channel1, channel2) if ch]

        if action in ("add", "update"):
            # FIX 1: Risolvi linkedid da channel1/channel2 via _channel_map
            linkedid = self._resolve_bridge_linkedid(entry)

            # Fallback per chiamate outbound (nessun unbridge precedente)
            if not linkedid and entry.get("outbound_trunk_name"):
                linkedid = uniqueid or channel1 or channel2 or channel

            if not linkedid:
                pbx_raw_logger.info(
                    "    -> Bridge IGNORATO: linkedid non risolvibile "
                    "(channel=%s channel1=%s channel2=%s)",
                    channel, channel1, channel2,
                )
                return

            # FIX 2: Traccia i canali bridge in _call_channels
            for ch in bridge_channels:
                self._channel_map[ch] = linkedid
                self._call_channels.setdefault(linkedid, set()).add(ch)
            pbx_raw_logger.info(
                "    -> Bridge canali tracciati: %s -> linkedid=%s",
                bridge_channels, linkedid,
            )

            # --- Registro chiamate: rilevamento outbound ---
            outbound_trunk = entry.get("outbound_trunk_name", "")
            inbound_trunk = entry.get("inbound_trunk_name", "")

            if outbound_trunk and not inbound_trunk:
                # Chiamata in uscita con risposta: registra nel database
                ext_num, int_ext, int_name = self._extract_bridge_parties(entry)
                bridge_time_val = entry.get("bridge_time", "")
                if ext_num:
                    self._log_outbound_call(
                        linkedid,
                        bridge_time_val or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ext_num, int_ext or "", int_name or "",
                    )
                pbx_raw_logger.info(
                    "    -> OUTBOUND registrata: linkedid=%s ext=%s",
                    linkedid, ext_num,
                )

            # --- Registro chiamate: aggiornamento inbound risposta ---
            if linkedid in self._incoming_linkedids and linkedid in self._call_log_meta:
                ext_num, int_ext, int_name = self._extract_bridge_parties(entry)
                bridge_time_val = entry.get("bridge_time", "")
                self._log_call_answered(
                    linkedid, int_ext or "", int_name or "", bridge_time_val,
                )

            # Notifica solo chiamate in ingresso esterne
            if linkedid not in self._incoming_linkedids:
                pbx_raw_logger.info(
                    "    -> Bridge IGNORATO: linkedid=%s non in incoming_linkedids",
                    linkedid,
                )
                return

            # Processa solo chiamate gia' viste in fase di squillo
            with self._lock:
                if linkedid not in self.active_calls:
                    pbx_raw_logger.info(
                        "    -> Bridge IGNORATO: linkedid=%s non in active_calls",
                        linkedid,
                    )
                    return

            callerid1 = entry.get("callerid1", "")
            callerid2 = entry.get("callerid2", "")
            name1 = entry.get("name1", "")
            name2 = entry.get("name2", "")
            bridge_time = entry.get("bridge_time", "")

            call_info = {
                "uniqueid": linkedid,
                "state": "connected",
                "callerid1": callerid1,
                "callerid2": callerid2,
                "name1": name1,
                "name2": name2,
                "bridge_time": bridge_time,
            }
            with self._lock:
                self.active_calls[linkedid] = call_info

            pbx_raw_logger.info(
                "    -> CHIAMATA CONNESSA: %s (%s) <-> %s (%s) linkedid=%s",
                callerid1, name1, callerid2, name2, linkedid,
            )
            logger.info(
                "Chiamata connessa: %s (%s) <-> %s (%s)",
                callerid1, name1, callerid2, name2,
            )
            self._broadcast_event({
                "event": "call_connect",
                "uniqueid": linkedid,
                "callerid1": callerid1,
                "callerid2": callerid2,
                "name1": name1,
                "name2": name2,
                "bridge_time": bridge_time,
            })

        elif action == "delete":
            # I delete bridge possono avere channel e/o channel2
            # Risolvi il linkedid da qualsiasi canale presente
            linked = None
            for ch in bridge_channels:
                if ch in self._channel_map:
                    linked = self._channel_map.pop(ch)
                    break
            # Rimuovi anche gli altri canali dal map
            for ch in bridge_channels:
                self._channel_map.pop(ch, None)

            pbx_raw_logger.info(
                "    -> Bridge DELETE: channels=%s -> linkedid=%s",
                bridge_channels, linked,
            )
            if linked:
                channels = self._call_channels.get(linked, set())
                for ch in bridge_channels:
                    channels.discard(ch)
                pbx_raw_logger.info(
                    "    -> Canali rimanenti per linkedid=%s: %s",
                    linked, channels,
                )
                if not channels:
                    self._call_channels.pop(linked, None)
                    self._incoming_linkedids.discard(linked)
                    self._finalize_call_log(linked)
                    with self._lock:
                        removed = self.active_calls.pop(linked, None)
                    if removed:
                        pbx_raw_logger.info(
                            "    -> CHIAMATA TERMINATA (bridge delete): linkedid=%s",
                            linked,
                        )
                        logger.info(
                            "Chiamata terminata (bridge delete): %s", linked
                        )
                        self._broadcast_event({
                            "event": "call_hangup",
                            "uniqueid": linked,
                        })

    # ------------------------------------------------------------------
    # Registro chiamate su database SQLite
    # ------------------------------------------------------------------

    def _init_call_log_db(self):
        """Crea la connessione SQLite e la tabella call_log nel thread del monitor."""
        if not self._db_path:
            return
        try:
            self._db = sqlite3.connect(self._db_path)
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    external_number TEXT NOT NULL,
                    internal_ext TEXT DEFAULT '',
                    internal_name TEXT DEFAULT '',
                    answered INTEGER DEFAULT 0,
                    duration INTEGER DEFAULT 0,
                    linkedid TEXT DEFAULT ''
                )"""
            )
            self._db.commit()
            logger.info("Tabella call_log inizializzata in %s", self._db_path)
        except Exception:
            logger.exception("Errore inizializzazione database call_log")
            self._db = None

    def _extract_bridge_parties(self, entry):
        """Identifica le parti interna/esterna di un evento bridge.

        Il canale con "trunk" nel nome identifica la parte esterna.

        Returns:
            Tupla (external_number, internal_ext, internal_name)
            oppure (None, None, None) se non determinabile.
        """
        ch1 = entry.get("channel1", "")
        ch2 = entry.get("channel2", "")
        id1 = entry.get("callerid1", "")
        id2 = entry.get("callerid2", "")
        name1 = entry.get("name1", "")
        name2 = entry.get("name2", "")

        if "trunk" in ch1.lower():
            return id1, id2, name2
        elif "trunk" in ch2.lower():
            return id2, id1, name1
        return None, None, None

    def _log_inbound_ring(self, linkedid, external_number):
        """Inserisce un record inbound quando inizia lo squillo."""
        if not self._db:
            return
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._db.execute(
                "INSERT INTO call_log "
                "(timestamp, direction, external_number, linkedid) "
                "VALUES (?, 'inbound', ?, ?)",
                (ts, external_number, linkedid),
            )
            self._db.commit()
            self._call_log_meta[linkedid] = {"bridge_time": None}
            logger.info(
                "Call log: inbound ring da %s (linkedid=%s)", external_number, linkedid
            )
        except Exception:
            logger.exception("Errore registrazione inbound ring")

    def _log_call_answered(self, linkedid, internal_ext, internal_name, bridge_time):
        """Aggiorna un record inbound quando la chiamata riceve risposta."""
        if not self._db:
            return
        try:
            self._db.execute(
                "UPDATE call_log SET answered = 1, internal_ext = ?, "
                "internal_name = ? WHERE linkedid = ?",
                (internal_ext, internal_name, linkedid),
            )
            self._db.commit()
            meta = self._call_log_meta.get(linkedid)
            if meta is not None:
                meta["bridge_time"] = bridge_time
            logger.info(
                "Call log: inbound risposta int=%s (%s) linkedid=%s",
                internal_ext, internal_name, linkedid,
            )
        except Exception:
            logger.exception("Errore aggiornamento inbound risposta")

    def _log_outbound_call(self, linkedid, timestamp, external_number,
                           internal_ext, internal_name):
        """Inserisce un record outbound (solo chiamate con risposta)."""
        if not self._db:
            return
        try:
            self._db.execute(
                "INSERT INTO call_log "
                "(timestamp, direction, external_number, internal_ext, "
                "internal_name, answered, linkedid) "
                "VALUES (?, 'outbound', ?, ?, ?, 1, ?)",
                (timestamp, external_number, internal_ext, internal_name, linkedid),
            )
            self._db.commit()
            self._call_log_meta[linkedid] = {"bridge_time": timestamp}
            logger.info(
                "Call log: outbound %s -> %s (linkedid=%s)",
                internal_ext, external_number, linkedid,
            )
        except Exception:
            logger.exception("Errore registrazione outbound")

    def _finalize_call_log(self, linkedid):
        """Calcola e salva la durata alla fine della chiamata."""
        meta = self._call_log_meta.pop(linkedid, None)
        if not meta or not self._db:
            return
        bridge_time_str = meta.get("bridge_time")
        if not bridge_time_str:
            return  # non risposta, duration resta 0
        try:
            bt = datetime.strptime(bridge_time_str, "%Y-%m-%d %H:%M:%S")
            duration = max(0, int((datetime.now() - bt).total_seconds()))
            self._db.execute(
                "UPDATE call_log SET duration = ? WHERE linkedid = ?",
                (duration, linkedid),
            )
            self._db.commit()
            logger.info(
                "Call log: durata %ds per linkedid=%s", duration, linkedid
            )
        except Exception:
            logger.exception("Errore aggiornamento durata chiamata")

    # ------------------------------------------------------------------
    # Broadcasting eventi ai subscriber (code thread-safe)
    # ------------------------------------------------------------------

    def _broadcast_event(self, event):
        """Invia un evento a tutte le code registrate.

        Le code piene vengono svuotate per evitare blocchi.
        """
        pbx_raw_logger.info("BROADCAST: %s", _pretty_json(event))
        with self._queues_lock:
            for q in self._event_queues:
                try:
                    q.put_nowait(event)
                except Exception:
                    pass
