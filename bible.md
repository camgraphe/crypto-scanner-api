# Bible du trader — Crypto (H1 + 15m/5m)

## A. Checklist d’entrée
- Contexte BTC (H1) : structure HL/LH, RSI, au-dessus/au-dessous de l’EMA50.
- Macro < 24 h ? (FOMC/CPI/ECB) → réduire risk (rf≈0.7) et éviter moonshots.
- Breadth > EMA50 (H1) : >60% = vent de dos, <35% = prudence.
- Actif : tendance up (EMA20>50>200 et prix > EMA20) OU force relative forte.
- Qualité breakout : HH20/HH50, RVOL≥0.8, OB+ ; éviter breakouts sur OB-.
- Liquidité OK (sinon cap allocation, slippage max 0.6–0.8%).
- R:R min visé ≈ 2.0 (stop ≤ ~ATR%×1.1).

## B. Patterns utiles (H1)
- Bullish engulfing (sur support/AVWAP ou après contraction).
- Hammer (rejet mèche basse, sur EMA20/AVWAP).
- Doji = indécision, pas un achat seul.
- Shooting star = alerte.

## C. Triggers rapides (timing 15m/5m)
- 15m momentum : close > EMA9 > EMA21 = “15m-mom↑”.
- 5m HH20 + RVOL5≥1.2 = breakout crédible.
- Prix vs AVWAP (jour) : privilégier > AVWAP.
- OB+ renforce un breakout ; OB- pénalise.

## D. Playbooks
1) Breakout propre : Entrée sur HH + >AVWAP ; Stop = max(swing-low, ATR%×1.1) ; TP +6% (50%), +12% (25%), puis trailing (≈ ATR%×1.2).
2) Squeeze dérivés : (funding décile ≤2, OIΔ+) + 15m-mom↑ + HH5m ; Stop ATR%×1.2 ; sorties agressives.
3) VCP : contraction visible (ATR% décroissant), break de la 3ᵉ contraction ; Stop pivot – ATR%×1.1.

## E. Risque & sizing
- Risk / trade = 3.5% × rf. Stack max ≈ 7% × rf.
- Buckets : Core(2–3) cap 20–25% ; Satellite(3–4) cap 8–10% ; Moonshot(1–2) cap 3–4%.
- Δ_stop% = max(ATR%×1.1, 0.8%) ; qty = min(risk_usdc / (entry×Δ_stop%), cap_usdc / entry).
- Si liq=false : cap réduit, slippage max 0.8%.

## F. Gestion active
- À +12% : stop ≥ BE+ et trailing on.
- BTC casse EMA50 (H1) à la baisse : alléger 1/3 du panier.
- Print macro imminent : geler nouvelles entrées, réduire tailles.

## G. À éviter
- Moyenner à la baisse ; FOMO > +2% de l’entrée planifiée ; breakout sur OB- / RVOL faible.
