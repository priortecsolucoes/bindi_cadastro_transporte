"""Cliente HTTP do Portal Bindi Log (integração RPA Vibra - cadastro de transporte).
Busca as viagens já planejadas e liberadas pelo Analista (conjunto, motorista,
horário do carregamento VIBRA e pedidos alocados), que o agente precisa
registrar na Programação FOB do Canal de Negócios, e devolve o resultado de
cada programação. Falha de conexão/autenticação na busca é pré-requisito
faltando - erro controlado, interrompe a execução."""

from typing import List, Optional

import requests

from Framework.RPAFrameworkException import RPAFrameworkException

REQUEST_TIMEOUT_SECONDS = 30


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_planned_trips(base_url: str, token: str, days_ahead: int) -> List[dict]:
    """GET /api/rpa/vibra/planejados - viagens liberadas com carregamento VIBRA
    cujos compartimentos ainda estão 'Programado', com data de carga entre hoje
    e hoje + days_ahead dias. Cada viagem traz tractorPlate, trailerPlates,
    driverName, driverTaxId, loadDate, loadTime e orders[] (orderNumber,
    distributorOrderNumber, stationName, stationDistributorName, items[])."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/planejados"
    try:
        response = requests.get(
            url, headers=_headers(token), params={"days": days_ahead}, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RPAFrameworkException(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RPAFrameworkException(
            f"Portal Bindi Log recusou a busca de viagens planejadas (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )

    try:
        trips = response.json().get("trips", [])
    except ValueError as exc:
        raise RPAFrameworkException(f"Resposta inválida do Portal Bindi Log ({url}): {response.text[:300]}") from exc

    print(f"[OK] {len(trips)} viagem(ns) planejada(s) recebida(s) do Portal Bindi Log.")
    return trips


def report_registration(
    base_url: str,
    token: str,
    trip_id: int,
    order_results: List[dict],
    distributor_message: Optional[str] = None,
) -> dict:
    """POST /api/rpa/vibra/programacoes - order_results é uma lista de
    {"orderId", "registered", "observation"?}. Cadastrado vira 'Registrado na
    distribuidora' no portal; com erro vira 'Erro no agendamento' com a
    observação em aberto, e a viagem só volta depois que o usuário alterá-la.
    Levanta RuntimeError (erro do item, não da execução)."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/programacoes"
    payload = {"tripId": trip_id, "distributorMessage": distributor_message or None, "orders": order_results}
    try:
        response = requests.post(url, headers=_headers(token), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Portal Bindi Log recusou o resultado da programação da viagem {trip_id} "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )
    return response.json()
