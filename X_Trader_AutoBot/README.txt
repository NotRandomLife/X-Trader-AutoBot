X-Trader AutoBot (Desktop)

Cosa fa
- Legge il segnale del sito come la pagina web: https://x-trader.cloud/latest.json
- Esegue ordini Binance Margin + SL/TP usando i parametri della GUI.
- Prefetch maxBorrowable 10s prima del boundary 5m e applica safety -leverage%.

"SITO: CONNESSO"
- Significa che l'ultima lettura di latest.json è OK negli ultimi 5 secondi.
- Se il sito è offline o la rete è KO, resta NON CONNESSO e non esegue trade.

Avvio (Windows)
1) pip install -r requirements.txt
2) python app.pyw
3) Premi START (ARMED)

Note
- Le API/Secret restano SOLO nella app desktop (mai nel sito).
