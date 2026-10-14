"""T12: checks on the redesigned /board page that Python CAN do.

No browser here, so the page's JavaScript is never executed. What is covered:
  1. source-level safety rules (no innerHTML, storage only via sessionStorage/teamToken, no external URLs ...)
  2. how the page is served (headers, no learner data, no token needed)
  3. feature markers that must exist in the source (copy, tabs, filters, CSV buttons, routes ...)
  4. the API contract: every field board.html reads exists with the right type on real data
  5. a contrast-ratio calculation on the colour variables used by the CSS
Behaviour, rendering, layout at 375px and tap sizes are NOT checked here (see docs/test-report.md).
"""
import csv
import io
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import Chat, register

from backend import config, messages, reports, scheduler
from backend.app import create_app
from backend.config import THAI_TZ

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "backend" / "static" / "board.html"
SRC = BOARD.read_text(encoding="utf-8")

_script = re.search(r"<script>(.*)</script>", SRC, re.S)
JS_RAW = _script.group(1)
HTML = SRC.replace(_script.group(0), "")
CSS = re.search(r"<style>(.*?)</style>", SRC, re.S).group(1)
HTML_NO_CSS = re.sub(r"<style>.*?</style>", "", HTML, flags=re.S)
# JS with comments removed ("//" only counts as a comment when it is not part of "http://")
JS = re.sub(r"(?m)(?<![:\"'\w])//[^\n]*$", "", re.sub(r"/\*.*?\*/", "", JS_RAW, flags=re.S))

THAI = re.compile(r"[฀-๿]")
STR_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
LITERALS = [a or b for a, b in STR_RE.findall(JS)]


def visible_html_text():
    body = re.sub(r"<(script|style)\b.*?</\1>", "", SRC, flags=re.S)
    texts = re.findall(r">([^<]+)<", body)
    attrs = re.findall(r'\b(?:placeholder|aria-label|title|alt)="([^"]*)"', body)
    return [t.strip() for t in texts + attrs if t.strip()]


# =====================================================================================================
# 1. source safety
# =====================================================================================================
FORBIDDEN_SINKS = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "setTimeout(\"", "setInterval(\"", "srcdoc", "DOMParser", "createContextualFragment"]


@pytest.mark.parametrize("word", FORBIDDEN_SINKS)
def test_no_html_injection_sinks_anywhere_in_the_page(word):
    assert word not in SRC, f"{word} found in board.html"


def test_no_inline_event_handlers_or_javascript_urls():
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", HTML), "inline on*= attribute in HTML"
    assert not re.search(r"javascript:", SRC, re.I)
    # handlers set from JS must go through addEventListener (the h() helper's "events" prop), never as attributes
    assert not re.search(r"setAttribute\(\s*[\"']on", JS)
    assert not re.search(r"\.on[a-z]+\s*=[^=]", JS), "element.onxxx = assignment in JS"


def test_page_stores_nothing_in_the_browser():
    for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB", "serviceWorker", "caches."):
        assert banned not in SRC, banned


def test_page_has_no_login_no_password_field_and_no_team_code():
    for banned in ('type="password"', "loginView", "loginForm", "teamCode", "logoutBtn", "รหัสทีม", "เข้าสู่ระบบ",
                   "ออกจากระบบ", "X-Team-Token", "teamToken", "getToken", "setToken", "AuthError", "forceLogin",
                   "/api/session", "openNote", "โหมดเปิดสำหรับการตรวจงาน"):
        assert banned not in SRC, banned
    assert "<form" not in HTML, "nothing on the page is submitted"


def test_no_external_urls_scripts_stylesheets_or_third_party_requests():
    found = re.findall(r"https?://[^\s\"'<>)]*", SRC)
    assert found == ["http://www.w3.org/2000/svg"], found
    assert not re.search(r"<script[^>]*\ssrc=", SRC), "external <script src>"
    assert len(re.findall(r"<script\b", SRC)) == 1
    assert not re.search(r"<link\b", SRC, re.I), "<link> (stylesheet/font/preload) present"
    assert "@import" not in CSS and "@font-face" not in CSS
    assert set(re.findall(r"url\(([^)]*)\)", SRC)) <= {'#pat-" + kind + "', '#pat-" + sg.kind + "'}
    for banned in ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "importScripts", "<img", "<iframe",
                   "<embed", "<object", "<audio", "<video", "new Image", "window.open", "console."):
        assert banned not in SRC, banned


def test_nothing_secret_or_learner_related_goes_into_location_history_or_title():
    assert not re.search(r"location\.(href|assign|replace)\b|location\s*=[^=]|location\.hash\s*=[^=]|window\.name", JS)
    assert not re.search(r"pushState|URLSearchParams|document\.referrer", JS)
    for m in re.finditer(r"history\.replaceState\(([^;]*)\)", JS):
        assert "getToken" not in m.group(1) and "nickname" not in m.group(1), m.group(0)
    used = set(re.findall(r"location\.(\w+)", JS))
    assert used <= {"pathname", "search", "hash"}, used
    # the only hash values the page ever writes or reads are the five fixed routes (other "#x" literals are CSS selectors)
    for lit in LITERALS:
        if lit.startswith("#/"):
            assert re.fullmatch(r"#/(home|learners|help|overview|settings)?", lit), lit
        elif lit.startswith("#"):
            assert re.fullmatch(r"#[A-Za-z][\w-]*( [a-z]+)?", lit), lit
    # every document.title assignment is static text or a NAV label, never learner data
    for m in re.finditer(r"document\.title\s*=\s*([^;]*);", JS):
        assert not re.search(r"nickname|display_name|detail|getToken", m.group(1)), m.group(0)


def test_every_server_call_goes_to_api_and_sends_no_credentials():
    fetches = [m.start() for m in re.finditer(r"\bfetch\(", JS)]
    assert len(fetches) == 2, "a new fetch() appeared: check what it calls, then update this test"
    assert not re.search(r"credentials\s*:", JS) and "Authorization" not in JS
    paths = re.findall(r"\bapi\(\s*([\"'][^\"']*[\"'])", JS)
    assert paths
    for p in paths:
        assert p.strip("\"'").startswith("/api/"), p
    assert re.search(r'fetch\("/api/export\.csv\?kind=', JS)
    methods = set(re.findall(r"method:\s*\"(\w+)\"", JS))
    assert methods == {"POST", "PUT"}, methods
    endpoints = {re.match(r"/api/\w+", p.strip("\"'")).group(0) for p in paths}
    assert endpoints == {"/api/triage", "/api/help", "/api/tasks", "/api/report", "/api/settings"}, endpoints


