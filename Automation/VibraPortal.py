"""Passos de automação do Canal de Negócios Vibra (cn.vibraenergia.com.br) via Selenium:
login (SSO Keycloak) e Programação FOB > Cadastrar.

O menu é Angular Material, mas a tela de Programação FOB é JSF (select2 +
jsf.ajax) - por isso as esperas por AJAX e o tratamento de elementos stale.
"""

import os
import re
import time
import unicodedata
from typing import Dict, List, Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from Utils.TokenApiClient import fetch_token, prepare_token

START_URL = "https://cn.vibraenergia.com.br/consultar-pedidos/"

DEFAULT_TIMEOUT_ENV = "VIBRA_TIMEOUT_S"
SLOW_SCREEN_TIMEOUT_ENV = "VIBRA_SLOW_SCREEN_TIMEOUT_S"


def _env_seconds(var_name: str, default: int) -> int:
    # Valor inválido é barrado no InitModule com erro claro; aqui só não pode quebrar o import.
    raw_value = os.environ.get(var_name, "").strip()
    return int(raw_value) if raw_value.isdigit() and int(raw_value) > 0 else default


# Os timeouts são usados como valor padrão de parâmetros, então precisam existir no import -
# que acontece antes do load_dotenv() do main.py. Não sobrescreve variáveis já definidas (Railway).
load_dotenv()
DEFAULT_TIMEOUT = _env_seconds(DEFAULT_TIMEOUT_ENV, 30)
# A tela após "Pesquisar" (e as seguintes) às vezes demora bastante.
SLOW_SCREEN_TIMEOUT = _env_seconds(SLOW_SCREEN_TIMEOUT_ENV, 600)
MFA_CODE_FILE = "mfa_code.txt"

# Menu lateral (Angular): recolhido mostra só ícones; os títulos (span.titulo) só
# entram no DOM quando o menu expande com o mouse em cima (hover).
MENU_BUTTON_SELECTOR = "app-menu-item"
MENU_NAV_ATTEMPTS = 3
MENU_ITEM_XPATH = (
    "//app-menu-item//div[contains(@class,'main-item')]"
    "[.//span[contains(@class,'titulo')][normalize-space()='{title}']]"
)
# Estrutura do item "Cadastrar" não confirmada via DOM - aceita item do menu ou link com o texto.
MENU_LEAF_XPATH = (
    "//app-menu-item//*[self::a or self::span or self::div or self::button]"
    "[normalize-space()='{title}'][not(*[normalize-space()='{title}'])]"
)
PLATE_INPUT_XPATH = "//input[@value='Buscar']/ancestor::div[1]//input[@type='text']"


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
def login(
    driver: webdriver.Chrome,
    username: str,
    password: str,
    mfa_wait_seconds: int,
    token_api_url: Optional[str] = None,
    token_api_token: Optional[str] = None,
    token_timeout_seconds: int = 120,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """Abre o portal (redireciona pro SSO), autentica e espera o menu carregar.
    Se o SSO pedir o código de 2FA (enviado por e-mail), busca o código na Vibra
    Token API (projeto Bindi-Email) quando token_api_url estiver definido. Sem a
    API, ou se o código não chegar, aguarda até mfa_wait_seconds para o usuário
    digitá-lo na janela do Chrome ou gravá-lo em MFA_CODE_FILE."""
    print("[INFO] Acessando o Canal de Negócios Vibra...")
    driver.get(START_URL)

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, MENU_BUTTON_SELECTOR) or d.find_elements(By.ID, "username")
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Nem a tela de login nem o menu do portal apareceram em {timeout}s. {_page_summary(driver)}"
        ) from exc

    if driver.find_elements(By.CSS_SELECTOR, MENU_BUTTON_SELECTOR):
        print("[OK] Sessão do SSO reaproveitada - login não foi necessário.")
        return

    # O ticket precisa ser emitido antes de enviar a senha, senão o e-mail do
    # código pode chegar antes e ser descartado como antigo.
    ticket = prepare_token(token_api_url, token_api_token) if token_api_url else None

    username_field = driver.find_element(By.ID, "username")
    username_field.clear()
    username_field.send_keys(username)
    password_field = driver.find_element(By.ID, "password")
    password_field.clear()
    password_field.send_keys(password)
    driver.find_element(By.ID, "loginBt").click()

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, MENU_BUTTON_SELECTOR) or _is_mfa_screen(d)
        )
    except TimeoutException as exc:
        raise RuntimeError(
            "Login não confirmado: o menu do portal não carregou. "
            f"Verifique usuário/senha. {_page_summary(driver)}"
        ) from exc

    if _is_mfa_screen(driver):
        if token_api_url and _submit_mfa_code_from_api(
            driver, token_api_url, token_api_token, ticket, token_timeout_seconds, timeout
        ):
            print("[OK] Login realizado com sucesso (código de 2FA lido do e-mail).")
            return
        _wait_manual_mfa_code(driver, mfa_wait_seconds)

    print("[OK] Login realizado com sucesso.")


