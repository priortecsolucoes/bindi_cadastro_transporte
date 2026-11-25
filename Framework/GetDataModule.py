"""GET_DATA — programações a registrar no Canal de Negócios (Programação FOB):
por padrão, as viagens planejadas no Portal Bindi Log, uma programação por
viagem (compartimentos de postos diferentes entram na mesma programação, trocando
o Pool na Vibra). O histórico continua por viagem + posto. Overrides para testes: PLANEJADOS_MOCK_PATH (JSON no
mesmo formato da resposta do portal - simula viagens planejadas) e
PROGRAMACOES_PATH (CSV local, colunas pool, placa e opcionalmente transportador).
Depois dos cadastros vem a fila de edição do portal (GET /api/rpa/vibra/edicoes,
ou EDICOES_MOCK_PATH): viagens já cadastradas que foram alteradas, uma por
viagem, com "isEdit". Nunca escreve."""

import csv
import json
import os
from typing import Dict, List, Optional

from Framework.InitModule import BotConfig
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import (
    EDIT_REQUEST_IN_DETAIL,
    SCHEDULE_DELETED_IN_DETAIL,
    SCHEDULE_NUMBER_IN_DETAIL,
    SCHEDULED_TIME_IN_DETAIL,
    STATUS_EDITED,
    STATUS_ERROR,
    STATUS_ERROR_REPORTED,
    STATUS_FINALIZED,
    STATUS_SCHEDULE_DELETED,
    output_path,
)
from Utils.PortalClient import fetch_edit_trips, fetch_planned_trips

REQUIRED_CSV_COLUMNS = ("pool", "placa")


def get_data(config: BotConfig) -> List[dict]:
    """Cada item tem "pool" (postos da programação, para o log), "pools"
    ([{"pool", "orders"}] - um por posto), "plate" (1ª carreta), "carrier" e, do portal,
    "plates" (carretas do conjunto, tentadas na ordem); itens do portal também trazem
    "tripId", "loadDate", "loadTime", "tractorPlate", "trailerPlates", "driverName", "driverTaxId",
    "orders" (pedidos da viagem, com itens, compartimentos e "vibraPool"),
    "reportToPortal" (False no mock) e "technicalErrors" (erros técnicos seguidos
    no histórico). Posto já FINALIZADO na viagem vem num item próprio, com
    "alreadyFinalized" e "scheduleNumber", só para reenviar o resultado ao portal.
    Os itens da fila de edição vêm por último, com "isEdit", "editRequestId" e
    "scheduleNumber" (programação a alterar); edição já EDITADA no histórico vem
    com "alreadyEdited", só para reenviar o resultado."""
    if config.schedules_path:
        return _load_from_csv(config.schedules_path, config.carrier)

    if config.planned_mock_path:
        trips = _load_mock_trips(config.planned_mock_path)
    else:
        trips = fetch_planned_trips(config.portal_api_url, config.portal_api_token, config.portal_pending_days)
    items: List[dict] = []
    for trip in trips:
        plates = _vibra_plates(trip)
        orders_by_pool: Dict[str, List[dict]] = {}
        for order in trip.get("orders", []):
            pool = order.get("stationDistributorName") or order.get("stationName")
            if not pool:
                print(f"[AVISO] Viagem {trip.get('tripId')}, pedido {order.get('orderNumber')}: portal não informou o posto - ignorado.")
                continue
            orders_by_pool.setdefault(pool, []).append({**order, "vibraPool": pool})

        for pool, orders in orders_by_pool.items():
            items.append(
                {
                    **_build_item(pool, plates[0], config.carrier),
                    "plates": plates,
                    "tripId": trip["tripId"],
                    "loadDate": trip.get("loadDate"),
                    "loadTime": trip.get("loadTime"),
                    "tractorPlate": trip.get("tractorPlate"),
                    "trailerPlates": trip.get("trailerPlates", []),
                    "driverName": trip.get("driverName"),
                    "driverTaxId": trip.get("driverTaxId"),
                    "orders": orders,
                    "pools": [{"pool": pool, "orders": orders}],
                }
            )

    history_path = output_path()
    history = _load_history(history_path)
    from_portal = not config.planned_mock_path
    pending: List[dict] = []
    trips_to_register: Dict[str, dict] = {}
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
            item["scheduleNumber"] = key_history.get("schedule_number", "")
            item["scheduledTime"] = key_history.get("scheduled_time", "")
            pending.append(item)
            continue
        _merge_into_trip(trips_to_register, item)
    pending.extend(trips_to_register.values())

    if finalized_count:
        action = "só o resultado será reenviado ao portal" if from_portal else "ignorada(s)"
        print(
            f"[INFO] {finalized_count} programação(ões) já FINALIZADA(S) na Vibra - {action} "
            f"(histórico em {history_path})."
        )
    print(f"[OK] {len(trips_to_register)} programação(ões) a registrar na Vibra.")
    return pending + _load_edit_items(config, history)


