# FantaLineup 26/27

Programma Python **senza dipendenze esterne** che consiglia la formazione ottimale per la tua lega fantacalcio, usando:

- **titolarità** (probabilità che un giocatore scenda in campo);
- **bonus e malus** classici (gol, assist, cartellini, rigori, gol subiti, ecc.);
- **modificatore difesa** (portiere + migliori difensori);
- **difficoltà dell'avversario** stimata dalla classifica e dal calendario della Serie A 2026/27;
- **moduli classici** (3-4-3, 4-3-3, 5-3-2, ...).

Il programma confronta in modo esatto tutte le combinazioni possibili della rosa e propone la formazione con il punteggio atteso più alto, mostrando anche panchina, capitano e moduli alternativi.

---

## Avvio rapido

Requisiti: **Python 3.10+** (testato con Python 3.13). Nessun `pip install`.

```powershell
python fantacalcio.py demo
```

Il comando crea (se non esiste) una lega dimostrativa con 8 squadre e calcola subito la formazione consigliata. Per scegliere un'altra squadra della demo:

```powershell
python fantacalcio.py consiglia --lega data/lega_demo.json --squadra "Real Fantasia"
```

Per vedere le squadre disponibili:

```powershell
python fantacalcio.py elenco-squadre --lega data/lega_demo.json
```

Se lanci il programma senza argomenti:

```powershell
python fantacalcio.py
```

parte il comando `consiglia` in modalità interattiva: ti chiede l'indirizzo della lega e la squadra.

---

## Comandi

| Comando | Descrizione |
| --- | --- |
| `consiglia` | Calcola la formazione consigliata (comando principale). |
| `update` | Scarica quotazioni, statistiche, probabili formazioni, classifica e calendario. |
| `demo` | Crea e usa una lega dimostrativa con 8 squadre. |
| `elenco-squadre` | Mostra le squadre di una lega e il numero di giocatori. |
| `importa` | Crea un file JSON di lega a partire da una rosa scritta a mano. |

### `consiglia`

```powershell
python fantacalcio.py consiglia --lega data/lega_demo.json --squadra "I Magnifici"
```

Opzioni principali:

| Opzione | Significato |
| --- | --- |
| `--lega`, `-l` | File JSON, URL JSON pubblico, JSON inline oppure `demo`. Default: `demo`. |
| `--squadra`, `-s` | Nome della squadra da ottimizzare. |
| `--giornata`, `-g` | Giornata di Serie A da considerare. Default: quella della lega o dei dati. |
| `--modulo`, `-m` | Moduli ammessi, separati da virgola. Es. `4-3-3,3-5-2`. |
| `--rischio` | Avversione al rischio (0 = neutrale). Default: `0.15`. |
| `--top` | Numero di moduli alternativi da mostrare. Default: `3`. |
| `--rosa` | Rosa manuale: file di testo o elenco separato da virgole. |
| `--json` | Output in JSON, utile per script o integrazioni. |
| `--aggiorna` | Aggiorna i dati prima di calcolare la formazione. |
| `--no-interattivo` | Non chiede input: usa i valori di default. |
| `--data-dir` | Cartella dei dati. Default: `./data`. |

Esempi:

```powershell
# Solo moduli con 4 difensori
python fantacalcio.py consiglia -l data/lega_demo.json -s "I Magnifici" -m 4-3-3,4-4-2

# Output JSON
python fantacalcio.py consiglia -l data/lega_demo.json -s "I Magnifici" --json

# Rosa manuale passata direttamente
python fantacalcio.py consiglia -s "La mia squadra" --rosa "Mandas, Balerdi, Bremer, Mancini, Dimarco, Bastoni, Paz N., Calhanoglu, Zaccagni, Barella, Politano, Malen, Noslin, Thuram, Carnesecchi"

# Rosa da file di testo (un giocatore per riga)
python fantacalcio.py consiglia -s "La mia squadra" --rosa rosa.txt
```

### `update`

```powershell
python fantacalcio.py update
```

Scarica i dati aggiornati da:

- Fantacalcio.it: quotazioni, statistiche, probabili formazioni;
- Wikipedia: classifica e calendario della Serie A 2026/27.

I file vengono scritti in `data/`:

| File | Contenuto |
| --- | --- |
| `data/giocatori_2627.json` | Giocatori, ruoli, quotazioni, statistiche, titolarità, stato. |
| `data/serie_a_2627.json` | Classifica, calendario e giornata corrente. |
| `data/impostazioni.json` | Impostazioni predefinite (modificabile). |

> L'aggiornamento richiede una connessione internet. Se non è disponibile, puoi continuare a usare l'ultima istantanea già presente in `data/` (senza `--aggiorna`).

