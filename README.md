# Rubrica LDAP

Frontend web per la gestione di una rubrica telefonica memorizzata su un server LDAP, pensato per ambienti con telefoni VoIP che leggono i contatti da LDAP. Include integrazione click-to-dial con centralino Grandstream UCM6202.

## Funzionalita'

- **Elenco contatti** ordinati alfabeticamente con ricerca per nome o numero
- **Due numeri di telefono** per contatto (l'attributo LDAP `telephoneNumber` e' multi-valore)
- **Aggiunta** di nuovi contatti (entry `inetOrgPerson`) con numero principale e secondario opzionale
- **Modifica** di nome e numeri di telefono
- **Eliminazione** con modale di conferma
- **Click-to-dial** tramite centralino Grandstream UCM6202: cliccando un numero nella rubrica, il telefono VoIP dell'operatore squilla e, alla risposta, viene chiamato il contatto
- **Monitor chiamate in tempo reale** via WebSocket: visualizza le chiamate attive con banner/notifiche browser e indicatore nella navbar
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
| `UCM_PORT` | Porta HTTPS/WSS del centralino | `8089` |
| `UCM_API_USER` | Utente API per click-to-dial (es. `cdrapi`) | `cdrapi` |
| `UCM_API_PASSWORD` | Password dell'utente API click-to-dial | *(vuoto)* |
| `PBX_API_USER` | Utente admin PBX per il monitor chiamate WebSocket | `adminpbx` |
| `PBX_API_PASSWORD` | Password dell'utente admin PBX | *(vuoto)* |

## Avvio

```bash
python app.py
```

L'applicazione sara' disponibile su `http://localhost:5000`.

### Produzione con Gunicorn e gevent

In produzione l'app gira con Gunicorn e worker **gevent** (necessario per il supporto SSE e il monitor chiamate in tempo reale). E' importante usare **1 solo worker** perche' il thread del monitor PBX deve essere condiviso tra tutte le connessioni.

```bash
pip install gunicorn gevent
gunicorn -w 1 -k gevent -b 0.0.0.0:5000 app:app
```

Un file `ldap-editor.service` per systemd e' incluso nel repository. Per installarlo:

```bash
sudo cp ldap-editor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ldap-editor
```

## Click-to-dial

L'integrazione con il centralino Grandstream UCM6202 permette di avviare chiamate direttamente dalla rubrica web:

1. **Seleziona il tuo interno** dal menu a tendina nell'intestazione della pagina (interni 1000-1006). La selezione viene salvata nel browser e mantenuta tra le sessioni.
2. **Clicca su un numero di telefono** nella lista contatti. Il sistema:
   - Invia una richiesta al centralino tramite l'API HTTPS (porta 8089)
   - Fa squillare il telefono VoIP dell'interno selezionato
   - Quando l'operatore risponde, il centralino chiama il numero del contatto
3. Una notifica toast conferma l'avvio della chiamata o segnala eventuali errori.

## Monitor chiamate in tempo reale

L'app si connette al centralino via WebSocket (`wss://host:8089/websockify`) e riceve gli eventi sulle chiamate in tempo reale. Le notifiche vengono inviate al browser tramite Server-Sent Events (SSE).

### Funzionamento

- All'avvio, un thread background si connette al PBX via WebSocket
- Si autentica con challenge/response MD5 (stesso utente `cdrapi` del click-to-dial)
- Si sottoscrive agli eventi `ExtensionStatus` e `ActiveCallStatus`
- Invia un heartbeat ogni 30 secondi per mantenere la sessione
- Se la connessione cade, riprova automaticamente ogni 10 secondi

### Interfaccia utente

- **Indicatore nella navbar**: icona telefono con badge numerico che mostra il conteggio delle chiamate attive. Cliccandoci si apre un pannello con i dettagli
- **Banner in-page** (alto a destra): mostrano le chiamate in arrivo (arancione) e connesse (verde), scompaiono 3 secondi dopo il riaggancio
- **Notifiche browser** (Notification API): popup del sistema operativo per le chiamate in arrivo, con il nome del chiamante se presente nella rubrica LDAP
- **Lookup rubrica**: quando arriva una chiamata, il numero viene cercato nella rubrica LDAP per mostrare il nome del contatto

### Notifiche browser

Le notifiche browser (Notification API) richiedono un **contesto sicuro**:

- **HTTPS** (con certificato valido o self-signed accettato dal browser)
- **localhost** (per lo sviluppo locale)

Se l'app gira su **HTTP in rete locale**, le notifiche browser **non funzioneranno**. In questo caso funzionano comunque i banner in-page e l'indicatore nella navbar, che non richiedono HTTPS.

Per abilitare le notifiche browser in rete locale, configurare un reverse proxy (es. nginx) con HTTPS e un certificato self-signed o Let's Encrypt.

### Note tecniche

- L'autenticazione con il UCM usa un flusso challenge/response con token MD5
- La sessione API viene mantenuta in cache e rinnovata automaticamente (scadenza 5 minuti)
- Il centralino usa un certificato SSL self-signed; le verifiche SSL sono disabilitate sia per l'API HTTPS che per il WebSocket
- I numeri vengono puliti prima dell'invio click-to-dial: rimossi prefisso `+39`, spazi, trattini e parentesi
- Gunicorn deve usare worker **gevent** con **1 solo worker** per supportare le connessioni SSE long-lived e condividere il thread del monitor PBX

## Struttura del progetto

```
ldap_editor/
├── app.py                  # Applicazione Flask (route, API, SSE)
├── config.py               # Configurazione da variabili d'ambiente
├── ldap_client.py          # Client LDAP per operazioni CRUD
├── ucm_client.py           # Client API Grandstream UCM6202 (click-to-dial)
├── pbx_monitor.py          # Monitor chiamate WebSocket (tempo reale)
├── audit_log.py            # Registro modifiche su SQLite
├── requirements.txt        # Dipendenze Python
├── .env.example            # Template configurazione
├── ldap-editor.service     # Unit systemd per Gunicorn + gevent
├── LICENSE                 # Licenza GPL-2.0
├── templates/
│   ├── base.html           # Template base con layout, navigazione e monitor chiamate
│   ├── index.html          # Elenco contatti con ricerca e click-to-dial
│   ├── add.html            # Form nuovo contatto (2 numeri)
│   ├── edit.html           # Form modifica contatto (2 numeri)
│   └── log.html            # Registro delle modifiche
└── static/
    └── style.css           # Stili dell'interfaccia
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

### GET /api/calls

Restituisce le chiamate attive correnti.

**Response** (JSON):
```json
{
  "calls": [
    {"uniqueid": "abc", "state": "connected", "callerid1": "1001", "callerid2": "0512345678", "name1": "Interno 1001", "name2": "Mario Rossi"}
  ]
}
```

### GET /api/events

Endpoint Server-Sent Events (SSE) per lo streaming in tempo reale degli eventi PBX. Il browser si connette con `EventSource` e riceve eventi tipizzati:

- `call_ring` — chiamata in arrivo (squillo)
- `call_connect` — chiamata connessa (risposta)
- `call_hangup` — chiamata terminata
- `extension_status` — cambio stato interno

### GET /api/lookup/\<number\>

Cerca un contatto nella rubrica LDAP tramite numero di telefono.

**Response** (JSON):
```json
{"name": "Mario Rossi"}
```

Se il numero non e' in rubrica: `{"name": null}`

## Licenza

Questo progetto e' distribuito sotto licenza **GNU General Public License v2.0**.
Vedi il file [LICENSE](LICENSE) per i dettagli.
