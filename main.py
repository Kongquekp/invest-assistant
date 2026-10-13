"""私人投资助手 · 单文件版（A股/场内ETF）
用法: python main.py scan | ledger | report | dashboard
"""
import csv
import json
import os
import re
import sys
import time
import datetime
from datetime import timezone, timedelta
from zoneinfo import ZoneInfo

# ============ 配置 ============
BASE = os.path.dirname(os.path.abspath(__file__))


def load_settings():
    with open(os.path.join(BASE, "config", "settings.json"), encoding="utf-8") as f:
        return json.load(f)


DATA = os.path.join(BASE, "data")


def _read_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ============ 数据源（重试+限速） ============
import akshare as ak  # noqa: E402

LAST_CALL = [0.0]
MIN_INTERVAL = 1.0


def _throttle():
    gap = time.time() - LAST_CALL[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    LAST_CALL[0] = time.time()


def _retry(fn, retries=3, base_delay=3):
    last = None
    for i in range(retries):
        try:
            _throttle()
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(base_delay * (i + 1))
    raise last


def etf_spot():
    return _retry(lambda: ak.fund_etf_spot_em())


def _hist(fn, code, days=160):
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y%m%d")
    df = _retry(lambda: fn(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"))
    if df is None or df.empty:
        raise ValueError(f"无历史数据: {code}")
    df = df.rename(columns={"日期": "date", "收盘": "close", "成交额": "amount"})
    df["date"] = df["date"].astype(str)
    keep = ["date", "close"] + (["amount"] if "amount" in df.columns else [])
    return df[keep].reset_index(drop=True)


def etf_hist(code, days=160):
    return _hist(ak.fund_etf_hist_em, code, days)


def stock_hist(code, days=160):
    return _hist(ak.stock_zh_a_hist, code, days)


def latest_price(code, is_etf=True):
    try:
        df = etf_hist(code, 5) if is_etf else stock_hist(code, 5)
        return float(df["close"].iloc[-1])
    except Exception:
        return None


# ============ 仓位规则 ============
def account_state(ledger_rows, latest_prices, settings):
    capital = float(settings["capital"])
    cash = capital
    holdings = {}
    for r in ledger_rows:
        amt = float(r["amount"])
        if r["action"] == "buy":
            cash -= amt
            h = holdings.setdefault(r["code"], {"name": r["name"], "shares": 0.0, "cost": 0.0})
            px = float(r.get("price") or 0)
            if px > 0:
                add = amt / px
                h["cost"] = (h["cost"] * h["shares"] + amt) / (h["shares"] + add)
                h["shares"] += add
        elif r["action"] == "sell":
            cash += amt
            h = holdings.get(r["code"])
            if h:
                px = float(r.get("price") or latest_prices.get(r["code"]) or h["cost"])
                h["shares"] = max(0.0, h["shares"] - amt / px)
                if h["shares"] <= 1e-6:
                    holdings.pop(r["code"], None)
    mv = sum(h["shares"] * latest_prices.get(c, h["cost"]) for c, h in holdings.items())
    nav = cash + mv
    return {
        "cash": round(cash, 2), "market_value": round(mv, 2), "nav": round(nav, 2),
        "pnl_pct": round((nav / capital - 1) * 100, 2),
        "invested_pct": round(mv / capital * 100, 1),
        "holdings": [{
            "code": c, "name": h["name"], "shares": round(h["shares"], 2),
            "cost": round(h["cost"], 4), "price": latest_prices.get(c),
            "value": round(h["shares"] * (latest_prices.get(c) or h["cost"]), 2),
        } for c, h in holdings.items()],
    }


def position_plan(settings, regime, account):
    capital = float(settings["capital"])
    nav = account["nav"]
    slots = max(0, settings["max_positions"] - len(account["holdings"]))
    pnl = account["pnl_pct"]
    if pnl <= settings["halt_threshold_pct"]:
        mode = "HALT"
    elif pnl <= settings["reduce_threshold_pct"]:
        mode = "REDUCE"
    else:
        mode = "NORMAL"
    single_pct = settings["max_single_pct"].get(regime, 15)
    if mode == "REDUCE":
        single_pct /= 2
    single_cap = min(nav * single_pct / 100, capital * single_pct / 100)
    min_cash = nav * settings["min_cash_pct"] / 100
    budget = max(0.0, min(account["cash"] - min_cash, single_cap * slots))
    return {
        "regime": regime, "sizing_mode": mode, "slots": slots,
        "max_single_amount": round(single_cap, 0),
        "total_buy_budget": round(budget, 0),
        "stop_loss_pct": settings["stop_loss_pct"],
        "take_profit_pct": settings["take_profit_pct"],
        "trailing_drawdown_pct": settings["trailing_drawdown_pct"],
        "min_cash": round(min_cash, 0),
        "rules_text": (
            f"市场状态={regime}，仓位模式={mode}；单只上限{round(single_cap)}元，"
            f"本轮预算{round(budget)}元，剩余仓位{slots}个；"
            f"止损-{settings['stop_loss_pct']}%，止盈+{settings['take_profit_pct']}%后"
            f"回落{settings['trailing_drawdown_pct']}%离场。"),
    }


# ============ 扫描 ============
THEMES = ["红利", "科技", "芯片", "半导体", "AI", "人工智能", "软件", "云计算", "通信", "电子",
          "医药", "医疗", "生物", "证券", "券商", "银行", "保险", "金融", "地产", "军工", "国防",
          "新能源", "光伏", "电池", "储能", "汽车", "有色", "稀土", "钢铁", "煤炭", "消费", "食品",
          "白酒", "家电", "农业", "畜牧", "传媒", "游戏", "旅游", "创业板", "科创板", "科创",
          "沪深300", "中证500", "中证1000", "上证50", "A500", "港股", "恒生", "中概", "纳斯达克",
          "标普", "德国", "日经", "亚太", "法国"]


def _theme(name):
    for kw in THEMES:
        if kw in name:
            return kw
    return "其他"


def today_str():
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def cmd_scan():
    settings = load_settings()
    cfg = settings["scan"]
    today = today_str()
    bench = settings["benchmark"]["code"]
    bench_hist = etf_hist(bench)
    if bench_hist["date"].iloc[-1] != today:
        print(f"[scan] {today} 非交易日或数据未更新，跳过")
        return
    spot = etf_spot()
    for kw in cfg["exclude_keywords"]:
        spot = spot[~spot["名称"].str.contains(kw, na=False)]
    spot = spot[spot["成交额"] >= cfg["min_turnover_wan"] * 10000]
    rows = []
    for _, r in spot.iterrows():
        code = str(r["代码"]).zfill(6)
        try:
            h = etf_hist(code, cfg["lookback_days"])
        except Exception:
            continue
        if len(h) < 60:
            continue
        close = h["close"]
        last = float(close.iloc[-1])
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean())
        ret20 = last / float(close.iloc[-21]) - 1 if len(close) > 20 else 0
        vol20 = float(close.pct_change().dropna().tail(20).std() * (252 ** 0.5))
        if vol20 > 0.5:
            continue
        trend_ok = last > ma20 and ma20 >= ma60 * 0.98
        score = 0.6 * ret20 + (0.4 if trend_ok else 0.0) - 0.5 * max(0.0, vol20 - 0.25)
        rows.append({"code": code, "name": str(r["名称"]), "theme": _theme(str(r["名称"])),
                     "price": round(last, 3), "ret20_pct": round(ret20 * 100, 2),
                     "ma20": round(ma20, 3), "ma60": round(ma60, 3),
                     "vol20_pct": round(vol20 * 100, 1), "trend_ok": trend_ok,
                     "turnover_yi": round(float(r["成交额"]) / 1e8, 2), "score": round(score, 4)})
    rows.sort(key=lambda x: x["score"], reverse=True)
    picked, cnt = [], {}
    for row in rows:
        if cnt.get(row["theme"], 0) >= cfg["max_per_theme"]:
            continue
        picked.append(row)
        cnt[row["theme"]] = cnt.get(row["theme"], 0) + 1
        if len(picked) >= cfg["top_n"]:
            break
    fixed = {str(s["code"]).zfill(6) for s in settings.get("fixed_symbols", [])}
    for row in rows:
        if row["code"] in fixed and all(p["code"] != row["code"] for p in picked):
            picked.insert(0, row)
    c = bench_hist["close"]
    regime = ("risk_on" if c.iloc[-1] > c.tail(20).mean() > c.tail(60).mean()
              else "risk_off" if c.iloc[-1] < c.tail(20).mean() < c.tail(60).mean()
              else "neutral")
    _write_json(os.path.join(DATA, "watchlist.json"), {
        "date": today, "regime": regime, "scanned": len(rows),
        "benchmark": {"code": bench, "name": settings["benchmark"]["name"],
                      "price": round(float(c.iloc[-1]), 3)},
        "candidates": picked})
    print(f"[scan] 扫描{len(rows)}只，入选{len(picked)}只，市场={regime}")


# ============ 记账 ============
FIELDS = ["issue", "date", "action", "code", "name", "price", "amount", "reason"]


def _ledger_csv_path():
    return os.path.join(DATA, "ledger.csv")


def cmd_ledger():
    import requests
    settings = load_settings()
    path = _ledger_csv_path()
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    known = {int(r["issue"]) for r in rows if r.get("issue")}
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if repo and token:
        try:
            url = f"https://api.github.com/repos/{repo}/issues"
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            issues, page = [], 1
            while True:
                r = requests.get(url, headers=headers, timeout=30,
                                 params={"state": "all", "labels": "trade",
                                         "per_page": 100, "page": page})
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                issues.extend(batch)
                page += 1
            for issue in issues:
                if issue.get("pull_request") or issue["number"] in known:
                    continue
                kv = {}
                for line in (issue.get("body") or "").splitlines():
                    m = re.match(r"^\s*([a-zA-Z_]+)\s*[:：]\s*(.+)$", line.strip())
                    if m:
                        kv[m.group(1).lower()] = m.group(2).strip()
                try:
                    row = {"issue": issue["number"],
                           "date": kv.get("date", issue["created_at"][:10]),
                           "action": kv["action"].lower(),
                           "code": str(kv["code"]).zfill(6),
                           "name": kv.get("name", ""),
                           "price": float(kv.get("price", 0) or 0),
                           "amount": float(kv["amount"]),
                           "reason": kv.get("reason", "")}
                    if row["action"] in ("buy", "sell"):
                        rows.append(row)
                except (KeyError, ValueError):
                    continue
        except Exception as e:  # noqa: BLE001
            print(f"[ledger] 拉取issue失败，沿用本地账本: {e}")
    rows.sort(key=lambda r: (r["date"], int(r["issue"] or 0)))
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    latest = {}
    for r in rows:
        if r["code"] not in latest:
            latest[r["code"]] = latest_price(r["code"], is_etf=True)
    account = account_state(rows, latest, settings)
    _write_json(os.path.join(DATA, "ledger.json"), {
        "updated": datetime.datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "trades": list(reversed(rows)), "account": account})
    print(f"[ledger] 共{len(rows)}笔，持仓{len(account['holdings'])}只，净值{account['nav']}元")
    cmd_dashboard()


# ============ 看板 ============
DASH_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f172a"><title>投资助手</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#0f172a;color:#e2e8f0;padding:12px 12px 40px}
.card{background:#1e293b;border-radius:14px;padding:14px;margin-bottom:12px}
h1{font-size:19px;margin-bottom:2px}h2{font-size:15px;color:#94a3b8;margin-bottom:10px;font-weight:600}
.sub{color:#64748b;font-size:12px}
.row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #293548;font-size:14px}
.row:last-child{border-bottom:none}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px}
.risk_on{background:#14532d;color:#86efac}.neutral{background:#713f12;color:#fde68a}.risk_off{background:#7f1d1d;color:#fca5a5}
.HALT{background:#7f1d1d;color:#fca5a5}.REDUCE{background:#713f12;color:#fde68a}.NORMAL{background:#14532d;color:#86efac}
.buy{color:#4ade80}.sell{color:#f87171}.hold{color:#facc15}
.num{font-variant-numeric:tabular-nums}.pos{color:#4ade80}.neg{color:#f87171}
.reason{color:#94a3b8;font-size:12px;margin-top:3px;line-height:1.5}
.warn{background:#422006;color:#fdba74;border-radius:10px;padding:10px;font-size:13px;line-height:1.6}
.empty{color:#475569;text-align:center;padding:16px 0;font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.stat{background:#0f172a;border-radius:10px;padding:10px}.stat .v{font-size:17px;font-weight:700;margin-top:2px}.stat .k{font-size:11px;color:#64748b}
</style></head><body>
<div class="card"><h1>📈 投资助手 <span class="sub" id="date"></span></h1>
<div class="sub">市场 <span id="regime" class="tag"></span> ｜ 模式 <span id="mode" class="tag"></span></div>
<div class="grid">
<div class="stat"><div class="k">总资产</div><div class="v num" id="nav"></div></div>
<div class="stat"><div class="k">累计盈亏</div><div class="v num" id="pnl"></div></div>
<div class="stat"><div class="k">可用现金</div><div class="v num" id="cash"></div></div>
<div class="stat"><div class="k">本轮买入预算</div><div class="v num" id="budget"></div></div>
</div></div>
<div class="card"><h2>🧭 市场解读</h2>
<div id="market_comment" class="sub" style="color:#cbd5e1;font-size:13px;line-height:1.7"></div>
<div id="risk" class="warn" style="margin-top:10px;display:none"></div></div>
<div class="card"><h2>🎯 今日操作单</h2><div id="candidates"></div></div>
<div class="card"><h2>💼 当前持仓</h2><div id="holdings"></div></div>
<div class="card"><h2>📒 操作记录（含当时理由）</h2><div id="trades"></div></div>
<div class="card"><h2>⚙️ 内置规则</h2><div class="sub" id="rules" style="font-size:12px;line-height:1.8"></div></div>
<script>
const D=__DATA__;const $=id=>document.getElementById(id);
const fmt=(n,d=0)=>n==null?"—":Number(n).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
const cls=v=>v>0?"pos":v<0?"neg":"";
$("date").textContent=D.report?.date||D.watchlist?.date||"";
const regime=D.watchlist?.regime||"neutral";
$("regime").textContent={risk_on:"强势",neutral:"震荡",risk_off:"弱势"}[regime];
$("regime").className="tag "+regime;
const mode=D.report?.plan?.sizing_mode||"NORMAL";
$("mode").textContent={NORMAL:"正常",REDUCE:"减仓",HALT:"停止买入"}[mode];
$("mode").className="tag "+mode;
$("nav").textContent=fmt(D.report?.nav)+" 元";
const pnl=D.report?.pnl_pct;
$("pnl").innerHTML=pnl==null?"—":`<span class="${cls(pnl)}">${pnl>0?"+":""}${fmt(pnl,2)}%</span>`;
$("cash").textContent=fmt(D.report?.cash)+" 元";
$("budget").textContent=fmt(D.report?.plan?.total_buy_budget)+" 元";
$("market_comment").textContent=D.report?.market_comment||"暂无";
if(D.report?.risk_warning){$("risk").style.display="block";$("risk").textContent="⚠️ "+D.report.risk_warning;}
const sigMap={buy:["🟢 买入","buy"],sell:["🔴 卖出","sell"],hold:["🟡 持有","hold"],watch:["⚪ 观察",""]};
const cand=D.report?.candidates||[];
$("candidates").innerHTML=cand.length?cand.map(c=>`
<div class="row" style="display:block"><div><b>${c.name||""}</b> <span class="sub">${c.code||""}</span>
<span class="${sigMap[c.signal]?.[1]||""}">${sigMap[c.signal]?.[0]||c.signal}</span>
${c.signal==="buy"?`<span class="num sub"> ≈${fmt(c.suggested_amount)}元 ｜ 止损 ${c.stop_loss} ｜ 止盈 ${c.take_profit}</span>`:""}</div>
<div class="reason">${c.reason||""}</div></div>`).join(""):`<div class="empty">今日无候选</div>`;
const holdings=D.ledger?.account?.holdings||[];
$("holdings").innerHTML=holdings.length?holdings.map(h=>{
const pnlH=h.cost&&h.price?(h.price/h.cost-1)*100:null;
return`<div class="row" style="display:block"><div><b>${h.name}</b> <span class="sub">${h.code}</span>
<span class="num ${cls(pnlH)}">${pnlH==null?"":(pnlH>0?"+":"")+fmt(pnlH,2)+"%"}</span></div>
<div class="reason">${fmt(h.shares,2)} 份 ｜ 成本 ${h.cost} ｜ 现价 ${h.price??"—"} ｜ 市值 ${fmt(h.value)} 元</div></div>`;
}).join(""):`<div class="empty">暂无持仓（交易后在 GitHub Issues 记账即可同步）</div>`;
const trades=(D.ledger?.trades||[]).slice(0,30);
$("trades").innerHTML=trades.length?trades.map(t=>`
<div class="row" style="display:block"><div><span class="${t.action}">${t.action==="buy"?"买入":"卖出"}</span>
<b>${t.name||t.code}</b> <span class="sub">${t.code} ｜ ${fmt(t.amount)}元</span></div>
<div class="reason">${t.date} ｜ ${t.reason||"未填写理由"}</div></div>`).join(""):`<div class="empty">暂无记录</div>`;
const s=D.settings||{};
$("rules").innerHTML=`本金 ${fmt(s.capital)} 元 ｜ 最大回撤 ${s.max_drawdown_pct}% ｜ 最多 ${s.max_positions} 只<br>
单笔止损 -${s.stop_loss_pct}% ｜ 止盈 +${s.take_profit_pct}% ｜ 现金 ≥${s.min_cash_pct}%<br>
账户回撤 -10% 新开仓减半，-15% 停止买入`;
</script></body></html>"""


def cmd_dashboard():
    settings = load_settings()
    watchlist = _read_json(os.path.join(DATA, "watchlist.json"), {})
    ledger = _read_json(os.path.join(DATA, "ledger.json"), None)
    if ledger is None:
        ledger = {"account": account_state([], {}, settings), "trades": []}
    report = _read_json(os.path.join(DATA, "report.json"), {})
    payload = {"settings": {k: settings[k] for k in
                            ["capital", "max_drawdown_pct", "max_positions",
                             "stop_loss_pct", "take_profit_pct", "min_cash_pct"]},
               "watchlist": watchlist, "ledger": ledger, "report": report}
    os.makedirs(os.path.join(BASE, "docs"), exist_ok=True)
    with open(os.path.join(BASE, "docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(DASH_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False)))
    print("[dashboard] docs/index.html 已生成")


# ============ 报告 ============
def deepseek_report(prompt, api_key):
    import requests
    r = requests.post("https://api.deepseek.com/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}"},
                      json={"model": "deepseek-chat", "temperature": 0.2,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content":
                                 "你是谨慎的A股ETF投资助手。只输出JSON，不输出任何多余文字。"
                                 "所有买卖金额必须严格遵守给定的仓位规则上限，不得编造行情数字。"},
                                {"role": "user", "content": prompt}]},
                      timeout=120)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def enforce_guardrails(ai, plan):
    if not isinstance(ai, dict):
        return None
    budget = plan["total_buy_budget"]
    used = 0.0
    for c in ai.get("candidates", []):
        if not isinstance(c, dict):
            continue
        sig = str(c.get("signal", "watch")).lower()
        amt = float(c.get("suggested_amount") or 0)
        if sig == "buy":
            if budget <= 0 or plan["slots"] <= 0:
                c["signal"] = "watch"
                c["suggested_amount"] = 0
                c["reason"] = (str(c.get("reason", "")) + "【规则压制：无买入预算】").strip()
                continue
            amt = min(amt, plan["max_single_amount"], budget - used)
            if amt < 100:
                c["signal"] = "watch"
                c["suggested_amount"] = 0
            else:
                c["suggested_amount"] = round(amt, 0)
                used += amt
        ep = float(c.get("entry_price") or 0)
        if ep > 0:
            c["stop_loss"] = round(ep * (1 - plan["stop_loss_pct"] / 100), 3)
            c["take_profit"] = round(ep * (1 + plan["take_profit_pct"] / 100), 3)
    return ai


def cmd_report():
    import requests
    settings = load_settings()
    watchlist = _read_json(os.path.join(DATA, "watchlist.json"), None)
    if not watchlist:
        print("[report] 无 watchlist，先运行 scan")
        return
    ledger = _read_json(os.path.join(DATA, "ledger.json"), None)
    if ledger is None:
        ledger = {"account": account_state([], {}, settings), "trades": []}
    plan = position_plan(settings, watchlist.get("regime", "neutral"), ledger["account"])
    sell_signals = []
    if settings.get("ma_exit", True):
        for h in ledger["account"].get("holdings", []):
            try:
                hh = etf_hist(h["code"], 120)
                cc = hh["close"]
                ma20 = float(cc.tail(20).mean())
                last = float(cc.iloc[-1])
                if last < ma20:
                    sell_signals.append({"code": h["code"], "name": h["name"], "signal": "sell",
                                         "reason": f"持仓跌破MA20({round(ma20, 3)})，趋势破坏，建议离场回收仓位"})
            except Exception:
                continue
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    ai = None
    if api_key:
        prompt = (
            "请根据以下真实数据生成今日决策报告，只输出JSON：\n"
            'JSON格式：{"market_comment": str, "candidates": [{"code","name",'
            '"signal":"buy"|"hold"|"watch","suggested_amount":number,'
            '"entry_price":number,"stop_loss":number,"take_profit":number,'
            '"reason":str}],"portfolio_comment":str,"risk_warning":str}\n'
            f"今日：{watchlist['date']}，市场状态：{watchlist['regime']}，基准：{watchlist['benchmark']}\n"
            f"候选池：{json.dumps(watchlist['candidates'], ensure_ascii=False)}\n"
            f"账户：{json.dumps(ledger['account'], ensure_ascii=False)}\n"
            f"历史操作：{json.dumps(ledger['trades'][:10], ensure_ascii=False)}\n"
            f"必须遵守的仓位规则：{plan['rules_text']}\n"
            f"单只上限{plan['max_single_amount']}元；总预算{plan['total_buy_budget']}元；"
            f"剩余仓位{plan['slots']}个；买入预算<=0时不得给出buy信号。")
        try:
            ai = deepseek_report(prompt, api_key)
        except Exception as e:  # noqa: BLE001
            print(f"[report] DeepSeek失败，降级规则简报: {e}")
    if ai is None:
        ai = {"market_comment": plan["rules_text"],
              "candidates": [
                  {"code": c["code"], "name": c["name"],
                   "signal": "buy" if plan["total_buy_budget"] > 100 else "watch",
                   "suggested_amount": min(plan["max_single_amount"], plan["total_buy_budget"]),
                   "entry_price": c["price"],
                   "reason": f"20日涨幅{c['ret20_pct']}%，趋势{'向好' if c['trend_ok'] else '一般'}"}
                  for c in watchlist["candidates"][:3]],
              "portfolio_comment": "", "risk_warning": "AI解读不可用，今日仅依据规则执行。"}
    if sell_signals:
        ai["candidates"] = sell_signals + ai.get("candidates", [])
    report = enforce_guardrails(ai, plan)
    report.update({"date": watchlist["date"], "plan": plan,
                   "nav": ledger["account"]["nav"], "cash": ledger["account"]["cash"],
                   "pnl_pct": ledger["account"]["pnl_pct"]})
    _write_json(os.path.join(DATA, "report.json"), report)
    icon = {"buy": "🟢买", "sell": "🔴卖", "hold": "🟡持", "watch": "⚪看"}
    lines = [f"📅 {watchlist['date']} ｜ 市场：**{watchlist['regime']}** ｜ 模式：**{plan['sizing_mode']}**",
             f"💰 净值 {report['nav']} 元（{report['pnl_pct']}%）｜ 现金 {report['cash']} 元 ｜ "
             f"本轮预算 **{plan['total_buy_budget']:.0f} 元**", "",
             f"🧭 {report.get('market_comment', '')}", "", "—— 今日操作单 ——"]
    for c in report.get("candidates", []):
        line = f"{icon.get(c.get('signal'), '⚪')} {c.get('name')}({c.get('code')}）：{c.get('reason', '')}"
        if c.get("signal") == "buy":
            line += (f"｜买入≈{c.get('suggested_amount'):.0f}元，止损{c.get('stop_loss')}，"
                     f"止盈{c.get('take_profit')}")
        lines.append(line)
    lines += ["", f"⚠️ {report.get('risk_warning', '')}",
              "数字来自仓位规则，AI仅负责解读；交易请自行在券商App完成。"]
    token = os.environ.get("PUSHPLUS_TOKEN")
    if token:
        try:
            r = requests.post("https://www.pushplus.plus/send", timeout=30,
                              data={"token": token, "title": f"投资助手 {watchlist['date']}",
                                    "content": "\n".join(lines), "template": "markdown"})
            print(f"[report] 推送: {r.status_code} {r.text[:150]}")
        except Exception as e:  # noqa: BLE001
            print(f"[report] 推送失败: {e}")
    else:
        print("[report] 未配置 PUSHPLUS_TOKEN，跳过推送")
    cmd_dashboard()


# ============ 入口 ============
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"scan": cmd_scan, "ledger": cmd_ledger, "report": cmd_report,
     "dashboard": cmd_dashboard}.get(cmd, lambda: print("用法: python main.py scan|ledger|report|dashboard"))()