def _submit_mfa_code_from_api(
    driver: webdriver.Chrome,
    token_api_url: str,
    token_api_token: str,
    ticket: Optional[int],
    token_timeout_seconds: int,
    timeout: int,
) -> bool:
    """Busca o código na Vibra Token API e o envia. Devolve False (para cair no
    modo manual) se o código não chegar ou não for aceito."""
    print(f"[INFO] Autenticação em 2 fatores: aguardando o código na Vibra Token API (até {token_timeout_seconds}s)...")
    try:
        code = fetch_token(token_api_url, token_api_token, ticket, token_timeout_seconds)
    except RuntimeError as exc:
        print(f"[AVISO] {exc} Passando para o modo manual.")
        return False

    print(f"[OK] Código de 2FA recebido do e-mail: {code}")
    if not _submit_mfa_code(driver, code):
        return False
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.find_elements(By.CSS_SELECTOR, MENU_BUTTON_SELECTOR))
    except TimeoutException:
        print(f"[AVISO] Código de 2FA {code} não foi aceito (menu não carregou). Passando para o modo manual.")
        return False
    return True


def _wait_manual_mfa_code(driver: webdriver.Chrome, mfa_wait_seconds: int) -> None:
    print(
        f"[AVISO] Autenticação em 2 fatores: digite na janela do Chrome o código enviado por "
        f"e-mail (ou grave-o em {MFA_CODE_FILE}). Aguardando até {mfa_wait_seconds}s..."
    )
    try:
        WebDriverWait(driver, mfa_wait_seconds, poll_frequency=2).until(
            lambda d: _try_mfa_code_file(d) or d.find_elements(By.CSS_SELECTOR, MENU_BUTTON_SELECTOR)
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Código de 2FA não informado (ou inválido) em {mfa_wait_seconds}s.") from exc


def _try_mfa_code_file(driver: webdriver.Chrome) -> bool:
    """Alternativa a digitar na janela: se MFA_CODE_FILE existir, digita o
    código dele na tela de 2FA e apaga o arquivo. Sempre devolve False
    (quem encerra a espera é o menu do portal carregando)."""
    if not os.path.isfile(MFA_CODE_FILE):
        return False
    with open(MFA_CODE_FILE, encoding="utf-8") as file:
        code = file.read().strip()
    os.remove(MFA_CODE_FILE)
    if code and _is_mfa_screen(driver) and _submit_mfa_code(driver, code):
        print(f"[INFO] Código de 2FA lido de {MFA_CODE_FILE} e enviado.")
    return False


def _submit_mfa_code(driver: webdriver.Chrome, code: str) -> bool:
    # Seletor do campo de código não confirmado via DOM - usa o primeiro input visível da tela.
    inputs = [
        field for field in driver.find_elements(By.CSS_SELECTOR, "form input:not([type=hidden])")
        if field.is_displayed() and field.is_enabled()
    ]
    if not inputs:
        print("[ERRO] Campo do código de 2FA não encontrado na tela.")
        return False
    inputs[0].clear()
    inputs[0].send_keys(code, Keys.ENTER)
    return True


def _page_summary(driver: webdriver.Chrome) -> str:
    """URL, título e início do texto da página - no Railway o screenshot fica
    preso no container, então o log precisa dizer o que apareceu na tela."""
    try:
        title = driver.title.strip()
        body_text = " ".join(driver.find_element(By.TAG_NAME, "body").text.split())
    except Exception as exc:  # noqa: BLE001 - resumo é diagnóstico, não pode esconder o erro original
        return f"URL atual: {driver.current_url} (não foi possível ler a página: {type(exc).__name__})"

    summary = f"URL atual: {driver.current_url} | título: '{title or '-'}' | texto: '{body_text[:300] or '(página em branco)'}'"
    if "access denied" in f"{title} {body_text}".lower():
        summary = "A Vibra recusou o acesso deste servidor (Access Denied). " + summary
    return summary


def _is_mfa_screen(driver: webdriver.Chrome) -> bool:
    return "Autenticação em 2 fatores" in driver.find_element(By.TAG_NAME, "body").text


# --------------------------------------------------------------------------
# Programação FOB > Cadastrar
# --------------------------------------------------------------------------
def open_fob_registration(driver: webdriver.Chrome, main_handle: str) -> None:
    """Parte sempre da tela inicial (fecha abas de itens anteriores) e chega à
    tela de Compartimentação: menu > Programação > Programação FOB > Cadastrar
    > Pesquisar."""
    for handle in driver.window_handles:
        if handle != main_handle:
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(main_handle)
    driver.get(START_URL)

    print("[INFO] Menu > Programação > Programação FOB > Cadastrar")
    # O menu recolhe sozinho se o hover "escapar" (animação, re-render do Angular):
    # cada passo confere o menu e a navegação inteira é refeita se ele fechar no meio.
    for attempt in range(1, MENU_NAV_ATTEMPTS + 1):
        try:
            _expand_side_menu(driver)
            _open_menu_group(driver, "Programação", next_title="Programação FOB")
            _open_menu_group(driver, "Programação FOB", next_title="Cadastrar")
            _click_menu_entry(driver, MENU_LEAF_XPATH.format(title="Cadastrar"), "Cadastrar")
            break
        except RuntimeError as exc:
            if attempt == MENU_NAV_ATTEMPTS:
                raise RuntimeError(f"Navegação no menu lateral falhou {MENU_NAV_ATTEMPTS}x: {exc}") from exc
            print(f"[AVISO] Menu lateral: {exc} Tentando de novo ({attempt + 1}/{MENU_NAV_ATTEMPTS})...")
            time.sleep(1)

    # O link pode abrir em outra aba
    time.sleep(1)
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])

    print("[INFO] Pesquisa de Remessas > Pesquisar")
    try:
        WebDriverWait(driver, SLOW_SCREEN_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "forumlarioPesquisa:comboBase"))
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Tela 'Pesquisa de Remessas' não carregou em {SLOW_SCREEN_TIMEOUT}s. URL atual: {driver.current_url}"
        ) from exc
    _wait_page_ready(driver)

    # Há 2 botões "Pesquisar" (um oculto num modal); pega o visível
    search_button = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
        lambda d: next(
            (b for b in d.find_elements(By.CSS_SELECTOR, "input[type='submit'][value='Pesquisar']")
             if b.is_displayed()),
            False,
        )
    )
    search_button.click()

    print("[INFO] Aguardando a tela de Compartimentação (pode demorar)...")
    try:
        WebDriverWait(driver, SLOW_SCREEN_TIMEOUT, poll_frequency=1).until(
            EC.presence_of_element_located((By.ID, "comboPool"))
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Tela de Compartimentação não carregou em {SLOW_SCREEN_TIMEOUT}s após 'Pesquisar'."
        ) from exc
    _wait_page_ready(driver)
    _install_ajax_monitor(driver)


