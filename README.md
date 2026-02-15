# Rubrica LDAP

Frontend web per la gestione di una rubrica telefonica memorizzata su un server LDAP, pensato per ambienti con telefoni VoIP che leggono i contatti da LDAP. Include integrazione click-to-dial con centralino Grandstream UCM6202.

## Funzionalita'

- **Elenco contatti** ordinati alfabeticamente con ricerca per nome o numero
- **Due numeri di telefono** per contatto (l'attributo LDAP `telephoneNumber` e' multi-valore)
- **Aggiunta** di nuovi contatti (entry `inetOrgPerson`) con numero principale e secondario opzionale
- **Modifica** di nome e numeri di telefono
- **Eliminazione** con modale di conferma
- **Click-to-dial** tramite centralino Grandstream UCM6202: cliccando un numero nella rubrica, il telefono VoIP dell'operatore squilla e, alla risposta, viene chiamato il contatto
- **Selettore interno** persistente (interni 1000-1006), salvato nel browser
- **Registro modifiche** (audit log) con storico di tutte le operazioni, dettagli dei campi modificati e indirizzo IP dell'operatore

## Requisiti

- Python 3.8+
- Un server LDAP (OpenLDAP, 389 Directory Server, ecc.) con schema `inetOrgPerson`
- Credenziali di amministrazione per il bind LDAP
- Centralino Grandstream UCM6202 con API HTTPS abilitata (per il click-to-dial)

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

### Parametri LDAP

| Variabile | Descrizione | Default |
|---|---|---|
| `FLASK_SECRET_KEY` | Chiave segreta per le sessioni Flask | `dev-key-change-me` |
| `LDAP_HOST` | Hostname o IP del server LDAP | `localhost` |
| `LDAP_PORT` | Porta del server LDAP | `389` |
| `LDAP_USE_SSL` | Usa connessione SSL (true/false) | `false` |
| `LDAP_BIND_DN` | DN dell'utente per l'autenticazione | `cn=admin,dc=pbx,dc=com` |
| `LDAP_BIND_PASSWORD` | Password dell'utente LDAP | *(vuoto)* |
| `LDAP_BASE_DN` | DN base dove cercare/creare i contatti | `dc=pbx,dc=com` |

### Parametri centralino UCM6202

| Variabile | Descrizione | Default |
|---|---|---|
| `UCM_HOST` | Indirizzo IP del centralino Grandstream | `192.168.0.240` |
| `UCM_PORT` | Porta HTTPS dell'API UCM | `8089` |
| `UCM_API_USER` | Utente API del centralino | `cdrapi` |
| `UCM_API_PASSWORD` | Password dell'utente API | *(vuoto)* |

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

## Click-to-dial

L'integrazione con il centralino Grandstream UCM6202 permette di avviare chiamate direttamente dalla rubrica web:

1. **Seleziona il tuo interno** dal menu a tendina nell'intestazione della pagina (interni 1000-1006). La selezione viene salvata nel browser e mantenuta tra le sessioni.
2. **Clicca su un numero di telefono** nella lista contatti. Il sistema:
   - Invia una richiesta al centralino tramite l'API HTTPS (porta 8089)
   - Fa squillare il telefono VoIP dell'interno selezionato
   - Quando l'operatore risponde, il centralino chiama il numero del contatto
3. Una notifica toast conferma l'avvio della chiamata o segnala eventuali errori.

### Note tecniche

- L'autenticazione con il UCM usa un flusso challenge/response con token MD5
- La sessione API viene mantenuta in cache e rinnovata automaticamente (scadenza 5 minuti)
- Il centralino usa un certificato SSL self-signed; le verifiche SSL sono disabilitate per le chiamate API
- I numeri vengono puliti prima dell'invio: rimossi prefisso `+39`, spazi, trattini e parentesi

## Struttura del progetto

```
ldap_editor/
├── app.py              # Applicazione Flask (route e logica)
├── config.py           # Configurazione da variabili d'ambiente
├── ldap_client.py      # Client LDAP per operazioni CRUD
├── ucm_client.py       # Client API Grandstream UCM6202 (click-to-dial)
├── audit_log.py        # Registro modifiche su SQLite
├── requirements.txt    # Dipendenze Python
├── .env.example        # Template configurazione
├── LICENSE             # Licenza GPL-2.0
├── templates/
│   ├── base.html       # Template base con layout, navigazione e selettore interno
│   ├── index.html      # Elenco contatti con ricerca e click-to-dial
│   ├── add.html        # Form nuovo contatto (2 numeri)
│   ├── edit.html       # Form modifica contatto (2 numeri)
│   └── log.html        # Registro delle modifiche
└── static/
    └── style.css       # Stili dell'interfaccia
```

## Struttura LDAP dei contatti

Ogni contatto viene memorizzato come entry `inetOrgPerson`. L'attributo `telephoneNumber` e' multi-valore e supporta fino a due numeri per contatto:

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
telephoneNumber: +39098765432
```

## API

### POST /api/call

Avvia una chiamata click-to-dial tramite il centralino UCM.

**Request body** (JSON):
```json
{
  "extension": "1001",
  "number": "+39 0585 372760"
}
```

**Response** (JSON):
```json
{"ok": true, "message": "Chiamata in corso verso 0585372760..."}
```

In caso di errore:
```json
{"ok": false, "message": "Errore di connessione al centralino: ..."}
```

## Licenza

Questo progetto e' distribuito sotto licenza **GNU General Public License v2.0**.
Vedi il file [LICENSE](LICENSE) per i dettagli.
