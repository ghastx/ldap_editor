# app.py - Applicazione Flask principale (route e logica di presentazione)
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

import json
import logging
import queue
import re
import phonenumbers

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for
from ldap3.core.exceptions import LDAPException

from audit_log import get_log, log_action
from config import Config
from ldap_client import LDAPClient
from pbx_monitor import PBXMonitor
from ucm_client import UCMClient, UCMError

# Configura il logging per vedere i messaggi del PBX monitor
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

app = Flask(__name__)
app.config.from_object(Config)

# Inizializza il client LDAP con i parametri dalla configurazione
ldap = LDAPClient(
    {
        "LDAP_HOST": app.config["LDAP_HOST"],
        "LDAP_PORT": app.config["LDAP_PORT"],
        "LDAP_USE_SSL": app.config["LDAP_USE_SSL"],
        "LDAP_BIND_DN": app.config["LDAP_BIND_DN"],
        "LDAP_BIND_PASSWORD": app.config["LDAP_BIND_PASSWORD"],
        "LDAP_BASE_DN": app.config["LDAP_BASE_DN"],
    }
)


# Inizializza il client UCM per il click-to-dial
ucm = UCMClient(
    host=app.config["UCM_HOST"],
    port=app.config["UCM_PORT"],
    user=app.config["UCM_API_USER"],
    password=app.config["UCM_API_PASSWORD"],
)

# Inizializza e avvia il monitor chiamate PBX in un thread background.
# Usa credenziali PBX dedicate (possono differire da quelle API click-to-dial).
pbx = PBXMonitor(
    host=app.config["UCM_HOST"],
    port=app.config["UCM_PORT"],
    user=app.config["PBX_API_USER"],
    password=app.config["PBX_API_PASSWORD"],
)
pbx.start()

# --- normalizza numero di telefono ---

def normalize_number(number):
    """Normalizza il numero nel formato E.164 (+39...) usando la libreria phonenumbers."""
    if not number:
        return None

    try:
        # Tenta il parsing assumendo l'Italia come regione predefinita
        parsed = phonenumbers.parse(number, "IT")
        
        # Verifica se il numero è formalmente valido
        if not phonenumbers.is_valid_number(parsed):
            return None
            
        # Ritorna il numero nel formato internazionale E.164 (es: +39...)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return None


# --- Route principali ---


@app.route("/")
def index():
    """Pagina principale: elenco dei contatti con ricerca opzionale.

    Parametri GET:
        q: stringa di ricerca (filtra per nome o numero di telefono).
    """
    search = request.args.get("q", "").strip()
    try:
        contacts = ldap.get_all_contacts()
    except LDAPException as e:
        flash(f"Errore di connessione LDAP: {e}", "danger")
        contacts = []

    # Filtra i risultati se e' presente una query di ricerca (su entrambi i numeri)
    if search:
        q = search.lower()
        contacts = [
            c
            for c in contacts
            if q in c["displayName"].lower()
            or q in c["telephoneNumber"]
            or q in c["telephoneNumber2"]
        ]

    return render_template("index.html", contacts=contacts, search=search)