def fill_registration(driver: webdriver.Chrome, pool: str, plate: str, carrier: str) -> Dict[str, str]:
    """Preenche Pool, Placa (+ Buscar) e Transportador na tela de Compartimentação.
    Devolve o texto das opções efetivamente selecionadas nos combos."""
    print(f"[INFO] Pool: {pool}")
    selected_pool = _select_select2(driver, "comboPool", pool)
    _wait_page_ready(driver)

    print(f"[INFO] Placa: {plate}")
    _fill_plate(driver, plate)

    print("[INFO] Buscar")
    _click(driver, By.XPATH, "//input[@type='submit' and @value='Buscar']")
    _wait_page_ready(driver)

    print(f"[INFO] Transportador: {carrier}")
    try:
        WebDriverWait(driver, SLOW_SCREEN_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "cmbTranspp"))
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Combo de transportador não apareceu após buscar a placa {plate} - verifique se a placa é válida."
        ) from exc
    selected_carrier = _select_select2(driver, "cmbTranspp", carrier)
    _wait_page_ready(driver)

    return {"pool": selected_pool, "carrier": selected_carrier}


# Linhas das tabelas da Compartimentação (JSF): "tblRemessas:N:row" e "tblVeiculo:N:row".
# Há cópias para celular (visible-xs, ids "...xs") - por isso só as linhas visíveis.
SHIPMENT_ROWS_XPATH = (
    "//tr[starts-with(@id,'tblRemessas:') and substring(@id, string-length(@id) - 3) = ':row']"
)
VEHICLE_ROWS_XPATH = (
    "//tr[starts-with(@id,'tblVeiculo:') and substring(@id, string-length(@id) - 3) = ':row']"
)
# O title na Vibra termina com ponto ("Carregar neste compartimento.") - por isso contains.
# "Todos (completar)" não tem esse title.
LOAD_COMPARTMENT_XPATH = ".//a[contains(@title,'Carregar neste compartimento')]"
CONTINUE_BUTTON_XPATH = "//a[contains(@class,'btn-primary')][normalize-space()='Continuar']"


