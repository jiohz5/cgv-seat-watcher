import base64
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import messagebox

from selenium.webdriver.common.action_chains import ActionChains
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

LOGIN_URL = "https://cgv.co.kr/mem/login?returnUrl=/mcv/mobileTicketList"
BOOK_URL = "https://cgv.co.kr/cnm/movieBook/movie"

APP_NAME = "용산 IMAX 헬퍼"
MOVIE_ALT = "오디세이 포스터"
SITE_NAME = "용산아이파크몰"
DAY_NUMBER = "31"
SHOW_TIME = "07:30"
PEOPLE_COUNT = 2

# 타겟 범위: G~J열 13~30번 중 2연석만. GUI/텔레그램에서 변경 예정.
EXCLUDE_ZONES = {"Light존"}
TARGET_ROWS = {"G", "H", "I", "J"}
SEAT_NO_MIN = 13
SEAT_NO_MAX = 30
REQUIRE_PAIR = True
CHECK_INTERVAL_SEC = 1
MAX_CONSECUTIVE_FAILS = 5
HEARTBEAT_MIN = 0
COOKIE_SAVE_MIN = 10

TG_TOKEN = os.environ.get("CGV_TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("CGV_TG_CHAT_ID", "")

NTFY_TOPIC = os.environ.get("CGV_NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("CGV_NTFY_SERVER", "https://ntfy.sh")
NPAY_PIN = os.environ.get("CGV_NPAY_PIN", "").strip()

HOOK_JS = r"""
(function(){
  if (window.__cgvHooked) return;
  window.__cgvHooked = true;
  window.__cgvSeat = null;

  var origFetch = window.fetch;
  window.fetch = function(){
    var p = origFetch.apply(this, arguments);
    try {
      var a0 = arguments[0];
      var url = (typeof a0 === 'string') ? a0 : (a0 && a0.url) || '';
      if (url.indexOf('searchIfSeatData') !== -1) {
        p.then(function(r){
          r.clone().json().then(function(d){ window.__cgvSeat = d; }).catch(function(){});
        }).catch(function(){});
      }
    } catch(e) {}
    return p;
  };

  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url){
    this.__cgvUrl = url;
    return origOpen.apply(this, arguments);
  };
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(){
    this.addEventListener('load', function(){
      try {
        if (this.__cgvUrl && this.__cgvUrl.indexOf('searchIfSeatData') !== -1) {
          window.__cgvSeat = JSON.parse(this.responseText);
        }
      } catch(e) {}
    });
    return origSend.apply(this, arguments);
  };
})();
"""

READ_JS = r"""
if (!window.__cgvSeat || !window.__cgvSeat.data) return null;
var seats = window.__cgvSeat.data.items[0].seats;
return seats
  .filter(function(s){ return s.seatSaleYn === 'Y'; })
  .map(function(s){
    return {seat: s.seatRowNm + s.seatNo, zone: s.szoneKindNm,
            x: parseInt(s.xcoordStartVal, 10), row: s.seatRowNm,
            no: parseInt(s.seatNo, 10)};
  });
"""

DRIVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver", "msedgedriver.exe")
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_profile")
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_cookies.json")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper_settings.json")

driver = None
refresh_xpath = None
recent_log = []
popup_shown = False
last_push = 0.0
booking_in_progress = False
selected_show_label = ""


def load_settings():
    global DAY_NUMBER, SHOW_TIME
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("day"):
            DAY_NUMBER = str(data["day"])
        if data.get("time"):
            SHOW_TIME = str(data["time"])
    except Exception:
        pass


def save_settings(day=None, show_time=None):
    global DAY_NUMBER, SHOW_TIME
    if day:
        DAY_NUMBER = str(day)
    if show_time:
        SHOW_TIME = str(show_time)
    try:
        cur = {}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                cur = json.load(f) or {}
        cur["day"] = DAY_NUMBER
        cur["time"] = SHOW_TIME
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        log(f"회차 저장: {DAY_NUMBER}일 {SHOW_TIME}")
    except Exception as e:
        log(f"회차 저장 실패: {type(e).__name__}")


load_settings()


def build_driver() -> webdriver.Edge:
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = screen_w // 2
    win_h = screen_h - 80

    options = webdriver.EdgeOptions()
    options.add_argument(f"--window-size={win_w},{win_h}")
    options.add_argument(f"--window-position={win_w},0")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    return webdriver.Edge(service=Service(DRIVER_PATH), options=options)


def save_cookies():
    if driver is None:
        return
    try:
        cookies = driver.get_cookies()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        log(f"세션 쿠키 {len(cookies)}개 저장됨")
    except Exception as e:
        log(f"쿠키 저장 실패: {type(e).__name__}")


def restore_cookies() -> int:
    if not os.path.exists(COOKIE_FILE):
        return 0
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception:
        return 0

    n = 0
    for c in cookies:
        c.pop("sameSite", None)
        try:
            driver.add_cookie(c)
            n += 1
        except Exception:
            continue
    return n


def is_logged_in() -> bool:
    try:
        names = {c["name"] for c in driver.get_cookies()}
        return "accessToken" in names
    except Exception:
        return False


def prepare_login():
    threading.Thread(target=auto_boot, daemon=True).start()
    set_status("브라우저 여는 중...")


def auto_boot():
    """bat/GUI 시작 시 드라이버 + CGV + 용산 + 회차 선택."""
    global driver
    try:
        if driver is None:
            driver = build_driver()
            driver.get("https://cgv.co.kr")
            restored = restore_cookies()
            if restored:
                log(f"저장된 쿠키 {restored}개 복원 시도")
            log(f"브라우저 실행 (프로필: {PROFILE_DIR})")

        if not is_logged_in():
            driver.get(LOGIN_URL)
            log("로그인 페이지 — 로그인되면 예매 화면으로 이어갑니다.")
            set_status("로그인 필요 — 브라우저에서 로그인하세요.")
            deadline = time.time() + 180
            while time.time() < deadline:
                if is_logged_in():
                    log("로그인 확인됨")
                    save_cookies()
                    break
                time.sleep(1.2)
            else:
                set_status("로그인 대기 시간 초과. 로그인 후 '예매창 이동'을 누르세요.")
                return

        open_booking_and_pick_show()
    except Exception as e:
        log(f"시작 실패: {type(e).__name__}: {e}")
        set_status(f"오류: {e}")


def list_days():
    days = []
    for el in driver.find_elements(By.XPATH, DAY_SCROLL_XPATH):
        try:
            if not el.is_displayed():
                continue
            t = (el.text or "").replace("\n", " ").strip()
            nums = [p for p in t.replace("일", " ").split() if p.isdigit()]
            if nums:
                days.append((nums[-1], t, el))
        except Exception:
            continue
    return days


def list_showtimes():
    """현재 날짜에 보이는 회차 HH:MM 목록."""
    found = []
    seen = set()
    xps = [
        '//button[.//span[contains(text(),":")]]',
        '//button[contains(@class,"time") or contains(@class,"Time")]',
    ]
    for xp in xps:
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if not el.is_displayed():
                    continue
                t = (el.text or "").replace("\n", " ").strip()
                import re
                m = re.search(r"(\d{1,2}:\d{2})", t)
                if not m:
                    continue
                hhmm = m.group(1)
                if len(hhmm) < 5:
                    a, b = hhmm.split(":")
                    hhmm = f"{int(a):02d}:{b}"
                if hhmm in seen:
                    continue
                seen.add(hhmm)
                found.append((hhmm, t[:40], el))
            except Exception:
                continue
        if found:
            break
    return found


def ask_showtime(options):
    """회차 선택 팝업. 저장값이 목록에 있으면 기본 선택."""
    result = {"val": None}

    def show():
        win = tk.Toplevel(root)
        win.title("회차 선택")
        win.geometry("360x420")
        win.transient(root)
        tk.Label(win, text=f"{SITE_NAME}\n회차를 선택하세요", justify="center").pack(pady=8)
        lb = tk.Listbox(win, height=14)
        lb.pack(fill="both", expand=True, padx=12)
        labels = [f"{hhmm}  |  {raw}" for hhmm, raw, _ in options]
        for s in labels:
            lb.insert("end", s)
        prefer = None
        for i, (hhmm, _, _) in enumerate(options):
            if hhmm == SHOW_TIME:
                prefer = i
                break
        if prefer is not None:
            lb.selection_set(prefer)
            lb.see(prefer)

        def ok():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("알림", "회차를 골라 주세요.", parent=win)
                return
            result["val"] = options[sel[0]][0]
            win.destroy()

        def cancel():
            win.destroy()

        bf = tk.Frame(win)
        bf.pack(pady=8)
        tk.Button(bf, text="선택", width=10, command=ok).pack(side="left", padx=6)
        tk.Button(bf, text="취소", width=10, command=cancel).pack(side="left", padx=6)
        win.grab_set()
        root.wait_window(win)

    root.after(0, show)
    # wait_window must run on UI thread; show() already does when called via after
    # so we spin until closed
    while True:
        if result["val"] is not None or not any(
            isinstance(w, tk.Toplevel) and w.winfo_exists() for w in root.winfo_children()
        ):
            # first loop may run before window exists
            pass
        time.sleep(0.15)
        tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        if result["val"] is not None:
            return result["val"]
        if not tops and result["val"] is None:
            # window not yet created
            continue


def pick_showtime_blocking(options):
    holder = {"done": False, "val": None}

    def ui():
        win = tk.Toplevel(root)
        win.title("회차 선택")
        win.geometry("380x440")
        win.attributes("-topmost", True)
        tk.Label(win, text=f"{SITE_NAME} · {MOVIE_ALT}\n회차를 선택하세요", justify="center").pack(pady=8)
        lb = tk.Listbox(win, height=14)
        lb.pack(fill="both", expand=True, padx=12)
        for hhmm, raw, _ in options:
            mark = " ★저장" if hhmm == SHOW_TIME else ""
            lb.insert("end", f"{hhmm}{mark}   {raw}")
        for i, (hhmm, _, _) in enumerate(options):
            if hhmm == SHOW_TIME:
                lb.selection_set(i)
                lb.see(i)
                break

        def finish(val):
            holder["val"] = val
            holder["done"] = True
            try:
                win.destroy()
            except Exception:
                pass

        tk.Button(win, text="이 회차로 이동", width=16,
                  command=lambda: finish(options[lb.curselection()[0]][0] if lb.curselection() else None)
                  ).pack(pady=6)
        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))

    root.after(0, ui)
    while not holder["done"]:
        time.sleep(0.1)
    return holder["val"]


