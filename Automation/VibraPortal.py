"""Passos de automação do Canal de Negócios Vibra (cn.vibraenergia.com.br) via Selenium:
login (SSO Keycloak) e Programação FOB > Cadastrar.

O menu é Angular Material, mas a tela de Programação FOB é JSF (select2 +
jsf.ajax) - por isso as esperas por AJAX e o tratamento de elementos stale.
"""

import os
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

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
from urllib3.exceptions import ReadTimeoutError

from Utils.TokenApiClient import fetch_token, prepare_token

HOME_URL = "https://cn.vibraenergia.com.br/home/#/"

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
# Enquanto a tela lenta carrega, o chromedriver segura os comandos e o Selenium desiste
# da resposta em 120s (ReadTimeoutError) - o navegador segue carregando, então a espera continua.
SLOW_SCREEN_IGNORED_ERRORS = (ReadTimeoutError, TimeoutException)
MAX_LOGGED_COMBO_OPTIONS = 40

# Menu lateral (Angular): recolhido mostra só ícones; os títulos (span.titulo) só
# entram no DOM quando o menu expande com o mouse em cima (hover).
# Há dois layouts: o antigo (app-menu-item, expande com hover) e a "NOVA HOME"
# (home-acesso-rapido-lateral, expande clicando em mat-icon.icone-expandir).
MENU_SELECTOR = "app-menu-item, home-acesso-rapido-lateral"
LEGACY_MENU_ICON_SELECTOR = "app-menu-item div.main-item"
MENU_EXPAND_BUTTON_SELECTOR = "home-acesso-rapido-lateral mat-icon.icone-expandir"
MENU_NAV_ATTEMPTS = 3
MENU_ITEM_XPATH = (
    "//app-menu-item//div[contains(@class,'main-item')]"
    "[.//span[contains(@class,'titulo')][normalize-space()='{title}']]"
    " | //home-acesso-rapido-lateral//*[normalize-space(text())='{title}']"
)
# Estrutura do item "Cadastrar" não confirmada via DOM - aceita item do menu ou link com o texto.
MENU_LEAF_XPATH = (
    "//app-menu-item//*[self::a or self::span or self::div or self::button]"
    "[normalize-space()='{title}'][not(*[normalize-space()='{title}'])]"
    " | //home-acesso-rapido-lateral//*[normalize-space(text())='{title}']"
)
PLATE_INPUT_XPATH = "//input[@value='Buscar']/ancestor::div[1]//input[@type='text']"

# Popup "Informativos" que a home abre por cima de tudo.
POPUP_CLOSE_XPATH = "//app-informativo-lateral//i[contains(@class,'md-icons')][normalize-space()='close']"
POPUP_WAIT_SECONDS = 10

# Unidade (empresa do grupo) ativa no cabeçalho e diálogo "Outras empresas do grupo":
# um mesmo login enxerga várias unidades, e as remessas dependem da unidade ativa.
CURRENT_COMPANY_SELECTOR = "div.dados-cliente span.nome"
COMPANY_SWITCH_TRIGGER_SELECTOR = "i.trigger-grupo-empresa"
COMPANY_OPTION_SELECTOR = "div.grupo-empresa"
COMPANY_OPTION_NAME_SELECTOR = "div.razao-social span"
COMPANY_FOOTER_BUTTON_XPATH = "//div[contains(@class,'footer-grupo-empresa')]//button[normalize-space()='{text}']"
COMPANY_SWITCH_TIMEOUT = 60
# Login com uma única unidade não mostra a seta de troca.
COMPANY_TRIGGER_WAIT_SECONDS = 10
SHIPMENT_LIST_WAIT_SECONDS = 10