def load_compartments(driver: webdriver.Chrome, orders: List[dict]) -> List[str]:
    """Carrega cada remessa no compartimento definido na programação do portal e
    clica em Continuar. Na Vibra cada compartimento é carregado cheio, então o
    volume alocado é a própria capacidade (igual ao portal Bindi Log).
    Devolve um resumo por compartimento ("C5 0265807114 ETANOL ... 3 m³")."""
    allocations = [
        (order["distributorOrderNumber"], item["productName"], compartment["number"], compartment["capacityM3"])
        for order in orders
        for item in order.get("items", [])
        for compartment in item.get("compartments", [])
    ]
    if not allocations:
        raise RuntimeError("A programação não tem nenhum compartimento alocado no portal - nada a carregar.")

    _install_ajax_monitor(driver)
    summary: List[str] = []
    for order_number, product_name, compartment_number, capacity_m3 in sorted(allocations, key=lambda a: a[2]):
        print(f"[INFO] Compartimento {compartment_number}: {product_name} {capacity_m3:g} m³ (ordem {order_number})")
        vehicle_row = _find_vehicle_row(driver, compartment_number)
        if _same_order_number(_vehicle_row_order(vehicle_row), order_number):
            print(f"[INFO] Compartimento {compartment_number} já carregado com a ordem {order_number} - mantido.")
            summary.append(f"C{compartment_number} {order_number} (já carregado)")
            continue
        _check_vehicle_row_free(vehicle_row, compartment_number, capacity_m3)

        shipment_row, shipment_product = _find_shipment_row(driver, order_number, product_name)
        _load_shipment_into_compartment(driver, shipment_row, compartment_number)

        loaded_order = _vehicle_row_order(_find_vehicle_row(driver, compartment_number))
        if not _same_order_number(loaded_order, order_number):
            raise RuntimeError(
                f"Compartimento {compartment_number} não ficou com a ordem {order_number} após o clique "
                f"(ordem na tela: {loaded_order or 'vazio'})."
            )
        print(f"[OK] Compartimento {compartment_number} carregado: {shipment_product} (ordem {order_number}).")
        summary.append(f"C{compartment_number} {order_number} {shipment_product} {capacity_m3:g} m³")

    print("[INFO] Carregamento do Veículo > Continuar")
    try:
        continue_button = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: _first_visible(d, CONTINUE_BUTTON_XPATH)
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Botão 'Continuar' do Carregamento do Veículo não apareceu em {DEFAULT_TIMEOUT}s.") from exc
    _scroll_and_click(driver, continue_button)
    _wait_page_ready(driver)
    return summary


SCHEDULE_TIME_SELECT_ID = "combo_horarios"
FINALIZE_BUTTON_ID = "botao_cria1"
# Bootstrap Datepicker inline: td.day; "old"/"new" = dias de outro mês, "disabled" = indisponível.
CALENDAR_DAYS_SELECTOR = "#datepickid .datepicker-days"
CALENDAR_MONTH_NAMES = [
    "JANEIRO", "FEVEREIRO", "MARCO", "ABRIL", "MAIO", "JUNHO",
    "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO",
]
SEARCH_TIMES_XPATH = "//a[normalize-space()='Pesquisar Horários']"
ERROR_MESSAGE_SELECTORS = ".alert-danger, .ui-messages-error, .ui-message-error, #mensagens .erro"
MESSAGE_SELECTORS = "#mensagens, .alert, .ui-messages, .ui-growl-message"


def schedule_and_finalize(driver: webdriver.Chrome, load_date: str, load_time: str) -> str:
    """Tela "Programação de Caminhão - Agendamento": escolhe no combo de horários a
    opção com a data e a hora de carregamento da programação do portal
    (loadDate "AAAA-MM-DD", loadTime "HH:MM") e clica em Finalizar. Devolve a
    mensagem que a Vibra mostrar depois (ou "")."""
    year, month, day = load_date.split("-")
    wanted_text = f"{day}/{month}/{year} - {load_time}"
    print(f"[INFO] Agendamento: data e hora do carregamento {wanted_text}")

    try:
        WebDriverWait(driver, SLOW_SCREEN_TIMEOUT).until(EC.presence_of_element_located((By.ID, SCHEDULE_TIME_SELECT_ID)))
    except TimeoutException as exc:
        raise RuntimeError(
            f"Tela de Agendamento não carregou em {SLOW_SCREEN_TIMEOUT}s após 'Continuar'. URL atual: {driver.current_url}"
        ) from exc
    _wait_page_ready(driver)
    _install_ajax_monitor(driver)

    _select_calendar_day(driver, int(year), int(month), int(day), wanted_text)

    option = _schedule_option(driver, wanted_text)
    if not option:
        raise RuntimeError(
            f"Horário {wanted_text} não está disponível na Vibra. "
            f"Opções na tela: {', '.join(_schedule_option_texts(driver)) or 'nenhuma'}."
        )
    Select(driver.find_element(By.ID, SCHEDULE_TIME_SELECT_ID)).select_by_value(option.get_attribute("value"))
    _wait_page_ready(driver)

    selected_text = Select(driver.find_element(By.ID, SCHEDULE_TIME_SELECT_ID)).first_selected_option.text.strip()
    if selected_text != wanted_text:
        raise RuntimeError(f"Combo de horários ficou em '{selected_text}' em vez de '{wanted_text}'.")
    print(f"[OK] Horário selecionado: {selected_text}")

    print("[INFO] Agendamento > Finalizar")
    _click(driver, By.ID, FINALIZE_BUTTON_ID)
    _wait_page_ready(driver)

    errors = [el.text.strip() for el in driver.find_elements(By.CSS_SELECTOR, ERROR_MESSAGE_SELECTORS)
              if el.is_displayed() and el.text.strip()]
    if errors:
        raise RuntimeError(f"Vibra recusou a programação ao Finalizar: {' | '.join(errors)}")
    messages = [el.text.strip() for el in driver.find_elements(By.CSS_SELECTOR, MESSAGE_SELECTORS)
                if el.is_displayed() and el.text.strip()]
    message = " | ".join(dict.fromkeys(messages))
    print(f"[OK] Programação finalizada na Vibra. {message}".rstrip())
    return message