@app.route("/add", methods=["GET", "POST"])
def add_contact():
    """Aggiunta di un nuovo contatto.

    GET: mostra il form vuoto.
    POST: valida i dati, crea l'entry LDAP e registra l'operazione nel log.
    Il displayName viene composto da givenName + sn (o solo sn per le aziende).
    L'uid viene generato dal displayName rimuovendo gli spazi.
    """
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        given_name = request.form.get("given_name", "").strip()
        sn = request.form.get("sn", "").strip()
        telephone = request.form.get("telephone", "").strip()
        telephone2 = request.form.get("telephone2", "").strip()

        if not sn or not telephone:
            flash("Cognome/Ragione Sociale e numero di telefono sono obbligatori.", "danger")
            return render_template(
                "add.html", title=title, given_name=given_name, sn=sn,
                telephone=telephone, telephone2=telephone2,
            )

        # Normalizza i numeri prima del salvataggio
        norm_tel = normalize_number(telephone)
        norm_tel2 = normalize_number(telephone2) if telephone2 else ""

        if not norm_tel or (telephone2 and not norm_tel2):
            flash("I numeri di telefono non sono nel formato corretto.", "danger")
            return render_template(
                "add.html", title=title, given_name=given_name, sn=sn,
                telephone=telephone, telephone2=telephone2,
            )

        # Compone il displayName dai campi non vuoti: titolo + nome + cognome
        display_name = " ".join(part for part in [title, given_name, sn] if part)
        # Genera l'uid dal displayName (stesso pattern delle entry esistenti)
        uid = display_name.replace(" ", "")
        try:
            ldap.add_contact(uid, display_name, sn, norm_tel, norm_tel2, given_name, title)
            detail = f"Nome: {display_name}, Tel: {telephone}"
            if telephone2:
                detail += f", Tel2: {telephone2}"
            log_action("aggiunto", uid, detail, request.remote_addr)
            flash(f"Contatto '{display_name}' aggiunto con successo.", "success")
            return redirect(url_for("index"))
        except LDAPException as e:
            flash(f"Errore nell'aggiunta del contatto: {e}", "danger")
            return render_template(
                "add.html", title=title, given_name=given_name, sn=sn,
                telephone=telephone, telephone2=telephone2,
            )

    return render_template("add.html", title="", given_name="", sn="", telephone="", telephone2="")


@app.route("/edit/<uid>", methods=["GET", "POST"])
def edit_contact(uid):
    """Modifica di un contatto esistente.

    GET: carica i dati attuali del contatto e mostra il form precompilato.
    POST: salva le modifiche, confronta i valori vecchi/nuovi e registra
          nel log solo i campi effettivamente cambiati.
    """
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        given_name = request.form.get("given_name", "").strip()
        sn = request.form.get("sn", "").strip()
        telephone = request.form.get("telephone", "").strip()
        telephone2 = request.form.get("telephone2", "").strip()

        if not sn or not telephone:
            flash("Cognome/Ragione Sociale e numero di telefono sono obbligatori.", "danger")
            return render_template(
                "edit.html",
                contact={
                    "uid": uid,
                    "displayName": " ".join(part for part in [title, given_name, sn] if part),
                    "sn": sn, "givenName": given_name, "title": title,
                    "telephoneNumber": telephone, "telephoneNumber2": telephone2,
                },
            )

        # Normalizza i numeri prima del salvataggio
        norm_tel = normalize_number(telephone)
        norm_tel2 = normalize_number(telephone2) if telephone2 else ""

        if not norm_tel or (telephone2 and not norm_tel2):
            flash("I numeri di telefono non sono nel formato corretto.", "danger")
            return render_template(
                "edit.html",
                contact={
                    "uid": uid,
                    "displayName": " ".join(part for part in [title, given_name, sn] if part),
                    "sn": sn, "givenName": given_name, "title": title,
                    "telephoneNumber": telephone, "telephoneNumber2": telephone2,
                },
            )

        # Compone il displayName dai campi non vuoti: titolo + nome + cognome
        display_name = " ".join(part for part in [title, given_name, sn] if part)

        try:
            # Legge i dati attuali prima della modifica per il confronto
            old_contact = ldap.get_contact(uid)
            ldap.update_contact(uid, display_name, sn, norm_tel, norm_tel2, given_name, title)

            # Costruisce i dettagli mostrando solo i campi modificati
            changes = []
            if old_contact and old_contact["displayName"] != display_name:
                changes.append(f"Nome: {old_contact['displayName']} -> {display_name}")
            if old_contact and old_contact["telephoneNumber"] != telephone:
                changes.append(f"Tel: {old_contact['telephoneNumber']} -> {telephone}")
            if old_contact and old_contact.get("telephoneNumber2", "") != telephone2:
                changes.append(f"Tel2: {old_contact.get('telephoneNumber2', '')} -> {telephone2}")
            log_action(
                "modificato", uid,
                "; ".join(changes) if changes else "Nessuna modifica rilevata",
                request.remote_addr,
            )
            flash(f"Contatto '{display_name}' aggiornato con successo.", "success")
            return redirect(url_for("index"))
        except LDAPException as e:
            flash(f"Errore nell'aggiornamento del contatto: {e}", "danger")
            return render_template(
                "edit.html",
                contact={
                    "uid": uid, "displayName": display_name,
                    "sn": sn, "givenName": given_name, "title": title,
                    "telephoneNumber": telephone, "telephoneNumber2": telephone2,
                },
            )

    try:
        contact = ldap.get_contact(uid)
    except LDAPException as e:
        flash(f"Errore nel caricamento del contatto: {e}", "danger")
        return redirect(url_for("index"))

    if not contact:
        flash("Contatto non trovato.", "danger")
        return redirect(url_for("index"))

    return render_template("edit.html", contact=contact)


