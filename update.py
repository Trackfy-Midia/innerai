"""
update.py — atualiza trackfy-performance dashboard (index.html + dash/index.html)
Roda via GitHub Actions (hourly) ou localmente.

Env var obrigatória:
  WIFIRE_AUTH — "Basic <base64>"  (ex: Basic dXNlcjpwYXNz)

Local sem GitHub Actions:
  Crie .env com:  WIFIRE_AUTH=Basic dXNlcjpwYXNz
  pip install requests python-dotenv
  python update.py
"""

import json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests

# ── CONFIG ────────────────────────────────────────────────────
BASE_URL        = "https://api.wifire.me"
ESTABLISHMENT   = "aeroportodemacae"
CUSTOM_FIELD_AI = "4436"
CAMPAIGN_START  = date(2026, 8, 6)
CAMPAIGN_END    = date(2026, 9, 5)
META_TOTAL      = 10_000
PHASE1_CUTOFF   = date(2026, 8, 11)
DAYS_IN_META    = 30  # meta/dia = META_TOTAL / DAYS_IN_META

ROOT       = Path(__file__).parent
DATA_FILE  = ROOT / "data" / "campaign.json"
HTML_FILES = [ROOT / "index.html", ROOT / "dash" / "index.html"]

# ── AUTH ─────────────────────────────────────────────────────
def get_headers():
    auth = os.environ.get("WIFIRE_AUTH", "").strip()
    if not auth:
        sys.exit("[erro] WIFIRE_AUTH não definido. Configure a env var ou crie .env")
    return {"Authorization": auth, "Content-Type": "application/json"}


# ── QUALIFICAÇÃO ─────────────────────────────────────────────
def is_qualified(c):
    phone = str(c.get("phone") or "").strip()
    if len(phone) < 8 or len(set(phone.replace("+", "").replace("-", ""))) <= 2:
        return False
    return any(
        t.get("id") == -1 and t.get("status") == "accepted"
        for t in c.get("terms", [])
    )

def get_ai_sub(c):
    for cf in c.get("custom_fields", []):
        if str(cf.get("field_id")) == CUSTOM_FIELD_AI:
            v = str(cf.get("value") or "").strip().lower()
            if v in ("", "não", "nao", "no"):
                return False
            return True
    return None


# ── API ──────────────────────────────────────────────────────
def _get_with_retry(url, headers, params=None, max_retries=4, timeout=90):
    """GET com retry exponencial em caso de timeout ou erro 5xx."""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            wait = 15 * (2 ** attempt)
            print(f"  [retry {attempt+1}/{max_retries}] {type(e).__name__} — aguardando {wait}s")
            if attempt == max_retries - 1:
                raise
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code >= 500:
                wait = 15 * (2 ** attempt)
                print(f"  [retry {attempt+1}/{max_retries}] HTTP {e.response.status_code} — aguardando {wait}s")
                if attempt == max_retries - 1:
                    raise
                time.sleep(wait)
            else:
                raise


def fetch_qualified(start: date, end: date) -> list:
    """Retorna lista de dicts {date, ai_sub, wifire_id} para qualificados no período."""
    h = get_headers()
    url = f"{BASE_URL}/establishments/customers"
    params = {
        "establishment": ESTABLISHMENT,
        "initialPeriod": start.isoformat(),
        "finalPeriod":   end.isoformat(),
        "limit":         100,
    }
    result, page, dry_pages = [], 0, 0
    while url:
        r = _get_with_retry(url, h, params=params if page == 0 else None)
        data = r.json()
        added = 0
        for c in data.get("data", []):
            if not is_qualified(c):
                continue
            d = str(c.get("min_created_at") or c.get("establishment_created_at") or "")[:10]
            if not d or d < CAMPAIGN_START.isoformat() or d > end.isoformat():
                continue
            result.append({"date": d, "ai_sub": get_ai_sub(c), "id": c.get("id")})
            added += 1
        page += 1
        dry_pages = 0 if added else dry_pages + 1
        if dry_pages >= 15:
            print(f"  [early stop] {dry_pages} páginas sem novos leads — encerrando na pág {page}")
            break
        url = data.get("nextpage")
        if url:
            time.sleep(6)  # respeita rate limit 10 req/min
        if page % 10 == 0:
            print(f"  pág {page}: {len(result)} qualificados acumulados")
    return result


