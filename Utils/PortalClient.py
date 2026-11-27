"""Cliente HTTP do Portal Bindi Log (integração RPA Vibra - cadastro de transporte).
Busca as viagens já planejadas e liberadas pelo Analista (conjunto, motorista,
horário do carregamento VIBRA e pedidos alocados), que o agente precisa
registrar na Programação FOB do Canal de Negócios, e devolve o resultado de
cada programação; também a fila de edição (viagens já cadastradas que foram
alteradas no portal). Falha de conexão/autenticação na busca é pré-requisito
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


def fetch_edit_trips(base_url: str, token: str) -> List[dict]:
    """GET /api/rpa/vibra/edicoes - fila de edição: viagens já cadastradas na Vibra
    que o usuário reabriu, alterou e liberou de novo. Mesmo formato de /planejados,
    com isEdit = true, editRequestId e distributorReference (programação a alterar)."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/edicoes"
    try:
        response = requests.get(url, headers=_headers(token), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RPAFrameworkException(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RPAFrameworkException(
            f"Portal Bindi Log recusou a busca da fila de edição (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )

    try:
        trips = response.json().get("trips", [])
    except ValueError as exc:
        raise RPAFrameworkException(f"Resposta inválida do Portal Bindi Log ({url}): {response.text[:300]}") from exc

    print(f"[OK] {len(trips)} viagem(ns) na fila de edição do Portal Bindi Log.")
    return trips


def report_edit(
    base_url: str,
    token: str,
    edit_request_id: int,
    trip_id: int,
    edited: bool,
    observation: Optional[str] = None,
    technical_detail: Optional[str] = None,
    distributor_message: Optional[str] = None,
    schedule_number: Optional[str] = None,
    scheduled_time: Optional[str] = None,
) -> dict:
    """POST /api/rpa/vibra/edicoes - resultado da edição de uma programação já
    cadastrada. edited=True: o portal fecha o pedido de edição e marca todos os
    pedidos Vibra da viagem como cadastrados (selo REG com schedule_number).
    edited=False: observation (obrigatória) vai para o usuário; a viagem só volta
    para a fila depois que ele corrigir e liberar de novo ou pedir "Tentar de novo".
    Levanta RuntimeError (erro do item, não da execução)."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/edicoes"
    payload = {
        "editRequestId": edit_request_id,
        "tripId": trip_id,
        "edited": edited,
        "observation": observation or None,
        "technicalDetail": technical_detail or None,
        "distributorMessage": distributor_message or None,
        "distributorReference": schedule_number or None,
        "scheduledTime": scheduled_time or None,
    }
    try:
        response = requests.post(url, headers=_headers(token), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Portal Bindi Log recusou o resultado da edição {edit_request_id} da viagem {trip_id} "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )
    return response.json()


def report_registration(
    base_url: str,
    token: str,
    trip_id: int,
    order_results: List[dict],
    distributor_message: Optional[str] = None,
    schedule_number: Optional[str] = None,
    scheduled_time: Optional[str] = None,
) -> dict:
    """POST /api/rpa/vibra/programacoes - order_results é uma lista de
    {"orderId", "registered", "observation"?, "technicalDetail"?}; schedule_number é o
    número da Programação FOB na Vibra e scheduled_time ("HH:MM") o horário que a Vibra
    aceitou (pode ser o livre mais próximo do planejado). O portal grava o resultado só na viagem
    (o status do pedido não muda): cadastrada sai da fila do agente; com erro fica
    em aberto até o usuário corrigir a viagem ou pedir "Tentar de novo".
    Levanta RuntimeError (erro do item, não da execução)."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/programacoes"
    payload = {
        "tripId": trip_id,
        "distributorMessage": distributor_message or None,
        "distributorReference": schedule_number or None,
        "scheduledTime": scheduled_time or None,
        "orders": order_results,
    }
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


def fetch_deletion_requests(base_url: str, token: str) -> List[dict]:
    """GET /api/rpa/vibra/exclusoes - fila de exclusão: programações de viagens
    excluídas no portal ("Excluir quadro") que precisam ser excluídas na Vibra.
    Cada item: deletionRequestId, tripId, distributorReference (nº da programação),
    loadDate, tractorPlate, requestedAt."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/exclusoes"
    try:
        response = requests.get(url, headers=_headers(token), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RPAFrameworkException(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RPAFrameworkException(
            f"Portal Bindi Log recusou a busca da fila de exclusão (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )

    try:
        deletions = response.json().get("deletions", [])
    except ValueError as exc:
        raise RPAFrameworkException(f"Resposta inválida do Portal Bindi Log ({url}): {response.text[:300]}") from exc

    print(f"[OK] {len(deletions)} programação(ões) na fila de exclusão do Portal Bindi Log.")
    return deletions


def report_deletion(
    base_url: str,
    token: str,
    deletion_request_id: int,
    deleted: bool,
    observation: Optional[str] = None,
    technical_detail: Optional[str] = None,
) -> dict:
    """POST /api/rpa/vibra/exclusoes - resultado da exclusão de uma programação.
    deleted=True também quando ela já não estava na Vibra (observation explica).
    Levanta RuntimeError (erro do item, não da execução)."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/exclusoes"
    payload = {
        "deletionRequestId": deletion_request_id,
        "deleted": deleted,
        "observation": observation or None,
        "technicalDetail": technical_detail or None,
    }
    try:
        response = requests.post(url, headers=_headers(token), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Portal Bindi Log recusou o resultado da exclusão {deletion_request_id} "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )
    return response.json()


def report_processing(base_url: str, token: str, trip_id: int, processing: bool, action: str) -> None:
    """POST /api/rpa/vibra/processando - liga (processing=True, ao começar a viagem) ou
    desliga o selo PROCESSANDO da viagem no quadro. action = 'register' ou 'edit'.
    Levanta RuntimeError; quem chama só avisa (o selo não pode parar o cadastro)."""
    url = f"{base_url.rstrip('/')}/api/rpa/vibra/processando"
    payload = {"tripId": trip_id, "processing": processing, "action": action}
    try:
        response = requests.post(url, headers=_headers(token), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"Falha ao conectar no Portal Bindi Log ({url}): {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"Portal Bindi Log recusou o status de processamento da viagem {trip_id} "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )
