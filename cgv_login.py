import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

LOGIN_URL = "https://cgv.co.kr/mem/login?returnUrl=/mcv/mobileTicketList"


def build_driver() -> webdriver.Edge:
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    # selenium-manager가 설치된 Edge 버전에 맞는 driver를 자동으로 받아온다
    return webdriver.Edge(options=options)


def fill_login_form(driver: webdriver.Edge, user_id: str, password: str) -> None:
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 15)

    id_input = wait.until(EC.presence_of_element_located((By.ID, "loginInput1")))
    pw_input = driver.find_element(By.ID, "loginInput2")

    id_input.clear()
    id_input.send_keys(user_id)
    pw_input.clear()
    pw_input.send_keys(password)

    # 캡차(#loginInput3)와 로그인 버튼 클릭은 자동화하지 않는다.
    # 봇 탐지 우회에 해당하므로 사용자가 직접 캡차를 보고 입력 후 로그인 버튼을 눌러야 한다.
    print("아이디/비밀번호 입력 완료. 캡차를 직접 입력하고 로그인 버튼을 눌러주세요.")


def wait_for_login(driver: webdriver.Edge) -> None:
    wait = WebDriverWait(driver, 300)  # 캡차 입력 + 로그인까지 넉넉히 대기
    wait.until(EC.url_changes(LOGIN_URL))
    print("로그인 완료 감지됨.")


def main() -> None:
    user_id = os.environ.get("CGV_ID")
    password = os.environ.get("CGV_PW")
    if not user_id or not password:
        raise SystemExit("환경변수 CGV_ID, CGV_PW를 설정한 뒤 실행하세요.")

    driver = build_driver()
    fill_login_form(driver, user_id, password)
    wait_for_login(driver)

    print("다음 단계(예매창 이동)로 진행하려면 Enter를 누르세요.")
    input()


if __name__ == "__main__":
    main()