def test_page_does_not_leak_user_text_into_attributes_that_execute_or_navigate():
    # the only href values are fixed routes plus the blob url of a CSV download
    hrefs = re.findall(r"\bhref:\s*([^,}]*)", JS)
    assert len(hrefs) >= 5
    for h in hrefs:
        assert re.match(r'\s*("#/[a-z]*"( \+ n\.r)?|o\.href|url\b)', h), h
    assert "download: \"tiis-\" + ex.kind + \".csv\"" in JS


# =====================================================================================================
# 3. feature markers
# =====================================================================================================
def test_static_structure_markers():
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in HTML
    assert '<html lang="th">' in HTML
    assert "prefers-color-scheme: dark" in CSS and "prefers-reduced-motion" in CSS
    assert "aria-current" in JS and 'role: "img"' in JS
    assert "30000" in JS and "document.hidden" in JS
    assert re.search(r"setInterval\(.*document\.hidden.*REFRESH_MS", JS)
    assert "visibilitychange" in JS
    assert '<nav class="nav" aria-label=' in HTML and 'id="skip"' in HTML
    assert 'role="status"' in HTML and 'role="alert"' in HTML


def test_containers_for_learner_data_are_empty_in_the_served_html():
    for cid in ("homeBody", "lrnSummary", "lrnList", "helpList", "ovBody", "taskList", "firstErr", "navList",
                "exportList", "regLine", "lrnCount", "helpNow", "bannerText"):
        m = re.search(r'id="%s"[^>]*>(.*?)</' % cid, HTML, re.S)
        assert m is not None, cid
        assert m.group(1).strip() == "", f"{cid} is not empty in the static HTML: {m.group(1)!r}"
    assert 'id="loginView"' not in HTML and '<div id="appView">' in HTML


NAV_LABELS = ["หน้าแรก", "ผู้เรียนที่ต้องตาม", "เรื่องขอความช่วยเหลือ", "ภาพรวมรายสัปดาห์", "ตั้งค่างานและส่งออก"]


def test_five_tabs_with_thai_names_and_hash_routes():
    for label in NAV_LABELS:
        assert f'full: "{label}"' in JS, label
    heads = re.findall(r'<h2 id="h-(\w+)" tabindex="-1">([^<]*)</h2>', HTML)
    assert [h for h, _ in heads] == ["home", "learners", "help", "overview", "settings"]
    assert [t for _, t in heads][1:] == NAV_LABELS[1:] and heads[0][1] == "วันนี้ต้องทำอะไร"
    assert re.search(r'const NAV = \[\s*\{ r: "home".*\{ r: "learners".*\{ r: "help".*\{ r: "overview".*\{ r: "settings"', JS, re.S)
    assert 'href: "#/home"' not in JS and '"#/" + n.r' in JS
    assert r"/^#\/([a-z]+)$/" in JS and 'addEventListener("hashchange"' in JS
    assert JS.count('"#/home"') >= 1   # unknown hash falls back to home
    assert len(re.findall(r'<section class="panel" id="p-', HTML)) == 5
    # only home is visible at the start; the four others carry hidden
    assert len(re.findall(r'<section class="panel" id="p-\w+"[^>]*\shidden>', HTML)) == 4


def test_status_labels_levels_and_progress_in_thai():
    for t in ("ต้องตามด่วน", "ควรติดตาม", "ปกติ"):
        assert f'"{t}"' in JS
    for t in ("รอทีมรับเรื่อง", "ทีมรับเรื่องแล้ว", "ทีมตอบแล้ว", "ปิดเรื่อง"):
        assert f'"{t}"' in JS
    for t in ("ทำเสร็จแล้ว", "กำลังทำ", "ยังไม่ได้เริ่ม", "ติดอยู่"):
        assert f'"{t}"' in JS
    for t in ("ยังเปิดรับรายงาน", "ปิดรับรายงานแล้ว", "ช่วงติดตามผล", "ยังไม่เริ่ม"):
        assert f'"{t}"' in JS
    for t in ("ใช้แล้ว", "ยังไม่ได้ใช้", "ติดปัญหา"):
        assert f'"{t}"' in JS


def test_help_filters_overdue_badge_and_action_buttons():
    labels = re.findall(r'\{ key: "(\w+)", label: "([^"]+)"', JS)
    assert [k for k, _ in labels] == ["open", "overdue", "waiting", "accepted", "replied", "closed", "all"]
    assert dict(labels)["overdue"] == "เกินกำหนดตอบ" and dict(labels)["all"] == "ทั้งหมด"
    assert "เกินกำหนดตอบ - รอทีมรับเรื่องเกิน 1 วันทำการ" in JS          # badge text, not colour only
    assert "รอเกิน 1 วันทำการ (ไม่นับวันหยุดนักขัตฤกษ์)" in JS
    for t in ("รับเรื่อง", "ตอบแล้ว", "เปิดใหม่", "ปิดเรื่อง"):
        assert f'"{t}"' in JS
    # buttons per status exactly as the backend allows
    assert 'r.status === "waiting") main.push(mk("primary block", "รับเรื่อง"' in JS
    assert 'r.status === "accepted") main.push(mk("primary block", "ตอบแล้ว"' in JS
    assert 'r.status === "replied") main.push(mk("block", "เปิดใหม่"' in JS
    assert 'st === "closed" ? h("p", { class: "small muted", text: "เรื่องนี้ปิดแล้ว" })' in JS   # closed: no buttons
    # one click, one request: the busy check comes before the first await
    body = JS[JS.index("async function changeStatus"):]
    assert body.index("S.busy.has(r.id)") < body.index("await api(")
    assert '"/api/help/" + encodeURIComponent(r.id) + "/status"' in JS


