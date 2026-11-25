"""
Automação Vibra Energia (cn.vibraenergia.com.br) - Programação FOB

Fluxo:
 1. Login com usuário/senha -> "Acessar"
 2. Espera até 5 min o usuário digitar o código de verificação -> "Continuar"
 3. Menu lateral -> Programação > Programação FOB > Consultar
 4. Preenche Data Programada (de/até), Base e Cliente vindos do banco -> "Pesquisar Programações"
 5. Percorre a coluna "Programação" (inclusive outras páginas) até achar o número vindo do banco
 6. Clica no lápis (Alterar Programação) -> "Alterar" no modal
 7. Espera carregar e clica em "Pesquisar"

Requisitos:
    pip install selenium psycopg2-binary

Variáveis de ambiente:
    VIBRA_USUARIO, VIBRA_SENHA
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
import time
from datetime import date, datetime

import psycopg2
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

URL = "https://cn.vibraenergia.com.br/"
TIMEOUT_PADRAO = 30
TIMEOUT_CODIGO = 5 * 60  # 5 minutos

# Seletores (os do login foram conferidos na página; os demais são por texto
# e podem precisar de ajuste após inspecionar as telas internas)
SEL_USUARIO = (By.ID, "username")
SEL_SENHA = (By.ID, "password")
SEL_ACESSAR = (By.ID, "loginBt")
# Botão que abre o menu lateral colapsável (ajuste se necessário)
SEL_TOGGLE_MENU = (
    By.XPATH,
    "//*[contains(@class,'menu-toggle') or contains(@class,'sidebar-toggle') "
    "or contains(@class,'hamburger') or contains(@class,'fa-bars') "
    "or contains(@class,'pi-bars') or @id='menu-button']",
)


# ----------------------------------------------------------------------------
# Banco de dados
# ----------------------------------------------------------------------------
def buscar_parametros_db() -> dict:
    """Busca os parâmetros da pesquisa. Ajuste a query para a sua tabela."""
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT data_programada_de,
                       data_programada_ate,
                       base,
                       cliente,
                       numero_programacao
                  FROM programacoes_fob_pendentes   -- <== ajuste aqui
                 WHERE processado = false
                 ORDER BY id
                 LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Nenhum registro pendente no banco.")
            return {
                "data_de": row[0],
                "data_ate": row[1],
                "base": str(row[2]).strip(),
                "cliente": str(row[3]).strip(),
                "numero_programacao": str(row[4]).strip(),
            }
    finally:
        conn.close()


def formatar_data(valor) -> str:
    if isinstance(valor, (date, datetime)):
        return valor.strftime("%d/%m/%Y")
    return str(valor).strip()


# ----------------------------------------------------------------------------
# Helpers Selenium
# ----------------------------------------------------------------------------
def esperar_ajax(driver, timeout=TIMEOUT_PADRAO):
    """Espera o fim das requisições AJAX (JSF/PrimeFaces/jQuery) e dos 'carregando'."""
    fim = time.time() + timeout
    while time.time() < fim:
        try:
            ocupado = driver.execute_script(
                """
                if (document.readyState !== 'complete') return true;
                if (window.jQuery && jQuery.active > 0) return true;
                if (window.PrimeFaces && PrimeFaces.ajax && PrimeFaces.ajax.Queue
                    && !PrimeFaces.ajax.Queue.isEmpty()) return true;
                const sels = '.ui-blockui, .blockUI, .loading, .spinner, [id*="ajaxStatus"], [id*="loading"]';
                return [...document.querySelectorAll(sels)].some(e => e.offsetParent !== null
                        && getComputedStyle(e).visibility !== 'hidden');
                """
            )
            if not ocupado:
                time.sleep(0.3)
                return
        except Exception:
            pass
        time.sleep(0.3)


def clicar(driver, elemento):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elemento)
    try:
        elemento.click()
    except (ElementClickInterceptedException, StaleElementReferenceException):
        driver.execute_script("arguments[0].click();", elemento)


def achar_visivel(driver, xpath, timeout=TIMEOUT_PADRAO):
    """Retorna o primeiro elemento visível que casa com o XPath."""
    def _cond(d):
        for e in d.find_elements(By.XPATH, xpath):
            try:
                if e.is_displayed():
                    return e
            except StaleElementReferenceException:
                continue
        return False
    return WebDriverWait(driver, timeout).until(_cond)


def clicar_por_texto(driver, texto, timeout=TIMEOUT_PADRAO, container=""):
    """Clica em botão/link/item de menu cujo texto (ou value) seja `texto`."""
    t = texto.strip()
    xpath = (
        f"{container}//*[self::button or self::a or self::span or self::li or self::div]"
        f"[normalize-space()='{t}']"
        f" | {container}//input[(@type='submit' or @type='button') and normalize-space(@value)='{t}']"
    )
    el = achar_visivel(driver, xpath, timeout)
    # sobe até o elemento clicável mais próximo
    try:
        el = el.find_element(By.XPATH, "./ancestor-or-self::*[self::a or self::button][1]")
    except NoSuchElementException:
        pass
    clicar(driver, el)
    esperar_ajax(driver)


def campo_apos_label(driver, label, posicao=1):
    xpath = (
        f"(//label[contains(normalize-space(),'{label}')]"
        f"/following::input[not(@type='hidden')])[{posicao}]"
    )
    return achar_visivel(driver, xpath)


def preencher_data(driver, elemento, valor):
    clicar(driver, elemento)
    elemento.send_keys(Keys.CONTROL, "a")
    elemento.send_keys(Keys.DELETE)
    elemento.send_keys(valor)
    elemento.send_keys(Keys.TAB)
    # fecha datepicker se ficou aberto
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    esperar_ajax(driver)


def selecionar_por_label(driver, label, texto):
    """
    Seleciona `texto` no campo após o rótulo `label`.
    Suporta <select> nativo, PrimeFaces selectOneMenu e autocomplete.
    """
    xpath = (
        f"(//label[contains(normalize-space(),'{label}')]/following::*"
        f"[self::select or contains(@class,'ui-selectonemenu') "
        f"or contains(@class,'ui-autocomplete') or contains(@class,'select2') "
        f"or (self::input and not(@type='hidden'))])[1]"
    )
    campo = WebDriverWait(driver, TIMEOUT_PADRAO).until(
        EC.presence_of_element_located((By.XPATH, xpath))
    )
    classes = campo.get_attribute("class") or ""
    tag = campo.tag_name.lower()

    # 1) <select> nativo
    if tag == "select":
        sel = Select(campo)
        try:
            sel.select_by_visible_text(texto)
        except NoSuchElementException:
            for op in sel.options:
                if texto.lower() in op.text.lower():
                    op.click()
                    break
            else:
                raise RuntimeError(f"Opção '{texto}' não encontrada em '{label}'.")
        esperar_ajax(driver)
        return

    # 2) PrimeFaces selectOneMenu / select2 (dropdown com lista)
    if "ui-selectonemenu" in classes or "select2" in classes:
        clicar(driver, campo)
        # se o painel tiver filtro, digita para filtrar
        filtros = [f for f in driver.find_elements(
            By.CSS_SELECTOR, ".ui-selectonemenu-filter, .select2-search__field") if f.is_displayed()]
        if filtros:
            filtros[0].send_keys(texto)
            esperar_ajax(driver)
        item = achar_visivel(
            driver,
            f"//li[(contains(@class,'ui-selectonemenu-item') or contains(@class,'select2-results__option'))"
            f" and contains(normalize-space(), '{texto}')]",
        )
        clicar(driver, item)
        esperar_ajax(driver)
        return

    # 3) Autocomplete / input de texto
    entrada = campo if tag == "input" else campo.find_element(By.XPATH, ".//input[not(@type='hidden')]")
    entrada.send_keys(Keys.CONTROL, "a")
    entrada.send_keys(Keys.DELETE)
    entrada.send_keys(texto)
    esperar_ajax(driver)
    try:
        item = achar_visivel(
            driver,
            f"//li[(contains(@class,'ui-autocomplete-item') or contains(@class,'ui-menu-item'))"
            f" and contains(normalize-space(), '{texto}')]",
            timeout=8,
        )
        clicar(driver, item)
    except TimeoutException:
        entrada.send_keys(Keys.TAB)
    esperar_ajax(driver)


# ----------------------------------------------------------------------------
# Etapas
# ----------------------------------------------------------------------------
def login(driver, usuario, senha):
    driver.get(URL)
    wait = WebDriverWait(driver, TIMEOUT_PADRAO)
    wait.until(EC.visibility_of_element_located(SEL_USUARIO)).send_keys(usuario)
    driver.find_element(*SEL_SENHA).send_keys(senha)
    clicar(driver, driver.find_element(*SEL_ACESSAR))
    esperar_ajax(driver)


def aguardar_codigo_verificacao(driver):
    """
    Espera até 5 min o usuário digitar o código e então clica em 'Continuar'.
    Se o próprio usuário clicar em 'Continuar' (ou a tela mudar), segue também.
    """
    print(">>> Digite o código de verificação no navegador (até 5 minutos)...")
    xpath_codigo = (
        "//input[not(@type='hidden') and not(@type='submit') and not(@type='button') and "
        "(contains(translate(@id,'CODEOTP','codeotp'),'code') or contains(translate(@name,'CODEOTP','codeotp'),'code') "
        "or contains(translate(@id,'OTP','otp'),'otp') or contains(translate(@name,'OTP','otp'),'otp') "
        "or contains(translate(@placeholder,'CÓDIGO','código'),'código'))]"
    )
    xpath_continuar = (
        "//*[self::button or self::a][normalize-space()='Continuar'] | "
        "//input[(@type='submit' or @type='button') and normalize-space(@value)='Continuar']"
    )

    fim = time.time() + TIMEOUT_CODIGO
    ultimo_valor, desde = "", time.time()
    while time.time() < fim:
        botoes = [b for b in driver.find_elements(By.XPATH, xpath_continuar) if b.is_displayed()]
        if not botoes and "sso.vibraenergia" not in driver.current_url:
            print("Tela de verificação concluída.")
            return
        campos = [c for c in driver.find_elements(By.XPATH, xpath_codigo) if c.is_displayed()]
        valor = "".join((c.get_attribute("value") or "") for c in campos)
        if valor != ultimo_valor:
            ultimo_valor, desde = valor, time.time()
        # código preenchido e parado por 2s -> clica em Continuar
        if len(valor) >= 4 and time.time() - desde >= 2 and botoes:
            clicar(driver, botoes[0])
            esperar_ajax(driver)
            WebDriverWait(driver, TIMEOUT_PADRAO).until(
                lambda d: "sso.vibraenergia" not in d.current_url
                or not [b for b in d.find_elements(By.XPATH, xpath_continuar) if b.is_displayed()]
            )
            print("Código enviado.")
            return
        time.sleep(1)
    raise TimeoutException("Código de verificação não informado em 5 minutos.")


def abrir_consulta_fob(driver):
    esperar_ajax(driver)
    try:
        toggle = achar_visivel(driver, SEL_TOGGLE_MENU[1], timeout=15)
        clicar(driver, toggle)
        time.sleep(0.5)
    except TimeoutException:
        print("Botão do menu lateral não encontrado (talvez já esteja aberto).")
    clicar_por_texto(driver, "Programação")
    clicar_por_texto(driver, "Programação FOB")
    clicar_por_texto(driver, "Consultar")
    esperar_ajax(driver)


def pesquisar_programacoes(driver, p):
    preencher_data(driver, campo_apos_label(driver, "Data Programada", 1), formatar_data(p["data_de"]))
    # o campo "até" costuma ser o 2º input depois do rótulo "Data Programada"
    preencher_data(driver, campo_apos_label(driver, "Data Programada", 2), formatar_data(p["data_ate"]))
    selecionar_por_label(driver, "Base", p["base"])
    selecionar_por_label(driver, "Cliente", p["cliente"])
    clicar_por_texto(driver, "Pesquisar Programações")
    esperar_ajax(driver)


def indice_coluna(driver, nome="Programação"):
    achar_visivel(driver, "//table//thead//th")
    ths = driver.find_elements(By.XPATH, "//table//thead//th")
    for i, th in enumerate(ths, start=1):
        if th.text.strip().lower() == nome.lower():
            return i
    raise RuntimeError(f"Coluna '{nome}' não encontrada.")


def localizar_e_alterar(driver, numero):
    col = indice_coluna(driver)
    while True:
        linhas = driver.find_elements(By.XPATH, "//table//tbody/tr[td]")
        for linha in linhas:
            try:
                cel = linha.find_element(By.XPATH, f"./td[{col}]")
            except NoSuchElementException:
                continue
            if cel.text.strip().lstrip("0") == numero.lstrip("0"):
                print(f"Programação {numero} encontrada.")
                lapis = linha.find_element(
                    By.XPATH,
                    ".//*[contains(@title,'Alterar') or contains(@class,'pencil') or contains(@class,'fa-edit') "
                    "or contains(@class,'pi-pencil') or contains(@class,'ui-icon-pencil')]"
                    "/ancestor-or-self::*[self::a or self::button][1]",
                )
                clicar(driver, lapis)
                esperar_ajax(driver)
                return
        # próxima página (PrimeFaces / genérico)
        proximas = [
            b for b in driver.find_elements(
                By.XPATH,
                "//*[contains(@class,'ui-paginator-next') or @aria-label='Next Page' "
                "or @aria-label='Próxima página']",
            )
            if b.is_displayed() and "disabled" not in (b.get_attribute("class") or "")
        ]
        if not proximas:
            raise RuntimeError(f"Programação {numero} não encontrada na pesquisa.")
        clicar(driver, proximas[0])
        esperar_ajax(driver)


def confirmar_alteracao_e_pesquisar(driver):
    modal = (
        "//div[(contains(@class,'ui-dialog') or contains(@class,'modal') or @role='dialog')"
        " and not(contains(@style,'display: none'))]"
    )
    clicar_por_texto(driver, "Alterar", container=modal)
    esperar_ajax(driver, timeout=60)
    clicar_por_texto(driver, "Pesquisar", timeout=60)
    esperar_ajax(driver)


# ----------------------------------------------------------------------------
def main():
    usuario = os.getenv("VIBRA_USUARIO")
    senha = os.getenv("VIBRA_SENHA")
    if not usuario or not senha:
        raise SystemExit("Defina VIBRA_USUARIO e VIBRA_SENHA.")

    params = buscar_parametros_db()
    print("Parâmetros:", params)

    opts = webdriver.ChromeOptions()
    opts.add_argument("--start-maximized")
    driver = webdriver.Chrome(options=opts)

    try:
        login(driver, usuario, senha)
        aguardar_codigo_verificacao(driver)
        abrir_consulta_fob(driver)
        pesquisar_programacoes(driver, params)
        localizar_e_alterar(driver, params["numero_programacao"])
        confirmar_alteracao_e_pesquisar(driver)
        print("Fluxo concluído.")
        input("Pressione ENTER para fechar o navegador...")
    except Exception:
        driver.save_screenshot("erro_vibra.png")
        print("Erro! Screenshot salvo em erro_vibra.png")
        raise
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