### `importa`

Risolve i nomi dei giocatori e crea un file di lega pronto all'uso:

```powershell
python fantacalcio.py importa --squadra "La mia squadra" --rosa rosa.txt --output data/la_mia_lega.json
```

`--rosa` accetta:

- un file di testo con **un giocatore per riga**;
- un elenco separato da virgole o punti e virgola.

Il programma riconosce anche forme come `Rossi F.`, `F. Rossi`, `Barella (INT)` e suggerisce nomi simili se un giocatore non viene trovato.

---

## Collegare la propria lega

### 1. File JSON (consigliato)

Crea un file come questo:

```json
{
  "nome": "La mia lega",
  "giornata": 10,
  "impostazioni": {
    "bonus_gol": 3,
    "bonus_assist": 1,
    "malus_ammonizione": -1,
    "malus_espulsione": -1,
    "moduli": ["4-3-3", "3-5-2", "4-4-2"]
  },
  "squadre": [
    {
      "nome": "La mia squadra",
      "allenatore": "Mario",
      "rosa": ["Mandas", "Balerdi", "Bremer", "Mancini", "Dimarco", "Bastoni", "Paz N.", "Calhanoglu", "Zaccagni", "Barella", "Politano", "Malen", "Noslin", "Thuram", "Carnesecchi"]
    },
    {
      "nome": "Avversario",
      "rosa": ["Svilar", "Dimarco", "Calhanoglu", "Thuram", "Bastoni", "Barella", "Bremer", "Mancini", "Zaccagni", "Malen", "Noslin"]
    }
  ]
}
```

La rosa può contenere:

- **nomi** (`"Barella"`, `"Barella (INT)"`);
- **ID** del dataset (`"4431"`);
- **oggetti** (`{"nome": "Barella", "squadra": "INT"}`).

In alternativa alla lista `squadre`, puoi usare una mappa `rose`:

```json
{
  "nome": "La mia lega",
  "rose": {
    "La mia squadra": ["Carnesecchi", "Bastoni", "Barella"],
    "Avversario": ["Svilar", "Dimarco", "Calhanoglu"]
  }
}
```

Per una singola squadra è sufficiente:

```json
{
  "squadra": "La mia squadra",
  "rosa": ["Carnesecchi", "Bastoni", "Barella"]
}
```

### 2. URL JSON pubblico

Se hai un endpoint che restituisce JSON (per esempio un foglio Google pubblicato come JSON, un server personale o un repository raw), puoi passarlo direttamente:

```powershell
python fantacalcio.py consiglia --lega "https://esempio.it/lega.json" --squadra "La mia squadra"
```

L'URL deve restituire **JSON**, non una pagina HTML.

### 3. Rosa manuale

Il modo più rapido per iniziare:

```powershell
python fantacalcio.py consiglia --squadra "La mia squadra" --rosa "Mandas, Balerdi, Bremer, Mancini, Dimarco, Bastoni, Paz N., Calhanoglu, Zaccagni, Barella, Politano, Malen, Noslin, Thuram, Carnesecchi"
```

Oppure con un file `rosa.txt`:

```text
# un giocatore per riga
Mandas
Balerdi
Bremer
Mancini
Dimarco
Bastoni
Paz N.
Calhanoglu
Zaccagni
Barella
Politano
Malen
Noslin
Thuram
Carnesecchi
```

### Perché non posso usare direttamente `leghe.fantacalcio.it`?

Gli indirizzi del tipo `https://leghe.fantacalcio.it/...` sono **privati e richiedono il login**. Uno script senza credenziali non può leggerli. Il programma te lo spiega e ti propone le alternative qui sopra.

Per usare la tua lega reale:

1. esporta la rosa in JSON dal sito, oppure
2. incolla i nomi con `--rosa`, oppure
3. crea il file JSON con `importa`.

---

## Impostazioni

Le impostazioni predefinite sono in `data/impostazioni.json`. Puoi sovrascriverle:

- nel file della lega, nella chiave `impostazioni`;
- modificando `data/impostazioni.json`.

