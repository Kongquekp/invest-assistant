"""平行世界回测器：5年历史模拟 + 300遍蒙特卡洛
用法: python backtest.py
产出: data/backtest.json, docs/backtest.html
注意: 回测使用"今日仍存续的ETF名单"，存在幸存者偏差，结果系统性偏乐观，仅用于了解策略脾气。
"""
import json
import os
import random
import time
import datetime

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config", "settings.json"), encoding="utf-8") as f:
    SETTINGS = json.load(f)

YEARS = 5
N_RUNS = 300
COMMISSION = 0.0001          # 万1
SLIP_BASE = 0.001            # 基准滑点 0.1%
MIN_TURNOVER = 1e8           # 当日成交额门槛 1亿
LOTS = 100                   # ETF最小交易单位100份
END_DATE = datetime.date.today().strftime("%Y%m%d")
START_DATE = (datetime.date.today() - datetime.timedelta(days=int(YEARS * 365.25) + 15)).strftime("%Y%m%d")

THEMES = ["红利", "科技", "芯片", "半导体", "AI", "人工智能", "软件", "云计算", "通信", "电子",
          "医药", "医疗", "生物", "证券", "券商", "银行", "保险", "金融", "地产", "军工", "国防",
          "新能源", "光伏", "电池", "储能", "汽车", "有色", "稀土", "钢铁", "煤炭", "消费", "食品",
          "白酒", "家电", "农业", "畜牧", "传媒", "游戏", "旅游", "创业板", "科创板", "科创",
          "沪深300", "中证500", "中证1000", "上证50", "A500", "港股", "恒生", "中概", "纳斯达克",
          "标普", "德国", "日经", "亚太", "法国"]


def theme_of(name):
    for kw in THEMES:
        if kw in name:
            return kw
    return "其他"


def load_data():
    import akshare as ak
    spot = ak.fund_etf_spot_em()
    excl = SETTINGS["scan"]["exclude_keywords"]
    names = {}
    for _, r in spot.iterrows():
        nm, code = str(r["名称"]), str(r["代码"]).zfill(6)
        if any(k in nm for k in excl):
            continue
        names[code] = nm
    bench = SETTINGS["benchmark"]["code"]
    names[bench] = SETTINGS["benchmark"]["name"]
    closes, amounts = {}, {}
    for idx, (code, nm) in enumerate(names.items()):
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                     start_date=START_DATE, end_date=END_DATE, adjust="qfq")
            if df is None or df.empty:
                continue
            df = df.rename(columns={"日期": "date", "收盘": "close", "成交额": "amount"})
            closes[code] = df.set_index("date")["close"]
            amounts[code] = df.set_index("date")["amount"]
        except Exception:
            continue
        time.sleep(0.8)
        if idx % 50 == 0:
            print(f"[data] 已抓 {idx + 1}/{len(names)}")
    close_df = pd.DataFrame(closes).sort_index()
    amount_df = pd.DataFrame(amounts).reindex(close_df.index)
    return close_df, amount_df, names


def precompute(close_df):
    ma20 = close_df.rolling(20, min_periods=20).mean()
    ma60 = close_df.rolling(60, min_periods=60).mean()
    ret20 = close_df / close_df.shift(20) - 1
    vol20 = close_df.pct_change().rolling(20, min_periods=20).std() * np.sqrt(252)
    trend = (close_df > ma20) & (ma20 >= ma60 * 0.98)
    score = (0.6 * ret20.fillna(0) + np.where(trend, 0.4, 0.0)
             - 0.5 * np.clip(vol20 - 0.25, 0, None)).fillna(0)
    return ma20, ma60, vol20, trend, score


