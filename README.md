# Bot trend + ritracciamento — Coinbase Advanced Trade

Bot di trading **spot** in Python: compra BTC, ETH e SOL con euro e li rivende.
Niente leva, niente vendite allo scoperto: vende solo ciò che ha comprato.

## Installazione

```
pip install -r requirements.txt
```

Serve Python 3.9 o più recente.

## Chiave API Coinbase

1. Vai su Coinbase Developer Platform (portal.cdp.coinbase.com) → API Keys → crea una chiave **Secret API Key**.
2. Permessi: **View** e **Trade**. **Non** dare il permesso Transfer (prelievi).
3. Algoritmo della firma: scegli **ECDSA** (è quello supportato con certezza dalla libreria ufficiale `coinbase-advanced-py`).
4. Scarica il file JSON e salvalo nella cartella del bot come `cdp_api_key.json`.
   In alternativa imposta le variabili d'ambiente `COINBASE_API_KEY` e `COINBASE_API_SECRET`.

Il file della chiave è segreto: non condividerlo e non caricarlo su GitHub (è già nel `.gitignore`).

## Strategia

Candele orarie di BTC-EUR, ETH-EUR, SOL-EUR.

- **Trend rialzista**: chiusura sopra la SMA 200 e SMA 20 sopra SMA 50.
- **Ritracciamento**: l'RSI(14) era sotto 40 sulla candela precedente e risale sopra 40 su quella appena chiusa.
- **Uscite** basate sull'ATR(14): stop loss a 1,5×ATR sotto l'entrata, take profit a 3×ATR sopra (rischio/rendimento 1:2).

## Limiti di rischio (`config.json`)

| Chiave | Default | Significato |
|---|---|---|
| `puntata_eur` | 6 | euro per operazione |
| `max_posizioni_aperte` | 1 | posizioni contemporanee |
| `max_operazioni_giorno` | 3 | acquisti al giorno (ora italiana) |
| `max_perdita_giornaliera_eur` | 1 | stop per la giornata |
| `commissione_taker` | 0.006 | commissione usata in paper e backtest (aggiornala con quella del tuo account) |
| `sl_offset_limite_pct` | 0.5 | distanza del prezzo limite sotto lo stop |
| `strategia.min_tp_su_commissioni` | 0 (spento) | se > 0, salta i segnali dove il take profit vale meno di X volte le commissioni andata e ritorno |

## Protezioni

- Lo **stop loss è un ordine stop-limit su Coinbase**: resta attivo anche se il bot si spegne.
- Il **take profit lo gestisce il bot**: quando il prezzo arriva al livello, annulla lo stop e vende a mercato.
- Se lo stop loss **non si riesce a piazzare**, il bot rivende subito e si ferma.
- Se lo stop risulta **annullato a mano** (o scaduto), il bot si ferma e lo scrive nel log.
- Se il prezzo scende in un colpo **sotto il limite** dello stop (lo stop-limit non verrebbe eseguito), il bot annulla lo stop e vende a mercato. Questo funziona solo mentre il bot è acceso.
- Lo stato (posizioni aperte, contatori del giorno) è salvato in `stato_live.json` / `stato_paper.json`: al riavvio il bot riprende da dove era.
- Quando si ferma per un problema di sicurezza, non riparte finché non controlli e lo avvii con `--sblocca`.

**Fermarlo**: `Ctrl+C`, oppure crea un file chiamato `STOP` nella cartella (cancellalo prima di riavviare).

## Modalità

```
python bot.py --check     # verifica chiave, coppie, candele, saldi e minimi d'ordine
python backtest.py        # backtest con divisione in-sample / out-of-sample
python bot.py             # paper: prezzi veri, operazioni simulate (non serve la chiave)
python bot.py --live      # soldi veri, chiede di scrivere CONFERMO
```

Ordine consigliato: `--check` → `backtest.py` → qualche settimana in paper → live solo se i numeri reggono.

## Registro operazioni

