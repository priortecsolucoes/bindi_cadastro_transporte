"""INIT — valida variáveis de ambiente, credenciais e pré-requisitos.
Nunca abre navegador nem lê dados de negócio aqui."""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from Framework.RPAFrameworkException import RPAFrameworkException
from Utils.DriverFactory import resolve_chrome_binary
from Utils.TokenApiClient import check_health

DEFAULT_TOKEN_API_URL = "https://emailtokenreader.up.railway.app"


@dataclass
class BotConfig:
    username: str
    password: str
    headless: bool
    chrome_binary_path: Optional[str]
    chrome_user_data_dir: Optional[str]
    chrome_profile_directory: Optional[str]
    chrome_user_agent: Optional[str]
    chrome_window_size: Optional[str]
    schedules_path: Optional[str]
    planned_mock_path: Optional[str]
    carrier: str
    mfa_wait_seconds: int
    token_api_url: Optional[str]
    token_api_token: Optional[str]
    token_timeout_seconds: int
    portal_api_url: str
    portal_api_token: str
    portal_pending_days: int


def initialize() -> BotConfig:
    """Carrega o .env e valida os pré-requisitos mínimos para a execução."""
    load_dotenv()

    username = os.environ.get("VIBRA_USUARIO")
    password = os.environ.get("VIBRA_SENHA")

    if not username or not password:
        raise RPAFrameworkException(
            "Defina as variáveis de ambiente VIBRA_USUARIO e VIBRA_SENHA no .env "
            "antes de executar este bot."
        )

    schedules_path = os.environ.get("PROGRAMACOES_PATH", "").strip() or None
    if schedules_path and not os.path.isfile(schedules_path):
        raise RPAFrameworkException(
            f"PROGRAMACOES_PATH está definido ({schedules_path}) mas o arquivo não existe."
        )

    planned_mock_path = os.environ.get("PLANEJADOS_MOCK_PATH", "").strip() or None
    if planned_mock_path and not os.path.isfile(planned_mock_path):
        raise RPAFrameworkException(
            f"PLANEJADOS_MOCK_PATH está definido ({planned_mock_path}) mas o arquivo não existe."
        )

    # A transportadora é sempre a própria Bindi Log - só muda se o nome no combo da Vibra mudar.
    carrier = os.environ.get("VIBRA_TRANSPORTADOR", "").strip() or "BINDI"

    portal_api_url = os.environ.get("PORTAL_API_URL", "").strip()
    portal_api_token = os.environ.get("PORTAL_API_TOKEN", "").strip()
    if not schedules_path and not planned_mock_path and (not portal_api_url or not portal_api_token):
        raise RPAFrameworkException(
            "Defina PORTAL_API_URL e PORTAL_API_TOKEN no .env - são obrigatórios para buscar "
            "os pedidos planejados no Portal Bindi Log (ou use PROGRAMACOES_PATH para testes)."
        )
    raw_pending_days = os.environ.get("PORTAL_PENDING_DAYS", "").strip() or "3"
    if not raw_pending_days.isdigit():
        raise RPAFrameworkException(
            f"PORTAL_PENDING_DAYS deve ser um inteiro não negativo; valor recebido: {raw_pending_days!r}"
        )
    portal_pending_days = int(raw_pending_days)

    raw_mfa_wait = os.environ.get("MFA_WAIT_SECONDS", "").strip() or "180"
    try:
        mfa_wait_seconds = int(raw_mfa_wait)
    except ValueError as exc:
        raise RPAFrameworkException(
            f"MFA_WAIT_SECONDS deve ser um número inteiro; valor recebido: {raw_mfa_wait!r}"
        ) from exc

    # Vibra Token API (projeto Bindi-Email): lê o código de 2FA no Gmail. Sem ela,
    # o código precisa ser digitado na janela ou gravado em mfa_code.txt.
    # VIBRA_TOKEN_API_URL=0 desliga a API (modo manual).
    raw_token_api_url = os.environ.get("VIBRA_TOKEN_API_URL", "").strip() or DEFAULT_TOKEN_API_URL
    token_api_url = None if raw_token_api_url == "0" else raw_token_api_url
    token_api_token = os.environ.get("VIBRA_TOKEN_API_TOKEN", "").strip() or None
    if token_api_url and not token_api_token:
        raise RPAFrameworkException(
            f"Defina VIBRA_TOKEN_API_TOKEN no .env (mesmo valor da variável API_TOKEN do serviço "
            f"bindi_email_token_reader no Railway) para ler o código de 2FA em {token_api_url}. "
            "Para informar o código manualmente, use VIBRA_TOKEN_API_URL=0."
        )
    raw_token_timeout = os.environ.get("TOKEN_TIMEOUT_S", "").strip() or "120"
    if not raw_token_timeout.isdigit() or int(raw_token_timeout) < 1:
        raise RPAFrameworkException(
            f"TOKEN_TIMEOUT_S deve ser um inteiro positivo; valor recebido: {raw_token_timeout!r}"
        )
    token_timeout_seconds = int(raw_token_timeout)
    if token_api_url:
        check_health(token_api_url)
        print(f"[OK] Vibra Token API disponível ({token_api_url}) - o código de 2FA será lido do e-mail.")
    else:
        print("[AVISO] VIBRA_TOKEN_API_URL=0 - o código de 2FA terá de ser informado manualmente.")

    headless = os.environ.get("CHROME_HEADLESS", "").strip() in ("1", "true", "True")
    # Perfil persistente próprio do bot: guarda os cookies do SSO da Vibra entre
    # execuções, senão o código de 2FA é pedido a cada execução (perfil temporário).
    chrome_user_data_dir = os.path.abspath(os.environ.get("CHROME_USER_DATA_DIR") or "chrome_profile")
    chrome_profile_directory = os.environ.get("CHROME_PROFILE_DIRECTORY") or None
    # Deixe CHROME_USER_AGENT vazio no .env por padrão - ver o motivo em
    # Utils/DriverFactory.create_driver (mismatch com os Client Hints).
    chrome_user_agent = os.environ.get("CHROME_USER_AGENT") or None
    chrome_window_size = os.environ.get("CHROME_WINDOW_SIZE") or None

    chrome_binary_path = resolve_chrome_binary(os.environ.get("CHROME_BINARY_PATH") or None)

    print("[OK] Ambiente validado (.env, credenciais e pré-requisitos).")

    return BotConfig(
        username=username,
        password=password,
        headless=headless,
        chrome_binary_path=chrome_binary_path,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_profile_directory=chrome_profile_directory,
        chrome_user_agent=chrome_user_agent,
        chrome_window_size=chrome_window_size,
        schedules_path=schedules_path,
        planned_mock_path=planned_mock_path,
        carrier=carrier,
        mfa_wait_seconds=mfa_wait_seconds,
        token_api_url=token_api_url,
        token_api_token=token_api_token,
        token_timeout_seconds=token_timeout_seconds,
        portal_api_url=portal_api_url,
        portal_api_token=portal_api_token,
        portal_pending_days=portal_pending_days,
    )
