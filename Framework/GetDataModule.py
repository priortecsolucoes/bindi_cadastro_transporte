"""GET_DATA — programações a registrar no Canal de Negócios (Programação FOB):
por padrão, as viagens planejadas no Portal Bindi Log, uma programação por
viagem + posto (pool). Overrides para testes: PLANEJADOS_MOCK_PATH (JSON no
mesmo formato da resposta do portal - simula viagens planejadas) e
PROGRAMACOES_PATH (CSV local, colunas pool, placa e opcionalmente transportador).
Nunca escreve."""

import csv
import json
import os
from typing import Dict, List, Optional

from Framework.InitModule import BotConfig
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import STATUS_ERROR, STATUS_ERROR_REPORTED, STATUS_FINALIZED, output_path
from Utils.PortalClient import fetch_planned_trips

REQUIRED_CSV_COLUMNS = ("pool", "placa")


def get_data(config: BotConfig) -> List[dict]:
    """Cada item tem "pool", "plate" e "carrier"; itens do portal também trazem
    "tripId", "loadDate", "loadTime", "trailerPlates", "driverName", "driverTaxId",
    "orders" (pedidos do posto na viagem, com itens e compartimentos),
    "reportToPortal" (False no mock), "technicalErrors" (erros técnicos seguidos
    no histórico) e, se já cadastrada na Vibra, "alreadyFinalized"."""
    if config.schedules_path:
        return _load_from_csv(config.schedules_path, config.carrier)

    if config.planned_mock_path:
        trips = _load_mock_trips(config.planned_mock_path)
    else:
        trips = fetch_planned_trips(config.portal_api_url, config.portal_api_token, config.portal_pending_days)
    items: List[dict] = []
    for trip in trips:
        orders_by_pool: Dict[str, List[dict]] = {}
        for order in trip.get("orders", []):
            pool = order.get("stationDistributorName") or order.get("stationName")
            if not pool:
                print(f"[AVISO] Viagem {trip.get('tripId')}, pedido {order.get('orderNumber')}: portal não informou o posto - ignorado.")
                continue
            orders_by_pool.setdefault(pool, []).append(order)

        for pool, orders in orders_by_pool.items():
            items.append(
                {
                    **_build_item(pool, trip["tractorPlate"], config.carrier),
                    "tripId": trip["tripId"],
                    "loadDate": trip.get("loadDate"),
                    "loadTime": trip.get("loadTime"),
                    "trailerPlates": trip.get("trailerPlates", []),
                    "driverName": trip.get("driverName"),
                    "driverTaxId": trip.get("driverTaxId"),
                    "orders": orders,
                }
            )

    history_path = output_path()
    history = _load_history(history_path)
    from_portal = not config.planned_mock_path
    pending: List[dict] = []
    finalized_count = 0
    for item in items:
        key_history = history.get(_item_key(item["tripId"], item["pool"]), {})
        item["reportToPortal"] = from_portal
        item["technicalErrors"] = key_history.get("technical_errors", 0)
        if key_history.get("finalized"):
            finalized_count += 1
            if not from_portal:
                continue
            # Já está na Vibra, mas o portal ainda devolve a viagem: o retorno anterior
            # não chegou. Só reenvia o resultado - cadastrar de novo duplicaria na Vibra.
            item["alreadyFinalized"] = True
        pending.append(item)

    if finalized_count:
        action = "só o resultado será reenviado ao portal" if from_portal else "ignorada(s)"
        print(
            f"[INFO] {finalized_count} programação(ões) já FINALIZADA(S) na Vibra - {action} "
            f"(histórico em {history_path})."
        )
    print(f"[OK] {len(pending) - (finalized_count if from_portal else 0)} programação(ões) a registrar na Vibra.")
    return pending


def _item_key(trip_id, pool: str) -> tuple:
    return str(trip_id).strip(), (pool or "").strip().upper()


def _load_history(history_path: str) -> Dict[tuple, dict]:
    """Por viagem + pool: se já foi FINALIZADA na Vibra (nunca cadastrar de novo)
    e quantos erros técnicos seguidos teve desde o último resultado enviado ao
    portal - depois de MAX_TECHNICAL_ATTEMPTS o erro vai para o portal em vez de
    repetir a cada ciclo."""
    if not os.path.isfile(history_path):
        return {}
    history: Dict[tuple, dict] = {}
    try:
        with open(history_path, newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                if not row.get("trip_id"):
                    continue
                entry = history.setdefault(
                    _item_key(row["trip_id"], row.get("pool", "")), {"finalized": False, "technical_errors": 0}
                )
                status = row.get("status")
                if status == STATUS_FINALIZED:
                    entry["finalized"] = True
                elif status == STATUS_ERROR:
                    entry["technical_errors"] += 1
                elif status == STATUS_ERROR_REPORTED:
                    entry["technical_errors"] = 0
        return history
    except OSError as exc:
        raise RPAFrameworkException(
            f"Falha ao ler o histórico {history_path} (aberto no Excel?) - sem ele não dá para "
            f"evitar cadastro duplicado na Vibra: {exc}"
        ) from exc


def _load_mock_trips(mock_path: str) -> List[dict]:
    try:
        with open(mock_path, encoding="utf-8-sig") as file:
            trips = json.load(file).get("trips", [])
    except (OSError, ValueError, AttributeError) as exc:
        raise RPAFrameworkException(f"Falha ao ler o mock de viagens planejadas {mock_path}: {exc}") from exc
    for trip in trips:
        if not trip.get("tripId") or not trip.get("tractorPlate"):
            raise RPAFrameworkException(f"{mock_path}: toda viagem precisa de tripId e tractorPlate ({trip}).")
    print(f"[AVISO] SIMULAÇÃO: {len(trips)} viagem(ns) lida(s) de {mock_path} (PLANEJADOS_MOCK_PATH) - portal não consultado.")
    return trips


def _load_from_csv(schedules_path: str, default_carrier: str) -> List[dict]:
    try:
        with open(schedules_path, newline="", encoding="utf-8-sig") as file:
            header = file.readline()
            file.seek(0)
            # CSV salvo pelo Excel em pt-BR usa ';'
            delimiter = ";" if ";" in header else ","
            reader = csv.DictReader(file, delimiter=delimiter)
            columns = {(name or "").strip().lower() for name in reader.fieldnames or []}
            missing = [column for column in REQUIRED_CSV_COLUMNS if column not in columns]
            if missing:
                raise RPAFrameworkException(
                    f"{schedules_path}: coluna(s) obrigatória(s) ausente(s): {', '.join(missing)}."
                )
            rows = [{(key or "").strip().lower(): (value or "").strip() for key, value in row.items()} for row in reader]
    except OSError as exc:
        raise RPAFrameworkException(f"Falha ao ler {schedules_path}: {exc}") from exc

    items: List[dict] = []
    for line_number, row in enumerate(rows, start=2):
        if not any(row.values()):
            continue
        if not all(row.get(column) for column in REQUIRED_CSV_COLUMNS):
            print(f"[AVISO] {schedules_path} linha {line_number}: pool e placa são obrigatórios - ignorada.")
            continue
        items.append(_build_item(row["pool"], row["placa"], row.get("transportador") or default_carrier))

    print(f"[OK] {len(items)} programação(ões) carregada(s) de {schedules_path} (override PROGRAMACOES_PATH).")
    return items


def _build_item(pool: str, plate: str, carrier: Optional[str]) -> dict:
    return {
        "pool": pool.strip(),
        "plate": plate.strip().upper().replace("-", ""),
        "carrier": (carrier or "").strip(),
    }