def open_booking_and_pick_show():
    global selected_show_label
    log("예매 페이지로 이동 중...")
    set_status("예매 페이지 이동 중...")
    driver.get(BOOK_URL)
    click_when_ready(
        f'//div[contains(@class,"swiper-slide")][.//img[@alt="{MOVIE_ALT}"]]',
        f"영화({MOVIE_ALT})",
    )
    select_theater()

    days = list_days()
    day_to_use = DAY_NUMBER
    day_ok = any(d[0] == DAY_NUMBER for d in days)
    if days and not day_ok:
        day_to_use = days[0][0]
        log(f"저장 날짜 {DAY_NUMBER}일 없음 → {day_to_use}일")
    click_when_ready(
        '//button[contains(@class,"dayScroll_scrollItem")]'
        f'[.//span[normalize-space(text())="{day_to_use}"]]',
        f"날짜({day_to_use}일)",
    )
    time.sleep(0.6)

    shows = list_showtimes()
    if not shows:
        raise TimeoutException("회차 목록을 읽지 못했습니다.")

    times = [s[0] for s in shows]
    log("회차 목록: " + ", ".join(times))

    chosen = SHOW_TIME if SHOW_TIME in times else None
    if chosen:
        log(f"저장 회차 유효: {chosen}")
    else:
        chosen = pick_showtime_blocking(shows)
        if not chosen:
            set_status("회차 선택이 취소되었습니다.")
            return
    save_settings(day=day_to_use, show_time=chosen)

    click_when_ready(
        f'//button[.//span[normalize-space(text())="{chosen}"]]',
        f"회차({chosen})",
    )
    WebDriverWait(driver, 15).until(EC.url_contains("selectVisitorCnt"))
    selected_show_label = f"{day_to_use}일 {chosen}"
    log(f"{chosen} 회차 인원/좌석 화면 도달")
    set_status(f"{selected_show_label} 도달. 감시 시작 가능.")
    save_cookies()
    try:
        root.after(0, lambda: btn_book.config(text=f"예매창 이동 ({chosen})"))
    except Exception:
        pass


def click_when_ready(xpath: str, label: str, timeout: int = 15):
    el = wait_visible(xpath, timeout)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    driver.execute_script("arguments[0].click();", el)
    log(f"{label} 선택 완료")
    set_status(f"{label} 선택 완료")


def wait_visible(xpath: str, timeout: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for el in driver.find_elements(By.XPATH, xpath):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue
        time.sleep(0.3)
    raise TimeoutException(f"보이는 요소를 찾지 못함: {xpath}")


def visible_exists(xpath: str) -> bool:
    for el in driver.find_elements(By.XPATH, xpath):
        try:
            if el.is_displayed():
                return True
        except Exception:
            continue
    return False


EMPTY_SITE_XPATH = '//*[contains(text(),"선택 된 극장이 없습니다")]'
DAY_SCROLL_XPATH = '//button[contains(@class,"dayScroll_scrollItem")]'


def select_theater():
    deadline = time.time() + 15
    while time.time() < deadline:
        if visible_exists(EMPTY_SITE_XPATH):
            break
        if visible_exists(DAY_SCROLL_XPATH):
            log("극장 이미 선택됨 — 건너뜀")
            return
        time.sleep(0.3)
    else:
        raise TimeoutException("극장 선택 영역이 나타나지 않음")

    click_when_ready('//*[contains(text(),"선택 된 극장이 없습니다")]/following::button[1]',
                     "극장 선택창 열기")
    click_when_ready(f'//*[normalize-space(text())="{SITE_NAME}"]', f"극장({SITE_NAME})")
    click_when_ready('//button[normalize-space(text())="극장선택"]', "극장 확정")


def goto_booking():
    if driver is None:
        messagebox.showwarning("알림", "브라우저가 없습니다. 잠시 기다리거나 다시 실행하세요.")
        return

    def task():
        try:
            if not is_logged_in():
                log("로그인이 안 된 상태입니다. 브라우저에서 먼저 로그인해주세요.")
                set_status("로그인이 필요합니다.")
                return
            open_booking_and_pick_show()
        except Exception as e:
            log(f"예매창 이동 실패: {type(e).__name__}: {e}")
            set_status(f"오류: {type(e).__name__} - 화면에서 직접 진행해주세요.")

    threading.Thread(target=task, daemon=True).start()


def _people_already_selected() -> bool:
    """인원 N이 이미 선택된 상태면 True (감시 전 미리 눌러둔 경우)."""
    xps = [
        f'//button[@aria-label="{PEOPLE_COUNT} 선택" and (@aria-pressed="true" or contains(@class,"active") or contains(@class,"selected"))]',
        f'//button[@aria-label="{PEOPLE_COUNT} 선택"][@aria-pressed="true"]',
    ]
    for xp in xps:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    return True
        except Exception:
            pass
    return False


def open_seat_screen() -> bool:
    try:
        if _people_already_selected():
            log(f"인원 {PEOPLE_COUNT}명 이미 선택됨 — 건너뜀")
        else:
            click_when_ready(
                f'//button[@aria-label="{PEOPLE_COUNT} 선택"]',
                f"인원 {PEOPLE_COUNT}명",
                timeout=5,
            )
        click_when_ready(
            '//button[normalize-space(text())="선택"]',
            "선택",
            timeout=6,
        )
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located(
                (By.XPATH, '//button[contains(@class,"seatMap_seatNumber")]')
            )
        )
        n = len(driver.find_elements(
            By.XPATH, '//button[contains(@class,"seatMap_seatNumber")]'
        ))
        n_ok = len(driver.find_elements(
            By.XPATH,
            '//button[contains(@class,"seatMap_seatNumber") and not(@disabled)]'
        ))
        log(f"좌석 맵 표시 확인 (버튼 {n}개 / 활성 {n_ok}개)")
        return True
    except Exception as e:
        log(f"좌석 화면 진입 실패: {type(e).__name__}")
        try:
            log(f"  URL: {driver.current_url}")
        except Exception:
            pass
        return False