Ogni operazione chiusa finisce in `operazioni.csv` (separatore `;`, si apre con Excel):
date di apertura e chiusura, coppia, quantità, prezzi, euro spesi e incassati, commissioni, risultato netto, motivo d'uscita e ID degli ordini Coinbase.
Utile per la dichiarazione dei redditi: per le regole aggiornate su cripto-attività chiedi conferma a un commercialista.

## Avvertenze

- Con 6 € a operazione le **commissioni pesano molto**: ai livelli base di Coinbase un giro completo (acquisto + vendita a mercato) costa circa 1–1,2%. Su un take profit del 2–3% ne mangiano una buona parte. Guarda il backtest prima di andare live.
- Il backtest assume, se stop e take profit cadono nella stessa candela, che sia scattato lo stop (scelta prudente).
- I risultati passati non garantiscono quelli futuri.

---

# Agente trend + notizie (agente.py)

Unisce le idee migliori di TradingAgents, Signalforge e dei nostri test:

| Parte | Da dove viene | Cosa fa |
|---|---|---|
| Trend giornaliero SOL (media 10 sopra media 30) | i nostri backtest | l'unica strategia promossa all'esame |
| Agente notizie | Signalforge | legge RSS (CoinDesk, Cointelegraph, Decrypt, ...) e GDELT in parallelo; Claude Haiku da' a ogni titolo un punteggio -1..+1 |
| Dibattito rialzista/ribassista + risk manager | TradingAgents | solo prima di un acquisto; puo' solo BLOCCARLO |
| Stop loss su Coinbase, blocchi di sicurezza | il nostro bot | protezione anche a bot spento |

Regole: compra solo se trend su + punteggio notizie >= 0 + nessuna notizia shock + dibattito approva.
Esce se il trend finisce, se arriva una notizia shock (molto negativa e rilevante, ultime 3 ore)
o se il punteggio scende sotto -0.5. Massimo 1 ingresso al giorno, pausa di 12 ore dopo un'uscita.
L'AI non piazza mai ordini da sola.

Velocita' e costi: ogni titolo e' valutato una sola volta (cache_notizie.json) e tutti i titoli
nuovi vanno a Claude in un'unica chiamata. Con Haiku e un controllo all'ora: pochi euro al mese.
Senza chiave Anthropic funziona lo stesso con un dizionario di parole chiave (meno preciso).

## Uso

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."      # facoltativa ma consigliata
python agente.py --check                    # prova fonti, Claude, prezzi
python agente.py                            # simulazione, un controllo ogni ora
python agente.py --una-volta                # un solo controllo (per cron)
```

Registri: `decisioni_agente.csv` (ogni controllo con punteggio notizie, trend e motivo),
`operazioni_agente.csv` (operazioni chiuse), `agente.log`.
Impostazioni in `config_agente.json` (coppia, puntata, soglie, fonti, modelli).

Importante: le notizie NON si possono testare sul passato in modo onesto (l'AI conosce gia'
come sono andate). L'unico test valido e' la simulazione dal vivo per qualche settimana:
confronta nel CSV le decisioni con e senza il filtro notizie prima di pensare al live.

## Modalita' veloce (2-4 operazioni al giorno)

`config_veloce.json`: candele da 15 minuti, media 9 sopra media 21, filtro notizie e dibattito
come la lenta, stop 2xATR, take profit 3xATR, massimo 4 ingressi al giorno, pausa di 1 ora dopo
un'uscita, controllo ogni 15 minuti. Registri separati: `decisioni_veloce.csv`,
`operazioni_veloce.csv`, `agente_veloce.log`.

Per confrontarle, falle girare INSIEME in simulazione, in due terminali:

```
python agente.py                              # lenta  (giornaliera)
python agente.py --config config_veloce.json  # veloce (15 minuti)
```

Nota: nei backtest su SOL senza notizie, 2-3 operazioni al giorno con commissione 0,6% hanno
perso molto. Lascia decidere i due CSV dopo qualche settimana prima di pensare al live.