class RegistrationError(RuntimeError):
    """Erro de dados da programação que o usuário corrige no Portal Bindi Log
    (horário indisponível, placa ou compartimento recusado...): user_message
    volta para o portal; o texto completo (str) traz o detalhe técnico para o log.
    Erro técnico (tela lenta, elemento não encontrado) continua RuntimeError."""

    def __init__(self, user_message: str, technical_detail: str = ""):
        super().__init__(f"{user_message} ({technical_detail})" if technical_detail else user_message)
        self.user_message = user_message


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
    driver.get(HOME_URL)

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, MENU_SELECTOR) or d.find_elements(By.ID, "username")
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Nem a tela de login nem o menu do portal apareceram em {timeout}s. {_page_summary(driver)}"
        ) from exc

    if driver.find_elements(By.CSS_SELECTOR, MENU_SELECTOR):
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
            lambda d: d.find_elements(By.CSS_SELECTOR, MENU_SELECTOR) or _is_mfa_screen(d)
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
        WebDriverWait(driver, timeout).until(lambda d: d.find_elements(By.CSS_SELECTOR, MENU_SELECTOR))
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
            lambda d: _try_mfa_code_file(d) or d.find_elements(By.CSS_SELECTOR, MENU_SELECTOR)
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
# Home e unidade (empresa do grupo) ativa
# --------------------------------------------------------------------------
def go_home(driver: webdriver.Chrome, main_handle: str) -> None:
    """Fecha as abas de tentativas anteriores, volta à home e fecha o popup
    'Informativos', que reabre a cada visita e bloqueia o menu."""
    for handle in driver.window_handles:
        if handle != main_handle:
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(main_handle)
    driver.get(HOME_URL)
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.presence_of_element_located((By.CSS_SELECTOR, MENU_SELECTOR)))
    except TimeoutException as exc:
        raise RuntimeError(f"Home da Vibra não carregou em {DEFAULT_TIMEOUT}s. {_page_summary(driver)}") from exc
    _close_informative_popup(driver)


def _close_informative_popup(driver: webdriver.Chrome) -> None:
    """O popup aparece alguns segundos depois da home; sem informativo pendente ele não aparece."""
    try:
        close_icon = WebDriverWait(driver, POPUP_WAIT_SECONDS, poll_frequency=0.5).until(
            lambda d: _first_visible(d, POPUP_CLOSE_XPATH)
        )
    except TimeoutException:
        return
    _scroll_and_click(driver, close_icon)
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(lambda d: not _first_visible(d, POPUP_CLOSE_XPATH))
    except TimeoutException as exc:
        raise RuntimeError("Popup 'Informativos' da home não fechou após clicar no X.") from exc
    print("[INFO] Popup 'Informativos' fechado.")


def select_company(driver: webdriver.Chrome, company_name: str) -> str:
    """Deixa ativa a unidade company_name (VIBRA_UNIDADE; precisa estar na home).
    O diálogo de troca pode mostrar só o nome, sem o código ("10076 - "), então
    compara também sem ele. Devolve a unidade ativa."""
    names = list(dict.fromkeys([company_name.strip(), company_name.split(" - ", 1)[-1].strip()]))
    current = read_current_company(driver)
    if _matching_company([current], names):
        print(f"[INFO] Unidade ativa na Vibra: {current}")
        return current

    target = _switch_company(driver, lambda options: _matching_company(options, names))
    if target:
        return target
    raise RuntimeError(
        f"Unidade '{company_name}' não encontrada entre as unidades da Vibra deste login "
        f"(ativa: {current}). Confira VIBRA_UNIDADE."
    )


def read_current_company(driver: webdriver.Chrome) -> str:
    try:
        return WebDriverWait(driver, DEFAULT_TIMEOUT).until(_current_company_or_empty)
    except TimeoutException as exc:
        raise RuntimeError(f"Unidade ativa não encontrada no cabeçalho da Vibra. {_page_summary(driver)}") from exc


def _matching_company(company_names: List[str], station_names: List[str]) -> Optional[str]:
    """Os nomes do posto vêm do mais específico para o mais genérico; compara sem acentos."""
    for station_name in station_names:
        wanted = _normalize_text(station_name)
        for company_name in company_names:
            if wanted and wanted in _normalize_text(company_name):
                return company_name
    return None


def _normalize_text(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().upper()
    return " ".join(plain.split())


def _switch_company(driver: webdriver.Chrome, choose_company) -> Optional[str]:
    """Abre 'Outras empresas do grupo' e troca para a unidade que choose_company
    escolher entre os nomes listados. Sem escolha, cancela o diálogo e devolve None."""
    try:
        trigger = WebDriverWait(driver, COMPANY_TRIGGER_WAIT_SECONDS).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, COMPANY_SWITCH_TRIGGER_SELECTOR))
        )
    except TimeoutException:
        print("[INFO] Login sem outras unidades na Vibra (seta de troca não apareceu).")
        return None
    _scroll_and_click(driver, trigger)
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: [o for o in d.find_elements(By.CSS_SELECTOR, COMPANY_OPTION_SELECTOR) if o.is_displayed()]
        )
    except TimeoutException as exc:
        raise RuntimeError("Diálogo 'Outras empresas do grupo' não abriu após clicar na seta.") from exc

    options = {}
    for option in driver.find_elements(By.CSS_SELECTOR, COMPANY_OPTION_SELECTOR):
        names = option.find_elements(By.CSS_SELECTOR, COMPANY_OPTION_NAME_SELECTOR)
        # textContent: o nome aparece truncado ("...") na tela, mas o texto completo está no DOM.
        name = (names[0].get_attribute("textContent") or "").strip() if names else ""
        if name:
            options[name] = option

    target = choose_company(list(options))
    button_text = "Trocar" if target else "Cancelar"
    if target:
        radio = options[target].find_element(By.CSS_SELECTOR, "input[type=radio]")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", radio)
        print(f"[INFO] Trocando para a unidade {target}...")
    _click(driver, By.XPATH, COMPANY_FOOTER_BUTTON_XPATH.format(text=button_text))
    if not target:
        return None

    try:
        WebDriverWait(driver, COMPANY_SWITCH_TIMEOUT).until(
            lambda d: _normalize_text(target) in _normalize_text(_current_company_or_empty(d))
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Troca para a unidade {target} não confirmada em {COMPANY_SWITCH_TIMEOUT}s "
            f"(cabeçalho mostra '{_current_company_or_empty(driver)}')."
        ) from exc
    return target


