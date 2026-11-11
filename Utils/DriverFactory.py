"""Criação do Chrome WebDriver com ajustes anti-detecção.

Todos os ajustes anti-detecção ficam concentrados aqui e são parametrizáveis
via .env (ver Framework/InitModule.py) — nenhum valor de fingerprint fica
hardcoded fora deste módulo.
"""

import json
import os
import subprocess
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService

from Framework.RPAFrameworkException import RPAFrameworkException

# Script injetado antes de qualquer JS da página (CDP
# Page.addScriptToEvaluateOnNewDocument), reunindo os sinais mais comuns que
# ferramentas anti-bot (Akamai Bot Manager incluso) usam para identificar o
# ChromeDriver:
#   - navigator.webdriver         -> true por padrão no ChromeDriver
#   - navigator.languages         -> vazio/inconsistente em alguns setups
#   - navigator.plugins           -> lista vazia é atípica de um Chrome real
#   - window.chrome               -> ausente quando o Chrome roda via CDP puro
#   - navigator.permissions.query -> comportamento diferente do Chrome real p/ notifications
# Isso reduz o sinal, mas não elimina fingerprinting mais avançado. Para
# bloqueios persistentes, reaproveitar um perfil real do Chrome
# (CHROME_USER_DATA_DIR/CHROME_PROFILE_DIRECTORY) continua sendo a mitigação
# mais eficaz.
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const originalPermissionsQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalPermissionsQuery(parameters)
);
"""


def resolve_chrome_binary(explicit_path: Optional[str]) -> Optional[str]:
    """Valida CHROME_BINARY_PATH quando definido. Se não for definido,
    devolve None e deixa o próprio ChromeDriver/Selenium Manager localizar o
    Chrome instalado - mais robusto entre máquinas do que uma lista fixa de
    caminhos prováveis."""
    if explicit_path and not os.path.isfile(explicit_path):
        raise RPAFrameworkException(
            f"CHROME_BINARY_PATH aponta para um arquivo inexistente: {explicit_path}"
        )
    return explicit_path


_headless_user_agent: Optional[str] = None


def _resolve_headless_user_agent(chrome_binary_path: Optional[str]) -> str:
    """User-Agent real do Chrome instalado com 'HeadlessChrome' trocado por 'Chrome'.
    A Akamai da Vibra responde 'Access Denied' só por causa dessa marca no headless
    (confirmado). Lido de um Chrome descartável, sem perfil, para acompanhar a
    versão instalada; o resto do User-Agent e os Client Hints já batem com ela.
    Uma vez por processo - o loop do main.py recria o driver a cada ciclo."""
    global _headless_user_agent
    if _headless_user_agent:
        return _headless_user_agent

    probe_options = webdriver.ChromeOptions()
    probe_options.add_argument("--headless=new")
    probe_options.add_argument("--no-sandbox")
    probe_options.add_argument("--disable-dev-shm-usage")
    if chrome_binary_path:
        probe_options.binary_location = chrome_binary_path
    probe = webdriver.Chrome(options=probe_options, service=ChromeService(log_output=subprocess.DEVNULL))
    try:
        real_user_agent = probe.execute_script("return navigator.userAgent")
    finally:
        probe.quit()

    _headless_user_agent = real_user_agent.replace("HeadlessChrome", "Chrome")
    return _headless_user_agent


_chrome_diagnosed = False


def _diagnose_chrome_binary(chrome_binary_path: Optional[str]) -> None:
    """Roda o binário do Chrome direto, fora do Selenium - o ChromeDriver só
    repassa um 'Chrome instance exited' genérico quando o processo morre na
    inicialização. Uma vez por processo; se o Chrome sobe, vira uma linha [OK].
    O stderr só é mostrado em caso de falha: com sucesso ele só tem avisos
    inofensivos de container (dbus etc.) que parecem erro no log."""
    global _chrome_diagnosed
    if _chrome_diagnosed:
        return
    _chrome_diagnosed = True

    # 'google-chrome' só existe no container Linux; no Windows o chrome.exe nem imprime --version.
    if os.name == "nt":
        return

    binary = chrome_binary_path or "google-chrome"
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
        smoke_test = subprocess.run(
            [binary, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--log-level=3",
             "--dump-dom", "about:blank"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - diagnóstico não pode derrubar a execução
        print(f"[AVISO] Não foi possível rodar o Chrome ({binary}) para diagnóstico: {exc}")
        return

    if version.returncode == 0 and smoke_test.returncode == 0:
        print(f"[OK] Chrome pronto: {version.stdout.strip() or binary}")
        return
    print(f"[AVISO] Chrome não abriu no diagnóstico ({binary}): versão exit={version.returncode}, "
          f"teste headless exit={smoke_test.returncode}.")
    details = (smoke_test.stderr or version.stderr or "").strip()
    if details:
        print(f"[AVISO] Saída do Chrome: {details[-1500:]}")


def create_driver(
    headless: bool = False,
    user_data_dir: Optional[str] = None,
    profile_directory: Optional[str] = None,
    user_agent: Optional[str] = None,
    window_size: Optional[str] = None,
    chrome_binary_path: Optional[str] = None,
) -> webdriver.Chrome:
    """Cria um Chrome WebDriver com ajustes que reduzem a chance de o portal
    (protegido por Akamai Bot Manager) responder 'Access Denied'.

    O ChromeDriver é quem abre o processo do Chrome (caminho padrão e mais
    testado do Selenium). Uma versão anterior deste módulo lançava o Chrome
    manualmente e conectava via depuração remota só para esconder o aviso
    "Chrome está sendo controlado por um software de teste automatizado" -
    mas isso se mostrou instável fora deste ambiente de desenvolvimento (o
    processo morria silenciosamente antes de abrir a porta de depuração, sem
    log algum). O aviso volta a aparecer com essa mudança, mas é puramente
    cosmético: não afeta navigator.webdriver (que o patch CDP abaixo trata) nem
    o funcionamento da automação.

    Parameters
    ----------
    headless : bool
        Em headless o User-Agent leva 'HeadlessChrome' e a Akamai bloqueia;
        sem `user_agent` explícito, ele é trocado pelo do Chrome normal
        (ver _resolve_headless_user_agent).
    user_data_dir, profile_directory : str, opcional
        Perfil real do Chrome (cookies `_abck`/`bm_sz` já validados pela
        Akamai nesse IP). É a mitigação mais eficaz contra o bloqueio; o
        Chrome não pode estar aberto com esse perfil ao rodar o script.
    user_agent : str, opcional
        Só defina se precisar forçar um valor específico. Por padrão (None)
        o Chrome usa o seu próprio User-Agent real (sem 'Headless' no headless) — sobrescrever via
        `--user-agent` muda `navigator.userAgent`, mas NÃO atualiza os
        Client Hints (`sec-ch-ua`, `sec-ch-ua-platform`, ...), que continuam
        refletindo a versão real do Chrome instalado. Um User-Agent que não
        bate com esses headers é, por si só, um sinal de automação.
    window_size : str, opcional
        Formato "LARGURA,ALTURA" (ex.: "1920,1080").
    chrome_binary_path : str, opcional
        Caminho do chrome.exe. Só use se precisar apontar para uma instalação
        específica; por padrão o ChromeDriver localiza o Chrome sozinho.
    """
    options = webdriver.ChromeOptions()

    # Não usar excludeSwitches=["enable-automation"] nem useAutomationExtension=False:
    # em versões recentes do Chrome (152+) isso derruba a sessão na hora
    # ("Chrome instance exited") - confirmado empiricamente.
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-infobars")
    options.add_argument("--lang=pt-BR")

    # Evita popups/diálogos que não apareceriam num uso manual normal do
    # perfil (senha salva, notificações) e que podem atrapalhar os seletores.
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option(
        "prefs",
        {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.default_content_setting_values.notifications": 2,
        },
    )

    if chrome_binary_path:
        options.binary_location = chrome_binary_path

    if not user_agent and headless:
        try:
            user_agent = _resolve_headless_user_agent(chrome_binary_path)
        except Exception as exc:
            raise RPAFrameworkException(
                "Não foi possível ler o User-Agent do Chrome para o modo headless "
                f"(sem ele a Vibra responde 'Access Denied'): {exc}"
            ) from exc

    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
        if profile_directory:
            options.add_argument(f"--profile-directory={profile_directory}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument(f"--window-size={window_size or '1920,1080'}")
        # Só entram junto do headless porque hoje é exatamente o caso "rodando
        # sem supervisão" (container Railway) - localmente com janela (não
        # headless) não precisa disso.
        # --no-sandbox: o Dockerfile roda como root (sem USER definido) e o
        # Chrome recusa o próprio sandbox nesse caso dentro de um container -
        # sem essa flag o processo simplesmente morre na hora
        # (SessionNotCreatedException "Chrome instance exited", confirmado).
        # --disable-dev-shm-usage: o /dev/shm padrão do Docker é só 64MB,
        # pequeno demais pro Chrome e causa o mesmo tipo de crash - manda usar
        # /tmp (disco) em vez de shared memory.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Só mensagens fatais do Chrome: no container ele imprime avisos de dbus
        # a cada abertura, que são inofensivos mas parecem erro no log.
        options.add_argument("--log-level=3")
        _diagnose_chrome_binary(chrome_binary_path)
    elif window_size:
        options.add_argument(f"--window-size={window_size}")

    # Saída do chromedriver descartada pelo mesmo motivo; falhas dele chegam como exceção.
    driver = webdriver.Chrome(options=options, service=ChromeService(log_output=subprocess.DEVNULL))

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _STEALTH_SCRIPT},
        )
    except Exception:
        print("[AVISO] Não foi possível aplicar o patch CDP anti-detecção.")

    return driver



def load_cookies(driver: webdriver.Chrome, path: str) -> None:
    """Restaura os cookies salvos na execução anterior (todos os domínios, via CDP).
    Os cookies do SSO são de sessão - o Chrome os descarta ao fechar, por isso
    ficam num arquivo próprio em vez de depender do perfil."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as file:
            cookies = json.load(file)
        driver.execute_cdp_cmd("Network.setCookies", {"cookies": cookies})
        print(f"[INFO] {len(cookies)} cookie(s) da sessão anterior restaurado(s) de {path}.")
    except Exception as exc:  # noqa: BLE001 - sem cookies o login só volta a pedir 2FA
        print(f"[AVISO] Falha ao restaurar cookies de {path} (seguindo sem eles): {exc}")


def save_cookies(driver: webdriver.Chrome, path: str) -> None:
    try:
        cookies = driver.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]
        with open(path, "w", encoding="utf-8") as file:
            json.dump(cookies, file)
    except Exception as exc:  # noqa: BLE001 - não pode mascarar o resultado da execução
        print(f"[AVISO] Falha ao salvar cookies em {path}: {exc}")