def _load_edit_items(config: BotConfig, history: Dict[tuple, dict]) -> List[dict]:
    """Fila de edição: uma programação por viagem (todos os postos), como no cadastro.
    Em simulação (PLANEJADOS_MOCK_PATH) o portal não é consultado; EDICOES_MOCK_PATH
    simula a fila."""
    if config.edit_mock_path:
        trips = _load_mock_trips(config.edit_mock_path, "fila de edição", "EDICOES_MOCK_PATH")
        from_portal = False
    elif config.planned_mock_path:
        return []
    else:
        trips = fetch_edit_trips(config.portal_api_url, config.portal_api_token)
        from_portal = True

    items: List[dict] = []
    already_edited = 0
    for trip in trips:
        if not trip.get("editRequestId"):
            raise RPAFrameworkException(f"Viagem {trip.get('tripId')} da fila de edição sem editRequestId: {trip}")
        if not trip.get("distributorReference"):
            # Sem o número não há como achar a programação na Consulta da Vibra.
            print(
                f"[AVISO] Viagem {trip['tripId']} (edição {trip['editRequestId']}): portal não informou o número "
                "da programação na Vibra - a edição vai falhar e o erro volta para o portal."
            )
        plates = _vibra_plates(trip)
        pools: Dict[str, List[dict]] = {}
        for order in trip.get("orders", []):
            pool = order.get("stationDistributorName") or order.get("stationName")
            if not pool:
                print(f"[AVISO] Viagem {trip['tripId']}, pedido {order.get('orderNumber')}: portal não informou o posto - ignorado.")
                continue
            pools.setdefault(pool, []).append({**order, "vibraPool": pool})
        if not pools:
            print(f"[AVISO] Viagem {trip['tripId']} (edição {trip['editRequestId']}): nenhum pedido com posto - ignorada.")
            continue

        edit_history = history.get(_edit_key(trip["editRequestId"]), {})
        item = {
            **_build_item(" / ".join(pools), plates[0], config.carrier),
            "plates": plates,
            "tripId": trip["tripId"],
            "loadDate": trip.get("loadDate"),
            "loadTime": trip.get("loadTime"),
            "tractorPlate": trip.get("tractorPlate"),
            "trailerPlates": trip.get("trailerPlates", []),
            "driverName": trip.get("driverName"),
            "driverTaxId": trip.get("driverTaxId"),
            "orders": [order for orders in pools.values() for order in orders],
            "pools": [{"pool": pool, "orders": orders} for pool, orders in pools.items()],
            "isEdit": True,
            "editRequestId": trip["editRequestId"],
            "scheduleNumber": str(trip.get("distributorReference") or ""),
            "reportToPortal": from_portal,
            "technicalErrors": edit_history.get("technical_errors", 0),
            # Programação antiga já excluída numa tentativa anterior: só falta cadastrar a nova.
            "scheduleDeleted": edit_history.get("deleted_schedule", ""),
            # Horário aceito pela Vibra no cadastro; nulo = carregamento alterado depois.
            "distributorScheduledTime": trip.get("distributorScheduledTime"),
        }
        if edit_history.get("finalized"):
            already_edited += 1
            if not from_portal:
                continue
            # Já alterada na Vibra, mas o retorno ao portal não chegou: só reenvia.
            item["alreadyEdited"] = True
            item["scheduleNumber"] = edit_history.get("schedule_number") or item["scheduleNumber"]
            item["scheduledTime"] = edit_history.get("scheduled_time", "")
        items.append(item)

    if already_edited:
        action = "só o resultado será reenviado ao portal" if from_portal else "ignorada(s)"
        print(f"[INFO] {already_edited} edição(ões) já EDITADA(S) na Vibra - {action}.")
    print(f"[OK] {len(items) - (already_edited if from_portal else 0)} programação(ões) a editar na Vibra.")
    return items