def _current_company_or_empty(driver: webdriver.Chrome) -> str:
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, CURRENT_COMPANY_SELECTOR)
        return (elements[0].get_attribute("textContent") or "").strip() if elements else ""
    except StaleElementReferenceException:
        return ""


# --------------------------------------------------------------------------
# Programação FOB > Cadastrar
# --------------------------------------------------------------------------
def open_fob_registration(driver: webdriver.Chrome) -> None:
    """Parte da home e chega à tela de Compartimentação: menu > Programação >
    Programação FOB > Cadastrar > Pesquisar."""
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
        WebDriverWait(driver, SLOW_SCREEN_TIMEOUT, ignored_exceptions=SLOW_SCREEN_IGNORED_ERRORS).until(
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
    # O chromedriver só responde ao clique quando a próxima tela termina de carregar, que pode
    # passar do timeout HTTP do Selenium. O clique já foi feito; a espera abaixo continua.
    try:
        driver.execute_script("var button = arguments[0]; setTimeout(function () { button.click(); }, 0);", search_button)
    except SLOW_SCREEN_IGNORED_ERRORS:
        print("[INFO] 'Pesquisar' clicado; a Vibra ainda está carregando a próxima tela.")

    print("[INFO] Aguardando a tela de Compartimentação (pode demorar)...")
    try:
        WebDriverWait(
            driver, SLOW_SCREEN_TIMEOUT, poll_frequency=1, ignored_exceptions=SLOW_SCREEN_IGNORED_ERRORS
        ).until(EC.presence_of_element_located((By.ID, "comboPool")))
    except TimeoutException as exc:
        raise RuntimeError(
            f"Tela de Compartimentação não carregou em {SLOW_SCREEN_TIMEOUT}s após 'Pesquisar'."
        ) from exc
    _wait_page_ready(driver)
    _install_ajax_monitor(driver)


def fill_registration(driver: webdriver.Chrome, pool: str, plates: List[str], carrier: str) -> Dict[str, str]:
    """Preenche Pool, Placa (+ Buscar) e Transportador na tela de Compartimentação.
    plates = carretas do conjunto, na ordem: se a Vibra recusar uma ("O veículo
    informado está inválido..."), tenta a próxima. Devolve o texto das opções
    selecionadas nos combos e a placa aceita."""
    selected_pool = _select_pool(driver, pool)

    refusals = []
    accepted_plate = ""
    for plate in plates:
        plate_errors = _search_plate(driver, plate)
        if not plate_errors:
            accepted_plate = plate
            break
        refusals.append(f"{plate} (Vibra: {' | '.join(plate_errors)})")
        print(f"[AVISO] A Vibra recusou a placa {plate}: {' | '.join(plate_errors)}")
    if not accepted_plate:
        raise RegistrationError(
            ("A Vibra recusou as placas " if len(refusals) > 1 else "A Vibra recusou a placa ")
            + "; ".join(refusals)
            + ". Confira o cadastro do veículo na Vibra com a central de atendimento ou troque o caminhão da viagem."
        )

    print(f"[INFO] Transportador: {carrier}")
    selected_carrier = _select_select2(driver, "cmbTranspp", carrier)
    _wait_page_ready(driver)

    return {"pool": selected_pool, "carrier": selected_carrier, "plate": accepted_plate}


def _search_plate(driver: webdriver.Chrome, plate: str) -> List[str]:
    """Placa + Buscar. Devolve os alertas de erro da Vibra (vazio = placa aceita)."""
    print(f"[INFO] Placa: {plate}")
    _fill_plate(driver, plate)
    # Alerta antigo na tela (inclusive da placa anterior) seria lido como resposta desta.
    _remove_error_messages(driver)
    print("[INFO] Buscar")
    _click(driver, By.XPATH, "//input[@type='submit' and @value='Buscar']")
    _wait_page_ready(driver)
    try:
        WebDriverWait(driver, SLOW_SCREEN_TIMEOUT).until(
            lambda d: _visible_texts(d, ERROR_MESSAGE_SELECTORS) or d.find_elements(By.ID, "cmbTranspp")
        )
    except TimeoutException as exc:
        raise RegistrationError(
            f"A Vibra não aceitou a placa {plate}. Confira o cadastro do veículo na Vibra "
            "para a BindiLog ou troque o caminhão da viagem.",
            f"combo de transportador não apareceu em {SLOW_SCREEN_TIMEOUT}s após Buscar",
        ) from exc
    return [_clean_alert_text(text) for text in _visible_texts(driver, ERROR_MESSAGE_SELECTORS)]


def _clean_alert_text(text: str) -> str:
    # O botão de fechar do alerta entra no texto ("X Fechar O veículo informado...").
    return re.sub(r"^\s*(?:[×xX]\s*)?Fechar\s*", "", text).strip()


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
POOL_NOT_FOUND_MESSAGE = (
    "Posto '{pool}' não encontrado no Pool da Programação FOB da Vibra (unidade VIBRA_UNIDADE). Confira o "
    "nome do posto na distribuidora (cadastro do posto) ou se a Vibra liberou esse posto para a BindiLog."
)


def load_compartments(
    driver: webdriver.Chrome, orders: List[dict], current_pool: str
) -> Tuple[List[str], Dict[str, str]]:
    """Carrega cada remessa no compartimento definido na programação do portal e
    clica em Continuar. Compartimentos de postos diferentes vão na mesma
    programação: antes de cada posto troca o Pool (order["vibraPool"]), que muda
    a Lista de Remessas sem desfazer o Carregamento do Veículo. Na Vibra cada
    compartimento é carregado cheio, então o volume alocado é a própria
    capacidade (igual ao portal Bindi Log).
    Devolve um resumo por compartimento ("C5 0265807114 ETANOL ... 3 m³") e o
    texto do Pool selecionado para cada posto trocado."""
    allocations = [
        (
            order.get("vibraPool") or current_pool,
            order["distributorOrderNumber"],
            item["productName"],
            compartment["number"],
            compartment["capacityM3"],
        )
        for order in orders
        for item in order.get("items", [])
        for compartment in item.get("compartments", [])
    ]
    if not allocations:
        raise RegistrationError("A viagem não tem nenhum compartimento alocado para os pedidos da Vibra.")
    # Um posto por vez (menos trocas de Pool), na ordem dos compartimentos.
    pool_order = list(dict.fromkeys(a[0] for a in sorted(allocations, key=lambda a: a[3])))
    allocations.sort(key=lambda a: (pool_order.index(a[0]), a[3]))

    _install_ajax_monitor(driver)
    summary: List[str] = []
    selected_pools: Dict[str, str] = {}
    loaded: List[Tuple[int, str]] = []
    for pool, order_number, product_name, compartment_number, capacity_m3 in allocations:
        if pool != current_pool:
            selected_pools[pool] = _select_pool(driver, pool)
            _check_still_loaded(driver, loaded, pool)
            current_pool = pool
        print(
            f"[INFO] Compartimento {compartment_number}: {product_name} {capacity_m3:g} m³ "
            f"(ordem {order_number}, posto {pool})"
        )
        vehicle_row = _find_vehicle_row(driver, compartment_number)
        if _same_order_number(_vehicle_row_order(vehicle_row), order_number):
            print(f"[INFO] Compartimento {compartment_number} já carregado com a ordem {order_number} - mantido.")
            summary.append(f"C{compartment_number} {order_number} (já carregado)")
            loaded.append((compartment_number, order_number))
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
        loaded.append((compartment_number, order_number))

    print("[INFO] Carregamento do Veículo > Continuar")
    try:
        continue_button = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: _first_visible(d, CONTINUE_BUTTON_XPATH)
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Botão 'Continuar' do Carregamento do Veículo não apareceu em {DEFAULT_TIMEOUT}s.") from exc
    _scroll_and_click(driver, continue_button)
    _wait_page_ready(driver)
    return summary, selected_pools


def _select_pool(driver: webdriver.Chrome, pool: str) -> str:
    print(f"[INFO] Pool: {pool}")
    selected = _select_select2(driver, "comboPool", pool, not_found_message=POOL_NOT_FOUND_MESSAGE.format(pool=pool))
    _wait_page_ready(driver)
    return selected


def _check_still_loaded(driver: webdriver.Chrome, loaded: List[Tuple[int, str]], pool: str) -> None:
    """Trocar o Pool não pode desfazer os compartimentos já carregados."""
    for compartment_number, order_number in loaded:
        order_on_screen = _vehicle_row_order(_find_vehicle_row(driver, compartment_number))
        if not _same_order_number(order_on_screen, order_number):
            raise RuntimeError(
                f"Ao trocar o Pool para {pool}, o compartimento {compartment_number} perdeu a ordem "
                f"{order_number} na Vibra (na tela: {order_on_screen or 'vazio'})."
            )


SCHEDULE_TIME_SELECT_ID = "combo_horarios"
FINALIZE_BUTTON_ID = "botao_cria1"
# Bootstrap Datepicker inline: td.day; "old"/"new" = dias de outro mês, "disabled" = indisponível.
CALENDAR_DAYS_SELECTOR = "#datepickid .datepicker-days"
CALENDAR_MONTH_NAMES = [
    "JANEIRO", "FEVEREIRO", "MARCO", "ABRIL", "MAIO", "JUNHO",
    "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO",
]
# Horário planejado indisponível: aceita o livre mais próximo até esta diferença.
MAX_SCHEDULE_TIME_DIFF_MINUTES = 60
SEARCH_TIMES_XPATH ="//a[normalize-space()='Pesquisar Horários']"
ERROR_MESSAGE_SELECTORS = ".alert-danger, .ui-messages-error, .ui-message-error, #mensagens .erro"
MESSAGE_SELECTORS = "#mensagens, .alert, .ui-messages, .ui-growl-message"
# Tela "Programação de Caminhão - Protocolo" depois do Finalizar:
# h2 "Número da Programação : 4041588304 - 19/11/2026 18:15" (confirmado via DOM).
SCHEDULE_NUMBER_PATTERN = re.compile(r"N[úu]mero da Programa[çc][ãa]o\s*:\s*(\d+)", re.IGNORECASE)


def schedule_and_finalize(driver: webdriver.Chrome, load_date: str, load_time: str) -> Tuple[str, str, str]:
    """Tela "Programação de Caminhão - Agendamento": escolhe no combo de horários a
    opção com a data e a hora de carregamento da programação do portal
    (loadDate "AAAA-MM-DD", loadTime "HH:MM") - ou o horário livre mais próximo, a até
    MAX_SCHEDULE_TIME_DIFF_MINUTES - e clica em Finalizar. Devolve o número da
    programação na Vibra (tela de Protocolo; "" se não for lido), a mensagem
    (com o aviso de horário ajustado) e o horário agendado."""
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

    adjustment_note = ""
    option = _schedule_option(driver, wanted_text)
    if not option:
        date_prefix = wanted_text.split(" - ")[0]
        free_times = [text.split(" - ")[-1] for text in _schedule_option_texts(driver) if text.startswith(date_prefix)]
        vibra_errors = _visible_texts(driver, ERROR_MESSAGE_SELECTORS)
        if not free_times:
            raise RegistrationError(
                f"Nenhum horário disponível na Vibra para {date_prefix}"
                + (f" (Vibra: {' | '.join(vibra_errors)})" if vibra_errors else "")
                + ". Ajuste a data de carga ou o horário do carregamento VIBRA na viagem.",
                f"Combo de horários: {', '.join(_schedule_option_texts(driver)) or 'vazio'}",
            )
        nearest_time = _nearest_time(load_time, free_times)
        if not nearest_time:
            raise RegistrationError(
                f"Horário {load_time} de {date_prefix} indisponível na Vibra e nenhum horário livre "
                f"a até {MAX_SCHEDULE_TIME_DIFF_MINUTES} min dele. Horários livres no dia: {', '.join(free_times)}. "
                "Ajuste o horário do carregamento VIBRA na viagem."
            )
        adjustment_note = f"Horário {load_time} indisponível na Vibra; agendado no mais próximo, {nearest_time}."
        print(f"[AVISO] {adjustment_note}")
        wanted_text = f"{date_prefix} - {nearest_time}"
        option = _schedule_option(driver, wanted_text)
    Select(driver.find_element(By.ID, SCHEDULE_TIME_SELECT_ID)).select_by_value(option.get_attribute("value"))
    _wait_page_ready(driver)

    selected_text = Select(driver.find_element(By.ID, SCHEDULE_TIME_SELECT_ID)).first_selected_option.text.strip()
    if selected_text != wanted_text:
        raise RuntimeError(f"Combo de horários ficou em '{selected_text}' em vez de '{wanted_text}'.")
    print(f"[OK] Horário selecionado: {selected_text}")

    print("[INFO] Agendamento > Finalizar")
    finalize_button = _wait_clickable(driver, By.ID, FINALIZE_BUTTON_ID)
    try:
        finalize_button.click()
    except ReadTimeoutError:
        # Só o timeout HTTP: o clique foi feito e a Vibra ainda está gerando o protocolo.
        print("[INFO] 'Finalizar' clicado; a Vibra ainda está carregando o protocolo.")
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", finalize_button)

    try:
        WebDriverWait(
            driver, SLOW_SCREEN_TIMEOUT, poll_frequency=1, ignored_exceptions=SLOW_SCREEN_IGNORED_ERRORS
        ).until(lambda d: _schedule_number(d) or _visible_texts(d, ERROR_MESSAGE_SELECTORS))
    except TimeoutException:
        pass
    errors = _visible_texts(driver, ERROR_MESSAGE_SELECTORS)
    if errors:
        raise RegistrationError(f"A Vibra recusou a programação ao Finalizar: {' | '.join(errors)}")

    schedule_number = _schedule_number(driver)
    message = " | ".join(dict.fromkeys(
        ([adjustment_note] if adjustment_note else []) + _visible_texts(driver, MESSAGE_SELECTORS)
    ))
    if schedule_number:
        print(f"[OK] Programação criada na Vibra: nº {schedule_number}. {message}".rstrip())
    else:
        # Já clicou em Finalizar sem erro: tratar como falha faria o próximo ciclo
        # cadastrar de novo (programação duplicada na Vibra).
        print(
            f"[AVISO] Finalizado sem erro, mas o número da programação não apareceu em {SLOW_SCREEN_TIMEOUT}s "
            f"- confira na Vibra. URL atual: {driver.current_url}"
        )
    return schedule_number, message, wanted_text.split(" - ")[-1]


def _nearest_time(wanted_time: str, free_times: List[str]) -> str:
    """Horário livre mais próximo do planejado, a até MAX_SCHEDULE_TIME_DIFF_MINUTES;
    no empate fica o mais cedo. "" se nenhum estiver dentro do limite."""
    def minutes(hhmm: str) -> int:
        hours, mins = hhmm.split(":")
        return int(hours) * 60 + int(mins)

    target = minutes(wanted_time)
    candidates = []
    for free_time in free_times:
        try:
            diff = abs(minutes(free_time) - target)
        except ValueError:
            continue
        if diff <= MAX_SCHEDULE_TIME_DIFF_MINUTES:
            candidates.append((diff, minutes(free_time), free_time))
    return min(candidates)[2] if candidates else ""


def _schedule_number(driver: webdriver.Chrome) -> str:
    match = SCHEDULE_NUMBER_PATTERN.search(driver.find_element(By.TAG_NAME, "body").text)
    return match.group(1) if match else ""


def _visible_texts(driver: webdriver.Chrome, css_selector: str) -> List[str]:
    return [el.text.strip() for el in driver.find_elements(By.CSS_SELECTOR, css_selector)
            if el.is_displayed() and el.text.strip()]


def _remove_error_messages(driver: webdriver.Chrome) -> None:
    driver.execute_script(
        "document.querySelectorAll(arguments[0]).forEach(el => el.remove());", ERROR_MESSAGE_SELECTORS
    )


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
        raise RegistrationError(
            f"Dia {day:02d}/{month:02d}/{year} está bloqueado para agendamento na Vibra. "
            "Ajuste a data de carga da viagem."
        )

    # A tela pode abrir com alerta de outra data ("Não há horários disponíveis para a
    # data solicitada"); sem removê-lo, ele seria lido como resposta do dia clicado.
    _remove_error_messages(driver)
    print(f"[INFO] Calendário: clicando no dia {day:02d}/{month:02d}/{year}")
    _scroll_and_click(driver, day_cells[0])
    _wait_page_ready(driver)

    # O combo é recarregado por AJAX depois do clique no dia; espera ele mostrar a data
    # certa ou a Vibra avisar que não há horário no dia.
    date_prefix = wanted_text.split(" - ")[0]
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: any(text.startswith(date_prefix) for text in _schedule_option_texts(d))
            or _visible_texts(d, ERROR_MESSAGE_SELECTORS)
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
    try:
        rows = WebDriverWait(driver, SHIPMENT_LIST_WAIT_SECONDS, poll_frequency=0.5).until(
            lambda d: _visible_rows(d, SHIPMENT_ROWS_XPATH)
        )
    except TimeoutException as exc:
        raise RegistrationError(
            f"Pedido Vibra {order_number} não está disponível para programação na Vibra "
            "(pode estar em análise, sem saldo, já programado ou com outro posto).",
            f"Lista de Remessas vazia após {SHIPMENT_LIST_WAIT_SECONDS}s",
        ) from exc

    entries = []
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) >= 5:
            entries.append((row, cells[1].text.strip(), cells[3].text.strip(), cells[4].text.strip()))
    same_order = [entry for entry in entries if _same_order_number(entry[3], order_number)]
    if not same_order:
        available = ", ".join(sorted({entry[3] for entry in entries}))
        raise RegistrationError(
            f"Pedido Vibra {order_number} não está disponível para programação na Vibra "
            "(pode estar em análise, sem saldo, já programado ou com outro posto).",
            f"ordens na tela: {available}",
        )

    wanted_words = _product_words(product_name)
    matches = [entry for entry in same_order if wanted_words <= _product_words(entry[1])]
    if not matches:
        if len(same_order) > 1:
            raise RegistrationError(
                f"Pedido Vibra {order_number}: produto '{product_name}' não identificado entre as remessas "
                f"da Vibra ({', '.join(entry[1] for entry in same_order)}). Confira o produto do pedido."
            )
        print(
            f"[AVISO] Produto '{product_name}' não bate com '{same_order[0][1]}' - "
            f"única remessa da ordem {order_number}, usando ela."
        )
        matches = same_order

    # Depois de carregar um compartimento a Vibra divide a remessa em linhas: a já
    # carregada fica com volume 0 e sem o botão de carregar.
    loadable = [
        entry for entry in matches
        if _volume_m3(entry[2]) > 0 and entry[0].find_elements(By.CSS_SELECTOR, "button[data-toggle='modal']")
    ]
    if not loadable:
        raise RegistrationError(
            f"Pedido Vibra {order_number} ({product_name}) sem saldo na Lista de Remessas da Vibra "
            f"(volumes na tela: {', '.join(entry[2] for entry in matches)}). Confira o volume alocado na viagem.",
        )

    row, vibra_product, volume_text, _ = loadable[0]
    print(f"[INFO] Remessa: {vibra_product} | volume disponível {volume_text} | ordem {order_number}")
    return row, vibra_product


