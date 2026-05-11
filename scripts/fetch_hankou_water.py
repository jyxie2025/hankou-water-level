#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import dataclasses
import html
import json
import re
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = ROOT / "data" / "hankou_water_levels.csv"
DOCS_DIR = ROOT / "docs"
DOCS_CSV = DOCS_DIR / "hankou_water_levels.csv"
DOCS_JSON = DOCS_DIR / "hankou_water_levels.json"
DOCS_HTML = DOCS_DIR / "index.html"

OFFICIAL_INDEX_URL = "https://www.cjhdj.com.cn/hdfw/sw/"
SEARCH_URL = "https://www.cjhdj.com.cn/igs/front/search.jhtml"
SEARCH_CODE = "2db44785ca114a77b36660665c0c1002"
SOURCE_NAME = "长江航道局水位"
try:
    TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TITLE_RE = re.compile(
    r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?P<hour>8|16)时水位"
)
STATION_KEYWORDS = ("汉口", "武汉关")
CSV_FIELDS = [
    "datetime",
    "date",
    "hour",
    "station",
    "water_level_m",
    "change_m",
    "source_title",
    "source_url",
    "fetched_at",
    "source_site",
]


@dataclasses.dataclass(frozen=True)
class Article:
    title: str
    url: str
    observed_at: datetime


@dataclasses.dataclass
class WaterRecord:
    datetime: str
    date: str
    hour: int
    station: str
    water_level_m: float
    change_m: float | None
    source_title: str
    source_url: str
    fetched_at: str
    source_site: str = SOURCE_NAME

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "WaterRecord":
        change_text = (row.get("change_m") or "").strip()
        return cls(
            datetime=row["datetime"],
            date=row["date"],
            hour=int(row["hour"]),
            station=row.get("station") or "汉口（武汉关）",
            water_level_m=float(row["water_level_m"]),
            change_m=float(change_text) if change_text else None,
            source_title=row.get("source_title") or "",
            source_url=row.get("source_url") or "",
            fetched_at=row.get("fetched_at") or "",
            source_site=row.get("source_site") or SOURCE_NAME,
        )

    def to_csv_row(self) -> dict[str, str]:
        return {
            "datetime": self.datetime,
            "date": self.date,
            "hour": str(self.hour),
            "station": self.station,
            "water_level_m": f"{self.water_level_m:.2f}",
            "change_m": "" if self.change_m is None else f"{self.change_m:+.2f}",
            "source_title": self.source_title,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "source_site": self.source_site,
        }

    def to_json_row(self) -> dict[str, object]:
        return {
            "datetime": self.datetime,
            "date": self.date,
            "hour": self.hour,
            "station": self.station,
            "water_level_m": self.water_level_m,
            "change_m": self.change_m,
            "source_title": self.source_title,
            "source_url": self.source_url,
            "source_site": self.source_site,
        }


class LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._title: str = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr = dict(attrs)
        href = attr.get("href")
        if not href:
            return
        self._href = urljoin(self.base_url, href)
        self._title = attr.get("title") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        text = self._title or "".join(self._text)
        self.links.append((clean_text(text), self._href))
        self._href = None
        self._title = ""
        self._text = []


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_td = False
        self._in_th = False
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"}:
            self._in_td = tag == "td"
            self._in_th = tag == "th"
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._in_td or self._in_th:
            text = data.replace("\xa0", " ").strip()
            if text:
                self._current_cell.append(text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and (self._in_td or self._in_th):
            self._current_row.append(clean_text("".join(self._current_cell)))
            self._in_td = False
            self._in_th = False
            self._current_cell = []
        elif tag == "tr" and self._current_row:
            self.rows.append(self._current_row)
            self._current_row = []


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_title_datetime(title: str) -> datetime | None:
    match = TITLE_RE.search(clean_text(title))
    if not match:
        return None
    parts = {key: int(value) for key, value in match.groupdict().items()}
    return datetime(
        parts["year"],
        parts["month"],
        parts["day"],
        parts["hour"],
        0,
        0,
        tzinfo=TZ,
    )


def fetch_text(url: str, *, timeout: int = 30, retries: int = 3) -> str:
    # The official site can fail TLS validation from automation clients, so this
    # mirrors browser behavior closely and uses an unverified context.
    context = ssl._create_unverified_context()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Referer": OFFICIAL_INDEX_URL,
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout, context=context) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="ignore")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def discover_from_index() -> list[Article]:
    html_text = fetch_text(OFFICIAL_INDEX_URL)
    parser = LinkParser(OFFICIAL_INDEX_URL)
    parser.feed(html_text)
    articles: dict[str, Article] = {}
    for title, url in parser.links:
        observed_at = parse_title_datetime(title)
        if not observed_at:
            continue
        if "/hdfw/sw/" not in url or not url.endswith(".shtml"):
            continue
        articles[url] = Article(title=title, url=url, observed_at=observed_at)
    return sorted(articles.values(), key=lambda item: item.observed_at)


