import json
import os
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

# 예매 대상 (필요하면 여기만 바꾸면 된다)
MOVIE_ALT = "오디세이 포스터"
SITE_NAME = "용산아이파크몰"
DAY_NUMBER = "31"
SHOW_TIME = "07:30"
PEOPLE_COUNT = 2  # 관람인원 (미리 눌러두는 용도)

# 타겟 범위: E~L열의 8~37번 좌석 중 '2연석'만 알림 대상.
EXCLUDE_ZONES = {"Light존"}
TARGET_ROWS = {"E", "F", "G", "H", "I", "J", "K", "L"}
SEAT_NO_MIN = 8
SEAT_NO_MAX = 37
REQUIRE_PAIR = True  # 붙어있는 2연석이 있을 때만 알린다
CHECK_INTERVAL_SEC = 5  # 새로고침 간격 기본값 (GUI에서 변경 가능)
MAX_CONSECUTIVE_FAILS = 5  # 이만큼 연속 실패하면 감시를 멈추고 폰으로 알린다
HEARTBEAT_MIN = 0  # "정상 작동 중" 알림 주기(분). 0이면 안 보냄 (필요하면 폰에서 /status)
COOKIE_SAVE_MIN = 10  # 이 분마다 세션 쿠키를 조용히 저장 (알림 없음)

