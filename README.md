# Rubrica LDAP

Frontend web per la gestione di una rubrica telefonica memorizzata su un server LDAP, pensato per ambienti con telefoni VoIP che leggono i contatti da LDAP.

## Funzionalita'

- **Elenco contatti** ordinati alfabeticamente con ricerca per nome o numero
- **Aggiunta** di nuovi contatti (entry `inetOrgPerson`)
- **Modifica** di nome e numero di telefono
- **Eliminazione** con modale di conferma
- **Registro modifiche** (audit log) con storico di tutte le operazioni, dettagli dei campi modificati e indirizzo IP dell'operatore

## Requisiti

- Python 3.8+
- Un server LDAP (OpenLDAP, 389 Directory Server, ecc.) con schema `inetOrgPerson`
- Credenziali di amministrazione per il bind LDAP

## Installazione

```bash
# Clona il repository
git clone <url-repository>
cd ldap_editor

# Crea un ambiente virtuale (consigliato)
python3 -m venv venv
source venv/bin/activate

# Installa le dipendenze
pip install -r requirements.txt
```

## Configurazione

Copia il file di esempio e modifica i parametri:

```bash
cp .env.example .env
```

Parametri disponibili nel file `.env`:

| Variabile | Descrizione | Default |
|---|---|---|
| `FLASK_SECRET_KEY` | Chiave segreta per le sessioni Flask | `dev-key-change-me` |
| `LDAP_HOST` | Hostname o IP del server LDAP | `localhost` |
| `LDAP_PORT` | Porta del server LDAP | `389` |
| `LDAP_USE_SSL` | Usa connessione SSL (true/false) | `false` |
| `LDAP_BIND_DN` | DN dell'utente per l'autenticazione | `cn=admin,dc=pbx,dc=com` |
| `LDAP_BIND_PASSWORD` | Password dell'utente LDAP | *(vuoto)* |
| `LDAP_BASE_DN` | DN base dove cercare/creare i contatti | `dc=pbx,dc=com` |

## Avvio

```bash
python app.py
```

L'applicazione sara' disponibile su `http://localhost:5000`.

Per ambienti di produzione si consiglia di usare un server WSGI come Gunicorn:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## Struttura del progetto

```
ldap_editor/
├── app.py              # Applicazione Flask (route e logica)
├── config.py           # Configurazione da variabili d'ambiente
├── ldap_client.py      # Client LDAP per operazioni CRUD
├── audit_log.py        # Registro modifiche su SQLite
├── requirements.txt    # Dipendenze Python
├── .env.example        # Template configurazione
├── LICENSE             # Licenza GPL-2.0
├── templates/
│   ├── base.html       # Template base con layout e navigazione
│   ├── index.html      # Elenco contatti con ricerca
│   ├── add.html        # Form nuovo contatto
│   ├── edit.html       # Form modifica contatto
│   └── log.html        # Registro delle modifiche
└── static/
    └── style.css       # Stili dell'interfaccia
```

## Struttura LDAP dei contatti

Ogni contatto viene memorizzato come entry `inetOrgPerson` con i seguenti attributi:

```ldif
dn: uid=NomeContatto,dc=pbx,dc=com
objectClass: top
objectClass: person
objectClass: organizationalPerson
objectClass: inetOrgPerson
uid: NomeContatto
cn: Nome Contatto
displayName: Nome Contatto
sn: Nome Contatto
telephoneNumber: +39012345678
```

## Licenza

Questo progetto e' distribuito sotto licenza **GNU General Public License v2.0**.
Vedi il file [LICENSE](LICENSE) per i dettagli.