def simulate(close_df, amount_df, names, ctx, seed=0, randomized=False):
    rng = random.Random(seed)
    ma20, ma60, vol20, trend, score = ctx
    dates = close_df.index
    bench = SETTINGS["benchmark"]["code"]
    cash = float(SETTINGS["capital"])
    holdings = {}
    pending = []
    nav_series = []
    trades = []

    def exec_buy(code, i, budget_left):
        nonlocal cash
        px = close_df[code].iloc[i]
        if pd.isna(px) or px <= 0:
            return budget_left
        slip = rng.uniform(0.0005, 0.003) if randomized else SLIP_BASE
        px_in = px * (1 + slip)
        lots = int(budget_left / (px_in * (1 + COMMISSION)) / LOTS) * LOTS
        if lots < LOTS:
            return budget_left
        cost = lots * px_in * (1 + COMMISSION)
        nav = cash + sum(h["shares"] * (close_df[c].iloc[i] if not pd.isna(close_df[c].iloc[i]) else h["cost"])
                         for c, h in holdings.items())
        if cost > cash - nav * SETTINGS["min_cash_pct"] / 100:
            return budget_left
        cash -= cost
        holdings[code] = {"shares": lots, "cost": px_in, "peak": px_in}
        trades.append({"date": str(dates[i])[:10], "code": code, "name": names.get(code, code),
                       "action": "buy", "price": round(px_in, 3), "amount": round(cost, 0),
                       "why": "signal", "pnl_pct": None})
        return budget_left - cost

    for i in range(len(dates)):
        day = dates[i]
        b_px = close_df[bench].iloc[i]
        # ---- 1. 卖出检查 ----
        for code in list(holdings):
            h = holdings[code]
            px = close_df[code].iloc[i]
            m20 = ma20[code].iloc[i]
            if pd.isna(px) or px <= 0:
                continue
            if px > h["peak"]:
                h["peak"] = px
            why = None
            if px <= h["cost"] * (1 - SETTINGS["stop_loss_pct"] / 100):
                why = "stop"
            elif (h["peak"] >= h["cost"] * (1 + SETTINGS["take_profit_pct"] / 100)
                  and px <= h["peak"] * (1 - SETTINGS["trailing_drawdown_pct"] / 100)):
                why = "trailing"
            elif SETTINGS.get("ma_exit", True) and not pd.isna(m20) and px < m20:
                why = "ma_exit"
            if why:
                gap = rng.uniform(0, 0.005) if (randomized and why in ("stop", "trailing")) else 0.0
                slip = (rng.uniform(0.0005, 0.003) if randomized else SLIP_BASE) + gap
                px_out = px * (1 - slip)
                proceeds = h["shares"] * px_out * (1 - COMMISSION)
                cash += proceeds
                trades.append({"date": str(day)[:10], "code": code, "name": names.get(code, code),
                               "action": "sell", "price": round(px_out, 3),
                               "amount": round(proceeds, 0), "why": why,
                               "pnl_pct": round((px_out / h["cost"] - 1) * 100, 2)})
                del holdings[code]
        # ---- 2. 账户状态与仓位计划 ----
        nav = cash + sum(h["shares"] * (close_df[c].iloc[i] if not pd.isna(close_df[c].iloc[i]) else h["cost"])
                         for c, h in holdings.items())
        nav_series.append(nav)
        if pd.isna(b_px) or i < 60:
            continue
        b_m20, b_m60 = close_df[bench].rolling(20).mean().iloc[i], close_df[bench].rolling(60).mean().iloc[i]
        regime = ("risk_on" if b_px > b_m20 > b_m60
                  else "risk_off" if b_px < b_m20 < b_m60 else "neutral")
        pnl_pct = (nav / float(SETTINGS["capital"]) - 1) * 100
        slots = max(0, SETTINGS["max_positions"] - len(holdings))
        if pnl_pct <= SETTINGS["halt_threshold_pct"]:
            continue
        single_pct = SETTINGS["max_single_pct"].get(regime, 15)
        if pnl_pct <= SETTINGS["reduce_threshold_pct"]:
            single_pct /= 2
        single_cap = min(nav, float(SETTINGS["capital"])) * single_pct / 100
        budget = max(0.0, min(cash - nav * SETTINGS["min_cash_pct"] / 100, single_cap * slots))
        # ---- 3. 延迟买入成交 ----
        for p in list(pending):
            if p[0] <= i and p[1] not in holdings:
                budget = exec_buy(p[1], i, budget)
            pending.remove(p)
        # ---- 4. 扫描选候选 ----
        if budget < 100 or slots <= 0:
            continue
        sc = score.iloc[i]
        mask = ((ma60.iloc[i].notna()) & (vol20.iloc[i].notna()) & (vol20.iloc[i] <= 0.5)
                & (amount_df.iloc[i] >= MIN_TURNOVER) & trend.iloc[i]
                & close_df.iloc[i].notna() & (close_df.iloc[i] > 0)
                & (~sc.index.isin(holdings.keys())))
        cand = sc[mask].sort_values(ascending=False)
        cnt = {}
        bought = 0
        for code in cand.index:
            if bought >= slots or budget < 100:
                break
            th = theme_of(names.get(code, ""))
            if cnt.get(th, 0) >= SETTINGS["scan"]["max_per_theme"]:
                continue
            if randomized and rng.random() < 0.05:
                continue
            cnt[th] = cnt.get(th, 0) + 1
            if randomized and rng.choice([0, 1]) == 1:
                pending.append((i + 1, code))
            else:
                budget = exec_buy(code, i, min(budget, single_cap))
            bought += 1

    nav_arr = np.array(nav_series, dtype=float)
    peak = np.maximum.accumulate(nav_arr)
    maxdd = float(((nav_arr / peak) - 1).min() * 100)
    sells = [t for t in trades if t["action"] == "sell"]
    wins = [t for t in sells if (t["pnl_pct"] or 0) > 0]
    days = len(nav_arr)
    return {
        "nav": nav_arr.tolist(),
        "final_ret_pct": round((nav_arr[-1] / float(SETTINGS["capital"]) - 1) * 100, 2),
        "ann_ret_pct": round(((nav_arr[-1] / float(SETTINGS["capital"])) ** (252 / max(days, 1)) - 1) * 100, 2),
        "max_dd_pct": round(maxdd, 2),
        "n_trades": len(sells),
        "win_rate_pct": round(len(wins) / max(len(sells), 1) * 100, 1),
        "trades": trades,
    }


