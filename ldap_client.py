# ldap_client.py - Client LDAP per operazioni CRUD sui contatti
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

from ldap3 import ALL, Connection, Server
from ldap3.core.exceptions import LDAPException


class LDAPClient:
    """Client per la gestione dei contatti su un server LDAP.

    Ogni contatto viene memorizzato come entry inetOrgPerson con i seguenti
    attributi: uid, cn, displayName, sn, telephoneNumber.
    L'attributo telephoneNumber e' multi-valore e supporta fino a due numeri.
    La connessione viene aperta e chiusa ad ogni operazione (bind/unbind).
    """

    def __init__(self, config):
        """Inizializza il client con i parametri di connessione.

        Args:
            config: dizionario con le chiavi LDAP_HOST, LDAP_PORT,
                    LDAP_USE_SSL, LDAP_BIND_DN, LDAP_BIND_PASSWORD,
                    LDAP_BASE_DN.
        """
        self.host = config["LDAP_HOST"]
        self.port = config["LDAP_PORT"]
        self.use_ssl = config["LDAP_USE_SSL"]
        self.bind_dn = config["LDAP_BIND_DN"]
        self.bind_password = config["LDAP_BIND_PASSWORD"]
        self.base_dn = config["LDAP_BASE_DN"]

    def _connect(self):
        """Crea e restituisce una connessione LDAP autenticata."""
        server = Server(self.host, port=self.port, use_ssl=self.use_ssl, get_info=ALL)
        conn = Connection(server, user=self.bind_dn, password=self.bind_password, auto_bind=True)
        return conn

    @staticmethod
    def _entry_to_dict(entry):
        """Converte un'entry ldap3 in un dizionario contatto.

        L'attributo telephoneNumber e' multi-valore: il primo valore viene
        mappato su 'telephoneNumber', il secondo (se presente) su 'telephoneNumber2'.
        """
        # telephoneNumber e' multi-valore: estraiamo la lista dei valori
        phones = list(entry.telephoneNumber) if entry.telephoneNumber else []
        given_name = ""
        if hasattr(entry, "givenName") and entry.givenName:
            given_name = str(entry.givenName)
        title = ""
        if hasattr(entry, "title") and entry.title:
            title = str(entry.title)
        return {
            "uid": str(entry.uid) if entry.uid else "",
            "cn": str(entry.cn) if entry.cn else "",
            "displayName": str(entry.displayName) if entry.displayName else "",
            "sn": str(entry.sn) if entry.sn else "",
            "givenName": given_name,
            "title": title,
            "telephoneNumber": str(phones[0]) if len(phones) > 0 else "",
            "telephoneNumber2": str(phones[1]) if len(phones) > 1 else "",
        }

    def get_all_contacts(self):
        """Recupera tutti i contatti inetOrgPerson ordinati per nome.

        Returns:
            Lista di dizionari con uid, cn, displayName, sn,
            telephoneNumber e telephoneNumber2.
        """
        conn = self._connect()
        try:
            conn.search(
                self.base_dn,
                "(objectClass=inetOrgPerson)",
                attributes=["uid", "cn", "displayName", "sn", "givenName", "title", "telephoneNumber"],
            )
            contacts = [self._entry_to_dict(entry) for entry in conn.entries]
            contacts.sort(key=lambda c: c["displayName"].lower())
            return contacts
        finally:
            conn.unbind()

    def get_contact(self, uid):
        """Recupera un singolo contatto tramite il suo uid.

        Args:
            uid: identificativo univoco del contatto.

        Returns:
            Dizionario con i dati del contatto (incluso telephoneNumber2),
            oppure None se non trovato.
        """
        conn = self._connect()
        try:
            # Il valore uid viene sanitizzato per prevenire LDAP injection
            conn.search(
                self.base_dn,
                f"(&(objectClass=inetOrgPerson)(uid={_escape_ldap_filter(uid)}))",
                attributes=["uid", "cn", "displayName", "sn", "givenName", "title", "telephoneNumber"],
            )
            if not conn.entries:
                return None
            return self._entry_to_dict(conn.entries[0])
        finally:
            conn.unbind()

    def add_contact(self, uid, display_name, sn, telephone, telephone2="", given_name="", title=""):
        """Aggiunge un nuovo contatto inetOrgPerson al server LDAP.

        Args:
            uid: identificativo univoco (usato anche come RDN).
            display_name: nome visualizzato (impostato anche come cn).
            sn: cognome o ragione sociale.
            telephone: numero di telefono principale.
            telephone2: secondo numero di telefono (opzionale).
            given_name: nome proprio (opzionale, per le persone).
            title: titolo (opzionale, es. "Dott.", "Ing.").

        Raises:
            LDAPException: se l'operazione di aggiunta fallisce.
        """
        conn = self._connect()
        dn = f"uid={uid},{self.base_dn}"
        # telephoneNumber e' multi-valore: salviamo uno o due numeri
        phones = [telephone]
        if telephone2:
            phones.append(telephone2)
        # Struttura conforme allo schema inetOrgPerson usato dai telefoni VoIP
        attributes = {
            "objectClass": ["top", "person", "organizationalPerson", "inetOrgPerson"],
            "uid": uid,
            "cn": display_name,
            "displayName": display_name,
            "sn": sn,
            "telephoneNumber": phones,
        }
        if given_name:
            attributes["givenName"] = given_name
        if title:
            attributes["title"] = title
        try:
            success = conn.add(dn, attributes=attributes)
            if not success:
                raise LDAPException(f"Failed to add contact: {conn.result['description']}")
        finally:
            conn.unbind()

    def update_contact(self, uid, display_name, sn, telephone, telephone2="", given_name="", title=""):
        """Aggiorna un contatto esistente.

        Modifica displayName, cn, sn, givenName, title e telephoneNumber.
        L'uid (usato come RDN) non viene modificato.

        Args:
            uid: identificativo del contatto da aggiornare.
            display_name: nuovo nome visualizzato.
            sn: nuovo cognome o ragione sociale.
            telephone: nuovo numero di telefono principale.
            telephone2: nuovo secondo numero di telefono (opzionale).
            given_name: nuovo nome proprio (opzionale).
            title: nuovo titolo (opzionale).

        Raises:
            LDAPException: se l'operazione di modifica fallisce.
        """
        conn = self._connect()
        dn = f"uid={uid},{self.base_dn}"
        # telephoneNumber e' multi-valore: salviamo uno o due numeri
        phones = [telephone]
        if telephone2:
            phones.append(telephone2)
        # MODIFY_REPLACE (2) sostituisce tutti i valori esistenti dell'attributo
        # MODIFY_DELETE (1) rimuove l'attributo
        changes = {
            "cn": [(2, [display_name])],
            "displayName": [(2, [display_name])],
            "sn": [(2, [sn])],
            "telephoneNumber": [(2, phones)],
        }
        # MODIFY_REPLACE (funzione numero 2 funziona sia per aggiungere che per sostituire,
        # e non da errore se l'attributo non esiste ancora
        if given_name:
            changes["givenName"] = [(2, [given_name])]
        else:
        # Rimuove givenName se il campo e' vuoto (es. ragione sociale)
        # Usa MODIFY_REPLACE (2) con lista vuota invece di MODIFY_DELETE (1)
        # Questo non da errore se l'attributo non esiste
            changes["givenName"] = [(2, [])]
        if title:
            changes["title"] = [(2, [title])]
        else:
            # Rimuove title se il campo e' vuoto
            # Usa MODIFY_REPLACE (2) con lista vuota invece di MODIFY_DELETE (1)
            # Questo non da errore se l'attributo non esiste
            changes["title"] = [(2, [])]
        try:
            success = conn.modify(dn, changes)
            if not success:
                raise LDAPException(f"Failed to update contact: {conn.result['description']}")
        finally:
            conn.unbind()

    def delete_contact(self, uid):
        """Elimina un contatto dal server LDAP.

        Args:
            uid: identificativo del contatto da eliminare.

        Raises:
            LDAPException: se l'operazione di eliminazione fallisce.
        """
        conn = self._connect()
        dn = f"uid={uid},{self.base_dn}"
        try:
            success = conn.delete(dn)
            if not success:
                raise LDAPException(f"Failed to delete contact: {conn.result['description']}")
        finally:
            conn.unbind()


def _escape_ldap_filter(value):
    """Sanitizza i caratteri speciali in un valore per filtri LDAP.

    Previene attacchi di LDAP injection escapando i caratteri che hanno
    un significato speciale nella sintassi dei filtri LDAP (RFC 4515).

    Args:
        value: stringa da sanitizzare.

    Returns:
        Stringa con i caratteri speciali sostituiti dalla loro
        rappresentazione esadecimale (es. * -> \\2a).
    """
    replacements = {
        "\\": "\\5c",
        "*": "\\2a",
        "(": "\\28",
        ")": "\\29",
        "\x00": "\\00",
    }
    result = value
    # Il backslash va escapato per primo per evitare doppi escape
    for char, escaped in replacements.items():
        result = result.replace(char, escaped)
    return result