def discover_from_search(days: int, max_pages: int) -> list[Article]:
    cutoff = datetime.now(TZ) - timedelta(days=days + 2)
    articles: dict[str, Article] = {}
    queries = ["8时水位", "16时水位"]
    for query in queries:
        for page in range(1, max_pages + 1):
            params = {
                "code": SEARCH_CODE,
                "searchWord": query,
                "siteId": "4",
                "pageSize": "50",
                "pageNumber": str(page),
                "orderBy": "time",
                "timeOrder": "desc",
            }
            url = f"{SEARCH_URL}?{urlencode(params)}"
            payload = json.loads(fetch_text(url))
            page_data = payload.get("page") or {}
            content = page_data.get("content") or []
            if not content:
                break

            page_datetimes: list[datetime] = []
            for item in content:
                title = clean_text(str(item.get("title") or ""))
                article_url = str(item.get("url") or "")
                observed_at = parse_title_datetime(title)
                if not observed_at:
                    continue
                page_datetimes.append(observed_at)
                if observed_at < cutoff:
                    continue
                if "/hdfw/sw/" not in article_url or not article_url.endswith(".shtml"):
                    continue
                articles[article_url] = Article(
                    title=title,
                    url=article_url,
                    observed_at=observed_at,
                )

            total_pages = int(page_data.get("totalPages") or page)
            if page >= total_pages:
                break
            if page_datetimes and max(page_datetimes) < cutoff:
                break
    return sorted(articles.values(), key=lambda item: item.observed_at)


def to_float(value: str) -> float | None:
    text = clean_text(value).replace(",", "")
    if text in {"", "-", "--", "—"}:
        return None
    match = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def extract_html_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.I | re.S)
    return clean_text(match.group(1)) if match else ""


def scrape_article(article: Article, fetched_at: str) -> WaterRecord | None:
    html_text = fetch_text(article.url)
    title = article.title or extract_html_title(html_text)
    observed_at = article.observed_at or parse_title_datetime(title)
    if not observed_at:
        print(f"Skip article with unrecognized time: {article.url}", file=sys.stderr)
        return None

    parser = TableParser()
    parser.feed(html_text)
    for row in parser.rows:
        if len(row) < 3:
            continue
        station = row[0]
        if not any(keyword in station for keyword in STATION_KEYWORDS):
            continue
        water_level = to_float(row[1])
        if water_level is None:
            continue
        change = to_float(row[2])
        return WaterRecord(
            datetime=observed_at.isoformat(),
            date=observed_at.date().isoformat(),
            hour=observed_at.hour,
            station=station,
            water_level_m=water_level,
            change_m=change,
            source_title=title,
            source_url=article.url,
            fetched_at=fetched_at,
        )

    print(f"Skip article without Hankou row: {article.url}", file=sys.stderr)
    return None


def load_existing(path: Path) -> list[WaterRecord]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [WaterRecord.from_csv_row(row) for row in reader if row.get("datetime")]