def test_closing_a_request_needs_confirmation_and_says_what_happens():
    assert 'role: "group", "aria-label": "ยืนยันปิดเรื่อง"' in JS
    assert "ปิดเรื่องนี้ใช่ไหม" in JS and "ปิดแล้วเปิดกลับมาไม่ได้" in JS
    assert '"ไม่ปิด"' in JS and "danger-solid" in JS and "Escape" in JS
    # the close button in the card only opens the panel; the request is sent from the confirm panel's button
    card_close = JS[JS.index('"-close", disabled: dis'):][:260]
    assert "S.confirming = id" in card_close and "changeStatus" not in card_close
    assert 'events: { click: () => changeStatus(r, "closed") }' in JS


def test_line_notice_wording_follows_pm_ruling_r9():
    assert "ระบบจะส่งข้อความแจ้งผู้เรียนทาง LINE (หากส่งไม่สำเร็จจะลองใหม่ให้เอง)" in JS
    assert "ทุกครั้งที่ทีมเปลี่ยนสถานะเรื่อง ระบบจะส่งข้อความแจ้งผู้เรียนทาง LINE (หากส่งไม่สำเร็จจะลองใหม่ให้เอง)" in HTML
    assert "ผู้เรียนได้รับแล้ว" not in SRC and "ส่งถึงผู้เรียนแล้ว" not in SRC


def test_duplicate_name_hint_has_no_hash_and_no_ref_word_per_pm_ruling_r7():
    assert '"ชื่อซ้ำกับคนอื่น (รหัสแยก " + x.ref + ")"' in JS
    assert '"(ไม่ระบุชื่อ)"' in JS
    # no literal that the user could read builds "#<something>" ; hash literals are fixed routes only
    assert not re.search(r"\"#\"\s*\+", JS) and not re.search(r"'#'\s*\+", JS)
    # a ref is only used for telling people apart, never printed alone
    assert not re.search(r"text:\s*[^,}]*\.ref\s*[,}]", JS)


def test_home_screen_cards_and_all_clear_message():
    assert '"ไม่มีเรื่องเร่งด่วน"' in JS
    for t in ("เกินกำหนดตอบ", "เรื่องรอทีมรับ", "ผู้เรียนที่ต้องตามด่วน"):
        assert f'title: "{t}"' in JS
    home = JS[JS.index("function renderHome"):JS.index("function learnerMatches")]
    assert home.count('unit: "เรื่อง"') == 2 and home.count('unit: "คน"') == 1
    assert "ยังไม่มีผู้เรียนลงทะเบียน" in JS
    # the cards show the same numbers as the help filter (one source: helpCount)
    assert 'helpCount("overdue")' in JS and 'helpCount("waiting")' in JS
    assert "pickHelp(\"overdue\")" in JS and "pickHelp(\"waiting\")" in JS
    # the learner card needs no click handler: it is a link to #/learners, where level order is the API's order
    assert 'href: "#/learners"' in JS


def test_learner_list_markers():
    for t in ("ซ่อนผู้ที่ปกติ", "ค้นหาชื่อเล่นหรือชื่อ LINE", "ไม่พบผู้เรียนที่ชื่อ", "ไม่มีเหตุผลเพิ่มเติม"):
        assert t in SRC, t
    assert re.search(r'<input id="lrnHide" type="checkbox" checked>', HTML)       # hidden by default
    assert "lrnHideNormal: true" in JS
    # the page never re-sorts: the order is the API's (it only groups consecutive levels)
    block = JS[JS.index("function renderLearners"):JS.index("const NEXT_STEP")]
    assert ".sort(" not in block
    assert "reasons.map" in JS


def test_overview_markers():
    for t in ("ลงทะเบียนแล้ว", "ยังไม่จบขั้นตอน", "ยังไม่ยินยอม", "ความมั่นใจเฉลี่ย", "ขอให้ทีมติดต่อ",
              "ยังไม่มีใครรายงาน", "ยังไม่มีผู้เรียนที่ต้องรายงานในสัปดาห์นี้", "ช่วงนี้ไม่มีรายงานรายสัปดาห์",
              "ยังไม่มีรายงานให้ดู", "ที่ยังไม่ปิดแยกตามหมวด", "ทุกคนส่งรายงานแล้ว", "สัปดาห์ที่ ", "(ติดตามผล)",
              "ไม่ส่งมา ", "สัปดาห์นี้ยังไม่ส่ง", "รายงานล่าสุดสัปดาห์ที่", "ยังไม่เคยรายงาน", "ผลติดตามหลังกิจกรรมหลัก",
              'ส่วนแท็บ "ผู้เรียนที่ต้องตาม" รวมหลายเหตุผลเข้าด้วยกัน'):
        assert t in JS, t
    assert 'kpi("ความมั่นใจเฉลี่ย"' in JS and '? "-" :' in JS and "/ 5" in JS      # the tile shows "-" with no data
    # division guard: percentages never computed with a zero total
    assert "t > 0 ? Math.round" in JS
    assert not [lit for lit in LITERALS if re.search(r"NaN|undefined|null|Infinity", lit)]
    assert "num = (x) => { const v = Number(x); return Number.isFinite(v) ? v : 0; }" in JS


def test_charts_are_hand_written_svg_with_text_alternatives():
    assert JS.count('role: "img"') >= 4
    assert JS.count("document.createElementNS") == 1 and "createElementNS(SVG_NS" in JS
    assert "viewBox" in JS and "preserveAspectRatio" in JS
    assert JS.count("legendTable(") >= 5          # a real table of numbers next to every chart
    assert "<caption" in JS.replace('"caption"', "<caption") or 'h("caption"' in JS
    # pattern fills so segments differ in grayscale
    for p in ("attn", "ok", "info", "slate"):
        assert f'pattern("{p}"' in JS
    # five main charts: A donut, B progress, C blockers, help status, follow-up
    for fn in ("function chartA", "function chartB", "function chartC", "function helpChart", "function followCard"):
        assert fn in JS, fn
    assert "แนวโน้มผู้รายงานรายสัปดาห์" in JS and "Promise.allSettled" in JS


EXPECTED_CSV = [("learners", "รายชื่อผู้เรียนและระดับที่ต้องตาม"), ("checkins", "รายงานความคืบหน้ารายสัปดาห์"),
                ("help", "เรื่องขอความช่วยเหลือ"), ("followups", "ผลติดตามหลังกิจกรรม (14 วัน)")]