| Chiave | Default | Significato |
| --- | --- | --- |
| `min_difensori_modificatore` | `4` | Difensori minimi per il modificatore. |
| `modificatore_portiere` | `true` | Include il portiere nella media del modificatore. |
| `tabella_modificatore` | `[[6.00, 6.49, 1], [6.50, 6.99, 3], [7.00, 99, 6]]` | Fasce del modificatore. |
| `voto_sostituto_modificatore` | `5.5` | Voto usato per chi non gioca. |
| `bonus_gol` | `3` | Bonus gol. |
| `bonus_assist` | `1` | Bonus assist. |
| `bonus_rigore_parato` | `3` | Bonus rigore parato. |
| `bonus_porta_inviolata` | `0` | Bonus porta inviolata (0 = disattivato). |
| `malus_ammonizione` | `-1` | Malus ammonizione. |
| `malus_espulsione` | `-1` | Malus espulsione. |
| `malus_autogol` | `-3` | Malus autogol. |
| `malus_rigore_sbagliato` | `-3` | Malus rigore sbagliato. |
| `malus_gol_subito` | `-1` | Malus per gol subito. |
| `gol_subito_solo_portiere` | `true` | Se `false`, il malus gol subito si applica anche ai difensori. |
| `moduli` | 8 moduli classici | Moduli ammessi. |
| `max_per_squadra_serie_a` | `0` | Limite di giocatori per squadra di Serie A (0 = nessun limite). |
| `panchina` | `12` | Numero di giocatori in panchina. |
| `rischio` | `0.15` | Peso dell'avversione al rischio. |
| `partite_shrink_voto` | `4` | Shrinkage della media voto verso 6.0. |
| `partite_shrink_rate` | `6` | Shrinkage delle frequenze (gol, assist, cartellini). |
| `fattore_casa_attacco` | `1.08` | Fattore casa usato se il calendario non ha abbastanza partite giocate. |
| `fattore_trasferta_attacco` | `0.92` | Fattore trasferta usato nello stesso caso. |
| `giornata` | `null` | Giornata predefinita della lega. |

Esempio di lega con modificatore personalizzato:

```json
{
  "nome": "Lega con modificatore custom",
  "impostazioni": {
    "min_difensori_modificatore": 4,
    "tabella_modificatore": [
      [6.00, 6.24, 1],
      [6.25, 6.49, 2],
      [6.50, 6.74, 4],
      [6.75, 6.99, 6],
      [7.00, 99, 8]
    ]
  },
  "squadre": [
    {"nome": "La mia squadra", "rosa": ["Mandas", "Balerdi", "Bremer", "Mancini", "Dimarco", "Bastoni", "Paz N.", "Calhanoglu", "Zaccagni", "Barella", "Politano", "Malen", "Noslin", "Thuram", "Carnesecchi"]}
  ]
}
```

---

## Come funziona

### 1. Difficoltà dell'avversario

Per ogni squadra il programma stima i gol attesi contro l'avversario combinando:

- media gol della Serie A;
- forza offensiva e difensiva della squadra, con shrinkage verso la media;
- fattore casa/trasferta;
- posizione e punti per partita dell'avversario.

La difficoltà finale va da **1** (molto facile) a **5** (molto difficile) ed è mostrata con le stelle.

### 2. Proiezione dei giocatori

Per ogni giocatore:

- **voto base**: media voto della stagione, corretta verso 6.0 in base alle presenze;
- **correzione matchup**: piccola variazione legata a difficoltà e casa/trasferta;
- **gol e assist attesi**: frequenza per presenza, con prior per ruolo e scala offensiva della squadra;
- **bonus/malus attesi**: gol, assist, rigori parati, cartellini, autogol, rigore sbagliato, gol subiti;
- **contributo atteso**:

```text
p * fantavoto_atteso_se_gioca + (1 - p) * voto_sostituto
```

dove `p` è la titolarità.

Il rischio associato a ogni giocatore combina l'incertezza sulla titolarità e la variabilità del rendimento:

```text
(1 - p) * 4 + p * (1 - p) * (fantavoto - sostituto)^2 * 0.1
```

### 3. Ottimizzazione

Per ogni modulo ammesso il programma enumera tutte le combinazioni di portiere, difensori, centrocampisti e attaccanti. La funzione obiettivo è:

```text
punti attesi + modificatore difesa - rischio * rischio_totale
```

Il modificatore difesa usa:

- il voto atteso **senza bonus** del portiere;
- i voti attesi dei **tre migliori difensori**;
- almeno 4 difensori a voto.

La tabella predefinita è quella "Fantacampionato":

| Media | Modificatore |
| --- | --- |
| 6.00–6.49 | +1 |
| 6.50–6.99 | +3 |
| ≥ 7.00 | +6 |

Se la rosa è molto numerosa e le combinazioni superano 4 milioni, il programma limita l'analisi ai migliori giocatori per ruolo e lo segnala negli avvisi.

### 4. Panchina e capitano

La panchina suggerita rispetta le quote classiche:

- 1 portiere;
- 4 difensori;
- 4 centrocampisti;
- 3 attaccanti.

