"""
Automação Vibra (cn.vibraenergia.com.br) - Programação FOB > Cadastrar

Fluxo:
  menu lateral > Programação > Programação FOB > Cadastrar > Pesquisar
  > aguarda a tela de Compartimentação (pode demorar) > Pool = POSTO QUICK
  > Placa = QUT8265 > Buscar > Transportador = BINDI

Requisitos:
  pip install selenium
  (Selenium 4.6+ baixa o chromedriver automaticamente)
"""

import time

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    ElementClickInterceptedException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------- config
URL_INICIAL = "https://cn.vibraenergia.com.br/consultar-pedidos/"
POOL_TEXTO = "POSTO QUICK"
PLACA = "QUT8265"
TRANSPORTADOR_TEXTO = "BINDI"

TIMEOUT_PADRAO = 30
TIMEOUT_TELA_LENTA = 300  # a tela após "Pesquisar" às vezes demora bastante

# Para reaproveitar o login, aponte para um perfil do Chrome dedicado ao robô
# (não use o perfil que está aberto ao mesmo tempo, o Chrome bloqueia).
CHROME_PROFILE_DIR = None  # ex.: r"C:\robos\chrome-vibra"


# ---------------------------------------------------------------- helpers
def criar_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--start-maximized")
    if CHROME_PROFILE_DIR:
        opts.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    return webdriver.Chrome(options=opts)


def instalar_monitor_ajax(driver):
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


def esperar_pagina_pronta(driver, timeout=TIMEOUT_TELA_LENTA):
    """Espera document.readyState, jQuery.active e o AJAX do JSF zerarem."""
    time.sleep(0.5)  # dá tempo da requisição começar
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


def clicar(driver, by, seletor, timeout=TIMEOUT_PADRAO):
    el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, seletor)))
    try:
        el.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", el)
    return el


def selecionar_select2(driver, select_id, texto, timeout=TIMEOUT_PADRAO):
    """Seleciona no <select id=select_id> (select2) a opção cujo texto contém `texto`.

    Não usa o campo de pesquisa do select2: o JSF re-renderiza o combo enquanto se
    digita e o texto se perde (ficava só "q"). Em vez disso, define o valor via
    jQuery e dispara o 'change', que chama o onchange/AJAX do JSF normalmente.
    """
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script(
            "var s=document.getElementById(arguments[0]);"
            "return !!(window.jQuery && s && s.options.length > 1);",
            select_id,
        )
    )
    escolhido = driver.execute_script(
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
        texto,
    )
    if not escolhido:
        raise RuntimeError(f"Opção contendo '{texto}' não encontrada em #{select_id}")
    print(f"   -> selecionado: {escolhido}")


# ---------------------------------------------------------------- fluxo
def main():
    driver = criar_driver()
    wait = WebDriverWait(driver, TIMEOUT_PADRAO)

    try:
        driver.get(URL_INICIAL)

        # Se não estiver logado, faça o login manualmente na janela aberta
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "button.iconMenu"))
            )
        except TimeoutException:
            input("Faça o login no navegador e, na tela de pedidos, tecle ENTER aqui...")
            driver.get(URL_INICIAL)

        # 1) Abrir menu lateral
        print("1. Abrindo menu lateral")
        clicar(driver, By.CSS_SELECTOR, "button.iconMenu")

        # 2) Programação
        print("2. Programação")
        clicar(driver, By.XPATH,
               "//button[contains(@class,'mat-mdc-menu-item')][.//span[normalize-space()='Programação']]")

        # 3) Programação FOB > Cadastrar
        print("3. Programação FOB > Cadastrar")
        clicar(driver, By.XPATH,
               "//button[contains(@class,'mat-mdc-menu-item')][.//span[normalize-space()='Programação FOB']]")
        clicar(driver, By.XPATH,
               "//a[contains(@class,'mat-mdc-menu-item')][normalize-space()='Cadastrar']")

        # Se o link abrir em outra aba, muda para ela
        time.sleep(1)
        if len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])

        # 4) Pesquisar (tela "Pesquisa de Remessas")
        print("4. Pesquisar")
        WebDriverWait(driver, TIMEOUT_TELA_LENTA).until(
            EC.presence_of_element_located((By.ID, "forumlarioPesquisa:comboBase"))
        )
        esperar_pagina_pronta(driver)
        # há 2 botões "Pesquisar" (um oculto num modal); pega o visível
        pesquisar = WebDriverWait(driver, TIMEOUT_PADRAO).until(
            lambda d: next(
                (b for b in d.find_elements(By.CSS_SELECTOR, "input[type='submit'][value='Pesquisar']")
                 if b.is_displayed()),
                False,
            )
        )
        pesquisar.click()

        # 5) Esperar a tela de Compartimentação carregar (às vezes demora)
        print("5. Aguardando a tela carregar (pode demorar)...")
        WebDriverWait(driver, TIMEOUT_TELA_LENTA, poll_frequency=1).until(
            EC.presence_of_element_located((By.ID, "comboPool"))
        )
        esperar_pagina_pronta(driver)
        instalar_monitor_ajax(driver)

        # 6) Pool = POSTO QUICK
        print(f"6. Pool: {POOL_TEXTO}")
        selecionar_select2(driver, "comboPool", POOL_TEXTO)
        esperar_pagina_pronta(driver)

        # 7) Placa
        print(f"7. Placa: {PLACA}")
        xpath_placa = "//input[@value='Buscar']/ancestor::div[1]//input[@type='text']"
        for _ in range(5):  # o JSF pode re-renderizar o campo (stale)
            try:
                placa = wait.until(EC.element_to_be_clickable((By.XPATH, xpath_placa)))
                placa.clear()
                placa.send_keys(PLACA)
                time.sleep(0.5)
                if placa.get_attribute("value") == PLACA:
                    break
            except StaleElementReferenceException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Não consegui preencher a placa")

        # 8) Buscar
        print("8. Buscar")
        clicar(driver, By.XPATH, "//input[@type='submit' and @value='Buscar']")
        esperar_pagina_pronta(driver)

        # 9) Transportador = BINDI
        print(f"9. Transportador: {TRANSPORTADOR_TEXTO}")
        WebDriverWait(driver, TIMEOUT_TELA_LENTA).until(
            EC.presence_of_element_located((By.ID, "cmbTranspp"))
        )
        selecionar_select2(driver, "cmbTranspp", TRANSPORTADOR_TEXTO)
        esperar_pagina_pronta(driver)

        print("Concluído.")
        input("ENTER para fechar o navegador...")

    except Exception:
        driver.save_screenshot("erro_vibra.png")
        print("Erro! Screenshot salvo em erro_vibra.png")
        raise
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