def _schedule_option(driver: webdriver.Chrome, wanted_text: str):
    for option in driver.find_elements(By.CSS_SELECTOR, f"#{SCHEDULE_TIME_SELECT_ID} option"):
        if option.text.strip() == wanted_text:
            return option
    return None


def _schedule_option_texts(driver: webdriver.Chrome) -> List[str]:
    return [o.text.strip() for o in driver.find_elements(By.CSS_SELECTOR, f"#{SCHEDULE_TIME_SELECT_ID} option")]


def _select_calendar_day(driver: webdriver.Chrome, year: int, month: int, day: int, wanted_text: str) -> None:
    """O combo de horários só lista os horários do dia marcado no calendário: navega
    até o mês da programação e clica no dia (sempre - mesmo que já pareça marcado)."""
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, CALENDAR_DAYS_SELECTOR))
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Calendário 'Data do Carregamento' não apareceu em {DEFAULT_TIMEOUT}s.") from exc

    for _ in range(24):
        shown_year, shown_month = _calendar_shown_month(driver)
        if (shown_year, shown_month) == (year, month):
            break
        arrow = "th.next" if (shown_year, shown_month) < (year, month) else "th.prev"
        _scroll_and_click(driver, driver.find_element(By.CSS_SELECTOR, f"{CALENDAR_DAYS_SELECTOR} {arrow}"))
        time.sleep(0.3)
    else:
        raise RuntimeError(f"Não foi possível navegar o calendário até {month:02d}/{year}.")

    day_cells = [
        cell for cell in driver.find_elements(By.CSS_SELECTOR, f"{CALENDAR_DAYS_SELECTOR} td.day")
        if cell.text.strip() == str(day) and not ({"old", "new"} & set((cell.get_attribute("class") or "").split()))
    ]
    if not day_cells:
        raise RuntimeError(f"Dia {day} não encontrado no calendário de {month:02d}/{year}.")
    if "disabled" in (day_cells[0].get_attribute("class") or "").split():
        raise RuntimeError(f"Dia {day:02d}/{month:02d}/{year} está bloqueado no calendário da Vibra (sem agendamento).")

    print(f"[INFO] Calendário: clicando no dia {day:02d}/{month:02d}/{year}")
    _scroll_and_click(driver, day_cells[0])
    _wait_page_ready(driver)

    # O combo é recarregado por AJAX depois do clique no dia; espera ele mostrar a data certa.
    date_prefix = wanted_text.split(" - ")[0]
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: any(text.startswith(date_prefix) for text in _schedule_option_texts(d))
        )
    except TimeoutException:
        search_links = [el for el in driver.find_elements(By.XPATH, SEARCH_TIMES_XPATH) if el.is_displayed()]
        if search_links:
            _scroll_and_click(driver, search_links[0])
            _wait_page_ready(driver)


def _calendar_shown_month(driver: webdriver.Chrome):
    headers = driver.find_elements(By.CSS_SELECTOR, f"{CALENDAR_DAYS_SELECTOR} th.datepicker-switch")
    if not headers:
        raise RuntimeError("Cabeçalho do mês (th.datepicker-switch) não encontrado no calendário da Vibra.")
    header = headers[0].text
    plain = unicodedata.normalize("NFKD", header).encode("ascii", "ignore").decode().upper().split()
    try:
        return int(plain[-1]), CALENDAR_MONTH_NAMES.index(plain[0]) + 1
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"Cabeçalho do calendário inesperado: '{header}'.") from exc


