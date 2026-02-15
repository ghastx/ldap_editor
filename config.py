# config.py - Configurazione dell'applicazione
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

from dotenv import load_dotenv

# Carica le variabili d'ambiente dal file .env (se presente)
load_dotenv()


class Config:
    """Configurazione Flask e LDAP caricata dalle variabili d'ambiente.

    Le impostazioni possono essere definite in un file .env nella root
    del progetto oppure come variabili d'ambiente di sistema.
    Vedi .env.example per un elenco completo dei parametri.
    """

    # Chiave segreta per le sessioni Flask (flash messages, CSRF)
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")

    # Parametri di connessione al server LDAP
    LDAP_HOST = os.environ.get("LDAP_HOST", "localhost")
    LDAP_PORT = int(os.environ.get("LDAP_PORT", 389))
    LDAP_USE_SSL = os.environ.get("LDAP_USE_SSL", "false").lower() == "true"

    # Credenziali di bind per l'autenticazione LDAP
    LDAP_BIND_DN = os.environ.get("LDAP_BIND_DN", "cn=admin,dc=pbx,dc=com")
    LDAP_BIND_PASSWORD = os.environ.get("LDAP_BIND_PASSWORD", "")

    # DN base sotto cui si trovano i contatti della rubrica
    LDAP_BASE_DN = os.environ.get("LDAP_BASE_DN", "dc=pbx,dc=com")

    # Parametri di connessione al centralino Grandstream UCM6202
    UCM_HOST = os.environ.get("UCM_HOST", "192.168.0.240")
    UCM_PORT = int(os.environ.get("UCM_PORT", 8089))
    UCM_API_USER = os.environ.get("UCM_API_USER", "cdrapi")
    UCM_API_PASSWORD = os.environ.get("UCM_API_PASSWORD", "")