def _edit_key(edit_request_id) -> tuple:
    return "EDIT", str(edit_request_id).strip()


def _merge_into_trip(trips: Dict[str, dict], item: dict) -> None:
    """Junta os postos ainda não finalizados da mesma viagem numa programação só."""
    trip = trips.get(str(item["tripId"]))
    if trip is None:
        trips[str(item["tripId"])] = {**item, "pools": list(item["pools"]), "orders": list(item["orders"])}
        return
    trip["pools"].extend(item["pools"])
    trip["orders"].extend(item["orders"])
    trip["pool"] = " / ".join(part["pool"] for part in trip["pools"])
    trip["technicalErrors"] = max(trip["technicalErrors"], item["technicalErrors"])


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
                edit_request = EDIT_REQUEST_IN_DETAIL.search(row.get("detail") or "")
                key = _edit_key(edit_request.group(1)) if edit_request else _item_key(row["trip_id"], row.get("pool", ""))
                entry = history.setdefault(key, {"finalized": False, "technical_errors": 0})
                status = row.get("status")
                if status in (STATUS_FINALIZED, STATUS_EDITED):
                    entry["finalized"] = True
                    number = SCHEDULE_NUMBER_IN_DETAIL.search(row.get("detail") or "")
                    if number:
                        entry["schedule_number"] = number.group(1)
                    scheduled = SCHEDULED_TIME_IN_DETAIL.search(row.get("detail") or "")
                    if scheduled:
                        entry["scheduled_time"] = scheduled.group(1)
                elif status == STATUS_SCHEDULE_DELETED:
                    deleted = SCHEDULE_DELETED_IN_DETAIL.search(row.get("detail") or "")
                    entry["deleted_schedule"] = deleted.group(1) if deleted else "?"
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


def _vibra_plates(trip: dict) -> List[str]:
    """A Vibra cadastra o veículo pela placa da carreta, não pela do cavalo (com a do
    cavalo responde "O veículo informado está inválido"). Vão todas as carretas do
    conjunto, na ordem: se a Vibra recusar a 1ª, o VibraPortal tenta a próxima."""
    trailer_plates = [_normalize_plate(plate) for plate in trip.get("trailerPlates") or [] if plate and plate.strip()]
    if trailer_plates:
        return list(dict.fromkeys(trailer_plates))
    print(
        f"[AVISO] Viagem {trip.get('tripId')}: portal não informou a placa da carreta - "
        f"usando a do cavalo ({trip.get('tractorPlate')})."
    )
    return [_normalize_plate(trip["tractorPlate"])]


def _normalize_plate(plate: str) -> str:
    return plate.strip().upper().replace("-", "")


def _load_mock_trips(mock_path: str, kind: str = "viagens planejadas", env_name: str = "PLANEJADOS_MOCK_PATH") -> List[dict]:
    try:
        with open(mock_path, encoding="utf-8-sig") as file:
            trips = json.load(file).get("trips", [])
    except (OSError, ValueError, AttributeError) as exc:
        raise RPAFrameworkException(f"Falha ao ler o mock de {kind} {mock_path}: {exc}") from exc
    for trip in trips:
        if not trip.get("tripId") or not (trip.get("trailerPlates") or trip.get("tractorPlate")):
            raise RPAFrameworkException(
                f"{mock_path}: toda viagem precisa de tripId e trailerPlates (ou tractorPlate) ({trip})."
            )
    print(f"[AVISO] SIMULAÇÃO: {len(trips)} viagem(ns) ({kind}) lida(s) de {mock_path} ({env_name}) - portal não consultado.")
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
        "plate": _normalize_plate(plate),
        "carrier": (carrier or "").strip(),
        "pools": [{"pool": pool.strip(), "orders": []}],
    }