@app.route("/delete/<uid>", methods=["POST"])
def delete_contact(uid):
    """Eliminazione di un contatto (solo POST per sicurezza).

    Prima di eliminare, salva i dati del contatto nel log per riferimento.
    """
    try:
        # Salva i dati del contatto prima dell'eliminazione per il log
        contact = ldap.get_contact(uid)
        ldap.delete_contact(uid)
        detail = ""
        if contact:
            detail = f"Nome: {contact['displayName']}, Tel: {contact['telephoneNumber']}"
            if contact.get("telephoneNumber2"):
                detail += f", Tel2: {contact['telephoneNumber2']}"
        log_action("eliminato", uid, detail, request.remote_addr)
        flash("Contatto eliminato con successo.", "success")
    except LDAPException as e:
        flash(f"Errore nell'eliminazione del contatto: {e}", "danger")
    return redirect(url_for("index"))


@app.route("/log")
def audit_log():
    """Visualizza il registro delle modifiche (ultime 200 operazioni)."""
    entries = get_log()
    return render_template("log.html", entries=entries)


# --- API click-to-dial ---


@app.route("/api/call", methods=["POST"])
def api_call():
    """Avvia una chiamata click-to-dial tramite il centralino UCM.

    Riceve un JSON con 'extension' e 'number'. Il numero viene inviato
    direttamente al PBX poiché è già normalizzato nella rubrica.

    Returns:
        JSON con 'ok': true/false e 'message' descrittivo.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify(ok=False, message="Richiesta non valida."), 400

    extension = data.get("extension", "").strip()
    number = data.get("number", "").strip()

    if not extension:
        return jsonify(ok=False, message="Seleziona prima il tuo interno."), 400
    if not number:
        return jsonify(ok=False, message="Numero di telefono mancante."), 400

    # Invia il numero così come arriva (è già normalizzato E.164 nella rubrica)
    display_number = number.strip("+39")

    try:
        ucm.dial_outbound(extension, number)
        return jsonify(ok=True, message=f"Chiamata in corso verso {display_number}...")
    except UCMError as e:
        return jsonify(ok=False, message=str(e)), 502


# --- API monitor chiamate ---


@app.route("/api/lookup/<number>")
def api_lookup(number):
    """Cerca un contatto nella rubrica LDAP tramite numero di telefono.

    Usato dal frontend per risolvere il nome del chiamante durante
    le notifiche di chiamata in arrivo.

    Returns:
        JSON con 'name' (displayName) se trovato, altrimenti null.
    """
    # Pulisce il numero: rimuove +39, spazi, trattini, parentesi
    number = normalize_number(number)
    if not number:
        return jsonify(name=None)
    try:
        contact = ldap.search_by_phone(number)
        if contact:
            return jsonify(name=contact["displayName"])
        return jsonify(name=None)
    except LDAPException:
        return jsonify(name=None)


@app.route("/api/calls")
def api_calls():
    """Restituisce le chiamate attive correnti in formato JSON."""
    return jsonify(calls=list(pbx.get_active_calls().values()))


@app.route("/api/events")
def api_events():
    """Endpoint Server-Sent Events per lo streaming degli eventi PBX.

    Ogni client SSE riceve una coda dedicata. Il monitor PBX vi inserisce
    gli eventi in tempo reale. Un commento keepalive viene inviato ogni
    30 secondi per rilevare connessioni interrotte.

    Richiede Gunicorn con worker gevent (1 greenlet per connessione SSE).
    """
    q = queue.Queue()
    pbx.subscribe_events(q)

    def stream():
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                except queue.Empty:
                    # Keepalive: commento SSE per mantenere la connessione
                    yield ":keepalive\n\n"
                    continue

                event_type = event.pop("event", "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except GeneratorExit:
            pass
        finally:
            pbx.unsubscribe_events(q)

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