def main():
    close_df, amount_df, names = load_data()
    print(f"[data] 共 {close_df.shape[1]} 只ETF，{len(close_df)} 个交易日")
    ctx = precompute(close_df)
    dates = [str(d)[:10] for d in close_df.index]

    base = simulate(close_df, amount_df, names, ctx, seed=42, randomized=False)
    print(f"[base] 5年收益 {base['final_ret_pct']}%，最大回撤 {base['max_dd_pct']}%，交易 {base['n_trades']} 笔")

    runs = [base]
    for k in range(N_RUNS):
        runs.append(simulate(close_df, amount_df, names, ctx, seed=1000 + k, randomized=True))
        if (k + 1) % 50 == 0:
            print(f"[mc] {k + 1}/{N_RUNS}")

    mat = np.array([r["nav"] for r in runs[1:]])
    p5 = np.percentile(mat, 5, axis=0).tolist()
    p50 = np.percentile(mat, 50, axis=0).tolist()
    p95 = np.percentile(mat, 95, axis=0).tolist()

    def pct(key, fn):
        vals = sorted(fn(r) for r in runs[1:])
        return {"p5": vals[int(N_RUNS * 0.05)], "p50": vals[N_RUNS // 2],
                "p95": vals[int(N_RUNS * 0.95)]}

    result = {
        "period": f"{dates[0]} ~ {dates[-1]}",
        "universe": int(close_df.shape[1]),
        "runs": N_RUNS + 1,
        "base": {k: base[k] for k in ["final_ret_pct", "ann_ret_pct", "max_dd_pct",
                                      "n_trades", "win_rate_pct"]},
        "mc": {
            "final_ret_pct": pct("final", lambda r: r["final_ret_pct"]),
            "max_dd_pct": pct("dd", lambda r: r["max_dd_pct"]),
            "win_rate_pct": pct("wr", lambda r: r["win_rate_pct"]),
            "n_trades": pct("nt", lambda r: r["n_trades"]),
        },
        "curves": {"dates": dates, "base": base["nav"], "p5": p5, "p50": p50, "p95": p95},
        "base_trades": base["trades"][-20:],
    }
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(os.path.join(BASE, "data", "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    render_html(result)
    print("[done] data/backtest.json 与 docs/backtest.html 已生成")


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f172a"><title>平行世界回测</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#0f172a;color:#e2e8f0;padding:12px 12px 40px}
.card{background:#1e293b;border-radius:14px;padding:14px;margin-bottom:12px}
h1{font-size:19px}h2{font-size:15px;color:#94a3b8;margin-bottom:10px}
.sub{color:#64748b;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.stat{background:#0f172a;border-radius:10px;padding:10px}
.stat .v{font-size:18px;font-weight:700;margin-top:2px}.stat .k{font-size:11px;color:#64748b}
.pos{color:#4ade80}.neg{color:#f87171}.num{font-variant-numeric:tabular-nums}
.warn{background:#422006;color:#fdba74;border-radius:10px;padding:10px;font-size:12px;line-height:1.7}
table{width:100%;font-size:12px;border-collapse:collapse}
td,th{padding:6px 4px;border-bottom:1px solid #293548;text-align:left}
th{color:#64748b;font-weight:500}
</style></head><body>
<div class="card"><h1>🌍 平行世界回测报告</h1>
<div class="sub" id="period"></div>
<div class="grid" id="cards"></div></div>
<div class="card"><h2>📈 净值曲线（蓝=基准世界，绿带=300个平行世界的5%~95%区间）</h2>
<svg id="chart" viewBox="0 0 350 180" style="width:100%;background:#0f172a;border-radius:10px"></svg>
<div class="sub" style="margin-top:6px">绿实线=中位世界 ｜ 蓝线=基准世界（无随机扰动）</div></div>
<div class="card"><h2>📋 基准世界最近交易</h2><table id="trades"></table></div>
<div class="card warn">⚠️ 本回测使用今日仍存续的ETF名单（幸存者偏差，结果偏乐观）；未考虑折溢价、流动性冲击与停牌；历史表现不代表未来。本报告用于理解策略的脾气（回撤深度、连亏频率、仓位暴露），不构成任何投资建议。</div>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
$("period").textContent=`区间 ${D.period} ｜ 样本池 ${D.universe} 只ETF ｜ ${D.runs} 个世界`;
const cls=v=>v>0?"pos":v<0?"neg":"";
const pct=v=>`<span class="${cls(v)}">${v>0?"+":""}${v}%</span>`;
const mc=D.mc;
$("cards").innerHTML=`
<div class="stat"><div class="k">5年总收益（基准世界）</div><div class="v num">${pct(D.base.final_ret_pct)}</div></div>
<div class="stat"><div class="k">300世界收益中位数</div><div class="v num">${pct(mc.final_ret_pct.p50)}</div></div>
<div class="stat"><div class="k">收益最差/最好世界</div><div class="v num">${pct(mc.final_ret_pct.p5)} ~ ${pct(mc.final_ret_pct.p95)}</div></div>
<div class="stat"><div class="k">最大回撤（中位/最差）</div><div class="v num neg">${mc.max_dd_pct.p50}% / ${mc.max_dd_pct.p5}%</div></div>
<div class="stat"><div class="k">胜率（中位）</div><div class="v num">${mc.win_rate_pct.p50}%</div></div>
<div class="stat"><div class="k">交易笔数（中位）</div><div class="v num">${mc.n_trades.p50} 笔</div></div>`;
const C=D.curves, n=C.dates.length;
const all=[...C.p95,...C.p5,...C.base];
const lo=Math.min(...all)*0.98, hi=Math.max(...all)*1.02;
const X=i=>10+i/(n-1)*330, Y=v=>170-(v-lo)/(hi-lo)*155;
let s=`<line x1="10" y1="170" x2="340" y2="170" stroke="#334155"/><line x1="10" y1="15" x2="10" y2="170" stroke="#334155"/>`;
s+=`<text x="14" y="14" fill="#64748b" font-size="9">${Math.round(hi)}</text><text x="14" y="168" fill="#64748b" font-size="9">${Math.round(lo)}</text>`;
const line=(arr,col,w)=>`<polyline fill="none" stroke="${col}" stroke-width="${w}" points="${arr.map((v,i)=>X(i).toFixed(1)+","+Y(v).toFixed(1)).join(" ")}"/>`;
const band=`<polygon fill="#14532d" opacity="0.35" points="${C.p95.map((v,i)=>X(i)+","+Y(v)).join(" ")} ${C.p5.slice().reverse().map((v,i)=>X(n-1-i)+","+Y(v)).join(" ")}"/>`;
s+=band+line(C.p50,"#4ade80",1.5)+line(C.base,"#60a5fa",1.5);
$("chart").innerHTML=s;
const t=D.base_trades.slice().reverse();
$("trades").innerHTML=t.length?`<tr><th>日期</th><th>标的</th><th>动作</th><th>价格</th><th>盈亏</th><th>原因</th></tr>`+t.map(x=>
`<tr><td>${x.date}</td><td>${x.name}</td><td>${x.action==="buy"?"买入":"卖出"}</td><td class="num">${x.price}</td><td class="num ${cls(x.pnl_pct)}">${x.pnl_pct==null?"—":x.pnl_pct+"%"}</td><td>${x.why}</td></tr>`).join(""):`<tr><td>无交易</td></tr>`;
</script></body></html>"""


def render_html(result):
    os.makedirs(os.path.join(BASE, "docs"), exist_ok=True)
    with open(os.path.join(BASE, "docs", "backtest.html"), "w", encoding="utf-8") as f:
        f.write(HTML.replace("__DATA__", json.dumps(result, ensure_ascii=False)))


if __name__ == "__main__":
    main()