def write_csv(path: Path, records: Iterable[WaterRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda item: item.datetime)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in ordered:
            writer.writerow(record.to_csv_row())


def merge_records(existing: Iterable[WaterRecord], new_records: Iterable[WaterRecord]) -> list[WaterRecord]:
    merged = {record.datetime: record for record in existing}
    for record in new_records:
        merged[record.datetime] = record
    return sorted(merged.values(), key=lambda item: item.datetime)


def dt_from_record(record: WaterRecord) -> datetime:
    return datetime.fromisoformat(record.datetime)


def recent_records(records: list[WaterRecord], days: int) -> list[WaterRecord]:
    if not records:
        return []
    max_dt = max(dt_from_record(record) for record in records)
    cutoff = max_dt - timedelta(days=days)
    return [record for record in records if dt_from_record(record) >= cutoff]


def generated_at_text() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def write_docs(records: list[WaterRecord], days: int, generated_at: str) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at,
        "station": "汉口（武汉关）",
        "default_days": days,
        "records": [record.to_json_row() for record in records],
        "all_record_count": len(records),
    }
    DOCS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(DOCS_CSV, records)
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")

    json_blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>汉口长江水位</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18212f;
      --muted: #617083;
      --line: #d9e2ec;
      --surface: #ffffff;
      --blue: #2563eb;
      --green: #059669;
      --bg: #f6f8fb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 34px;
    }}
    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: clamp(26px, 3vw, 38px);
      line-height: 1.1;
      letter-spacing: 0;
    }}
    .subhead {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(3, minmax(132px, 1fr));
      gap: 10px;
      min-width: min(100%, 470px);
    }}
    .metric {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px 12px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
    }}
    .metric strong {{
      display: block;
      font-size: 17px;
      line-height: 1.2;
      white-space: nowrap;
    }}
    .chart-shell {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 460px;
      height: min(74vh, 780px);
      resize: both;
      overflow: hidden;
      box-shadow: 0 18px 50px rgba(24, 33, 47, 0.08);
    }}
    .range-controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin: 0 0 12px;
    }}
    .range-controls button,
    .range-controls input {{
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      color: var(--ink);
      font: inherit;
      font-size: 13px;
    }}
    .range-controls button {{
      padding: 0 12px;
      cursor: pointer;
    }}
    .range-controls button.active {{
      border-color: var(--blue);
      background: #eff6ff;
      color: var(--blue);
      font-weight: 600;
    }}
    .range-controls input {{
      width: 72px;
      padding: 0 9px;
    }}
    #chart {{
      width: 100%;
      height: 100%;
    }}
    footer {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    a {{
      color: var(--blue);
      text-decoration: none;
    }}
    a:hover {{ text-decoration: underline; }}
    @media (max-width: 760px) {{
      main {{ width: min(100vw - 20px, 1180px); padding-top: 18px; }}
      header {{ display: block; }}
      .metrics {{ grid-template-columns: 1fr; margin-top: 14px; }}
      .range-controls button {{ flex: 1 1 auto; }}
      .chart-shell {{ min-height: 430px; height: 68vh; resize: vertical; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>汉口（武汉关）长江水位</h1>
        <p class="subhead">长江航道局公开水位表，08:00 与 16:00，全部累积数据，默认近 {days} 天</p>
      </div>
      <section class="metrics" aria-label="最新数据">
        <div class="metric"><span>最新 08:00</span><strong id="latest-morning">--</strong></div>
        <div class="metric"><span>最新 16:00</span><strong id="latest-evening">--</strong></div>
        <div class="metric"><span>记录数</span><strong id="record-count">--</strong></div>
      </section>
    </header>
    <nav class="range-controls" aria-label="时间范围">
      <button type="button" data-days="7">1周</button>
      <button type="button" data-days="30">1月</button>
      <button type="button" data-days="90">3个月</button>
      <button type="button" data-days="180">6个月</button>
      <button type="button" data-days="365">1年</button>
      <button type="button" data-all="true">全部</button>
      <input id="years-input" type="number" min="1" max="50" value="2" aria-label="自定义年数" />
      <button type="button" id="apply-years">N年</button>
    </nav>
    <section class="chart-shell" aria-label="水位动态图">
      <div id="chart"></div>
    </section>
    <footer>
      <span id="generated-at"></span>
      <span><a href="./hankou_water_levels.csv">CSV</a> · <a href="{OFFICIAL_INDEX_URL}">数据来源</a></span>
    </footer>
  </main>
  <script type="application/json" id="water-data">{json_blob}</script>
  <script>
    const payload = JSON.parse(document.getElementById("water-data").textContent);
    const records = payload.records;
    const latestTime = records.length
      ? new Date(Math.max(...records.map((record) => new Date(record.datetime).getTime())))
      : null;
    const earliestTime = records.length
      ? new Date(Math.min(...records.map((record) => new Date(record.datetime).getTime())))
      : null;
    const localeOptions = {{
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false
    }};
    const byHour = (hour) => records.filter((record) => record.hour === hour);
    const formatLevel = (record) => record ? `${{record.water_level_m.toFixed(2)}} m` : "--";
    const latestByHour = (hour) => byHour(hour).at(-1);
    document.getElementById("latest-morning").textContent = formatLevel(latestByHour(8));
    document.getElementById("latest-evening").textContent = formatLevel(latestByHour(16));
    document.getElementById("record-count").textContent = `${{records.length}} / ${{payload.all_record_count}}`;
    document.getElementById("generated-at").textContent =
      `更新于 ${{new Intl.DateTimeFormat("zh-CN", localeOptions).format(new Date(payload.generated_at))}}`;

    const setActiveButton = (button) => {{
      document.querySelectorAll(".range-controls button").forEach((item) => item.classList.remove("active"));
      if (button) button.classList.add("active");
    }};

    const setRange = (days, button = null) => {{
      if (!latestTime) return;
      const start = new Date(latestTime.getTime() - Number(days) * 24 * 60 * 60 * 1000);
      Plotly.relayout("chart", {{
        "xaxis.range": [start.toISOString(), latestTime.toISOString()]
      }});
      setActiveButton(button);
    }};

    const setAllRange = (button = null) => {{
      if (!latestTime || !earliestTime) return;
      Plotly.relayout("chart", {{
        "xaxis.range": [earliestTime.toISOString(), latestTime.toISOString()]
      }});
      setActiveButton(button);
    }};

    const makeTrace = (hour, name, color) => {{
      const data = byHour(hour);
      return {{
        x: data.map((record) => record.datetime),
        y: data.map((record) => record.water_level_m),
        customdata: data.map((record) => [
          record.change_m === null ? "" : `${{record.change_m >= 0 ? "+" : ""}}${{record.change_m.toFixed(2)}} m`,
          record.source_title,
          record.source_url
        ]),
        mode: "lines+markers",
        name,
        line: {{ color, width: 2.5 }},
        marker: {{ size: 7, color, line: {{ color: "#ffffff", width: 1 }} }},
        hovertemplate:
          "%{{x|%Y-%m-%d %H:%M}}<br>" +
          "水位 %{{y:.2f}} m<br>" +
          "涨落 %{{customdata[0]}}<extra>" + name + "</extra>"
      }};
    }};

    const layout = {{
      autosize: true,
      margin: {{ l: 62, r: 28, t: 30, b: 54 }},
      paper_bgcolor: "#ffffff",
      plot_bgcolor: "#ffffff",
      hovermode: "x unified",
      legend: {{ orientation: "h", y: 1.08, x: 0 }},
      xaxis: {{
        type: "date",
        title: "",
        gridcolor: "#edf2f7",
        rangeslider: {{ visible: true, thickness: 0.08 }}
      }},
      yaxis: {{
        title: "水位 (m)",
        gridcolor: "#edf2f7",
        zeroline: false,
        fixedrange: false
      }}
    }};

    const config = {{
      responsive: true,
      scrollZoom: true,
      displaylogo: false,
      modeBarButtonsToRemove: ["lasso2d", "select2d"]
    }};

    Plotly.newPlot("chart", [
      makeTrace(8, "08:00", "#2563eb"),
      makeTrace(16, "16:00", "#059669")
    ], layout, config).then(() => {{
      const defaultButton = document.querySelector(`[data-days="${{payload.default_days}}"]`);
      setRange(payload.default_days || 60, defaultButton);
    }});

    document.querySelectorAll("[data-days]").forEach((button) => {{
      button.addEventListener("click", () => setRange(Number(button.dataset.days), button));
    }});
    document.querySelector("[data-all]").addEventListener("click", (event) => setAllRange(event.currentTarget));
    document.getElementById("apply-years").addEventListener("click", (event) => {{
      const years = Math.max(1, Number(document.getElementById("years-input").value) || 1);
      setRange(years * 365, event.currentTarget);
    }});

    const chart = document.getElementById("chart");
    new ResizeObserver(() => Plotly.Plots.resize(chart)).observe(chart.parentElement);
  </script>
</body>
</html>
"""
    DOCS_HTML.write_text(html_text, encoding="utf-8")


def collect_articles(days: int, max_search_pages: int, include_search: bool) -> list[Article]:
    articles: dict[str, Article] = {}
    for source_name, loader in [("index", discover_from_index)]:
        try:
            for article in loader():
                articles[article.url] = article
        except Exception as exc:
            print(f"Warning: {source_name} discovery failed: {exc}", file=sys.stderr)
    if include_search:
        try:
            for article in discover_from_search(days, max_search_pages):
                articles[article.url] = article
        except Exception as exc:
            print(f"Warning: search discovery failed: {exc}", file=sys.stderr)
    cutoff = datetime.now(TZ) - timedelta(days=days + 2)
    return sorted(
        (article for article in articles.values() if article.observed_at >= cutoff),
        key=lambda item: item.observed_at,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Hankou Yangtze water levels and build Plotly docs.")
    parser.add_argument("--days", type=int, default=60, help="Number of days to show in the chart.")
    parser.add_argument("--search-pages", type=int, default=8, help="Search API pages to inspect for backfill links.")
    parser.add_argument("--no-search", action="store_true", help="Only use the official water-level index page.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fetched_at = generated_at_text()
    existing = load_existing(DATA_CSV)
    articles = collect_articles(args.days, args.search_pages, not args.no_search)
    print(f"Discovered {len(articles)} candidate water-level articles.")

    scraped: list[WaterRecord] = []
    known_datetimes = {record.datetime for record in existing}
    for article in articles:
        if article.observed_at.isoformat() in known_datetimes:
            continue
        try:
            record = scrape_article(article, fetched_at)
        except Exception as exc:
            print(f"Warning: failed to scrape {article.url}: {exc}", file=sys.stderr)
            continue
        if record:
            scraped.append(record)

    records = merge_records(existing, scraped)
    if not records:
        print("No Hankou water-level records were found.", file=sys.stderr)
        return 1

    write_csv(DATA_CSV, records)
    write_docs(records, args.days, fetched_at)
    print(f"Added {len(scraped)} new records; total records: {len(records)}.")
    print(f"Wrote {DATA_CSV.relative_to(ROOT)} and {DOCS_HTML.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
