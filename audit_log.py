# audit_log.py - Registro delle modifiche (audit log) su database SQLite
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

import os
import sqlite3
from datetime import datetime


# Il database viene creato nella stessa directory dell'applicazione
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.db")


def _get_db():
    """Apre una connessione al database SQLite e crea la tabella se necessario.

    La tabella audit_log memorizza:
    - id: chiave primaria auto-incrementale
    - timestamp: data e ora dell'operazione
    - action: tipo di operazione (aggiunto, modificato, eliminato)
    - contact_uid: uid del contatto interessato
    - details: descrizione delle modifiche effettuate
    - user_ip: indirizzo IP di chi ha eseguito l'operazione
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            contact_uid TEXT NOT NULL,
            details TEXT,
            user_ip TEXT
        )"""
    )
    conn.commit()
    return conn


def log_action(action, contact_uid, details="", user_ip=""):
    """Registra un'operazione nel log delle modifiche.

    Args:
        action: tipo di operazione ('aggiunto', 'modificato', 'eliminato').
        contact_uid: uid del contatto su cui e' stata eseguita l'operazione.
        details: descrizione testuale delle modifiche (es. campi modificati).
        user_ip: indirizzo IP del client che ha effettuato la richiesta.
    """
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO audit_log (timestamp, action, contact_uid, details, user_ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, contact_uid, details, user_ip),
        )
        conn.commit()
    finally:
        conn.close()


def get_log(limit=200):
    """Recupera le ultime voci del registro modifiche.

    Args:
        limit: numero massimo di voci da restituire (default: 200).

    Returns:
        Lista di dizionari ordinati dal piu' recente al meno recente,
        ciascuno con id, timestamp, action, contact_uid, details, user_ip.
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
