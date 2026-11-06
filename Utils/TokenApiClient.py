"""Cliente HTTP da Vibra Token API (projeto Bindi-Email): lê no Gmail o código de
2FA que a Vibra envia por e-mail. Falha de conexão/autenticação é pré-requisito
faltando - erro controlado, interrompe a execução."""

from typing import Optional

import requests

from Framework.RPAFrameworkException import RPAFrameworkException

REQUEST_TIMEOUT_SECONDS = 30


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def check_health(base_url: str) -> None:
    url = f"{base_url.rstrip('/')}/health"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RPAFrameworkException(f"Vibra Token API fora do ar ({url}): {exc}") from exc
    if response.status_code != 200:
        raise RPAFrameworkException(f"Vibra Token API respondeu HTTP {response.status_code} em {url}.")


def prepare_token(base_url: str, token: str) -> int:
    """POST /token/prepare - chamar ANTES de enviar usuário/senha. Devolve o ticket
    (último UID de e-mail da Vibra), que separa o código deste login dos antigos."""
    url = f"{base_url.rstrip('/')}/token/prepare"
    try:
        response = requests.post(url, headers=_headers(token), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RPAFrameworkException(f"Falha ao conectar na Vibra Token API ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RPAFrameworkException(
            f"Vibra Token API recusou o preparo do token (HTTP {response.status_code}): {response.text[:300]}"
        )
    return response.json()["ticket"]


def fetch_token(base_url: str, token: str, ticket: Optional[int], timeout_seconds: int) -> str:
    """GET /token - long-poll: bloqueia até o e-mail com o código chegar.
    Levanta RuntimeError se o código não chegar no prazo."""
    url = f"{base_url.rstrip('/')}/token"
    params = {"timeout_seconds": timeout_seconds}
    if ticket is not None:
        params["ticket"] = ticket
    try:
        # Timeout HTTP maior que o long-poll, senão o cliente desiste antes da API responder.
        response = requests.get(
            url, headers=_headers(token), params=params, timeout=timeout_seconds + REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RPAFrameworkException(f"Falha ao conectar na Vibra Token API ({url}): {exc}") from exc

    if response.status_code == 404:
        raise RuntimeError(f"Código de verificação não chegou em {timeout_seconds}s (ticket {ticket}).")
    if response.status_code != 200:
        raise RPAFrameworkException(
            f"Vibra Token API recusou a busca do token (HTTP {response.status_code}): {response.text[:300]}"
        )
    return response.json()["token"]