def _find_shipment_row(driver: webdriver.Chrome, order_number: str, product_name: str):
    """Linha da Lista de Remessas com a ordem do pedido e o produto. O nome do
    produto difere entre os sistemas ("Etanol Hidratado" x "ETANOL HIDRATADO
    COMBUSTIVEL"): casa quando todas as palavras do portal aparecem na Vibra."""
    rows = _visible_rows(driver, SHIPMENT_ROWS_XPATH)
    if not rows:
        raise RuntimeError("Lista de Remessas vazia ou não carregada na tela de Compartimentação.")

    entries = []
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) >= 5:
            entries.append((row, cells[1].text.strip(), cells[3].text.strip(), cells[4].text.strip()))
    same_order = [entry for entry in entries if _same_order_number(entry[3], order_number)]
    if not same_order:
        available = ", ".join(sorted({entry[3] for entry in entries}))
        raise RuntimeError(f"Ordem {order_number} não está na Lista de Remessas da Vibra (ordens na tela: {available}).")

    wanted_words = _product_words(product_name)
    matches = [entry for entry in same_order if wanted_words <= _product_words(entry[1])]
    if not matches:
        if len(same_order) > 1:
            raise RuntimeError(
                f"Ordem {order_number}: produto '{product_name}' não identificado entre as remessas "
                f"{', '.join(entry[1] for entry in same_order)}."
            )
        print(
            f"[AVISO] Produto '{product_name}' não bate com '{same_order[0][1]}' - "
            f"única remessa da ordem {order_number}, usando ela."
        )
        matches = same_order

    row, vibra_product, volume_text, _ = matches[0]
    print(f"[INFO] Remessa: {vibra_product} | volume disponível {volume_text} | ordem {order_number}")
    return row, vibra_product


def _load_shipment_into_compartment(driver: webdriver.Chrome, shipment_row, compartment_number: int) -> None:
    """Botão verde da remessa -> modal "Em qual compartimento deseja carregar esta
    remessa?" -> botão do compartimento (link com onclick do JSF)."""
    open_button = shipment_row.find_element(By.CSS_SELECTOR, "button[data-toggle='modal']")
    modal_id = (open_button.get_attribute("data-target") or "").lstrip("#")
    if not modal_id:
        raise RuntimeError("Botão de carregar a remessa sem data-target do modal de compartimentos.")
    _scroll_and_click(driver, open_button)

    try:
        modal = WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.visibility_of_element_located((By.ID, modal_id)))
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: any(link.is_displayed() for link in modal.find_elements(By.XPATH, LOAD_COMPARTMENT_XPATH))
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Modal de compartimentos ({modal_id}) não mostrou os botões de compartimento em {DEFAULT_TIMEOUT}s."
        ) from exc

    links_by_number = {}
    for link in modal.find_elements(By.XPATH, LOAD_COMPARTMENT_XPATH):
        # O ícone do caminhão vem de CSS: só os dígitos do texto contam.
        digits = re.sub(r"\D", "", link.get_attribute("textContent") or "")
        if digits:
            links_by_number[int(digits)] = link
    load_link = links_by_number.get(int(compartment_number))
    if load_link is None:
        raise RuntimeError(
            f"Compartimento {compartment_number} não está no modal da Vibra "
            f"(compartimentos disponíveis: {', '.join(map(str, sorted(links_by_number))) or 'nenhum'})."
        )

    _scroll_and_click(driver, load_link)
    _wait_page_ready(driver)
    # O JSF re-renderiza a tela; se o modal Bootstrap sobrar aberto, o fundo escuro bloqueia os próximos cliques.
    driver.execute_script(
        "if (window.jQuery) { jQuery('.modal.in').modal('hide'); }"
        "document.querySelectorAll('.modal-backdrop').forEach(function (b) { b.remove(); });"
        "document.body.classList.remove('modal-open');"
    )


def _find_vehicle_row(driver: webdriver.Chrome, compartment_number: int):
    def matching_row(d):
        for row in _visible_rows(d, VEHICLE_ROWS_XPATH):
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 6 and cells[1].text.strip() == str(compartment_number):
                return row
        return False

    try:
        return WebDriverWait(driver, DEFAULT_TIMEOUT).until(matching_row)
    except TimeoutException as exc:
        raise RuntimeError(
            f"Compartimento {compartment_number} não existe no Carregamento do Veículo da Vibra "
            "(o caminhão cadastrado lá tem outra compartimentação?)."
        ) from exc


def _vehicle_row_order(vehicle_row) -> str:
    order_text = vehicle_row.find_elements(By.TAG_NAME, "td")[5].text.strip()
    return "" if order_text in ("", "-") else order_text


def _check_vehicle_row_free(vehicle_row, compartment_number: int, capacity_m3: float) -> None:
    current_order = _vehicle_row_order(vehicle_row)
    if current_order:
        raise RuntimeError(f"Compartimento {compartment_number} já está carregado na Vibra com a ordem {current_order}.")

    capacity_select = vehicle_row.find_elements(By.TAG_NAME, "td")[2].find_elements(By.TAG_NAME, "select")
    if not capacity_select:
        return
    vibra_capacity_text = _selected_option_text(capacity_select[0])
    vibra_capacity = _parse_vibra_volume(vibra_capacity_text)
    if vibra_capacity is not None and abs(vibra_capacity - capacity_m3) > 0.001:
        raise RuntimeError(
            f"Compartimento {compartment_number}: capacidade na Vibra é {vibra_capacity_text} m³ e no portal "
            f"{capacity_m3:g} m³ - o volume carregado seria diferente do programado."
        )


