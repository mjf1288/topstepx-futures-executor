import asyncio, os
from project_x_py import ProjectX

async def check():
    async with ProjectX.from_env() as client:
        await client.authenticate()
        for sym in ['MES', 'MNQ', 'MYM', 'MGC']:
            try:
                bars = await client.get_bars(sym, days=10, interval=1, unit=4)
                print(f'{sym}: {len(bars)} daily bars')
                closes = bars['close'].to_list()
                highs = bars['high'].to_list()
                lows = bars['low'].to_list()
                tick_val = {'MES': 5.0, 'MNQ': 2.0, 'MYM': 0.50, 'MGC': 10.0}[sym]

                trs3 = []
                for i in [-3, -2, -1]:
                    tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                    trs3.append(tr)
                atr3 = sum(trs3) / len(trs3)

                print(f'  3d ATR = {atr3:.1f} pts (${atr3 * tick_val:.0f})')
                for mult in [0.1, 0.15, 0.2, 0.236, 0.382]:
                    stop = atr3 * mult
                    target = stop * 2.618
                    print(f'    {mult}x: stop={stop:.1f}pts (${stop * tick_val:.0f}) | target={target:.1f}pts (${target * tick_val:.0f})')
            except Exception as e:
                print(f'{sym}: error {e}')

asyncio.run(check())