# 폰 푸시 알림 — 텔레그램 우선, 없으면 ntfy.sh. 둘 다 설정하면 둘 다 보낸다.
# 토큰/키는 코드에 넣지 말고 환경변수로만 넘긴다.
TG_TOKEN = os.environ.get("CGV_TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("CGV_TG_CHAT_ID", "")

NTFY_TOPIC = os.environ.get("CGV_NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("CGV_NTFY_SERVER", "https://ntfy.sh")

# 페이지가 스스로 보내는 좌석조회 응답을 가로채서 읽는다 (추가 요청 없음).
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

# Selenium Manager가 쓰는 msedgedriver.azureedge.net이 이 환경에서 막혀 있어 driver를 직접 지정한다
DRIVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver", "msedgedriver.exe")
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_profile")
# CGV 로그인 토큰이 세션 쿠키(브라우저 닫으면 소멸)라 프로필만으론 로그인이 안 남는다.
# 종료할 때 저장해뒀다가 다음 실행에 되살린다.
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_cookies.json")

driver = None  # 전역으로 유지해서 다음 단계에서도 이어서 사용
refresh_xpath = None  # 처음 성공한 새로고침 셀렉터를 기억해둔다
recent_log = []  # 폰에서 /log 로 꺼내볼 최근 로그
popup_shown = False  # 웹모드 팝업을 이미 띄웠는지
last_push = 0.0  # 마지막 폰 알림 시각


def build_driver() -> webdriver.Edge:
    # 화면 오른쪽 절반만 차지하도록 위치/크기를 잡는다
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = screen_w // 2
    win_h = screen_h - 80  # 작업표시줄 여유

    options = webdriver.EdgeOptions()
    options.add_argument(f"--window-size={win_w},{win_h}")
    options.add_argument(f"--window-position={win_w},0")
    # 전용 프로필을 쓰면 로그인 쿠키가 남아 다음 실행부터 재로그인/캡차가 필요 없다
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    return webdriver.Edge(service=Service(DRIVER_PATH), options=options)


def save_cookies():
    """세션 쿠키까지 파일로 남겨둔다 (종료 시 호출)."""
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
    """저장해둔 쿠키를 되살린다. 도메인에 먼저 들어가 있어야 한다."""
    if not os.path.exists(COOKIE_FILE):
        return 0
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception:
        return 0

    n = 0
    for c in cookies:
        c.pop("sameSite", None)  # 드라이버가 거부하는 경우가 있어 뺀다
        try:
            driver.add_cookie(c)
            n += 1
        except Exception:
            continue
    return n


def is_logged_in() -> bool:
    """CGV가 returnUrl로 리다이렉트를 안 해줄 때가 있어 URL 대신 쿠키로 판단한다."""
    try:
        names = {c["name"] for c in driver.get_cookies()}
        return "accessToken" in names
    except Exception:
        return False


def prepare_login():
    def task():
        global driver
        try:
            driver = build_driver()

            # 쿠키를 넣으려면 해당 도메인에 먼저 접속해 있어야 한다
            driver.get("https://cgv.co.kr")
            restored = restore_cookies()
            if restored:
                log(f"저장된 쿠키 {restored}개 복원 시도")

            log(f"브라우저 실행 (프로필: {PROFILE_DIR})")
            if is_logged_in():
                log("이전 세션 유지됨 — 재로그인 불필요.")
                set_status("이미 로그인된 상태입니다.")
            else:
                driver.get(LOGIN_URL)
                log("로그인 페이지 열림 — 직접 로그인해주세요.")
                set_status("로그인 페이지 열림. 직접 로그인하세요.")
        except Exception as e:
            log(f"브라우저 실행 실패: {type(e).__name__}: {e}")
            set_status(f"오류: {e}")

    threading.Thread(target=task, daemon=True).start()
    set_status("브라우저 여는 중...")


def click_when_ready(xpath: str, label: str, timeout: int = 15):
    """클래스명이 빌드마다 바뀌는 해시라서 텍스트 기반 XPath로 찾는다."""
    el = wait_visible(xpath, timeout)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    # 포스터 위에 투명 오버레이가 덮여 있는 곳이 있어 일반 click은 가로채인다
    driver.execute_script("arguments[0].click();", el)
    log(f"{label} 선택 완료")
    set_status(f"{label} 선택 완료")


def wait_visible(xpath: str, timeout: int = 15):
    """숨겨진 모달 안에도 같은 요소가 있어서, 화면에 보이는 것만 골라야 한다."""
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
    """극장이 선택돼 있지 않으면 골라준다. 이미 돼 있으면 건너뛴다."""
    # 아직 렌더 전일 수 있으니 '극장 없음' 또는 '날짜 목록' 중 하나가 뜰 때까지 기다린다
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
        messagebox.showwarning("알림", "먼저 '로그인 준비'로 브라우저를 열고 로그인해주세요.")
        return

    def task():
        try:
            if not is_logged_in():
                log("로그인이 안 된 상태입니다. 브라우저에서 먼저 로그인해주세요.")
                set_status("로그인이 필요합니다.")
                return

            log("예매 페이지로 이동 중...")
            set_status("예매 페이지 이동 중...")
            driver.get(BOOK_URL)

            # 영화는 상단 슬라이더에서 고른다 (숨은 모달에도 같은 포스터가 있으니 보이는 것만)
            click_when_ready(f'//div[contains(@class,"swiper-slide")][.//img[@alt="{MOVIE_ALT}"]]',
                             f"영화({MOVIE_ALT})")

            select_theater()

            click_when_ready('//button[contains(@class,"dayScroll_scrollItem")]'
                             f'[.//span[normalize-space(text())="{DAY_NUMBER}"]]',
                             f"날짜({DAY_NUMBER}일)")

            click_when_ready(f'//button[.//span[normalize-space(text())="{SHOW_TIME}"]]',
                             f"회차({SHOW_TIME})")

            # 회차 선택이 끝나면 인원/좌석 선택 화면(/cnm/selectVisitorCnt)으로 넘어간다
            WebDriverWait(driver, 15).until(EC.url_contains("selectVisitorCnt"))
            log(f"{SHOW_TIME} 회차 인원/좌석 화면 도달")
            set_status(f"{SHOW_TIME} 회차 인원/좌석 화면 도달.")
            save_cookies()  # 로그인된 상태를 확보한 시점에 바로 남겨둔다
        except Exception as e:
            log(f"예매창 이동 실패: {type(e).__name__}: {e}")
            set_status(f"오류: {type(e).__name__} - 화면에서 직접 진행해주세요.")

    threading.Thread(target=task, daemon=True).start()


def open_seat_screen() -> bool:
    try:
        click_when_ready(
            f'//button[@aria-label="{PEOPLE_COUNT} 선택"]',
            f"인원 {PEOPLE_COUNT}명",
            timeout=8,
        )
        click_when_ready(
            '//button[normalize-space(text())="선택"]',
            "선택",
            timeout=8,
        )
        ...
        return True
    except Exception:
        return False

def click_seat_pair(pair_label: str) -> bool:
    """
    'A17+A18' → 첫 칸(A17) 클릭.
    일반석: 한 번에 2연석 선택될 수 있음.
    장애인/우대석: 클릭 후 확인 팝업이 뜸 (다음 함수에서 처리).
    """
    first = pair_label.split("+")[0].strip()
    xpath = (
        f'//button[contains(@class,"seatMap_seatNumber") '
        f'and not(@disabled) '
        f'and not(contains(@class,"Disabled"))]'
        f'//span[normalize-space()="{first}"]/parent::button'
    )
    try:
        el = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        time.sleep(0.2)
        el.click()
        log(f"좌석 클릭: {first} ({pair_label})")
        return True
    except Exception as e:
        log(f"좌석 클릭 실패 ({first}): {type(e).__name__}")
        return False

def dismiss_preferential_popup(timeout: float = 3.0) -> bool:
    """
    장애인/우대석 클릭 시 뜨는 팝업의 '확인' 버튼.
    일반석은 팝업이 없으면 False만 반환하고 넘어가면 됨.
    """
    xpath = (
        '//button[contains(@class,"btn-100") and contains(@class,"fill-main") '
        'and normalize-space()="확인"]'
    )
    end = time.time() + timeout
    while time.time() < end:
        try:
            btns = driver.find_elements(By.XPATH, xpath)
            for b in btns:
                if b.is_displayed() and b.is_enabled():
                    b.click()
                    log("장애인/우대석 팝업 '확인' 클릭")
                    time.sleep(0.3)
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    log("확인 팝업 없음 (일반석이거나 이미 닫힘)")
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
        btn.click()
        log("선택완료 클릭")
        return True
    except Exception as e:
        log(f"선택완료 실패: {type(e).__name__}")
        return False

def click_second_pay_button(timeout: float = 12.0) -> bool:
    """좌석 후 첫 결제하기 다음, 같은 자리의 '결제하기' (c-white)."""
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
                    if not btn.is_displayed():
                        continue
                    if btn.get_attribute("disabled"):
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
    """Npay 이미지 버튼."""
    xpaths = [
        '//button[.//img[@alt="Npay"]]',
        '//img[@alt="Npay"]/parent::button',
        '//button[.//img[contains(@src,"cgv") and contains(@src,"Npay") or contains(@alt,"Npay")]]',
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    btn = el if el.tag_name == "button" else el
                    if not btn.is_displayed():
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", btn)
                    log("Npay 버튼 클릭")
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("Npay 버튼 실패")
    return False


def click_agree_all_terms(timeout: float = 12.0) -> bool:
    """전체 약관 동의하기 (label chkAll)."""
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
                    time.sleep(0.3)
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    log("전체 약관 동의 실패")
    return False


def click_amount_pay_button(timeout: float = 12.0) -> bool:
    """{금액}원 결제하기 (btn-font2way 등)."""
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
                    if not btn.is_displayed():
                        continue
                    if btn.get_attribute("disabled"):
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
    """네이버페이 '동의하고 결제하기'."""
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
                    if not btn.is_displayed():
                        continue
                    if btn.get_attribute("disabled"):
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
    """6자리 보안키패드 등장 대기. 입력은 사람이 함."""
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

def auto_select_and_pay(targets, treat_as_preferential: bool = False) -> tuple:
    """
    targets: ['A17+A18'] 형식
    treat_as_preferential=True → 클릭마다 확인 팝업 대기 (장애인/우대 테스트용)
    """
    if not targets:
        return False, "타겟 없음"

    pair = targets[0]
    parts = [p.strip() for p in pair.split("+")]
    first = parts[0]
    second = parts[1] if len(parts) > 1 else None

    # 1) 첫 좌석
    if not click_seat_pair(first):
        return False, f"{first} 클릭 실패 (매진/disabled/DOM)"

    if treat_as_preferential:
        dismiss_preferential_popup()
    else:
        # 일반석: 팝업 없을 수 있음. 짧게만 시도
        dismiss_preferential_popup(timeout=1.0)

    time.sleep(0.4)

    # 2) 장애인석은 두 번째 칸도 눌러야 할 수 있음
    if second and treat_as_preferential:
        if click_seat_pair(second):
            dismiss_preferential_popup()
            time.sleep(0.3)
        else:
            log(f"{second} 추가 클릭 실패 — 이미 짝 선택됐을 수 있음")

    # 3) 선택완료
    if not click_select_complete():
        return False, f"{pair} 선택 후 '선택완료' 실패"

    time.sleep(0.8)

    # 4) 결제하기
    if not click_pay_button():
        return False, f"{pair} 선택완료 OK, '결제하기' 실패 (수동)"

    time.sleep(1.0)

    # --- 결제 화면 이후 ---
    if not click_second_pay_button():
        return False, "1차 결제하기 OK, 2차 결제하기 실패 (수동)"

    time.sleep(1.2)

    if not click_npay_button():
        return False, "2차 결제 OK, Npay 클릭 실패 (수동)"

    time.sleep(1.0)

    if not click_agree_all_terms():
        return False, "Npay OK, 전체약관 동의 실패 (수동)"

    time.sleep(0.5)

    if not click_amount_pay_button():
        return False, "약관 OK, 금액 결제하기 실패 (수동)"

    time.sleep(1.0)

    if not click_npay_agree_and_pay():
        return False, "금액 결제 OK, 동의하고 결제하기 실패 (수동)"

    # 6자리: 자동 입력 안 함
    if wait_secure_keyboard():
        try:
            notify_phone(
                "CGV 6자리 입력 필요",
                f"{pair}까지 자동 완료. 보안키패드에서 비밀번호 6자리를 직접 입력하세요.",
            )
        except Exception:
            pass
        bring_browser_front()
        return True, f"{pair} → 결제 직전 OK. 6자리는 직접 입력"
    return True, f"{pair} → 동의하고 결제까지 OK (키패드 미감지, 화면 확인)"

def test_full_flow_like_watch():
    """
    감시 중 타겟 2연석 발견 시와 같은 행위 전체 테스트.
    인원 2 선택 → 선택 → (좌석맵) → A17+A18 → 확인 팝업
    → 선택완료 → 결제하기
    """
    def task():
        if driver is None:
            log("브라우저 없음 — 로그인·예매창(회차)까지 먼저 여세요.")
            return

        test_targets = ["A17+A18"]  # 우대/장애인 2연석 테스트
        log(f"[테스트] 감시 발견 후 전체 흐름 시작 — {test_targets}")

        # ① 인원 2명 + 선택 → 좌석 맵 (기존 함수 재사용)
        if not open_seat_screen():
            msg = "인원 선택/좌석 화면 진입 실패"
            log(f"[테스트] 실패 — {msg}")
            try:
                notify_phone("CGV 테스트 실패", msg)
            except Exception:
                pass
            return

        time.sleep(1.0)  # 좌석 맵 렌더 대기

        # ② 좌석 클릭 ~ 결제하기 (우대석 팝업 포함)
        ok, detail = auto_select_and_pay(
            test_targets,
            treat_as_preferential=True,
        )
        log(f"[테스트] {'성공' if ok else '실패'} — {detail}")
        try:
            notify_phone("CGV 테스트", detail)
        except Exception:
            pass

    threading.Thread(target=task, daemon=True).start()
def click_seat_pair(pair_label: str) -> bool:
    """
    'A17+A18' → 첫 칸 클릭.
    ElementClickInterceptedException 대비: JS 클릭 사용.
    """
    first = pair_label.split("+")[0].strip()
    xpath = (
        f'//button[contains(@class,"seatMap_seatNumber") '
        f'and not(@disabled) '
        f'and not(contains(@class,"Disabled"))]'
        f'//span[normalize-space()="{first}"]/parent::button'
    )
    try:
        el = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
        # 화면 중앙으로 스크롤
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", el
        )
        time.sleep(0.35)

        # 가리는 오버레이/헤더가 있으면 pointer-events 잠시 끄기 (선택)
        driver.execute_script("""
            var sels = [
              '[class*="header"]', '[class*="Header"]',
              '[class*="dim"]', '[class*="Dim"]',
              '[class*="loading"]', '[class*="Loading"]',
              '[class*="toast"]', '[class*="modal"]'
            ];
            sels.forEach(function(s){
              document.querySelectorAll(s).forEach(function(n){
                if (n && n.style) n.setAttribute('data-cgv-pe', n.style.pointerEvents || '');
              });
            });
        """)

        # 네이티브 click 대신 JS click (Intercepted 회피)
        driver.execute_script("arguments[0].click();", el)
        log(f"좌석 클릭(JS): {first} ({pair_label})")
        time.sleep(0.3)
        return True
    except Exception as e:
        log(f"좌석 클릭 실패 ({first}): {type(e).__name__}: {e}")
        # 디버그: 버튼이 아예 없는건지, disabled인지
        try:
            any_btn = driver.find_elements(
                By.XPATH,
                f'//button[contains(@class,"seatMap_seatNumber")]'
                f'//span[normalize-space()="{first}"]/parent::button'
            )
            if not any_btn:
                log(f"  → DOM에 {first} 버튼 없음")
            else:
                b = any_btn[0]
                log(
                    f"  → 존재함 disabled={b.get_attribute('disabled')}, "
                    f"class={b.get_attribute('class')[:80]}"
                )
        except Exception:
            pass
        return False
    

def click_pay_button() -> bool:
    """
    하단 고정바(.botFix)의 '결제하기' 버튼.
    금액(34,000원 등)은 회차마다 다르므로 텍스트로만 찾음.
    """
    # 여러 후보 (DOM 변형 대비)
    xpaths = [
        '//div[contains(@class,"botFix")]//button[contains(., "결제하기")]',
        '//div[contains(@class,"double-btn-wrap")]//button[contains(., "결제하기")]',
        '//button[contains(@class,"fill-main") and contains(., "결제하기")]',
    ]
    end = time.time() + 15
    last_err = ""
    while time.time() < end:
        for xp in xpaths:
            try:
                btns = driver.find_elements(By.XPATH, xp)
                for btn in btns:
                    if not btn.is_displayed():
                        continue
                    if btn.get_attribute("disabled"):
                        continue
                    # 하단 바로 스크롤 + JS 클릭
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.25)
                    driver.execute_script("arguments[0].click();", btn)
                    log("결제하기 클릭(JS)")
                    return True
            except Exception as e:
                last_err = type(e).__name__
        time.sleep(0.3)

    log(f"결제하기 클릭 실패: TimeoutException ({last_err})")
    # 디버그: 화면에 버튼 텍스트가 보이는지
    try:
        raw = driver.find_elements(
            By.XPATH, '//button[contains(@class,"fill-main")]'
        )
        for b in raw:
            t = (b.text or "").replace("\n", " ").strip()
            if t:
                log(f"  fill-main 버튼 발견: '{t}' disabled={b.get_attribute('disabled')}")
    except Exception:
        pass
    return False
    
def select_people():
    """버튼으로 직접 부를 때 (감시 전 미리 눌러두는 용도)."""
    if driver is None:
        messagebox.showwarning("알림", "먼저 예매창으로 이동해주세요.")
        return
    threading.Thread(target=open_seat_screen, daemon=True).start()


def click_refresh():
    """관람인원 옆 새로고침 아이콘을 누른다."""
    candidates = [
        '//button[@aria-label="새로고침"]',
        '//button[normalize-space(text())="새로고침"]',
        '//*[contains(text(),"관람인원")]/following::button[1]',
    ]
    global refresh_xpath

    # 한 번 찾은 셀렉터는 기억해두고 바로 쓴다 (매번 후보를 훑으면 사이클마다 수 초를 버린다)
    if refresh_xpath:
        try:
            driver.find_element(By.XPATH, refresh_xpath).click()
            return True
        except Exception:
            refresh_xpath = None  # 화면이 바뀐 듯하니 다시 탐색

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
    """새로고침 후 새 응답이 올 때까지만 기다린다 (고정 sleep 대신 폴링)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = driver.execute_script(READ_JS)
        if rows is not None:
            return rows
        time.sleep(0.1)
    return None


def find_adjacent_pairs(seats):
    """좌표가 붙어있는 2연석을 찾는다 (좌석 폭 2 단위)."""
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
    """E~L열 8~37번, Light존 제외 — 이 범위만 관심 대상."""
    return (s["zone"] not in EXCLUDE_ZONES
            and s["row"].upper() in TARGET_ROWS
            and s["no"] is not None
            and SEAT_NO_MIN <= s["no"] <= SEAT_NO_MAX)


def row_index(row_name: str) -> int:
    """좌석 열 이름을 앞에서 몇 번째 줄인지로 바꾼다 (A=1, B=2 ...)."""
    if len(row_name) == 1 and row_name.isalpha():
        return ord(row_name.upper()) - ord("A") + 1
    return 0


def build_alert_text(seats, pairs, attempt) -> str:
    """폰 알림에 들어갈 상세 내용."""
    paired = set()
    for p in pairs:
        a, b = p.split("+")
        paired.update((a, b))

    # 2연석을 먼저 보여준다 (이게 목표), 단독석은 참고용으로 뒤에
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
        f"타겟 E~L열 {SEAT_NO_MIN}~{SEAT_NO_MAX}번\n"
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
    """웹모드에서만 뜨는 안내 팝업 (소리 없음, 한 번만)."""
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


def announce(seats, pairs, attempt):
    text = build_alert_text(seats, pairs, attempt)
    global popup_shown, last_push

    if mode_var.get() == "web":
        monitoring.clear()
        log("웹모드: 자동 선택·결제 진입 시도")

        if not open_seat_screen():
            notify_phone("CGV 실패", "좌석 화면 진입 실패\n" + text)
            bring_browser_front()
            return

        targets = pairs if REQUIRE_PAIR else seats
        ok, detail = auto_select_and_pay(targets)

        notify_phone(
            "CGV 좌석 확보 시도" if ok else "CGV 수동 필요",
            text + "\n\n" + detail,
        )
        bring_browser_front()
        if not popup_shown:
            popup_shown = True
            show_popup(len(seats), pairs)
        last_push = time.time()
    else:
        # 외출모드: 알림만 (자동 클릭 안 함)
        notify_phone("CGV seat open!", text)
        last_push = time.time()


def monitor_loop():
    attempt = 0
    fails = 0
    last_beat = time.time()
    last_save = time.time()

    while monitoring.is_set():
        attempt += 1
        try:
            # 후킹 주입 + 직전 응답 비우기 (새로 온 것만 읽기 위해)
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

                # 사이클마다 한 줄만 남긴다 (시각은 반복돼서 빼고, 회차로 구분)
                log(f"[{attempt}회] 전체 {len(rows)}석 · Light존 {light} · "
                    f"타겟 {len(seats)}석 · 2연석 {len(pairs)}쌍 · {took:.1f}s",
                    stamp=False)

                hit = pairs if REQUIRE_PAIR else seats
                if hit:
                    log(f"★ 좌석: {', '.join(s['seat'] for s in seats)}")
                    log(f"★ 2연석: {', '.join(pairs) if pairs else '없음'}")
                    set_status(f"★ 2연석 {len(pairs)}쌍 — 예매하세요!")
                    announce(seats, pairs, attempt)
                    if not monitoring.is_set():
                        break  # 웹모드: 화면 상태를 지키려고 감시를 멈춘다
                else:
                    set_status(f"빈자리 없음 — 감시 중 ({attempt}회차)")

            fails = 0  # 여기까지 왔으면 정상 사이클
        except Exception as e:
            fails += 1
            log(f"  → 조회 실패({fails}회 연속): {type(e).__name__}: {e}")
            set_status(f"조회 실패 {fails}회 — 계속 시도합니다")

            # 브라우저가 죽었거나 세션이 끊긴 경우: 조용히 멈추지 말고 폰으로 알린다
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

        # 세션 저장은 조용히 (알림 없음) — 강제종료/크래시 대비
        if time.time() - last_save >= COOKIE_SAVE_MIN * 60:
            last_save = time.time()
            save_cookies()

        # "정상 작동 중" 알림은 기본적으로 끈다. 상태가 궁금하면 폰에서 /status.
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
    """감시 시작. remote=True면 GUI 팝업 없이 결과 문자열만 돌려준다."""
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

    # 시작 알림은 보내지 않는다 (본인이 누른 동작이라 놀랄 일만 늘어난다)
    threading.Thread(target=monitor_loop, daemon=True).start()
    return f"감시를 시작했습니다 ({get_interval()}초 간격)."


def stop_monitor(remote: bool = False) -> str:
    was_on = monitoring.is_set()
    if was_on:
        log("원격 명령으로 감시 중지" if remote else "사용자가 감시를 중지했습니다.")
    monitoring.clear()
    set_status("감시 중지됨.")
    return "감시를 중지했습니다." if was_on else "감시 중이 아니었습니다."




def send_browser_shot() -> str:
    """현재 브라우저 화면을 폰으로 보낸다 (원격 진단용)."""
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
        return ""  # 사진 자체가 응답
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
        err = send_browser_shot()
        return err  # 성공하면 빈 문자열
    if cmd in ("mode", "m"):
        want = arg.strip().lower()
        if want in ("web", "웹"):
            root.after(0, lambda: mode_var.set("web"))
            return "웹모드로 바꿨습니다 (팝업 O, 폰 알림 1회)."
        if want in ("away", "외출"):
            root.after(0, lambda: mode_var.set("away"))
            return "외출모드로 바꿨습니다 (팝업 X, 폰 알림 반복)."
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
    """폰에서 보낸 명령을 받아 처리한다 (본인 chat_id만 허용)."""
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
                # 본인 외 다른 사람이 봇을 찾아내도 조작하지 못하게 막는다
                if chat_id != str(TG_CHAT_ID):
                    continue
                log(f"원격 명령 수신: {text}")
                reply = handle_command(text)
                if reply:
                    notify_telegram(reply)
        except Exception:
            time.sleep(5)  # 네트워크 문제면 잠시 쉬었다 재시도


def notify_telegram(text: str):
    """텔레그램 봇으로 메시지를 보낸다."""
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
    """봇에게 아무 메시지나 보낸 뒤 이 버튼을 누르면 chat_id를 찾아준다."""
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
    """ntfy.sh로 폰에 푸시를 보낸다."""
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                # HTTP 헤더는 ASCII만 안전하므로 제목은 영문, 한글 내용은 본문에 담는다
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
    """설정된 모든 채널로 알림. 실패해도 감시는 계속되게 조용히 넘어간다."""
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
    """GUI 입력칸의 감시 주기(초). 잘못 입력하면 기본값으로."""
    try:
        return max(1, int(interval_var.get()))
    except (ValueError, tk.TclError):
        return CHECK_INTERVAL_SEC


def set_status(text: str):
    status_var.set(text)


def log(text: str, stamp: bool = True):
    """하단 로그창에 한 줄 남긴다 (UI 스레드에서 실행).

    매 사이클 반복되는 줄은 시각이 의미 없어서 stamp=False로 회차만 남긴다.
    """
    line = (f"[{time.strftime('%H:%M:%S')}] {text}\n" if stamp else f"{text}\n")

    recent_log.append(line.rstrip())
    del recent_log[:-200]  # 원격 조회용으로 최근 것만 들고 있는다

    def append():
        log_box.configure(state="normal")
        log_box.insert("end", line)
        log_box.see("end")
        log_box.configure(state="disabled")

    root.after(0, append)


root = tk.Tk()
root.title("CGV 예매 보조")
root.geometry("520x760")

status_var = tk.StringVar(value="대기 중")
interval_var = tk.StringVar(value=str(CHECK_INTERVAL_SEC))
mode_var = tk.StringVar(value="away")  # web=PC 앞, away=외출
monitoring = threading.Event()

tk.Label(root, textvariable=status_var, wraplength=480, justify="left").pack(pady=10, padx=10)

btn_prepare = tk.Button(root, text="로그인 준비", width=20, command=prepare_login)
btn_prepare.pack(pady=5)

btn_book = tk.Button(root, text=f"예매창 이동 ({SHOW_TIME})", width=20, command=goto_booking)
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
    text="좌석맵 자동선택 테스트 (A17)",
    width=28,
    command=test_full_flow_like_watch,
)
btn_test_auto.pack(pady=5)

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
    """창을 닫을 때 세션 쿠키를 저장하고 브라우저를 정상 종료한다."""
    monitoring.clear()
    save_cookies()
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)

# 폰에서 명령을 받는 원격 제어 스레드
if TG_TOKEN and TG_CHAT_ID:
    threading.Thread(target=telegram_command_loop, daemon=True).start()
    log("원격 제어 활성화 — 폰에서 /help 를 보내보세요.")
else:
    log("원격 제어 꺼짐 (CGV_TG_TOKEN / CGV_TG_CHAT_ID 미설정)")

root.mainloop()