def _selected_option_text(select_element) -> str:
    for option in select_element.find_elements(By.TAG_NAME, "option"):
        if option.is_selected():
            return option.text.strip()
    return ""


def _parse_vibra_volume(text: str) -> Optional[float]:
    # Vibra mostra m³ com 3 casas e vírgula decimal: "3,000" = 3 m³, "1.234,500" = 1234,5 m³.
    try:
        return float(text.strip().replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _same_order_number(vibra_text: str, order_number: str) -> bool:
    vibra_number = vibra_text.strip().lstrip("0")
    return bool(vibra_number) and vibra_number == str(order_number).strip().lstrip("0")


def _product_words(name: str) -> set:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    return set(re.findall(r"[A-Z0-9]+", plain))


def _visible_rows(driver: webdriver.Chrome, xpath: str) -> list:
    rows = []
    for row in driver.find_elements(By.XPATH, xpath):
        try:
            if row.is_displayed():
                rows.append(row)
        except StaleElementReferenceException:
            continue
    return rows


def _scroll_and_click(driver: webdriver.Chrome, element) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)


def _fill_plate(driver: webdriver.Chrome, plate: str) -> None:
    wait = WebDriverWait(driver, DEFAULT_TIMEOUT)
    for _ in range(5):  # o JSF pode re-renderizar o campo (stale)
        try:
            plate_input = wait.until(EC.element_to_be_clickable((By.XPATH, PLATE_INPUT_XPATH)))
            plate_input.clear()
            plate_input.send_keys(plate)
            time.sleep(0.5)
            if plate_input.get_attribute("value") == plate:
                return
        except StaleElementReferenceException:
            pass
        time.sleep(1)
    raise RuntimeError(f"Não foi possível preencher a placa {plate} (campo re-renderizado pelo JSF).")


# --------------------------------------------------------------------------
# Helpers JSF / select2
# --------------------------------------------------------------------------
def _install_ajax_monitor(driver: webdriver.Chrome) -> None:
    """Conta as requisições AJAX do JSF (jsf.ajax) para sabermos quando terminam."""
    driver.execute_script(
        """
        if (window.__ajaxMonInstalado) return;
        window.__ajaxPendentes = 0;
        if (window.jsf && jsf.ajax) {
            jsf.ajax.addOnEvent(function (d) {
                if (d.status === 'begin') window.__ajaxPendentes++;
                if (d.status === 'success') window.__ajaxPendentes = Math.max(0, window.__ajaxPendentes - 1);
            });
            jsf.ajax.addOnError(function () {
                window.__ajaxPendentes = Math.max(0, window.__ajaxPendentes - 1);
            });
        }
        window.__ajaxMonInstalado = true;
        """
    )


def _wait_page_ready(driver: webdriver.Chrome, timeout: int = SLOW_SCREEN_TIMEOUT) -> None:
    """Espera document.readyState, jQuery.active e o AJAX do JSF zerarem."""
    time.sleep(0.5)  # dá tempo da requisição começar
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.5).until(
            lambda d: d.execute_script(
                """
                if (document.readyState !== 'complete') return false;
                if (window.jQuery && jQuery.active > 0) return false;
                if (window.__ajaxPendentes && window.__ajaxPendentes > 0) return false;
                return true;
                """
            )
        )
    except TimeoutException as exc:
        raise RuntimeError(f"A página não terminou de carregar (AJAX pendente) em {timeout}s.") from exc


def _expand_side_menu(driver: webdriver.Chrome) -> None:
    """Estaciona o mouse sobre o primeiro ícone do menu lateral (fica dentro do menu
    mesmo depois de expandido) e espera os títulos aparecerem. Os cliques seguintes
    são via JS, sem mover o ponteiro - mover o mouse até o item é o que fazia o
    menu recolher no caminho."""
    title_xpath = MENU_ITEM_XPATH.format(title="Programação")
    try:
        icon = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, f"{MENU_BUTTON_SELECTOR} div.main-item"))
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Menu lateral não apareceu em {DEFAULT_TIMEOUT}s. URL atual: {driver.current_url}") from exc

    ActionChains(driver).move_to_element(icon).perform()
    if _wait_visible(driver, title_xpath, 5):
        return
    # Hover real às vezes não chega ao Angular (janela sem foco/headless): dispara os eventos via JS.
    driver.execute_script(
        "for (var el = arguments[0]; el; el = el.parentElement) {"
        "  ['mouseenter', 'mouseover'].forEach(function (t) {"
        "    el.dispatchEvent(new MouseEvent(t, {bubbles: t === 'mouseover', view: window}));"
        "  });"
        "}",
        icon,
    )
    if not _wait_visible(driver, title_xpath, 5):
        raise RuntimeError("Menu lateral não expandiu com o mouse em cima: título 'Programação' não apareceu.")