# ── STATE ────────────────────────────────────────────────────
def load_state() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text("utf-8"))
    return {"last_sync_date": None, "counts": {}, "seg": {"yes": 0, "no": 0}}

def save_state(s: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), "utf-8")


# ── KPIs ─────────────────────────────────────────────────────
def fmt(n: int) -> str:
    return f"{n:,.0f}".replace(",", ".")

def fmtpct(v: float) -> str:
    return f"{v:.1f}".replace(".", ",") + "%"

def compute_kpis(state: dict, now: datetime) -> dict:
    today  = now.date()
    counts = state["counts"]

    timeline, cum = [], 0
    d = CAMPAIGN_START
    while d <= today:
        ds  = d.isoformat()
        n   = counts.get(ds, 0)
        cum += n
        meta_acc = min((d - CAMPAIGN_START).days + 1, DAYS_IN_META) * (META_TOTAL // DAYS_IN_META)
        timeline.append({
            "date":       ds,
            "daily":      n,
            "cumulative": cum,
            "meta_acc":   meta_acc,
            "gap":        cum - meta_acc,
            "partial":    d == today,
        })
        d += timedelta(days=1)

    total    = cum
    complete = [t for t in timeline if not t["partial"]]
    p1 = [t for t in complete if date.fromisoformat(t["date"]) <  PHASE1_CUTOFF]
    p2 = [t for t in complete if date.fromisoformat(t["date"]) >= PHASE1_CUTOFF]

    a1    = sum(t["daily"] for t in p1) / len(p1) if p1 else 0
    a2    = sum(t["daily"] for t in p2) / len(p2) if p2 else 0
    a_all = sum(t["daily"] for t in complete) / len(complete) if complete else 0

    days_elapsed = (today - CAMPAIGN_START).days + 1
    days_rem     = max((CAMPAIGN_END - today).days, 1)
    ritmo        = round((META_TOTAL - total) / days_rem)
    gx           = round(a2 / a1, 1) if a1 else 0
    gp           = round((a2 - a1) / a1 * 100) if a1 else 0

    seg       = state["seg"]
    yes, no   = seg.get("yes", 0), seg.get("no", 0)
    seg_total = max(yes + no, 1)

    p2_last = max((t["date"] for t in p2), default=None)
    if p2_last:
        p2d      = date.fromisoformat(p2_last)
        p2_fmt   = f"{p2d.day:02d}/{p2d.month:02d}"
        p2_label = f"11 a {p2_fmt}"
        p2_desc  = f"11-{p2_fmt} completos · {today.day:02d}/{today.month:02d} em andamento"
    else:
        p2_label = p2_desc = ""

    pct       = total / META_TOTAL * 100
    last_ci   = len(complete) - 1
    proj_base = timeline[last_ci]["cumulative"] if last_ci >= 0 else 0

    accel_labels = [t["date"][8:] + "/" + t["date"][5:7] for t in timeline]
    is_p2        = [date.fromisoformat(t["date"]) >= PHASE1_CUTOFF for t in timeline]

    return {
        "updated_at":    now.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "header_date":   now.strftime("%d/%m/%Y"),
        "period_value":  f"{CAMPAIGN_START.day:02d}/{CAMPAIGN_START.month:02d} — {today.day:02d}/{today.month:02d}/{today.year}",
        "period_label":  f"{days_elapsed} dias · dados até {today.day:02d}/{today.month:02d} (parcial)",
        "total":         total,
        "total_fmt":     fmt(total),
        "kpi_desc":      f"desde {CAMPAIGN_START.strftime('%d/%m/%Y')} — Nível 01 (Aceito + Contato email ou telefone) · {today.day:02d}/{today.month:02d} em andamento",
        "remaining_fmt": fmt(META_TOTAL - total),
        "pct_fmt":       fmtpct(pct),
        "pct_raw":       round(pct, 2),
        "progress_fill": f"{pct:.2f}%",
        "progress_note": f"{days_elapsed} dias rodando · {days_rem} dias restantes",
        "progress_label":fmt(total) + " entregues",
        "avg_current":   round(a_all),
        "ritmo_nec":     ritmo,
        "phase2_label":  p2_label,
        "phase2_avg":    round(a2),
        "phase2_desc":   p2_desc,
        "growth_fmt":    f"+{gp}%",
        "growth_desc":   f"fase 2 vs. fase 1 · {gx}× de aceleração",
        "no_fmt":        fmt(no),
        "no_pct":        fmtpct(no / seg_total * 100),
        "yes_fmt":       fmt(yes),
        "yes_pct":       fmtpct(yes / seg_total * 100),
        "bar_no_w":      f"{no/seg_total*100:.1f}%",
        "bar_yes_w":     f"{yes/seg_total*100:.1f}%",
        "last_ci":       last_ci,
        "proj_base":     proj_base,
        "avg_rate":      round(a_all),
        "nec_rate":      ritmo,
        "actual":        [t["cumulative"] for t in timeline],
        "accel_labels":  accel_labels,
        "accel_values":  [t["daily"] for t in timeline],
        "is_phase2":     is_p2,
        "meta_line":     [333] * len(timeline),
        "timeline":      timeline,
        "days_elapsed":  days_elapsed,
        "days_rem":      days_rem,
    }


# ── HTML UPDATE ───────────────────────────────────────────────
def _rx(pattern, repl, html, flags=re.DOTALL):
    new = re.sub(pattern, repl, html, count=1, flags=flags)
    if new == html:
        print(f"  [warn] padrão não encontrado: {pattern[:60]}")
    return new

def update_html(path: Path, k: dict):
    html = path.read_text("utf-8")

    # ── window._DS block ─────────────────────────────────────
    ds_json = json.dumps(k, ensure_ascii=False, separators=(",", ":"))
    html = _rx(
        r'(<script id="ds">).*?(</script>)',
        rf"\g<1>window._DS={ds_json};\g<2>",
        html,
    )

    # ── Header date ──────────────────────────────────────────
    html = _rx(
        r"(RELATÓRIO DE PERFORMANCE · )\d{2}/\d{2}/\d{4}",
        rf"\g<1>{k['header_date']}",
        html,
    )

    # ── Period ───────────────────────────────────────────────
    html = _rx(
        r'(<span class="period-value">)[\d/\s——]+</span>',
        rf"\g<1>{k['period_value']}</span>",
        html,
    )
    html = _rx(
        r'(<span class="period-label">)\d+ dias[^<]+</span>',
        rf"\g<1>{k['period_label']}</span>",
        html,
    )

    # ── KPI qualificados ─────────────────────────────────────
    html = _rx(
        r'(class="kpi-value">)[\d\.]+(<\/div>\s*<div class="kpi-desc">desde)',
        rf"\g<1>{k['total_fmt']}\g<2>",
        html,
    )
    html = _rx(
        r'(desde \d{2}/\d{2}/\d{4} [^<]+· )\d{2}/\d{2} em andamento',
        rf"\g<1>{k['header_date'][:5]} em andamento",
        html,
    )

    # ── KPI leads em aberto ──────────────────────────────────
    html = _rx(
        r'(class="kpi-value red">)[\d\.]+(<\/div>\s*<div class="kpi-desc">necessários)',
        rf"\g<1>{k['remaining_fmt']}\g<2>",
        html,
    )

    # ── Progress bar ─────────────────────────────────────────
    html = _rx(
        r'(class="progress-pct">)\d+,\d+%',
        rf"\g<1>{k['pct_fmt']}",
        html,
    )
    html = _rx(
        r'(\d+ dias rodando · )\d+ dias restantes',
        rf"\g<1>{k['days_rem']} dias restantes",
        html,
    )
    html = _rx(
        r'(progress-fill" style="width:)\d+\.\d+%',
        rf"\g<1>{k['progress_fill']}",
        html,
    )
    html = _rx(
        r'(>)[\d\.]+ entregues(<\/span>\s*<span>10\.000 meta)',
        rf"\g<1>{k['progress_label']}\g<2>",
        html,
    )

    # ── Rate cards (só index.html tem esses) ─────────────────
    html = _rx(
        r'(Média atual entregue.*?rate-card-value red">)\d+(\s*<span class="rate-card-unit">)',
        rf"\g<1>{k['avg_current']}\g<2>",
        html,
    )
    html = _rx(
        r'(Ritmo necessário agora.*?rate-card-value red">)\d+(\s*<span class="rate-card-unit">)',
        rf"\g<1>{k['ritmo_nec']}\g<2>",
        html,
    )

    # ── Fase 2 ───────────────────────────────────────────────
    html = _rx(
        r'(Fase 2 — média \()[\d a-z/]+(\))',
        rf"\g<1>{k['phase2_label']}\g<2>",
        html,
    )
    html = _rx(
        r'(class="kpi-value red">)\d+(<span style="font-size:18px.*?/dia</span>)',
        rf"\g<1>{k['phase2_avg']}\g<2>",
        html,
    )
    html = _rx(
        r'(class="kpi-desc">)11-\d{2}/\d{2} completos · \d{2}/\d{2} em andamento',
        rf"\g<1>{k['phase2_desc']}",
        html,
    )

    # ── Crescimento ──────────────────────────────────────────
    html = _rx(
        r'(Crescimento de ritmo.*?kpi-value white">)\+\d+%',
        rf"\g<1>{k['growth_fmt']}",
        html,
    )
    html = _rx(
        r'(class="kpi-desc">fase 2 vs\. fase 1 · )[\d\.]+× de aceleração',
        rf"\g<1>{k['growth_desc'].split(' · ')[1]}",
        html,
    )

    # ── Segmentação ──────────────────────────────────────────
    html = _rx(
        r'(Não assinam IA.*?kpi-value white">)[\d\.]+(<span)',
        rf"\g<1>{k['no_fmt']}\g<2>",
        html,
    )
    html = _rx(
        r'(64[,\d]+% · público-alvo)',
        rf"{k['no_pct']} · público-alvo",
        html,
    )
    html = _rx(
        r'(Já assinam alguma IA.*?kpi-value red">)[\d\.]+(<span)',
        rf"\g<1>{k['yes_fmt']}\g<2>",
        html,
    )
    html = _rx(
        r'(35[,\d]+% · usuários com experiência)',
        rf"{k['yes_pct']} · usuários com experiência",
        html,
    )

    # Barra visual
    html = _rx(
        r'(width:6[34\d]+\.\d+%;background:var\(--black\))',
        rf"width:{k['bar_no_w']};background:var(--black)",
        html,
    )
    html = _rx(
        r'(width:3[45\d]+\.\d+%;background:var\(--red\))',
        rf"width:{k['bar_yes_w']};background:var(--red)",
        html,
    )
    html = _rx(r'(>6[34\d]+,[0-9]+%<\/span>\s*</div>\s*<div.*?background:var\(--red\).*?>)3[45\d]+,[0-9]+%',
        rf"\g<1>{k['yes_pct'].replace('%','')},{k['yes_pct'].split(',')[1] if ',' in k['yes_pct'] else '0'}%",
        html,
    ) if False else html  # skip complex inline — handled by _DS renderer

    # Legend
    html = _rx(
        r'Não assinam IA — [\d\.]+ leads',
        f"Não assinam IA — {k['no_fmt']} leads",
        html,
    )
    html = _rx(
        r'Já assinam IA — [\d\.]+ leads',
        f"Já assinam IA — {k['yes_fmt']} leads",
        html,
    )

    # Parágrafo segmentação
    html = _rx(
        r'Os [\d\.]+ leads que já usam plataformas[^<]+Os [\d\.]+ sem assinatura são o perfil',
        f"Os {k['yes_fmt']} leads que já usam plataformas de IA representam uma oportunidade de migração — usuários com fluência e disposição comprovada para pagar por IA, potencialmente insatisfeitos com a ferramenta atual. Os {k['no_fmt']} sem assinatura são o perfil",
        html,
    )

    # ── Chart legend ─────────────────────────────────────────
    html = _rx(
        r'Projeção ritmo atual \(\d+/dia\)',
        f"Projeção ritmo atual ({k['avg_current']}/dia)",
        html,
    )
    html = _rx(
        r'Ritmo necessário \(\d+/dia\)',
        f"Ritmo necessário ({k['ritmo_nec']}/dia)",
        html,
    )

    # ── JS: ACTUAL array ─────────────────────────────────────
    actual_js = "[" + ",".join(
        str(v) if v is not None else "null"
        for v in k["actual"]
    ) + ","
    # Pad with nulls to 31 days
    nulls = ",".join(["null"] * max(0, 31 - len(k["actual"])))
    if nulls:
        actual_js += "\n      " + nulls
    actual_js += "]"
    html = _rx(
        r"const ACTUAL\s*=\s*\[[\d,\s\nnull]*\];",
        f"const ACTUAL = {actual_js};",
        html,
    )

    # ── JS: PROJ_AVG / PROJ_NEED ─────────────────────────────
    ci   = k["last_ci"]
    base = k["proj_base"]
    html = _rx(
        r"(// Projection at current avg.*?if\(i < )\d+(\) return null;\s*return Math\.min\()\d+ \+ \(i-\d+\)\*\d+",
        rf"\g<1>{ci+1}\g<2>{base} + (i-{ci+1})*{k['avg_rate']}",
        html,
    )
    html = _rx(
        r"(// Needed pace.*?if\(i < )\d+(\) return null;\s*return Math\.min\()\d+ \+ \(i-\d+\)\*\d+",
        rf"\g<1>{ci+1}\g<2>{base} + (i-{ci+1})*{k['nec_rate']}",
        html,
    )

    # ── JS: dataset labels ───────────────────────────────────
    html = _rx(
        r"label: 'Projeção ritmo atual \(\d+/dia\)'",
        f"label: 'Projeção ritmo atual ({k['avg_current']}/dia)'",
        html,
    )
    html = _rx(
        r"label: 'Ritmo necessário \(\d+/dia\)'",
        f"label: 'Ritmo necessário ({k['ritmo_nec']}/dia)'",
        html,
    )

    # ── JS: pointRadius ──────────────────────────────────────
    html = _rx(
        r"context\.dataIndex === \d+ \? 5 : 3",
        f"context.dataIndex === {ci+1} ? 5 : 3",
        html,
    )

    # ── JS: acceleration chart ───────────────────────────────
    labels_js = "[" + ",".join(f"'{l}'" for l in k["accel_labels"]) + "]"
    values_js = "[" + ",".join(str(v) for v in k["accel_values"]) + "]"
    phase2_js = "[" + ",".join("true" if p else "false" for p in k["is_phase2"]) + "]"
    meta_js   = "[" + ",".join(str(v) for v in k["meta_line"]) + "]"

    html = _rx(
        r"const labels\s*=\s*\[[^\]]+\];",
        f"const labels = {labels_js};",
        html,
    )
    html = _rx(
        r"const values\s*=\s*\[[^\]]+\];",
        f"const values = {values_js};",
        html,
    )
    html = _rx(
        r"const isPhase2\s*=\s*\[[^\]]+\];",
        f"const isPhase2 = {phase2_js};",
        html,
    )
    html = _rx(
        r"(data:\s*)\[333(?:,333)+\]",
        rf"\g<1>{meta_js}",
        html,
    )

    # ── Daily table ──────────────────────────────────────────
    tl = k["timeline"]
    complete = [t for t in tl if not t["partial"]]
    today_row = next((t for t in tl if t["partial"]), None)

    def tr(t, partial=False):
        d    = date.fromisoformat(t["date"])
        wday = ["seg","ter","qua","qui","sex","sáb","dom"][d.weekday()]
        gap_num = t["gap"]
        gap_fmt = ("−" if gap_num < 0 else "+") + fmt(abs(gap_num))
        cls = ' class="partial"' if partial else ""
        badge = '<span class="partial-badge">parcial</span>' if partial else ""
        return (
            f'      <tr{cls}>'
            f'<td>{d.day:02d}/{d.month:02d}<span class="weekday">{wday}</span>{badge}</td>'
            f'<td>{fmt(t["daily"])}</td>'
            f'<td class="num-main">{fmt(t["daily"])}</td>'
            f'<td>{fmt(t["cumulative"])}</td>'
            f'<td>{fmt(t["meta_acc"])}</td>'
            f'<td class="gap">{gap_fmt}</td>'
            f'</tr>'
        )

    # Build new tbody rows
    rows = "\n".join(tr(t) for t in complete)
    if today_row:
        rows += "\n" + tr(today_row, partial=True)

    tfoot_note = f"* {date.fromisoformat(tl[-1]['date']).strftime('%d/%m')} dia em andamento — dados até horário de geração"

    html = _rx(
        r"<tbody>\s*(.*?)</tbody>\s*<tfoot>.*?</tfoot>",
        f"<tbody>\n{rows}\n    </tbody>\n    <tfoot>\n      <tr><td colspan=\"6\">{tfoot_note}</td></tr>\n    </tfoot>",
        html,
    )

    # ── Footer date ──────────────────────────────────────────
    html = _rx(
        r"última atualização \d{2}/\d{2}/\d{4}",
        f"última atualização {k['header_date']}",
        html,
    )

    # ── body data-updated-at ─────────────────────────────────
    if 'data-updated-at="' in html:
        html = _rx(
            r'data-updated-at="[^"]*"',
            f'data-updated-at="{k["updated_at"]}"',
            html,
        )
    else:
        html = html.replace("<body>", f'<body data-updated-at="{k["updated_at"]}">', 1)
        html = html.replace("<body ", f'<body data-updated-at="{k["updated_at"]}" ', 1)

    # ── Consolidado (index.html only) ─────────────────────────
    seg_total = k["no_fmt"] + k["yes_fmt"]  # strings, need ints
    seg_yes_n = k["timeline"]  # get from seg state
    # update consolidado via _DS renderer (skip direct replacement here)

    path.write_text(html, "utf-8")
    print(f"  ✓ {path.name}")


# ── MAIN ─────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    today   = now_utc.date()

    state = load_state()
    last  = state.get("last_sync_date")

    # Determine fetch window
    same_day = last and last == today.isoformat()
    if same_day:
        # Mesmo dia: busca só quem acessou hoje e registrou hoje
        fetch_start = today
        print(f"[sync parcial] {fetch_start} (mesmo dia — só conta novos de hoje)")
    elif last:
        # Novo dia: pega desde ontem (sobreposição de 1 dia)
        fetch_start = date.fromisoformat(last) - timedelta(days=1)
        print(f"[sync incremental] {fetch_start} → {today}")
    else:
        fetch_start = CAMPAIGN_START
        print(f"[sync completo] {fetch_start} → {today}")

    api_ok = True
    try:
        customers = fetch_qualified(fetch_start, today)
        print(f"  {len(customers)} qualificados no período {fetch_start} → {today}")
    except Exception as e:
        print(f"  [AVISO] API indisponível — usando dados armazenados. Erro: {e}")
        api_ok = False
        customers = []

    if api_ok:
        counts = dict(state.get("counts", {}))

        if same_day:
            # Parcial: só atualiza o count de hoje; dias anteriores ficam intactos
            today_str = today.isoformat()
            counts[today_str] = sum(1 for c in customers if c["date"] == today_str)
        else:
            # Full/incremental: substitui todos os dias do período buscado
            new_counts: dict = {}
            for c in customers:
                ds = c["date"]
                new_counts[ds] = new_counts.get(ds, 0) + 1
            for ds, n in new_counts.items():
                counts[ds] = n

        state["counts"] = counts
        state["last_sync_date"] = today.isoformat()

        if fetch_start <= CAMPAIGN_START:
            state["seg"] = {
                "yes": sum(1 for c in customers if c["ai_sub"] is True),
                "no":  sum(1 for c in customers if c["ai_sub"] is False),
            }

    save_state(state)

    k = compute_kpis(state, now_utc)
    print(f"  KPIs: {k['total_fmt']} leads ({k['pct_fmt']}) · ritmo {k['avg_current']}/dia · nec {k['ritmo_nec']}/dia")

    for f in HTML_FILES:
        if f.exists():
            update_html(f, k)
        else:
            print(f"  [skip] {f} não encontrado")

    print("Done.")


if __name__ == "__main__":
    main()