Il capitano è il giocatore di movimento con il punteggio atteso più alto (se la tua lega prevede il capitano).

---

## Output

Ogni riga della formazione mostra:

| Colonna | Significato |
| --- | --- |
| `Ruolo` | P, D, C, A. |
| `Giocatore` | Nome del giocatore. |
| `Squadra` | Squadra di Serie A. |
| `Avversario` | Avversario della giornata. |
| `C/T` | Casa o trasferta. |
| `Diff.` | Difficoltà da 1 a 5 stelle. |
| `Tit.` | Titolarità stimata. |
| `Voto` | Voto atteso senza bonus. |
| `Bonus` | Valore atteso dei bonus. |
| `Malus` | Valore atteso dei malus. |
| `Punti` | Contributo atteso pesato per titolarità. |

Il riepilogo include punteggio atteso, modificatore difesa, rischio, titolarità media, combinazioni valutate, panchina, capitano e moduli alternativi.

Con `--json` ottieni un oggetto con formazione, panchina, alternative, modificatore e avvisi, pronto per essere consumato da altri script.

---

## Test

La suite usa solo la libreria standard e non richiede rete:

```powershell
python -m unittest discover -s tests -v
```

I test coprono parsing dati, modificatore difesa, matchup, motore previsionale, ottimizzatore, risoluzione dei nomi e caricamento della lega.

---

## Risoluzione dei problemi

### "File lega non trovato"

Controlla il percorso passato con `--lega`. Se non hai ancora una lega, usa `demo` o crea un file JSON come mostrato sopra.

### "Gli indirizzi https://leghe.fantacalcio.it/... richiedono il login"

È il comportamento previsto: le leghe private non sono accessibili senza credenziali. Esporta la rosa in JSON oppure usa `--rosa`.

### "Giocatore ... non trovato nel dataset"

- Controlla il nome o aggiungi il codice squadra: `"Rossi (GEN)"`.
- Esegui `python fantacalcio.py update` per aggiornare il dataset.
- Usa `importa` per vedere i suggerimenti sui nomi simili.

### "Non è stato possibile costruire una formazione valida"

Possibili cause:

- la rosa non ha abbastanza giocatori per i moduli richiesti;
- il limite `max_per_squadra_serie_a` è troppo restrittivo;
- i moduli scelti con `--modulo` non sono compatibili con la rosa.

Prova ad aggiungere moduli o ad alzare il limite per squadra.

### "Rosa incompleta: mancano ..."

La rosa deve contenere almeno un portiere, tre difensori, tre centrocampisti e un attaccante per i moduli classici. Con moduli più offensivi servono più giocatori per ruolo.

### Caratteri strani o errore Unicode su Windows

Il programma forza l'output UTF-8 e usa un fallback ASCII per le stelle. Se il terminale non supporta UTF-8, aggiorna Windows Terminal o esegui da PowerShell 7.

### L'aggiornamento non scarica i dati

Verifica la connessione e riprova. Se l'aggiornamento non riesce, puoi continuare a usare l'ultima istantanea in `data/` senza `--aggiorna`. Se il sito cambia struttura, il parsing potrebbe richiedere un aggiornamento del codice.

---

## Struttura del progetto

```text
fantacalcio.py          # entry point
fanta/
  cli.py                # comandi e input interattivo
  config.py             # squadre, moduli, impostazioni predefinite
  dataio.py             # lettura/scrittura JSON e parsing
  league.py             # lega, rose e risoluzione nomi
  matchup.py            # difficoltà e gol attesi
  engine.py             # proiezioni dei giocatori
  optimizer.py          # enumerazione formazioni e modificatore
  report.py             # output console/JSON
  demo.py               # lega dimostrativa
  updater.py            # download e parsing dati live
data/
  giocatori_2627.json   # istantanea giocatori
  serie_a_2627.json     # classifica e calendario
  impostazioni.json     # impostazioni
  lega_demo.json        # lega di esempio
tests/
  test_core.py          # test unitari
```

---

## Note e limiti

- Il programma è un **supporto decisionale**, non una garanzia di risultato: le probabilità di titolarità e le medie sono stime.
- I dati sono un'istantanea della stagione 2026/27; esegui `update` prima di ogni giornata per avere valori aggiornati.
- Il modello è pensato per il **fantacalcio classico** (ruoli P/D/C/A e bonus classici), non per il Mantra.
- Le leghe private di Fantacalcio.it richiedono l'esportazione manuale della rosa.
- Il modificatore difesa predefinito è quello "Fantacampionato"; se la tua lega usa regole diverse, personalizza `tabella_modificatore` e `min_difensori_modificatore`.

