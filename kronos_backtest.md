# Kronos sullo storico di SOL-EUR

Generato 2026-11-10 10:58 UTC. Una previsione a 24 ore al giorno per circa 60 giorni, 10 scenari ciascuna, usando solo i dati disponibili in quel momento.

| Modello | Storia | Direzione giusta | Errore medio | Errore di "resta uguale" | Media prevista | Media reale | Ancoraggio alla media | Correlazione col reale | Operazioni (≥ +2,3%, prob ≥ 70%) | € segnali | € comprando sempre |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Kronos-small | 336 h | 42% | 7.56% | 2.28% | +2.97% | -0.16% | 0.97 | -0.04 | 36 | -55.91 | -81.02 |
| Kronos-small | 168 h | 57% | 2.35% | 2.28% | -0.08% | -0.16% | 0.62 | +0.05 | 1 | -2.19 | -81.02 |
| Kronos-small | 72 h | 60% | 2.16% | 2.28% | -0.07% | -0.16% | 0.79 | +0.25 | 1 | +1.34 | -81.02 |
| Kronos-mini | 336 h | 55% | 2.45% | 2.28% | -0.07% | -0.16% | 0.27 | +0.13 | 1 | -1.40 | -81.02 |

Come leggerla:
- **Errore medio** più basso di quello di "resta uguale" = il modello aggiunge informazione.
- **Media prevista** molto diversa da quella reale = sbilanciamento sistematico.
- **Ancoraggio** vicino a 1 = la previsione copia solo il ritorno alla media della finestra.
- **Correlazione col reale** sopra +0,2 con molte previsioni = segnale interessante; intorno a 0 = rumore.
- **€ segnali**: risultato con commissioni comprando solo sui segnali forti.