def _volume_m3(text: str) -> float:
    """Volume da Lista de Remessas em m³ com 3 casas: '7,000' = 7 m³."""
    try:
        return float(text.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


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
        raise RegistrationError(
            f"A Vibra não permite carregar a remessa no compartimento {compartment_number} "
            f"(compartimentos aceitos: {', '.join(map(str, sorted(links_by_number))) or 'nenhum'}). "
            "Ajuste a alocação dos compartimentos na viagem."
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
        raise RegistrationError(
            f"Compartimento {compartment_number} não existe no caminhão cadastrado na Vibra "
            "(compartimentação diferente da do portal). Confira o caminhão ou a alocação da viagem.",
            f"linha do compartimento não apareceu em {DEFAULT_TIMEOUT}s no Carregamento do Veículo",
        ) from exc


def _vehicle_row_order(vehicle_row) -> str:
    order_text = vehicle_row.find_elements(By.TAG_NAME, "td")[5].text.strip()
    return "" if order_text in ("", "-") else order_text


def _check_vehicle_row_free(vehicle_row, compartment_number: int, capacity_m3: float) -> None:
    current_order = _vehicle_row_order(vehicle_row)
    if current_order:
        raise RegistrationError(
            f"Compartimento {compartment_number} já está ocupado na Vibra com o pedido {current_order}. "
            "Confira a alocação dos compartimentos na viagem."
        )

    capacity_select = vehicle_row.find_elements(By.TAG_NAME, "td")[2].find_elements(By.TAG_NAME, "select")
    if not capacity_select:
        return
    vibra_capacity_text = _selected_option_text(capacity_select[0])
    vibra_capacity = _parse_vibra_volume(vibra_capacity_text)
    if vibra_capacity is not None and abs(vibra_capacity - capacity_m3) > 0.001:
        raise RegistrationError(
            f"Compartimento {compartment_number}: capacidade na Vibra é {vibra_capacity_text} m³ e no portal "
            f"{capacity_m3:g} m³. Confira o caminhão da viagem ou o cadastro dele na Vibra."
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
        WebDriverWait(driver, timeout, poll_frequency=0.5, ignored_exceptions=SLOW_SCREEN_IGNORED_ERRORS).until(
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
    if _wait_visible(driver, title_xpath, 0):
        return
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: _first_visible_css(d, MENU_EXPAND_BUTTON_SELECTOR) or _first_visible_css(d, LEGACY_MENU_ICON_SELECTOR)
        )
    except TimeoutException as exc:
        raise RuntimeError(f"Menu lateral não apareceu em {DEFAULT_TIMEOUT}s. URL atual: {driver.current_url}") from exc

    expand_button = _first_visible_css(driver, MENU_EXPAND_BUTTON_SELECTOR)
    if expand_button:
        _scroll_and_click(driver, expand_button)
        if _wait_visible(driver, title_xpath, 5):
            return
        # Clique nativo pode cair no <svg> interno sem disparar o handler do Angular.
        driver.execute_script("arguments[0].click();", expand_button)
        if _wait_visible(driver, title_xpath, 5):
            return
        raise RuntimeError("Menu lateral não expandiu após clicar no botão: título 'Programação' não apareceu.")

    icon = _first_visible_css(driver, LEGACY_MENU_ICON_SELECTOR)
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


def _first_visible_css(driver: webdriver.Chrome, selector: str):
    for element in driver.find_elements(By.CSS_SELECTOR, selector):
        try:
            if element.is_displayed():
                return element
        except StaleElementReferenceException:
            continue
    return False


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


def _wait_clickable(driver: webdriver.Chrome, by: str, selector: str, timeout: int = DEFAULT_TIMEOUT):
    try:
        return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, selector)))
    except TimeoutException as exc:
        raise RuntimeError(f"Elemento não ficou clicável em {timeout}s: {selector}") from exc