def debug_seat_state(seat_label: str) -> None:
    """클릭 실패 시 DOM에 그 좌석이 있는지, 매진/비활성인지 남긴다."""
    try:
        log(f"  [debug] URL={driver.current_url}")
        any_map = driver.find_elements(
            By.XPATH, '//button[contains(@class,"seatMap_seatNumber")]'
        )
        log(f"  [debug] 맵 좌석버튼 수={len(any_map)}")

        raw = driver.find_elements(
            By.XPATH,
            f'//button[contains(@class,"seatMap_seatNumber")]'
            f'//span[normalize-space()="{seat_label}"]/parent::button'
        )
        if not raw:
            log(f"  [debug] DOM에 '{seat_label}' 버튼 없음 (맵 미로딩/다른화면/이미 사라짐)")
            # 같은 열 번호만 샘플
            row = "".join(ch for ch in seat_label if ch.isalpha())
            sample = []
            for b in any_map[:30]:
                t = (b.text or "").replace("\n", "").strip()
                if t.startswith(row):
                    sample.append(t)
            if sample:
                log(f"  [debug] {row}열 샘플: {', '.join(sample[:12])}")
            return

        b = raw[0]
        cls = (b.get_attribute("class") or "")[:120]
        log(
            f"  [debug] '{seat_label}' 존재 "
            f"displayed={b.is_displayed()} enabled={b.is_enabled()} "
            f"disabled={b.get_attribute('disabled')} class={cls}"
        )
    except Exception as e:
        log(f"  [debug] 좌석상태 조회 실패: {type(e).__name__}: {e}")


def click_seat_pair(pair_label: str) -> bool:
    first = pair_label.split("+")[0].strip()
    n_map = len(driver.find_elements(
        By.XPATH, '//button[contains(@class,"seatMap_seatNumber")]'
    ))
    if n_map == 0:
        log(f"맵 버튼 0개 — {first} 대기 단축")
        deadline = time.time() + 1.2
        while time.time() < deadline and n_map == 0:
            time.sleep(0.1)
            n_map = len(driver.find_elements(
                By.XPATH, '//button[contains(@class,"seatMap_seatNumber")]'
            ))
        if n_map == 0:
            log(f"좌석 클릭 실패 ({first}): 맵 없음")
            debug_seat_state(first)
            return False

    clicked = driver.execute_script("""
        var label = arguments[0];
        var btns = document.querySelectorAll('button[class*="seatMap_seatNumber"]');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            if (b.disabled) continue;
            if ((b.className || '').indexOf('Disabled') !== -1) continue;
            var t = (b.innerText || '').replace(/\\s+/g, '');
            if (t === label || t.indexOf(label) !== -1) {
                b.scrollIntoView({block:'center', inline:'center'});
                b.click();
                return true;
            }
        }
        return false;
    """, first)
    if clicked:
        log(f"좌석 클릭(JS): {first}")
        time.sleep(0.12)
        return True

    log(f"좌석 클릭 실패 ({first}): 활성 버튼 없음")
    debug_seat_state(first)
    return False


def _popup_info(b) -> str:
    txt = (b.text or "").replace("\n", " ").strip()[:40]
    cls = (b.get_attribute("class") or "")[:80]
    aria = b.get_attribute("aria-label") or ""
    near = ""
    try:
        near = driver.execute_script("""
            var n = arguments[0];
            for (var i = 0; i < 6 && n; i++) {
                var t = (n.innerText || '').trim().split('\\n')[0];
                if (t && t.length < 40) return t;
                n = n.parentElement;
            }
            return '';
        """, b) or ""
    except Exception:
        pass
    return f"text='{txt}' aria='{aria}' class='{cls}' near='{near}'"


def dismiss_any_confirm_popup(timeout: float = 1.2) -> bool:
    """확인/예만 클릭. 닫기는 맵을 닫을 수 있어 기록만 한다."""
    confirm_xps = [
        '//button[contains(@class,"fill-main") and normalize-space()="확인"]',
        '//button[normalize-space()="확인"]',
        '//button[normalize-space()="예"]',
    ]
    close_xps = [
        '//button[normalize-space()="닫기"]',
        '//button[contains(@aria-label,"닫기")]',
    ]
    end = time.time() + timeout
    clicked = False
    seen_close = False
    while time.time() < end:
        if not seen_close:
            for xp in close_xps:
                try:
                    for b in driver.find_elements(By.XPATH, xp):
                        if b.is_displayed():
                            log(f"[팝업관찰] 닫기 발견 — 클릭안함 | {_popup_info(b)}")
                            seen_close = True
                except Exception:
                    pass
        for xp in confirm_xps:
            try:
                for b in driver.find_elements(By.XPATH, xp):
                    if b.is_displayed() and b.is_enabled():
                        log(f"[팝업클릭] 확인 | {_popup_info(b)}")
                        driver.execute_script("arguments[0].click();", b)
                        clicked = True
                        time.sleep(0.12)
            except Exception:
                pass
        if clicked:
            return True
        time.sleep(0.1)
    return False


def wait_two_seats_selected(timeout: float = 5.0) -> bool:
    xpath = (
        '//button[contains(@class,"btn-100") and contains(@class,"fill-main") '
        'and normalize-space()="선택완료"]'
    )
    end = time.time() + timeout
    while time.time() < end:
        try:
            for btn in driver.find_elements(By.XPATH, xpath):
                if btn.is_displayed() and not btn.get_attribute("disabled"):
                    log("2좌석 선택 확인 (선택완료 활성화)")
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    log("2좌석 선택 미확인 (선택완료 비활성/타임아웃)")
    return False


def click_select_complete() -> bool:
    xpath = (
        '//button[contains(@class,"btn-100") and contains(@class,"fill-main") '
        'and normalize-space()="선택완료"]'
    )
    try:
        btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        driver.execute_script("arguments[0].click();", btn)
        log("선택완료 클릭")
        return True
    except Exception as e:
        log(f"선택완료 실패: {type(e).__name__}")
        return False