def test_four_csv_buttons_with_thai_names_and_backend_kinds():
    block = JS[JS.index("const EXPORTS = ["):JS.index("function setStatus")]
    found = re.findall(r'\{ kind: "(\w+)", name: "([^"]+)"', block)
    assert found == EXPECTED_CSV
    assert [k for k, _ in found] == list(reports.CSV_KINDS)
    assert 'download: "tiis-" + ex.kind + ".csv"' in JS
    assert "ดาวน์โหลดแล้ว: " in JS and "ดาวน์โหลดไม่สำเร็จ ลองใหม่อีกครั้ง" in JS
    assert "res.status === 401" not in JS and "AuthError" not in JS      # no login, so no 401 handling


def test_task_editor_markers():
    assert 'maxlength: "120"' in JS and 'maxlength: "500"' in JS
    assert '"/api/tasks/" + week, { method: "PUT"' in JS
    assert "บันทึกแล้ว" in JS and "ยังไม่ได้บันทึก" in JS and "สัปดาห์ปัจจุบัน" in JS
    # built once: ensureTaskEditor is guarded by taskBuilt and refresh only touches the chips
    assert "if (S.taskBuilt || !S.tasks.length) return;" in JS
    assert "taskList" in JS[JS.index("function ensureTaskEditor"):][:200]
    refresh_zone = JS[JS.index("function renderAll"):JS.index("function init()")]
    assert "taskList" not in refresh_zone and "taskItem" not in refresh_zone


def test_no_banned_words_in_text_the_user_can_read():
    banned = re.compile(r"triage|snapshot|token|\bapi\b|\bref\b|คะแนน|Triage Board", re.I)
    visible = visible_html_text() + [lit for lit in LITERALS if THAI.search(lit)]
    bad = [t for t in visible if banned.search(t)]
    assert not bad, bad
    # English-only literals that contain a banned word must be technical strings, not copy
    tech = re.compile(r"^(/api/|X-Team-Token|teamToken$|.*\.ref$)")
    odd = [lit for lit in LITERALS if not THAI.search(lit) and banned.search(lit) and not tech.match(lit)]
    assert not odd, odd


# =====================================================================================================
# CSS facts that can be read from the source
# =====================================================================================================
def _vars(block):
    return {k: v.upper() for k, v in re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})", block)}


LIGHT = _vars(re.search(r":root\s*\{([^}]*)\}", CSS).group(1))
DARK = {**LIGHT, **_vars(re.search(r"prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", CSS).group(1))}