def _click(driver: webdriver.Chrome, by: str, selector: str, timeout: int = DEFAULT_TIMEOUT):
    element = _wait_clickable(driver, by, selector, timeout)
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)
    return element


def _select_select2(
    driver: webdriver.Chrome,
    select_id: str,
    text: str,
    timeout: int = DEFAULT_TIMEOUT,
    not_found_message: Optional[str] = None,
) -> str:
    """Seleciona no <select id=select_id> (select2) a opção cujo texto contém `text`.

    Não usa o campo de pesquisa do select2: o JSF re-renderiza o combo enquanto se
    digita e o texto se perde (ficava só "q"). Em vez disso, define o valor via
    jQuery e dispara o 'change', que chama o onchange/AJAX do JSF normalmente.
    """
    # Espera a opção procurada, não uma quantidade de opções: unidade com um só posto
    # traz o Pool com uma única opção já selecionada, sem "Selecione".
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                """
                var s = document.getElementById(arguments[0]), t = arguments[1].toUpperCase();
                return !!(window.jQuery && s && Array.from(s.options).some(function (o) {
                    return o.text.toUpperCase().indexOf(t) >= 0;
                }));
                """,
                select_id,
                text,
            )
        )
    except TimeoutException as exc:
        detail = (
            f"opção contendo '{text}' não apareceu em #{select_id} em {timeout}s; "
            f"opções na tela: {_combo_options_text(driver, select_id)}"
        )
        if not_found_message:
            raise RegistrationError(not_found_message, detail) from exc
        raise RuntimeError(f"Combo: {detail}.") from exc

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
        if not_found_message:
            raise RegistrationError(
                not_found_message,
                f"opção contendo '{text}' não encontrada em #{select_id}; "
                f"opções na tela: {_combo_options_text(driver, select_id)}",
            )
        raise RuntimeError(f"Opção contendo '{text}' não encontrada em #{select_id}.")
    print(f"[OK]   -> selecionado: {selected}")
    return selected


def _combo_options_text(driver: webdriver.Chrome, select_id: str) -> str:
    options = driver.execute_script(
        """
        var s = document.getElementById(arguments[0]);
        return s ? Array.from(s.options).map(function (o) { return o.text.trim(); }).filter(Boolean) : null;
        """,
        select_id,
    )
    if options is None:
        return "combo não está na tela"
    if not options:
        return "nenhuma"
    hidden_count = len(options) - MAX_LOGGED_COMBO_OPTIONS
    return " / ".join(options[:MAX_LOGGED_COMBO_OPTIONS]) + (f" (+{hidden_count})" if hidden_count > 0 else "")