def click_pay_button() -> bool:
    xpaths = [
        '//div[contains(@class,"botFix")]//button[contains(., "결제하기")]',
        '//div[contains(@class,"double-btn-wrap")]//button[contains(., "결제하기")]',
        '//button[contains(@class,"fill-main") and contains(., "결제하기")]',
    ]
    end = time.time() + 12
    last_err = ""
    while time.time() < end:
        for xp in xpaths:
            try:
                for btn in driver.find_elements(By.XPATH, xp):
                    if not btn.is_displayed():
                        continue
                    if btn.get_attribute("disabled"):
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", btn)
                    log("결제하기 클릭(JS)")
                    return True
            except Exception as e:
                last_err = type(e).__name__
        time.sleep(0.3)

    log(f"결제하기 클릭 실패: TimeoutException ({last_err})")
    try:
        raw = driver.find_elements(By.XPATH, '//button[contains(@class,"fill-main")]')
        for b in raw:
            t = (b.text or "").replace("\n", " ").strip()
            if t:
                log(f"  fill-main 버튼 발견: '{t}' disabled={b.get_attribute('disabled')}")
    except Exception:
        pass
    return False


def click_second_pay_button(timeout: float = 12.0) -> bool:
    xpaths = [
        '//button[contains(@class,"fill-main") and contains(@class,"c-white") and contains(., "결제하기")]',
        '//button[contains(@class,"btn-100") and contains(@class,"c-white") and normalize-space()="결제하기"]',
        '//button[contains(@class,"fill-main") and contains(., "결제하기")]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                for btn in driver.find_elements(By.XPATH, xp):
                    if not btn.is_displayed() or btn.get_attribute("disabled"):
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", btn)
                    log("2차 결제하기 클릭")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("2차 결제하기 실패")
    return False


def click_npay_button(timeout: float = 15.0) -> bool:
    xpaths = [
        '//button[.//img[@alt="Npay"]]',
        '//img[@alt="Npay"]/parent::button',
        '//button[.//img[contains(@alt,"Npay")]]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    if not el.is_displayed():
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", el)
                    log("Npay 버튼 클릭")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("Npay 버튼 실패")
    return False


def click_agree_all_terms(timeout: float = 12.0) -> bool:
    xpaths = [
        '//label[@for="chkAll"]',
        '//label[contains(@class,"chck-text") and contains(., "전체 약관")]',
        '//label[contains(@class,"chck-icon")]',
        '//input[@id="chkAll"]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    if not el.is_displayed():
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", el)
                    log("전체 약관 동의 클릭")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("전체 약관 동의 실패")
    return False


def click_amount_pay_button(timeout: float = 12.0) -> bool:
    xpaths = [
        '//button[contains(@class,"btn-font2way") and contains(., "결제하기")]',
        '//button[contains(@class,"fill-main") and contains(@class,"c-white") and contains(., "결제하기")]',
        '//button[contains(@class,"fill-main") and contains(., "원") and contains(., "결제하기")]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                for btn in driver.find_elements(By.XPATH, xp):
                    if not btn.is_displayed() or btn.get_attribute("disabled"):
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", btn)
                    log("금액 결제하기 클릭")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("금액 결제하기 실패")
    return False


def click_npay_agree_and_pay(timeout: float = 15.0) -> bool:
    xpaths = [
        '//button[contains(., "동의하고 결제하기")]',
        '//button[contains(@class,"ButtonBox-module") and contains(., "동의하고 결제하기")]',
        '//button[contains(@class,"color-npayGreen") or contains(@class,"npayGreen")]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                for btn in driver.find_elements(By.XPATH, xp):
                    if not btn.is_displayed() or btn.get_attribute("disabled"):
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", btn)
                    log("동의하고 결제하기 클릭")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("동의하고 결제하기 실패")
    return False


def wait_secure_keyboard(timeout: float = 20.0) -> bool:
    xpaths = [
        '//div[contains(@class,"SecureKeyboard")]',
        '//div[@id="keyboard"]',
        '//h2[contains(., "입력 키패드")]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                els = driver.find_elements(By.XPATH, xp)
                if any(e.is_displayed() for e in els):
                    log("보안키패드 표시됨 — 6자리는 직접 입력하세요")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("보안키패드 대기 타임아웃")
    return False


def switch_to_npay_keyboard() -> bool:
    """키패드가 있는 document(창/iframe)로 전환."""
    def has_kb():
        return bool(driver.find_elements(
            By.CSS_SELECTOR, "#keyboard, [class*='SecureKeyboard']"
        ))

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    if has_kb():
        return True
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            if has_kb():
                return True
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    handles = list(driver.window_handles)
    current = None
    try:
        current = driver.current_window_handle
    except Exception:
        pass
    for h in handles:
        try:
            driver.switch_to.window(h)
            driver.switch_to.default_content()
            if has_kb():
                return True
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.frame(frame)
                    if has_kb():
                        return True
                except Exception:
                    pass
        except Exception:
            continue
    if current:
        try:
            driver.switch_to.window(current)
        except Exception:
            pass
    return has_kb()


def _ocr_digit(img):
    """한 칸 이미지에서 숫자 1개. pytesseract 필요."""
    try:
        import pytesseract
        from PIL import ImageOps, ImageFilter
    except Exception:
        return ""
    g = ImageOps.grayscale(img)
    g = g.resize((g.width * 4, g.height * 4))
    g = ImageOps.autocontrast(g)
    g = g.point(lambda p: 255 if p > 140 else 0)
    g = g.filter(ImageFilter.SHARPEN)
    cfg = "--psm 10 -c tessedit_char_whitelist=0123456789"
    try:
        txt = pytesseract.image_to_string(g, config=cfg) or ""
    except Exception:
        txt = ""
    digits = re.sub(r"\D", "", txt)
    return digits[:1]


def read_npay_sprite_map():
    """
    스프라이트 한 장을 잘라 칸→숫자 맵을 만든다.
    반환: {'1-1':'9', '1-2':'1', ...}
    """
    from PIL import Image

    data = driver.execute_script("""
        var el = document.querySelector('[class*="SecureKeyboard_number"]');
        if (!el) return null;
        var st = el.getAttribute('style') || '';
        var m = st.match(/base64,([A-Za-z0-9+/=]+)/);
        if (m) return m[1];
        var cs = getComputedStyle(el).backgroundImage || '';
        m = cs.match(/base64,([A-Za-z0-9+/=]+)/);
        return m ? m[1] : null;
    """)
    if not data:
        log("PIN OCR: 스프라이트 base64 없음")
        return {}

    raw = base64.b64decode(data)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    cw, ch = img.width / 3.0, img.height / 4.0
    cells = [
        ("1-1", 0, 0), ("1-2", 1, 0), ("1-3", 2, 0),
        ("2-1", 0, 1), ("2-2", 1, 1), ("2-3", 2, 1),
        ("3-1", 0, 2), ("3-2", 1, 2), ("3-3", 2, 2),
        ("4-2", 1, 3),
    ]
    debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pin_ocr_debug")
    os.makedirs(debug_dir, exist_ok=True)
    img.save(os.path.join(debug_dir, "sprite.png"))

    mapping = {}
    for pos, col, row in cells:
        box = (
            int(col * cw), int(row * ch),
            int((col + 1) * cw), int((row + 1) * ch),
        )
        crop = img.crop(box)
        crop.save(os.path.join(debug_dir, f"{pos}.png"))
        d = _ocr_digit(crop)
        mapping[pos] = d
        log(f"PIN OCR {pos} → '{d or '?'}'")
    return mapping


def click_npay_key(pos_or_text: str) -> bool:
    if pos_or_text in ("전체삭제", "지우기"):
        xp = f'//div[contains(@class,"SecureKeyboard") or @id="keyboard"]//button[contains(., "{pos_or_text}")]'
    else:
        xp = (
            f'//*[contains(@class,"SecureKeyboard_key-{pos_or_text}")]'
            f'/ancestor::button[1]'
        )
    try:
        els = driver.find_elements(By.XPATH, xp)
        if not els:
            els = driver.find_elements(
                By.XPATH,
                f'//*[contains(@class,"key-{pos_or_text}")]/ancestor::button[1]',
            )
        if not els:
            return False
        driver.execute_script("arguments[0].click();", els[0])
        return True
    except Exception:
        return False


def enter_npay_pin(pin: str = None) -> bool:
    """키패드가 떠 있을 때 PIN 6자리를 OCR 매핑 후 클릭."""
    pin = (pin or NPAY_PIN).strip()
    if not pin or not pin.isdigit() or len(pin) != 6:
        log("PIN OCR: CGV_NPAY_PIN 이 6자리 숫자가 아님 — 수동 입력")
        return False
    if not switch_to_npay_keyboard():
        log("PIN OCR: 키패드 document를 못 찾음")
        return False
    mapping = read_npay_sprite_map()
    found = [v for v in mapping.values() if v]
    log(f"PIN OCR 인식 {len(found)}/10: {mapping}")
    if len(set(found)) < 8:
        log("PIN OCR: 인식 부족 — 수동 입력. pin_ocr_debug 폴더 확인")
        return False
    digit_to_pos = {}
    for pos, d in mapping.items():
        if d and d not in digit_to_pos:
            digit_to_pos[d] = pos
    for ch in pin:
        pos = digit_to_pos.get(ch)
        if not pos:
            log(f"PIN OCR: '{ch}' 칸을 못 찾음")
            return False
        if not click_npay_key(pos):
            log(f"PIN OCR: {pos} 클릭 실패")
            return False
        log(f"PIN OCR: {pos} 클릭")
        time.sleep(0.22)
    log("PIN OCR: 6자리 클릭 완료")
    return True


def test_pin_ocr_only():
    def task():
        if driver is None:
            log("브라우저 없음")
            return
        log("[테스트] PIN OCR 시작")
        ok = enter_npay_pin()
        log(f"[테스트] PIN OCR {'성공' if ok else '실패'}")

    threading.Thread(target=task, daemon=True).start()


def auto_select_and_pay(targets, treat_as_preferential=None) -> tuple:
    """
    일반석 실사용.
    targets: ['K16+K17', 'K17+K18', ...] 순서대로 시도.
    treat_as_preferential 인자는 호환용으로만 받고 무시한다.
    """
    if not targets:
        return False, "타겟 없음"

    pairs = []
    for t in targets:
        if isinstance(t, dict):
            pairs.append(str(t.get("seat", t)))
        else:
            pairs.append(str(t))

    last_err = ""
    for pair in pairs:
        parts = [p.strip() for p in pair.split("+") if p.strip()]
        if not parts:
            continue
        first = parts[0]
        second = parts[1] if len(parts) > 1 else None

        log(f"좌석 시도: {pair} ({'우대' if treat_as_preferential else '일반'})")

        if not click_seat_pair(first):
            last_err = f"{first} 클릭 실패"
            log(last_err)
            continue

        time.sleep(0.15)
        if treat_as_preferential:
            dismiss_any_confirm_popup(timeout=2.0)
        else:
            dismiss_any_confirm_popup(timeout=0.4)

        if treat_as_preferential and second:
            if click_seat_pair(second):
                time.sleep(0.15)
                dismiss_any_confirm_popup(timeout=2.0)
            else:
                log(f"{second} 추가 클릭 실패")

        if not wait_two_seats_selected(timeout=1.5):
            if second and not treat_as_preferential:
                log(f"2석 미확인 → 둘째 칸 클릭: {second}")
                if click_seat_pair(second):
                    time.sleep(0.15)
                    dismiss_any_confirm_popup(timeout=0.6)
            if not wait_two_seats_selected(timeout=1.5):
                last_err = f"{pair} 클릭 후 2좌석 선택이 확인되지 않음"
                log(last_err)
                continue

        if not click_select_complete():
            last_err = f"{pair} 선택완료 실패"
            log(last_err)
            continue

        time.sleep(0.2)
        dismiss_any_confirm_popup(timeout=1.2)
        time.sleep(0.2)

        if not click_pay_button():
            last_err = f"{pair} 선택완료 OK, 결제하기 실패"
            log(last_err)
            dismiss_any_confirm_popup(timeout=1.5)
            if not click_pay_button():
                continue

        time.sleep(1.0)
        if not click_second_pay_button():
            last_err = f"{pair} 2차 결제하기 실패"
            log(last_err)
            continue

        time.sleep(1.2)
        if not click_npay_button():
            return False, f"{pair} Npay 클릭 실패"

        time.sleep(1.0)
        if not click_agree_all_terms():
            return False, f"{pair} 전체약관 동의 실패"

        time.sleep(0.5)
        if not click_amount_pay_button():
            return False, f"{pair} 금액 결제하기 실패"

        time.sleep(1.0)
        if not click_npay_agree_and_pay():
            return False, f"{pair} 동의하고 결제하기 실패"

        keyboard_ok = wait_secure_keyboard()
        title = "CGV Npay 결제 대기"
        if keyboard_ok:
            body = (
                f"{pair} 자동 진행 완료.\n"
                f"Npay 6자리 키패드가 떠 있습니다.\n"
                f"원격으로 PIN 입력해 주세요.\n"
                f"{time.strftime('%H:%M:%S')}"
            )
            detail = f"{pair} → Npay 결제 완료 대기 중"
            if NPAY_PIN:
                if enter_npay_pin():
                    detail = f"{pair} → PIN OCR 입력 완료"
                else:
                    detail = f"{pair} → 키패드 (OCR 실패·수동)"
        else:
            body = (
                f"{pair} 동의하고 결제까지 완료.\n"
                f"키패드 미감지 — 화면 확인.\n"
                f"{time.strftime('%H:%M:%S')}"
            )
            detail = f"{pair} → 결제 직전 (키패드 미감지)"

        notify_phone_repeat(title, body, times=10, interval_sec=3.0)
        try:
            bring_browser_front()
        except Exception:
            pass
        return True, detail

    return False, last_err or "모든 후보 좌석 시도 실패"


def notify_phone_repeat(title: str, message: str, times: int = 10, interval_sec: float = 3.0):
    def task():
        for i in range(times):
            try:
                notify_phone(title, f"[{i + 1}/{times}]\n{message}")
                log(f"알림 전송 {i + 1}/{times}")
            except Exception as e:
                log(f"알림 실패 {i + 1}/{times}: {type(e).__name__}")
            if i + 1 < times:
                time.sleep(interval_sec)

    threading.Thread(target=task, daemon=True).start()


def find_preferential_pair():
    """맵에서 우대/장애인석 라벨 2개를 고른다."""
    labels = driver.execute_script("""
        var out = [];
        var btns = document.querySelectorAll('button[class*="seatMap_seatNumber"]');
        for (var i=0;i<btns.length;i++){
          var b=btns[i];
          if (b.disabled) continue;
          var c=b.className||'';
          if (c.indexOf('Disabled')!==-1) continue;
          if (c.indexOf('Preferential')===-1 && c.indexOf('preferential')===-1
              && c.indexOf('Wheel')===-1 && c.indexOf('wheel')===-1
              && c.indexOf('장애')===-1 && c.indexOf('우대')===-1) continue;
          var t=(b.innerText||'').replace(/\\s+/g,'').trim();
          if (t) out.push(t);
        }
        return out;
    """) or []
    log(f"[테스트] 우대석 후보: {labels[:12]}")
    if len(labels) >= 2:
        return f"{labels[0]}+{labels[1]}"
    if len(labels) == 1:
        return labels[0]
    return None


def test_full_flow_like_watch():
    """장애인/우대 2석 → 확인 팝업 → 결제 → Npay 키패드까지."""
    def task():
        if driver is None:
            log("브라우저 없음 — 시작/로그인 후 회차 화면까지 가세요.")
            return
        log("[테스트] 우대 2석 결제 플로우 시작")
        if not open_seat_screen():
            log("[테스트] 실패 — 좌석 화면 진입 실패")
            return
        time.sleep(0.5)
        pair = find_preferential_pair()
        if not pair:
            log("[테스트] 실패 — 맵에서 우대석 2개를 못 찾음")
            return
        log(f"[테스트] 대상 {pair}")
        ok, detail = auto_select_and_pay([pair], treat_as_preferential=True)
        log(f"[테스트] {'성공' if ok else '실패'} — {detail}")

    threading.Thread(target=task, daemon=True).start()


def select_people():
    if driver is None:
        messagebox.showwarning("알림", "먼저 예매창으로 이동해주세요.")
        return
    threading.Thread(target=open_seat_screen, daemon=True).start()


def click_refresh():
    candidates = [
        '//button[@aria-label="새로고침"]',
        '//button[normalize-space(text())="새로고침"]',
        '//*[contains(text(),"관람인원")]/following::button[1]',
    ]
    global refresh_xpath

    if refresh_xpath:
        try:
            driver.find_element(By.XPATH, refresh_xpath).click()
            return True
        except Exception:
            refresh_xpath = None

    for xpath in candidates:
        try:
            el = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            el.click()
            refresh_xpath = xpath
            log(f"  (새로고침 셀렉터 확정: {xpath})")
            return True
        except Exception:
            continue
    return False


def wait_for_fresh_data(timeout: float = 4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = driver.execute_script(READ_JS)
        if rows is not None:
            return rows
        time.sleep(0.1)
    return None


def find_adjacent_pairs(seats):
    by_row = {}
    for s in seats:
        by_row.setdefault(s["row"], []).append(s)

    pairs = []
    for row, items in by_row.items():
        items.sort(key=lambda s: s["x"])
        for a, b in zip(items, items[1:]):
            if b["x"] - a["x"] == 2:
                pairs.append(f'{a["seat"]}+{b["seat"]}')
    return pairs


def in_target(s) -> bool:
    return (s["zone"] not in EXCLUDE_ZONES
            and s["row"].upper() in TARGET_ROWS
            and s["no"] is not None
            and SEAT_NO_MIN <= s["no"] <= SEAT_NO_MAX)


def row_index(row_name: str) -> int:
    if len(row_name) == 1 and row_name.isalpha():
        return ord(row_name.upper()) - ord("A") + 1
    return 0


def build_alert_text(seats, pairs, attempt) -> str:
    paired = set()
    for p in pairs:
        a, b = p.split("+")
        paired.update((a, b))

    pair_lines = []
    for p in sorted(pairs, key=lambda p: (row_index(p[0]), p)):
        idx = row_index(p[0])
        pair_lines.append(f"  {p}  ({idx}열)" if idx else f"  {p}")

    solo = [s for s in seats if s["seat"] not in paired]
    solo_lines = []
    for s in sorted(solo, key=lambda s: (row_index(s["row"]), s["x"])):
        idx = row_index(s["row"])
        solo_lines.append(f"  {s['seat']} ({idx}열)" if idx else f"  {s['seat']}")

    body = f"\n★ 2연석 {len(pairs)}쌍\n" + ("\n".join(pair_lines) if pair_lines else "  없음")
    if solo_lines:
        body += f"\n\n단독석 {len(solo_lines)}석 (참고)\n" + "\n".join(solo_lines)

    return (
        f"오디세이 IMAX 08.31 {SHOW_TIME} / 용산아이파크몰\n"
        f"타겟 G~J열 {SEAT_NO_MIN}~{SEAT_NO_MAX}번\n"
        f"확인 시각 {time.strftime('%H:%M:%S')} ({attempt}회차)\n"
        + body
        + f"\n\n중지하려면 /stop"
    )


def bring_browser_front():
    def raise_it():
        try:
            driver.switch_to.window(driver.current_window_handle)
        except Exception:
            pass

    root.after(0, raise_it)


def show_popup(count: int, pairs):
    global popup_shown
    if popup_shown:
        return
    popup_shown = True

    def open_it():
        root.attributes("-topmost", True)
        root.lift()
        root.after(100, lambda: root.attributes("-topmost", False))
        messagebox.showinfo(
            "빈자리 발견",
            f"2연석 {len(pairs)}쌍이 나왔습니다.\n{', '.join(pairs)}\n\n지금 예매하세요.",
        )

    root.after(0, open_it)


def _restart_monitor_after_fail(delay_sec: float = 3.0):
    def task():
        global booking_in_progress
        time.sleep(delay_sec)
        booking_in_progress = False
        if monitoring.is_set():
            log("이미 감시 중 — 재시작 생략")
            return
        try:
            url = driver.current_url if driver else ""
            if driver and "selectVisitorCnt" not in url:
                log("인원 화면으로 복귀 시도")
                driver.back()
                time.sleep(1.2)
                if "selectVisitorCnt" not in (driver.current_url or ""):
                    driver.back()
                    time.sleep(1.0)
        except Exception as e:
            log(f"화면 복귀 실패: {type(e).__name__}")
        log("예매 실패 후 감시 재시작")
        try:
            msg = start_monitor(remote=True)
            log(f"감시 재시작 결과: {msg}")
            notify_phone("CGV 재감시 시작", f"{time.strftime('%H:%M:%S')}\n{msg}")
        except Exception as e:
            log(f"감시 재시작 실패: {type(e).__name__}: {e}")
            notify_phone(
                "CGV 재감시 실패",
                f"{time.strftime('%H:%M:%S')}\n직접 감시 시작을 눌러 주세요.",
            )

    threading.Thread(target=task, daemon=True).start()


def announce(seats, pairs, attempt):
    global booking_in_progress, popup_shown, last_push

    if booking_in_progress:
        log("이미 예매 진행 중 — announce 무시")
        return

    booking_in_progress = True
    monitoring.clear()
    log("타겟 2연석 확보 — 감시 중지, 자동 예매 진행")

    text = build_alert_text(seats, pairs, attempt)
    targets = pairs if REQUIRE_PAIR else [
        s["seat"] if isinstance(s, dict) else str(s) for s in seats
    ]

    if not targets:
        notify_phone("CGV 재감시", "타겟 목록 없음. 재시작합니다.")
        last_push = time.time()
        _restart_monitor_after_fail()
        return

    if not open_seat_screen():
        notify_phone_repeat(
            "CGV 선점 실패 · 재감시",
            f"인원/선택 진입 실패\n{time.strftime('%H:%M:%S')}",
            times=2,
            interval_sec=2.0,
        )
        last_push = time.time()
        _restart_monitor_after_fail()
        return

    time.sleep(0.4)

    ok, detail = auto_select_and_pay(targets)
    log(f"자동 예매 결과: {'성공' if ok else '실패'} — {detail}")

    if ok:
        try:
            notify_phone("CGV 자동 진행 완료", f"{detail}\n{time.strftime('%H:%M:%S')}")
        except Exception:
            pass
        try:
            bring_browser_front()
        except Exception:
            pass
        last_push = time.time()
        return

    notify_phone_repeat(
        "CGV 선점 실패 · 재감시",
        f"{detail}\n감시를 다시 시작합니다.\n{time.strftime('%H:%M:%S')}",
        times=2,
        interval_sec=2.0,
    )
    try:
        bring_browser_front()
    except Exception:
        pass
    last_push = time.time()
    _restart_monitor_after_fail()


def monitor_loop():
    attempt = 0
    fails = 0
    last_beat = time.time()
    last_save = time.time()

    while monitoring.is_set():
        attempt += 1
        try:
            driver.execute_script(HOOK_JS + "\nwindow.__cgvSeat = null;")

            t0 = time.time()
            if not click_refresh():
                log("새로고침 버튼을 못 찾았습니다. 감시를 중단합니다.")
                set_status("새로고침 버튼 탐색 실패 — 중지됨")
                notify_phone(
                    "CGV watch STOPPED",
                    f"{time.strftime('%H:%M:%S')} 새로고침 버튼을 찾지 못해 감시가 멈췄습니다.\n"
                    "PC를 확인해주세요.",
                )
                monitoring.clear()
                break

            rows = wait_for_fresh_data()
            took = time.time() - t0

            if rows is None:
                log(f"[{attempt}회] 좌석 응답 없음 (후킹 확인 필요) · {took:.1f}s", stamp=False)
                set_status("좌석 데이터 대기 중...")
            else:
                light = sum(1 for r in rows if r["zone"] in EXCLUDE_ZONES)
                seats = [r for r in rows if in_target(r)]
                pairs = find_adjacent_pairs(seats)

                log(
                    f"[{attempt}] A{len(rows)}/L{light}/T{len(seats)}/2연석{len(pairs)}"
                    f"  - [{time.strftime('%H:%M:%S')}]",
                    stamp=False,
                )

                hit = pairs if REQUIRE_PAIR else seats
                if hit:
                    log(f"★ 좌석: {', '.join(s['seat'] for s in seats)}")
                    log(f"★ 2연석: {', '.join(pairs) if pairs else '없음'}")
                    set_status(f"★ 2연석 {len(pairs)}쌍 — 예매 진행")
                    announce(seats, pairs, attempt)
                    if not monitoring.is_set():
                        break
                else:
                    set_status(f"빈자리 없음 — 감시 중 ({attempt}회차)")

            fails = 0
        except Exception as e:
            fails += 1
            log(f"  → 조회 실패({fails}회 연속): {type(e).__name__}: {e}")
            set_status(f"조회 실패 {fails}회 — 계속 시도합니다")

            if fails >= MAX_CONSECUTIVE_FAILS:
                log(f"연속 {fails}회 실패로 감시를 중단합니다.")
                set_status("연속 실패 — 감시 중지됨")
                notify_phone(
                    "CGV watch STOPPED",
                    f"{time.strftime('%H:%M:%S')} 연속 {fails}회 조회 실패로 감시가 멈췄습니다.\n"
                    f"({type(e).__name__}) PC를 확인해주세요.",
                )
                monitoring.clear()
                break

        if time.time() - last_save >= COOKIE_SAVE_MIN * 60:
            last_save = time.time()
            save_cookies()

        if HEARTBEAT_MIN > 0 and time.time() - last_beat >= HEARTBEAT_MIN * 60:
            last_beat = time.time()
            notify_phone(
                "CGV watch alive",
                f"{time.strftime('%H:%M:%S')} 감시 정상 작동 중 ({attempt}회차 확인 완료)\n"
                "아직 빈자리 없음.",
                priority="low",
            )

        for _ in range(get_interval()):
            if not monitoring.is_set():
                return
            time.sleep(1)


def start_monitor(remote: bool = False) -> str:
    if driver is None:
        msg = "브라우저가 없습니다. PC에서 '로그인 준비'와 '예매창 이동'을 먼저 해주세요."
        if not remote:
            messagebox.showwarning("알림", msg)
        return msg
    if monitoring.is_set():
        return "이미 감시 중입니다."
    global popup_shown, last_push
    popup_shown = False
    last_push = 0.0

    monitoring.set()
    try:
        driver.execute_script(HOOK_JS)
    except Exception:
        pass
    log(f"모드: {'웹(PC 앞)' if mode_var.get() == 'web' else '외출'}")
    log(f"감시 시작 (간격 {get_interval()}초, 타겟 "
        f"{''.join(sorted(TARGET_ROWS))}열 {SEAT_NO_MIN}~{SEAT_NO_MAX}번"
        f"{', 2연석만' if REQUIRE_PAIR else ''})")
    channels = []
    if TG_TOKEN and TG_CHAT_ID:
        channels.append("텔레그램")
    if NTFY_TOPIC:
        channels.append("ntfy")
    log(f"폰 알림: {', '.join(channels) if channels else '꺼짐'}")
    set_status("감시 시작...")

    threading.Thread(target=monitor_loop, daemon=True).start()
    return f"감시를 시작했습니다 ({get_interval()}초 간격)."


def stop_monitor(remote: bool = False) -> str:
    global booking_in_progress
    was_on = monitoring.is_set()
    if was_on:
        log("원격 명령으로 감시 중지" if remote else "사용자가 감시를 중지했습니다.")
    monitoring.clear()
    booking_in_progress = False
    set_status("감시 중지됨.")
    return "감시를 중지했습니다." if was_on else "감시 중이 아니었습니다."


def send_browser_shot() -> str:
    if driver is None:
        return "브라우저가 아직 없습니다."
    try:
        png = driver.get_screenshot_as_png()
    except Exception as e:
        return f"화면 캡처 실패: {type(e).__name__}"

    boundary = "----cgvshot" + str(int(time.time()))
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TG_CHAT_ID}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
        f"filename=\"shot.png\"\r\nContent-Type: image/png\r\n\r\n".encode(),
        png,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=20)
        return ""
    except Exception as e:
        return f"전송 실패: {type(e).__name__}"


def build_status() -> str:
    state = "감시 중" if monitoring.is_set() else "정지"
    mode = "웹(PC 앞)" if mode_var.get() == "web" else "외출"
    url = ""
    try:
        url = driver.current_url if driver else "브라우저 없음"
    except Exception:
        url = "브라우저 응답 없음"
    tail = "\n".join(recent_log[-6:]) if recent_log else "(로그 없음)"
    return (f"상태: {state}\n"
            f"모드: {mode}\n"
            f"주기: {get_interval()}초\n"
            f"시각: {time.strftime('%H:%M:%S')}\n"
            f"위치: {url}\n\n최근 로그:\n{tail}")


def handle_command(text: str) -> str:
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower().lstrip("/")

    if cmd in ("status", "s"):
        return build_status()
    if cmd in ("watch", "go"):
        return start_monitor(remote=True)
    if cmd in ("stop", "halt"):
        return stop_monitor(remote=True)
    if cmd == "log":
        return "\n".join(recent_log[-20:]) or "(로그 없음)"
    if cmd == "shot":
        return send_browser_shot()
    if cmd in ("mode", "m"):
        want = arg.strip().lower()
        if want in ("web", "웹"):
            root.after(0, lambda: mode_var.set("web"))
            return "웹모드로 바꿨습니다."
        if want in ("away", "외출"):
            root.after(0, lambda: mode_var.set("away"))
            return "외출모드로 바꿨습니다."
        return f"현재 모드: {'웹' if mode_var.get() == 'web' else '외출'}\n바꾸려면 /mode away 또는 /mode web"

    if cmd == "interval":
        try:
            n = max(1, int(arg))
        except ValueError:
            return "사용법: /interval 5"
        root.after(0, lambda: interval_var.set(str(n)))
        return f"주기를 {n}초로 바꿨습니다. (다음 감시 시작부터 적용)"
    if cmd in ("help", "start"):
        return ("사용 가능한 명령\n"
                "/status — 현재 상태와 최근 로그\n"
                "/watch — 감시 시작\n"
                "/stop — 감시 중지\n"
                "/log — 최근 로그 20줄\n"
                "/shot — 지금 브라우저 화면 사진\n"
                "/interval 5 — 감시 주기 변경\n"
                "/mode away|web — 외출/웹 모드 전환")
    return f"모르는 명령입니다: {text}\n/help 를 보내보세요."


def telegram_command_loop():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?timeout=25"
            if offset is not None:
                url += f"&offset={offset}"
            with urllib.request.urlopen(url, timeout=40) as r:
                data = json.load(r)

            for u in data.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = msg.get("text")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text:
                    continue
                if chat_id != str(TG_CHAT_ID):
                    continue
                log(f"원격 명령 수신: {text}")
                reply = handle_command(text)
                if reply:
                    notify_telegram(reply)
        except Exception:
            time.sleep(5)


def notify_telegram(text: str):
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        log("  → 텔레그램 알림 전송됨")
    except Exception as e:
        log(f"  → 텔레그램 알림 실패: {type(e).__name__}")


def find_telegram_chat_id():
    def task():
        if not TG_TOKEN:
            log("CGV_TG_TOKEN 환경변수가 없습니다.")
            return
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=10
            ) as resp:
                data = json.load(resp)
            chats = {
                str(u["message"]["chat"]["id"])
                for u in data.get("result", [])
                if "message" in u
            }
            if chats:
                for cid in chats:
                    log(f"chat_id 발견: {cid}")
                log("이 값을 CGV_TG_CHAT_ID 환경변수에 넣고 다시 실행하세요.")
            else:
                log("메시지가 없습니다. 텔레그램에서 봇에게 아무 말이나 보낸 뒤 다시 눌러주세요.")
        except Exception as e:
            log(f"chat_id 조회 실패: {type(e).__name__}: {e}")

    threading.Thread(target=task, daemon=True).start()


def notify_ntfy(title: str, message: str, priority: str = "urgent"):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", "ignore").decode() or "CGV",
                "Priority": priority,
                "Tags": "movie_camera",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        log("  → ntfy 알림 전송됨")
    except Exception as e:
        log(f"  → ntfy 알림 실패: {type(e).__name__}")


def notify_phone(title: str, message: str, priority: str = "urgent"):
    notify_telegram(f"{title}\n{message}")
    notify_ntfy(title, message, priority)


def send_test_push():
    def task():
        if not (TG_TOKEN and TG_CHAT_ID) and not NTFY_TOPIC:
            log("알림 채널이 설정되지 않았습니다 (CGV_TG_TOKEN/CGV_TG_CHAT_ID 또는 CGV_NTFY_TOPIC).")
            set_status("폰 알림 미설정")
            return
        log("테스트 알림 전송 시도")
        notify_phone("CGV Test", "테스트 알림입니다. 이게 보이면 설정 완료.", "default")

    threading.Thread(target=task, daemon=True).start()


def get_interval() -> int:
    try:
        return max(1, int(interval_var.get()))
    except (ValueError, tk.TclError):
        return CHECK_INTERVAL_SEC


def set_status(text: str):
    status_var.set(text)


def log(text: str, stamp: bool = True):
    line = (f"[{time.strftime('%H:%M:%S')}] {text}\n" if stamp else f"{text}\n")
    recent_log.append(line.rstrip())
    del recent_log[:-200]

    def append():
        log_box.configure(state="normal")
        log_box.insert("end", line)
        log_box.see("end")
        log_box.configure(state="disabled")

    root.after(0, append)


root = tk.Tk()
root.title(APP_NAME)
root.geometry("520x800")

status_var = tk.StringVar(value="대기 중")
interval_var = tk.StringVar(value=str(CHECK_INTERVAL_SEC))
mode_var = tk.StringVar(value="away")
monitoring = threading.Event()

tk.Label(root, textvariable=status_var, wraplength=480, justify="left").pack(pady=10, padx=10)

btn_prepare = tk.Button(root, text="브라우저 재시작", width=22, command=prepare_login)
btn_prepare.pack(pady=5)

btn_book = tk.Button(root, text=f"예매창/회차 선택 ({SHOW_TIME})", width=22, command=goto_booking)
btn_book.pack(pady=5)

btn_people = tk.Button(root, text=f"인원 {PEOPLE_COUNT}명 선택", width=20, command=select_people)
btn_people.pack(pady=5)

mode_frame = tk.Frame(root)
mode_frame.pack(pady=5)
tk.Label(mode_frame, text="모드:").pack(side="left")
tk.Radiobutton(mode_frame, text="웹(PC 앞)", variable=mode_var, value="web").pack(side="left")
tk.Radiobutton(mode_frame, text="외출", variable=mode_var, value="away").pack(side="left")

interval_frame = tk.Frame(root)
interval_frame.pack(pady=5)
tk.Label(interval_frame, text="감시 주기(초):").pack(side="left")
tk.Entry(interval_frame, textvariable=interval_var, width=6, justify="center").pack(side="left", padx=5)

btn_watch = tk.Button(root, text="빈자리 감시 시작", width=20, command=start_monitor)
btn_watch.pack(pady=5)

btn_stop = tk.Button(root, text="감시 중지", width=20, command=stop_monitor)
btn_stop.pack(pady=5)

btn_test_push = tk.Button(root, text="폰 알림 테스트", width=20, command=send_test_push)
btn_test_push.pack(pady=5)

btn_test_auto = tk.Button(
    root,
    text="테스트: 우대2석 → Npay키패드",
    width=28,
    command=test_full_flow_like_watch,
)
btn_test_auto.pack(pady=5)

btn_pin_ocr = tk.Button(
    root,
    text="테스트: PIN OCR (키패드 열린 상태)",
    width=28,
    command=test_pin_ocr_only,
)
btn_pin_ocr.pack(pady=5)

btn_chat_id = tk.Button(root, text="텔레그램 chat_id 찾기", width=20, command=find_telegram_chat_id)
btn_chat_id.pack(pady=5)

btn_save = tk.Button(root, text="세션 저장", width=20,
                     command=lambda: threading.Thread(target=save_cookies, daemon=True).start())
btn_save.pack(pady=5)

log_frame = tk.Frame(root)
log_frame.pack(fill="both", expand=True, padx=10, pady=(10, 10))

tk.Label(log_frame, text="로그", anchor="w").pack(fill="x")

log_scroll = tk.Scrollbar(log_frame)
log_scroll.pack(side="right", fill="y")

log_box = tk.Text(log_frame, height=12, wrap="word", state="disabled",
                  yscrollcommand=log_scroll.set)
log_box.pack(side="left", fill="both", expand=True)
log_scroll.config(command=log_box.yview)


def on_close():
    monitoring.clear()
    save_cookies()
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)

if TG_TOKEN and TG_CHAT_ID:
    threading.Thread(target=telegram_command_loop, daemon=True).start()
    log("원격 제어 활성화 — 폰에서 /help 를 보내보세요.")
else:
    log("원격 제어 꺼짐 (CGV_TG_TOKEN / CGV_TG_CHAT_ID 미설정)")

log(f"{APP_NAME} 시작 — 브라우저를 엽니다.")
root.after(400, lambda: threading.Thread(target=auto_boot, daemon=True).start())
root.mainloop()