def _lum(hexcolor):
    rgb = [int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (foreground var, background var, minimum ratio, regex proving the CSS really pairs them)
TEXT = 4.5
UI = 3.0
PAIRS = [
    ("--text", "--bg", TEXT, r"body\s*\{[^}]*background:\s*var\(--bg\);\s*color:\s*var\(--text\)"),
    ("--text", "--card", TEXT, r"\.btn\s*\{[^}]*background:\s*var\(--card\);\s*color:\s*var\(--text\)"),
    ("--muted", "--bg", TEXT, r"\.muted\s*\{\s*color:\s*var\(--muted\)"),
    ("--muted", "--card", TEXT, r"\.card\.lv-normal\s*\{[^}]*color:\s*var\(--muted\)"),
    ("--urgent", "--urgent-bg", TEXT, r"\.chip\.urgent\s*\{\s*background:\s*var\(--urgent-bg\);\s*color:\s*var\(--urgent\)"),
    ("--attn", "--attn-bg", TEXT, r"\.chip\.attn\s*\{\s*background:\s*var\(--attn-bg\);\s*color:\s*var\(--attn\)"),
    ("--ok", "--ok-bg", TEXT, r"\.chip\.ok\s*\{\s*background:\s*var\(--ok-bg\);\s*color:\s*var\(--ok\)"),
    ("--info", "--info-bg", TEXT, r"\.chip\.info\s*\{\s*background:\s*var\(--info-bg\);\s*color:\s*var\(--info\)"),
    ("--slate", "--slate-bg", TEXT, r"\.chip\.slate\s*\{\s*background:\s*var\(--slate-bg\);\s*color:\s*var\(--slate\)"),
    ("--text", "--line", TEXT, r"\.chip\.cat\s*\{\s*background:\s*var\(--line\);\s*color:\s*var\(--text\)"),
    ("--on-strong", "--info", TEXT, r"\.btn\.primary\s*\{\s*background:\s*var\(--info\);[^}]*color:\s*var\(--on-strong\)"),
    ("--on-strong", "--urgent", TEXT, r"\.btn\.danger-solid\s*\{\s*background:\s*var\(--urgent\);[^}]*color:\s*var\(--on-strong\)"),
    ("--on-strong", "--urgent", TEXT, r"\.overdue-bar\s*\{[^}]*background:\s*var\(--urgent\);\s*color:\s*var\(--on-strong\)"),
    ("--on-strong", "--attn", TEXT, r"\.badge-n\.attn\s*\{\s*background:\s*var\(--attn\)"),
    ("--on-strong", "--ok", TEXT, r"\.mark\.good\s*\{\s*background:\s*var\(--ok\);\s*color:\s*var\(--on-strong\)"),
    ("--urgent", "--card", TEXT, r"\.btn\.danger\s*\{\s*border-color:\s*var\(--urgent\);\s*color:\s*var\(--urgent\)"),
    ("--urgent", "--bg", TEXT, r"\.status\.bad\s*\{\s*color:\s*var\(--urgent\)"),
    ("--ok", "--bg", TEXT, r"\.status\.good\s*\{\s*color:\s*var\(--ok\)"),
    ("--urgent", "--urgent-bg", TEXT, r"\.hotbox\s+\.big\s*\{\s*color:\s*var\(--urgent\)|\.hotbox\s*\{[^}]*background:\s*var\(--urgent-bg\)"),
    ("--info", "--info-bg", TEXT, r"\.nav a\[aria-current=\"page\"\]\s*\{[^}]*color:\s*var\(--info\)"),
    ("--muted", "--urgent-bg", TEXT, r"\.confirm\s*\{[^}]*background:\s*var\(--urgent-bg\)"),
    # non-text (UI components): input border, focus ring, status borders
    ("--bs", "--bg", UI, r"border:\s*1\.5px solid var\(--bs\)"),
    ("--bs", "--card", UI, r"border:\s*1\.5px solid var\(--bs\)"),
    ("--focus", "--bg", UI, r":focus-visible\s*\{\s*outline:\s*3px solid var\(--focus\)"),
    ("--focus", "--card", UI, r":focus-visible\s*\{\s*outline:\s*3px solid var\(--focus\)"),
    ("--urgent", "--card", UI, r"border-left-color:\s*var\(--urgent\)"),
    ("--attn", "--card", UI, r"border-left-color:\s*var\(--attn\)"),
]


@pytest.mark.parametrize("theme,palette", [("light", LIGHT), ("dark", DARK)])
@pytest.mark.parametrize("fg,bg,need,css", PAIRS, ids=[f"{p[0]}-on-{p[1]}" for p in PAIRS])
def test_colour_contrast_meets_wcag_for_pairs_the_css_really_uses(theme, palette, fg, bg, need, css):
    assert re.search(css, CSS), f"CSS no longer pairs {fg} with {bg}: update the test table"
    got = ratio(palette[fg], palette[bg])
    assert got >= need, f"{theme}: {fg} {palette[fg]} on {bg} {palette[bg]} = {got:.2f}:1 (needs {need}:1)"


def test_css_basics_for_mobile_and_text_size():
    assert re.search(r"body\s*\{[^}]*font:\s*400 16px", CSS) and "overflow-wrap: anywhere" in CSS
    assert re.search(r"min-height:\s*44px;\s*min-width:\s*44px", CSS)                 # .btn
    assert re.search(r"input\[type=text\][^{]*\{[^}]*min-height:\s*48px[^}]*font:\s*400 16px", CSS)   # inputs >= 16px
    assert "max-width: 960px" in CSS
    sizes = [int(x) for x in re.findall(r"font-size:\s*(\d+)px", CSS)] + [int(x) for x in re.findall(r"font:[^;]*?\b(\d+)px", CSS)]
    assert sizes and min(sizes) >= 13, sorted(set(sizes))
    small = sorted(set(s for s in sizes if s < 14))
    assert small in ([], [13]), f"text smaller than 14px: {small}"       # 13px is only the aria-hidden badge digits


# =====================================================================================================
# 2. serving
# =====================================================================================================
@pytest.fixture
def anon(app_client, db, bot):
    """A second Flask client on the same data (a visitor with only the link)."""
    return create_app(db, bot).test_client()


def test_board_is_served_as_html_no_store_without_a_token(anon):
    r = anon.get("/board")
    assert r.status_code == 200
    assert r.mimetype == "text/html" and "no-store" in r.headers["Cache-Control"]
    assert r.get_data() == BOARD.read_bytes()
    assert "Triage Board" not in r.get_data(as_text=True) and "ติดตามผู้เรียน TIIS" in r.get_data(as_text=True)


def test_board_contains_no_learner_data_even_when_the_database_is_full(anon, bot, db, clock):
    uid = "Usecretuid0001zzzz"
    c = register(bot, uid, "ชื่อลับมากสุดยอด99")
    db.update_user(uid, display_name="LINE ลับ Secret Name 77")
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.say("รายละเอียดลับ-รหัส-5551234")
    body = anon.get("/board").get_data(as_text=True)
    for secret in ("ชื่อลับมากสุดยอด99", "LINE ลับ Secret Name 77", "5551234", uid, "zzzz", "team-test-token"):
        assert secret not in body, secret
    # nothing is reflected from the query string either
    reflected = anon.get("/board?nickname=ZZREFLECT&token=ZZREFLECT").get_data(as_text=True)
    assert "ZZREFLECT" not in reflected
    # the data comes only from the API calls the page makes after it loads
    assert anon.get("/api/triage").status_code == 200 and anon.get("/api/help").status_code == 200


def test_other_routes_keep_their_behaviour(anon):
    r = anon.get("/health")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert anon.get("/no-such-page").status_code == 404
    assert anon.get("/webhook").status_code == 405
    assert anon.get("/api/no-such-endpoint").status_code == 404
    assert anon.get("/board/").status_code == 404
    assert anon.post("/board").status_code == 405


@pytest.mark.parametrize("path", ["/api/triage", "/api/help", "/api/tasks", "/api/report", "/api/report?week=1",
                                  "/api/followups", "/api/export.csv?kind=help"])
def test_every_endpoint_the_page_calls_answers_without_any_credentials(anon, path):
    assert anon.get(path).status_code == 200
    assert anon.get(path, headers={"X-Team-Token": "ignored"}).status_code == 200      # a stray header changes nothing


# =====================================================================================================
# 4. API contract: fields the page reads
# =====================================================================================================
def set_day(clock, monkeypatch, day):
    clock.d = config.PROJECT_START + timedelta(days=day)
    monkeypatch.setattr(config, "DEMO_TODAY", clock.d.isoformat())


def checkin(c, progress, conf, extra=()):
    c.say("รายงานความคืบหน้า")
    c.tap(progress)
    c.tap(str(conf))
    for label in extra:
        c.tap(label)


EVIL_DETAIL = "<script>alert(1)</script>\n<img src=x onerror=alert(2)> " + "ก" * 300 + " " + "x" * 120
EVIL_NICK = "<b>บอลด์</b>"
EVIL_LINE = "LINE <img src=x onerror=alert(3)>"


@pytest.fixture
def world(bot, db, clock, monkeypatch, app_client):
    """Several learners, every help status, an overdue request, duplicate nicknames, hostile text, follow-ups."""
    set_day(clock, monkeypatch, 1)
    a = register(bot, "Ulearn0001aaaa", "ซ้ำ")                 # no LINE name, nickname shared with b
    checkin(a, "ติดอยู่", 1, ("เครื่องมือ", "ได้เลย"))
    b = register(bot, "Ulearn0002bbbb", "ซ้ำ")
    db.update_user("Ulearn0002bbbb", display_name=EVIL_LINE)
    checkin(b, "เสร็จแล้ว", 5)
    c = register(bot, "Ulearn0003cccc", "บอลด์")
    db.update_user("Ulearn0003cccc", nickname=EVIL_NICK)
    d = register(bot, "Ulearn0004dddd", "ดี")
    checkin(d, "กำลังทำ", 4)
    e = register(bot, "Ulearn0005eeee", "ปิด")
    register(bot, "Ulearn0006ffff", "เงียบ")                    # never reports
    h_overdue = db.create_help("Ulearn0003cccc", "tool", EVIL_DETAIL)
    old = (datetime.now(THAI_TZ) - timedelta(days=10)).isoformat()
    db.update_help(h_overdue, created_at=old)
    d.say("ขอความช่วยเหลือ")
    d.tap("เนื้อหา")
    d.tap("ข้าม")
    h_replied = db.list_help()[-1]["id"]
    e.say("ขอความช่วยเหลือ")
    e.tap("เครื่องมือ")
    e.tap("ข้าม")
    h_closed = db.list_help()[-1]["id"]
    h_orphan = db.create_help("Ughost0000zzzz", "other", None)   # request whose user row is gone
    for hid, steps in ((h_replied, ("accepted", "replied")), (h_closed, ("accepted", "replied", "closed"))):
        for st in steps:
            assert app_client.post(f"/api/help/{hid}/status", json={"status": st}).status_code == 200
    scheduler.run_scheduled(db, scheduler.FOLLOWUPS["fu_mid"])
    b.event({"type": "postback", "data": "s=fu&v=used"})
    return {"overdue": h_overdue, "replied": h_replied, "closed": h_closed, "orphan": h_orphan}


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def check_triage(rows):
    levels = {"high": 0, "medium": 1, "normal": 2}
    prog = {None, "done", "doing", "not_started", "stuck"}
    for r in rows:
        assert isinstance(r["nickname"], str) and r["nickname"], r
        assert r["display_name"] is None or isinstance(r["display_name"], str)
        assert isinstance(r["ref"], str) and len(r["ref"]) == 4
        assert r["level"] in levels
        assert isinstance(r["reasons"], list) and r["reasons"] and all(isinstance(x, str) and x for x in r["reasons"])
        assert r["last_progress"] in prog
        assert "user_id" not in r
    order = [levels[r["level"]] for r in rows]
    assert order == sorted(order), "levels are not grouped high -> medium -> normal, the page would repeat headings"


def check_help(rows):
    for r in rows:
        assert is_int(r["id"]) and r["status"] in ("waiting", "accepted", "replied", "closed")
        assert isinstance(r["overdue"], bool)
        assert r["nickname"] is None or isinstance(r["nickname"], str)
        assert r["display_name"] is None or isinstance(r["display_name"], str)
        assert isinstance(r["ref"], str) and len(r["ref"]) == 4
        assert r["detail"] is None or isinstance(r["detail"], str)
        assert r["category"] in ("content", "tool", "confidence", "other")
        assert datetime.fromisoformat(r["created_at"]).tzinfo is not None      # browsers need the offset to show local time
        assert "user_id" not in r
        if r["overdue"]:
            assert r["status"] == "waiting"


def check_tasks(d):
    assert is_int(d["current_week"]) and 1 <= d["current_week"] <= 6
    assert [t["week"] for t in d["tasks"]] == [1, 2, 3, 4]
    for t in d["tasks"]:
        assert isinstance(t["title"], str) and t["title"] and isinstance(t["description"], str)


def check_report(r, week):
    for k in ("week", "current_week", "registered", "onboarding_incomplete", "awaiting_consent", "eligible",
              "reported", "wants_contact"):
        assert is_int(r[k]), (k, r[k])
    assert r["week"] == week and 1 <= r["current_week"] <= 6
    assert r["status"] in ("open", "closed", "followup", "future")
    assert r["avg_confidence"] is None or isinstance(r["avg_confidence"], (int, float))
    assert (r["avg_confidence"] is None) == (r["reported"] == 0)
    assert set(r["progress"]) == {"done", "doing", "not_started", "stuck"} and all(is_int(v) for v in r["progress"].values())
    assert sum(r["progress"].values()) == r["reported"]                 # chart B total = chart A numerator
    assert r["reported"] <= r["eligible"] <= r["registered"]
    assert all(is_int(v) and v > 0 for v in r["blockers"].values())
    assert set(r["blockers"]) <= {"dont_know_start", "no_time", "tool_hard", "not_confident", "other"}, r["blockers"]
    assert sum(r["blockers"].values()) <= r["reported"]
    for n in r["not_reported"]:
        assert isinstance(n["nickname"], str) and (n["display_name"] is None or isinstance(n["display_name"], str))
        assert isinstance(n["ref"], str) and is_int(n["silent_weeks"]) and n["silent_weeks"] >= 0
        assert n["last_report_week"] is None or is_int(n["last_report_week"])
    if r["status"] in ("followup", "future"):
        assert r["not_reported"] == []
    else:
        assert len(r["not_reported"]) == r["eligible"] - r["reported"]
    h = r["help"]
    assert set(h["by_status"]) == {"waiting", "accepted", "replied", "closed"} and all(is_int(v) for v in h["by_status"].values())
    assert set(h["open_by_category"]) == {"content", "tool", "confidence", "other"}
    assert is_int(h["overdue"]) and h["overdue"] <= h["by_status"]["waiting"]
    assert set(r["followups"]) == {"fu_mid", "fu_final"}
    for f in r["followups"].values():
        assert all(is_int(f[k]) for k in ("asked", "answered", "pending"))
        assert f["pending"] == max(f["asked"] - f["answered"], 0)
        assert set(f["outcomes"]) == {"used", "not_yet", "stuck"}
        assert sum(f["outcomes"].values()) == f["answered"]


def test_triage_contract_with_rich_data(app_client, world):
    rows = app_client.get("/api/triage").get_json()["learners"]
    check_triage(rows)
    assert len(rows) == 6
    # nicknames that look like HTML arrive as plain strings, untouched
    assert EVIL_NICK in [r["nickname"] for r in rows]
    assert EVIL_LINE in [r["display_name"] for r in rows]
    same = [r for r in rows if r["nickname"] == "ซ้ำ"]
    assert len(same) == 2 and len({r["ref"] for r in same}) == 2          # page tells them apart by ref
    assert sum(1 for r in same if r["display_name"]) == 1                 # one has a LINE name, one needs the hint
    assert app_client.get("/api/triage").mimetype == "application/json"


def test_help_contract_with_rich_data(app_client, world):
    resp = app_client.get("/api/help")
    assert resp.mimetype == "application/json"
    rows = resp.get_json()["requests"]
    check_help(rows)
    by_id = {r["id"]: r for r in rows}
    assert by_id[world["overdue"]]["overdue"] is True and by_id[world["overdue"]]["detail"] == EVIL_DETAIL
    assert by_id[world["replied"]]["status"] == "replied" and by_id[world["closed"]]["status"] == "closed"
    assert by_id[world["orphan"]]["nickname"] is None and by_id[world["orphan"]]["detail"] is None   # page prints "(ไม่ระบุชื่อ)"
    assert {r["status"] for r in rows} == {"waiting", "replied", "closed"}
    # filters used by the page: ?status= still works and the page's overdue count equals the report's
    assert {r["status"] for r in app_client.get("/api/help?status=waiting").get_json()["requests"]} == {"waiting"}
    rep = app_client.get("/api/report?week=1").get_json()
    assert rep["help"]["overdue"] == sum(1 for r in rows if r["overdue"]) == 1
    for st in ("waiting", "accepted", "replied", "closed"):
        assert rep["help"]["by_status"][st] == sum(1 for r in rows if r["status"] == st)


def test_tasks_contract(app_client, world):
    check_tasks(app_client.get("/api/tasks").get_json())


def test_followups_endpoint_contract(app_client, world):
    rows = app_client.get("/api/followups").get_json()["followups"]
    assert len(rows) == 1 and {"user_id", "kind", "outcome", "created_at"} <= set(rows[0]) and rows[0]["outcome"] == "used"
    # NOTE: the page never calls /api/followups (it reads the same data through /api/report -> followups)


@pytest.mark.parametrize("day", [1, 8, 15, 22, 30, 40])
def test_report_contract_for_every_week_at_every_stage_of_the_project(app_client, world, clock, monkeypatch, day):
    set_day(clock, monkeypatch, day)
    seen = set()
    for week in range(1, 7):
        r = app_client.get(f"/api/report?week={week}").get_json()
        check_report(r, week)
        seen.add(r["status"])
        expect = ("followup" if week > 4 else "future" if week > r["current_week"]
                  else "open" if week == r["current_week"] and day < 28 else "closed")
        assert r["status"] == expect, (day, week, r["status"])
    default = app_client.get("/api/report").get_json()
    assert default["week"] == default["current_week"]
    if day == 1:
        assert seen == {"open", "future", "followup"}
    if day == 40:
        assert seen == {"closed", "followup"}


def test_report_numbers_for_the_rich_world(app_client, world):
    r = app_client.get("/api/report?week=1").get_json()
    assert (r["registered"], r["eligible"], r["reported"]) == (6, 6, 3)
    assert r["progress"] == {"done": 1, "doing": 1, "not_started": 0, "stuck": 1}
    assert r["wants_contact"] == 1
    assert {n["nickname"] for n in r["not_reported"]} == {EVIL_NICK, "ปิด", "เงียบ"}
    assert r["followups"]["fu_mid"]["asked"] == 6 and r["followups"]["fu_mid"]["answered"] == 1
    assert r["followups"]["fu_mid"]["outcomes"]["used"] == 1 and r["followups"]["fu_final"]["asked"] == 0


def test_report_rejects_bad_weeks_with_400_json(app_client):
    for bad in ("0", "7", "-1", "abc", "1.5"):
        r = app_client.get(f"/api/report?week={bad}")
        assert r.status_code == 400 and r.get_json()["error"], bad


def test_empty_database_returns_data_the_page_can_draw_as_empty_states(app_client):
    assert app_client.get("/api/triage").get_json() == {"learners": []}
    assert app_client.get("/api/help").get_json() == {"requests": []}
    check_tasks(app_client.get("/api/tasks").get_json())
    for week in range(1, 7):
        r = app_client.get(f"/api/report?week={week}").get_json()
        check_report(r, week)
        assert (r["registered"], r["eligible"], r["reported"]) == (0, 0, 0)
        assert r["avg_confidence"] is None and r["not_reported"] == [] and r["blockers"] == {}
        assert r["help"]["overdue"] == 0 and sum(r["help"]["by_status"].values()) == 0
        assert r["followups"]["fu_mid"]["asked"] == 0
    assert app_client.get("/api/followups").get_json() == {"followups": []}


def test_week_with_registrations_but_no_reports(app_client, bot, clock, monkeypatch):
    set_day(clock, monkeypatch, 1)
    register(bot, "Ulearn0001aaaa", "เอ")
    half = Chat(bot, "Uhalf000000hhhh")
    half.say("สวัสดี")
    half.tap("ยินยอม")                                             # consented, no nickname yet
    r = app_client.get("/api/report?week=1").get_json()
    check_report(r, 1)
    assert (r["registered"], r["eligible"], r["reported"], r["onboarding_incomplete"]) == (1, 1, 0, 1)
    assert r["avg_confidence"] is None and sum(r["progress"].values()) == 0     # page shows "ยังไม่มีใครรายงาน"


# ---- every snake_case field name in the page's JS exists in a real API payload ----------------------------------
def _collect(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _collect(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, out)
    elif isinstance(obj, str):
        out.add(obj)


def test_every_snake_case_name_in_the_page_exists_in_real_api_payloads(app_client, world, clock, monkeypatch):
    names = set()
    for day in (1, 30):
        set_day(clock, monkeypatch, day)
        for path in ["/api/triage", "/api/help", "/api/tasks", "/api/settings"] + [f"/api/report?week={w}" for w in range(1, 7)]:
            _collect(app_client.get(path).get_json(), names)
    names |= {"learners", "requests", "tasks", "current_week"}
    names |= set(messages.BLOCKERS)                      # blocker keys the fixtures do not happen to contain
    names |= {"title_required", "title_too_long", "description_too_long"}      # PUT /api/tasks error codes (own test below)
    names |= {"confirm_shift", "reports_exist"}      # PUT /api/settings: the request flag and the 409 code (own tests in test_start_date.py)
    js_names = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", JS))
    ignorable = set()      # none expected: everything snake_case in the page should be an API name
    missing = sorted(js_names - names - ignorable)
    assert not missing, f"page reads names the API never sends (or they only appear in states the fixtures lack): {missing}"


def test_field_names_the_page_reads_are_all_present_in_the_js_source():
    reads = {
        "triage": ["level", "nickname", "display_name", "ref", "reasons", "last_progress"],
        "help": ["id", "status", "overdue", "nickname", "display_name", "ref", "detail", "category", "created_at"],
        "tasks": ["week", "title", "description", "current_week"],
        "report": ["week", "current_week", "status", "registered", "onboarding_incomplete", "awaiting_consent",
                   "eligible", "reported", "not_reported", "progress", "blockers", "avg_confidence", "wants_contact",
                   "help", "followups", "by_status", "open_by_category", "overdue", "asked", "answered", "pending",
                   "outcomes", "silent_weeks", "last_report_week"],
    }
    for group, fields in reads.items():
        for f in fields:
            assert re.search(rf"\.{f}\b", JS), f"{group}.{f} is not read by the page any more: update this table"


# ---- status transitions the buttons rely on ------------------------------------------------------------------
def _fresh_help(bot):
    c = register(bot, "Uflow00000aaaa", "ไหล")
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.tap("ข้าม")
    return c


def test_status_transitions_match_the_buttons_the_page_offers(app_client, bot, db):
    _fresh_help(bot)
    hid = db.list_help()[0]["id"]
    post = lambda to: app_client.post(f"/api/help/{hid}/status", json={"status": to})   # noqa: E731
    assert post("replied").status_code == 409                       # waiting -> replied: no button for it
    assert post("bogus").status_code == 400 and post(None).status_code == 400
    r = post("accepted")
    assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["request"]["status"] == "accepted"
    assert post("replied").status_code == 200
    assert post("accepted").status_code == 200                      # replied -> accepted: "เปิดใหม่"
    assert post("replied").status_code == 200
    assert post("closed").status_code == 200
    for to in ("waiting", "accepted", "replied"):
        assert post(to).status_code == 409                           # closed is final: the page shows no buttons
    assert post("closed").status_code == 200                        # same value again is accepted as a no-op
    assert app_client.post("/api/help/99999/status", json={"status": "accepted"}).status_code == 404
    assert db.get_help(hid)["status"] == "closed"


def test_close_is_allowed_straight_from_waiting_accepted_and_replied(app_client, bot, db):
    for i, path in enumerate([(), ("accepted",), ("accepted", "replied")]):
        c = register(bot, f"Uclose000{i}aaaa", f"ปิด{i}")
        c.say("ขอความช่วยเหลือ")
        c.tap("เนื้อหา")
        c.tap("ข้าม")
        hid = db.list_help()[-1]["id"]
        for st in path:
            assert app_client.post(f"/api/help/{hid}/status", json={"status": st}).status_code == 200
        assert app_client.post(f"/api/help/{hid}/status", json={"status": "closed"}).status_code == 200


def test_status_endpoint_needs_no_credentials_and_still_validates(anon, bot):
    assert anon.post("/api/help/1/status", json={"status": "accepted"}).status_code == 404      # no such request
    _fresh_help(bot)
    assert anon.post("/api/help/1/status", json={"status": "bogus"}).status_code == 400
    assert anon.post("/api/help/1/status", json={"status": "accepted"}).status_code == 200


# ---- tasks editor contract -----------------------------------------------------------------------------------
def test_task_save_contract_and_error_codes_the_page_translates(app_client):
    err_in_js = set(re.findall(r"(\w+_(?:required|too_long)):", JS[JS.index("const TASK_ERR"):][:600]))
    ok = app_client.put("/api/tasks/2", json={"title": "ก" * 120, "description": "ข" * 500})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True and len(ok.get_json()["task"]["title"]) == 120
    assert app_client.get("/api/tasks").get_json()["tasks"][1]["title"] == "ก" * 120
    seen = set()
    for body, code in (({"title": ""}, "title_required"), ({"title": "   "}, "title_required"), ({}, "title_required"),
                       ({"title": "ก" * 121}, "title_too_long"),
                       ({"title": "ok", "description": "ข" * 501}, "description_too_long")):
        r = app_client.put("/api/tasks/1", json=body)
        assert r.status_code == 400 and r.get_json()["error"] == code, (body, r.get_json())
        seen.add(code)
    assert seen == err_in_js, (seen, err_in_js)
    assert app_client.put("/api/tasks/5", json={"title": "x"}).status_code == 404
    assert app_client.put("/api/tasks/0", json={"title": "x"}).status_code == 404
    assert app_client.get("/api/tasks").get_json()["tasks"][0]["title"] != ""


def test_task_text_with_html_is_stored_as_plain_text(app_client):
    app_client.put("/api/tasks/3", json={"title": "<img src=x onerror=alert(1)>", "description": "<script>1</script>"})
    t = app_client.get("/api/tasks").get_json()["tasks"][2]
    assert t["title"] == "<img src=x onerror=alert(1)>" and t["description"] == "<script>1</script>"


# ---- CSV ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", list(reports.CSV_KINDS))
def test_csv_export_each_kind_with_data(app_client, world, kind):
    r = app_client.get(f"/api/export.csv?kind={kind}")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert r.headers["Content-Disposition"] == f"attachment; filename=tiis-{kind}.csv"
    assert "no-store" in r.headers["Cache-Control"]
    text = r.get_data(as_text=True)
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert len(rows) >= 2, f"{kind}: header only although the database has data"
    assert all(len(row) == len(rows[0]) for row in rows)
    assert "Ulearn" not in text and "Ughost" not in text              # LINE ids never leave the server


@pytest.mark.parametrize("kind", list(reports.CSV_KINDS))
def test_csv_export_each_kind_when_empty_is_just_a_header(app_client, kind):
    r = app_client.get(f"/api/export.csv?kind={kind}")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
    assert len(rows) == 1 and len(rows[0]) >= 4


def test_csv_rejects_unknown_kind_and_works_without_credentials(app_client, anon):
    assert app_client.get("/api/export.csv?kind=users").status_code == 400
    assert app_client.get("/api/export.csv").status_code == 400
    assert anon.get("/api/export.csv?kind=help").status_code == 200


def test_csv_neutralises_spreadsheet_formulas(app_client, bot, db):
    register(bot, "Uform00000aaaa", "x")
    db.update_user("Uform00000aaaa", nickname="=HYPERLINK(\"http://evil\",\"x\")")
    text = app_client.get("/api/export.csv?kind=learners").get_data(as_text=True)
    assert ",=HYPERLINK" not in text and "\"=HYPERLINK" not in text