def _ensure_side_menu_expanded(driver: webdriver.Chrome) -> None:
    if not _wait_visible(driver, MENU_ITEM_XPATH.format(title="Programação"), 0):
        print("[AVISO] Menu lateral recolheu - expandindo de novo.")
        _expand_side_menu(driver)


def _open_menu_group(driver: webdriver.Chrome, title: str, next_title: str) -> None:
    """Abre o grupo `title` do menu lateral. Se o subitem `next_title` já estiver
    visível, o grupo já está aberto - clicar de novo o recolheria."""
    _ensure_side_menu_expanded(driver)
    if _menu_entry_visible(driver, next_title, 1):
        return
    _click_menu_entry(driver, MENU_ITEM_XPATH.format(title=title), title)
    # O subitem abre com animação (expandCollapse: altura 0 -> auto).
    if _menu_entry_visible(driver, next_title, DEFAULT_TIMEOUT // 3):
        return
    if not _wait_visible(driver, MENU_ITEM_XPATH.format(title="Programação"), 0):
        raise RuntimeError(f"o menu recolheu depois do clique em '{title}'.")
    raise RuntimeError(f"menu '{title}' foi clicado mas o subitem '{next_title}' não apareceu.")


def _menu_entry_visible(driver: webdriver.Chrome, title: str, timeout: float) -> bool:
    return _wait_visible(driver, MENU_ITEM_XPATH.format(title=title), timeout) or _wait_visible(
        driver, MENU_LEAF_XPATH.format(title=title), 0
    )


def _click_menu_entry(driver: webdriver.Chrome, xpath: str, title: str) -> None:
    _ensure_side_menu_expanded(driver)
    for _ in range(3):
        try:
            element = WebDriverWait(driver, 5, poll_frequency=0.25).until(lambda d: _first_visible(d, xpath))
        except TimeoutException as exc:
            raise RuntimeError(f"item '{title}' do menu lateral não ficou visível.") from exc
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'}); arguments[0].click();", element)
            return
        except StaleElementReferenceException:
            continue  # Angular re-renderizou o item entre achar e clicar
    raise RuntimeError(f"item '{title}' do menu lateral ficou sendo re-renderizado e não pôde ser clicado.")


def _wait_visible(driver: webdriver.Chrome, xpath: str, timeout: float) -> bool:
    if timeout <= 0:
        return bool(_first_visible(driver, xpath))
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.25).until(lambda d: _first_visible(d, xpath))
        return True
    except TimeoutException:
        return False


def _first_visible(driver: webdriver.Chrome, xpath: str):
    for element in driver.find_elements(By.XPATH, xpath):
        try:
            if element.is_displayed() and element.size["height"] > 0:
                return element
        except StaleElementReferenceException:
            continue
    return False


def _click(driver: webdriver.Chrome, by: str, selector: str, timeout: int = DEFAULT_TIMEOUT):
    try:
        element = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, selector)))
    except TimeoutException as exc:
        raise RuntimeError(f"Elemento não ficou clicável em {timeout}s: {selector}") from exc
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)
    return element


def _select_select2(driver: webdriver.Chrome, select_id: str, text: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Seleciona no <select id=select_id> (select2) a opção cujo texto contém `text`.

    Não usa o campo de pesquisa do select2: o JSF re-renderiza o combo enquanto se
    digita e o texto se perde (ficava só "q"). Em vez disso, define o valor via
    jQuery e dispara o 'change', que chama o onchange/AJAX do JSF normalmente.
    """
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                "var s=document.getElementById(arguments[0]);"
                "return !!(window.jQuery && s && s.options.length > 1);",
                select_id,
            )
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Combo #{select_id} não carregou as opções em {timeout}s.") from exc

    selected = driver.execute_script(
        """
        var s = document.getElementById(arguments[0]), t = arguments[1].toUpperCase();
        try { jQuery(s).select2('close'); } catch (e) {}
        for (var i = 0; i < s.options.length; i++) {
            if (s.options[i].text.toUpperCase().indexOf(t) >= 0) {
                jQuery(s).val(s.options[i].value).trigger('change');
                return s.options[i].text;
            }
        }
        return null;
        """,
        select_id,
        text,
    )
    if not selected:
        raise RuntimeError(f"Opção contendo '{text}' não encontrada em #{select_id}.")
    print(f"[OK]   -> selecionado: {selected}")
    return selected
