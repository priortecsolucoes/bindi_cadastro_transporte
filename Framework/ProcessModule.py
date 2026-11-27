"""PROCESS — login no Canal de Negócios Vibra e, para cada programação (viagem
planejada no Portal Bindi Log + posto), abre Programação FOB > Cadastrar,
preenche, finaliza e devolve o resultado ao portal. Erro em uma programação não
interrompe as demais.

Retorno ao portal, gravado só na viagem (o status do pedido não muda):
cadastrada -> selo REG com o número da programação na Vibra. Erro de dados
(RegistrationError: horário, placa, compartimento...) -> selo ERRO com a mensagem
para o usuário corrigir; o portal só devolve a viagem depois que ela for alterada,
então o erro não se repete a cada ciclo. Erro técnico é
tentado de novo nos próximos ciclos e só vai ao portal depois de
MAX_TECHNICAL_ATTEMPTS tentativas seguidas.

Depois dos cadastros, na mesma sessão, vem a fila de edição (itens com "isEdit"):
viagem já cadastrada que o usuário reabriu, alterou e liberou de novo. A
programação existente é aberta em Programação FOB > Consultar > Alterar e
refeita com os dados atuais da viagem; o resultado volta em
POST /api/rpa/vibra/edicoes, com as mesmas regras de erro do cadastro."""

import os
from datetime import date, timedelta
from typing import List

from selenium.common.exceptions import WebDriverException

from Automation.VibraPortal import (
    PlateChangedError,
    RegistrationError,
    ScheduleNotFoundError,
    compartments_already_match,
    delete_schedule,
    fill_edit_registration,
    fill_registration,
    find_schedule,
    go_home,
    load_compartments,
    login,
    open_fob_query,
    open_fob_registration,
    open_schedule_for_edit,
    report_step,
    schedule_and_finalize,
    select_company,
    set_step_reporter,
    unload_mismatched_compartments,
)
from Framework.InitModule import BotConfig
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import (
    DELETION_REQUEST_LABEL,
    EDIT_REQUEST_LABEL,
    SCHEDULE_DELETED_LABEL,
    SCHEDULE_NUMBER_LABEL,
    SCHEDULED_LABEL,
    STATUS_ERROR,
    STATUS_EDITED,
    STATUS_ERROR_REPORTED,
    STATUS_FILLED,
    STATUS_FINALIZED,
    STATUS_SCHEDULE_DELETED,
    LogModule,
)
from Utils.DriverFactory import create_driver, load_cookies, save_cookies
from Utils.PortalClient import report_deletion, report_edit, report_processing, report_registration

DEFAULT_COOKIES_PATH = "vibra_cookies.json"
MAX_TECHNICAL_ATTEMPTS = 3
# Etapa do selo PROCESSANDO: curta, porque é chamada várias vezes por viagem.
STEP_REQUEST_TIMEOUT_SECONDS = 10
TECHNICAL_ERROR_MESSAGE = (
    "Falha técnica do agente ao cadastrar a Programação FOB na Vibra ({attempts} tentativas seguidas). "
    "Cadastre manualmente se for urgente e acione o suporte da automação (veja 'Detalhes técnicos'). "
    "Para o agente tentar de novo, use 'Tentar de novo' na viagem."
)
TECHNICAL_DELETION_ERROR_MESSAGE = (
    "Falha técnica do agente ao excluir a Programação FOB {schedule_number} na Vibra ({attempts} tentativas "
    "seguidas). Exclua manualmente na Vibra e acione o suporte da automação."
)
TECHNICAL_EDIT_ERROR_MESSAGE = (
    "Falha técnica do agente ao alterar a Programação FOB {schedule_number} na Vibra ({attempts} tentativas "
    "seguidas). Altere manualmente na Vibra se for urgente e acione o suporte da automação (veja 'Detalhes "
    "técnicos'). Para o agente tentar de novo, use 'Tentar de novo' na viagem."
)


def process_items(items: List[dict], config: BotConfig, log_module: LogModule) -> None:

    for item in items:
        if item.get("alreadyFinalized"):
            _resend_finalized_result(config, item, log_module)
        elif item.get("alreadyEdited"):
            _resend_edited_result(config, item, log_module)
        elif item.get("alreadyDeleted"):
            _resend_deleted_result(config, item, log_module)
    items = [
        item for item in items
        if not item.get("alreadyFinalized") and not item.get("alreadyEdited") and not item.get("alreadyDeleted")
    ]
    # Cadastros, depois edições, depois exclusões (o GetDataModule já devolve nessa ordem).
    items.sort(key=lambda item: 2 if item.get("isDeletion") else 1 if item.get("isEdit") else 0)

    if not items:
        print("[INFO] Nenhuma programação a registrar, editar ou excluir na Vibra - não abrindo o navegador.")
        return

    try:
        driver = create_driver(
            headless=config.headless,
            user_data_dir=config.chrome_user_data_dir,
            profile_directory=config.chrome_profile_directory,
            user_agent=config.chrome_user_agent,
            window_size=config.chrome_window_size,
            chrome_binary_path=config.chrome_binary_path,
        )
    except Exception as exc:  # noqa: BLE001 - sem navegador nenhuma programação roda
        raise RPAFrameworkException(f"Não foi possível abrir o Chrome: {short_error(exc)}") from exc

    cookies_path = os.environ.get("VIBRA_COOKIES_PATH", "").strip() or DEFAULT_COOKIES_PATH
    logged_in = False
    # O login vale para a sessão inteira; no quadro ele aparece como etapa da 1ª viagem.
    first_trip = items[0] if _shows_processing(items[0]) else None
    if first_trip:
        _set_processing(config, first_trip, True)
    try:
        load_cookies(driver, cookies_path)
        try:
            login(
                driver,
                config.username,
                config.password,
                config.mfa_wait_seconds,
                token_api_url=config.token_api_url,
                token_api_token=config.token_api_token,
                token_timeout_seconds=config.token_timeout_seconds,
            )
        except RPAFrameworkException:
            if first_trip:
                _set_processing(config, first_trip, False)
            raise
        except Exception as exc:  # noqa: BLE001 - sem login nenhuma programação roda; tenta no próximo ciclo
            if first_trip:
                _set_processing(config, first_trip, False)
            screenshot_path = _save_screenshot(driver, "erro_login.png")
            raise RPAFrameworkException(
                f"Login na Vibra falhou: {short_error(exc)} (screenshot: {screenshot_path}). "
                f"{len(items)} programação(ões) ficam para o próximo ciclo."
            ) from exc
        logged_in = True
        save_cookies(driver, cookies_path)
        main_handle = driver.current_window_handle

        for index, item in enumerate(items, start=1):
            print("=" * 55)
            trip_label = f"viagem {item['tripId']} ({item['loadDate']} {item['loadTime']}) | " if item.get("tripId") else ""
            if item.get("isDeletion"):
                action = f"Exclusão da programação {item.get('scheduleNumber') or '(sem nº)'}"
            elif item.get("isEdit"):
                action = f"Edição da programação {item.get('scheduleNumber') or '(sem nº)'}"
            else:
                action = "Programação"
            print(
                f"[INFO] {action} {index}/{len(items)}: {trip_label}pool {item['pool']} | "
                f"placa {item['plate']} | transportador {item['carrier']}"
            )
            for order in item.get("orders", []):
                products = ", ".join(
                    f"{i['productName']} {i['qtyM3']:g} m³ (comp. {'/'.join(str(c['number']) for c in i['compartments'])})"
                    for i in order.get("items", [])
                )
                print(
                    f"[INFO]   Pedido {order['orderNumber']} | Vibra {order['distributorOrderNumber']} | "
                    f"{order.get('vibraPool', item['pool'])} | {products}"
                )
            if item is not first_trip:
                _set_processing(config, item, True)
            try:
                if item.get("isDeletion"):
                    step_result = _delete_trip_schedule(driver, main_handle, config, item)
                elif item.get("isEdit"):
                    step_result = _edit_trip(driver, main_handle, config, item, log_module)
                else:
                    step_result = _register_trip(driver, main_handle, config, item)
                company, selected, status, detail, schedule_number, vibra_message, scheduled_time = step_result
                detail = f"Unidade Vibra: {company} | {detail}"
            except Exception as exc:  # noqa: BLE001 - erro por programação não pode parar o loop
                kind = "exclusao" if item.get("isDeletion") else "edicao" if item.get("isEdit") else "programacao"
                screenshot_name = f"erro_{kind}_{index}.png"
                screenshot_path = _save_screenshot(driver, screenshot_name)
                _handle_error(config, item, exc, screenshot_path, log_module)
                _set_processing(config, item, False)
                continue

            print(f"[OK] Programação da placa {item['plate']}: {status}.")
            if status in (STATUS_FINALIZED, STATUS_EDITED, STATUS_SCHEDULE_DELETED) and item.get("reportToPortal"):
                if item.get("isDeletion"):
                    report = _report_deleted
                elif item.get("isEdit"):
                    report = _report_edited
                else:
                    report = _report_registered
                try:
                    report(config, item, schedule_number, vibra_message, scheduled_time)
                except Exception as exc:  # noqa: BLE001 - já está na Vibra; o próximo ciclo reenvia
                    print(f"[ERRO] Placa {item['plate']} {status.lower()} na Vibra, mas o retorno ao portal falhou: {exc}")
                    detail = f"{detail} | Retorno ao portal falhou (reenviado no próximo ciclo): {exc}"
            _set_processing(config, item, False)
            _write_result(log_module, item, status, selected, detail)
    finally:
        set_step_reporter(None)
        # Só com login confirmado: cookies de uma tentativa falha sobrescreveriam uma sessão boa.
        if logged_in:
            save_cookies(driver, cookies_path)
        try:
            driver.quit()
        except Exception as exc:  # noqa: BLE001 - falha ao encerrar não pode mascarar um erro anterior
            print(f"[AVISO] Falha ao encerrar o Chrome ao final da execução (ignorado): {exc}")


def _register_trip(driver, main_handle: str, config: BotConfig, item: dict) -> tuple:
    """Uma programação por viagem, sempre na unidade VIBRA_UNIDADE: o Pool dela
    lista todos os postos, e load_compartments troca o Pool antes dos
    compartimentos de cada posto."""
    go_home(driver, main_handle)
    company = select_company(driver, config.company)
    open_fob_registration(driver)
    first_pool = item["pools"][0]["pool"]
    filled = fill_registration(driver, first_pool, item.get("plates") or [item["plate"]], item["carrier"])
    if filled["plate"] != item["plate"]:
        print(f"[INFO] Vibra aceitou a carreta {filled['plate']} (a {item['plate']} foi recusada).")
        item["plate"] = filled["plate"]
    selected = {"pools": {first_pool: filled["pool"]}, "carrier": filled["carrier"]}
    if not item.get("orders"):
        detail = "Sem pedidos (CSV de teste) - compartimentos e agendamento não feitos"
        print(f"[AVISO] {detail}.")
        return company, selected, STATUS_FILLED, detail, "", "", ""

    loaded, switched_pools = load_compartments(driver, item["orders"], first_pool)
    selected["pools"].update(switched_pools)
    schedule_number, vibra_message, scheduled_time = schedule_and_finalize(driver, item["loadDate"], item["loadTime"])
    detail = (
        "Compartimentos: " + "; ".join(loaded)
        + f" | {SCHEDULED_LABEL} {item['loadDate']} {scheduled_time} | Finalizado"
        + (f" | {SCHEDULE_NUMBER_LABEL} {schedule_number}" if schedule_number else " | Nº da programação não lido")
        + (f" | Vibra: {vibra_message}" if vibra_message else "")
    )
    return company, selected, STATUS_FINALIZED, detail, schedule_number, vibra_message, scheduled_time


def _edit_trip(driver, main_handle: str, config: BotConfig, item: dict, log_module: LogModule) -> tuple:
    """Fila de edição. A alteração da Vibra (lápis) não muda data/horário, então:
    - data ou horário mudou: exclui a programação (lixeira da Consulta) e cadastra
      uma nova pelo fluxo do cadastro;
    - senão: Alterar > Compartimentação, mexendo só nos compartimentos que mudaram."""
    old_number = item["scheduleNumber"]
    if item.get("scheduleDeleted"):
        print(f"[INFO] Programação {old_number} já excluída numa tentativa anterior - cadastrando a nova.")
        return _recreate_schedule(driver, main_handle, config, item, old_number)

    go_home(driver, main_handle)
    company = select_company(driver, config.company)
    open_fob_query(driver)
    date_from, date_to = _edit_search_window(item["loadDate"], config.edit_search_days)
    current = find_schedule(driver, old_number, date_from, date_to, config.vibra_base, _query_clients(item))

    change = _schedule_change(item, current)
    if change:
        return _delete_and_recreate(driver, main_handle, config, item, log_module, change)

    print("[INFO] Data e horário mantidos - alterando só os compartimentos.")
    report_step("Data, horário e placa mantidos - editando só os compartimentos")
    open_schedule_for_edit(driver, old_number)
    first_pool = item["pools"][0]["pool"]
    # Placa e transportador ficam fixos na alteração: só confere a placa e seleciona o Pool.
    try:
        filled = fill_edit_registration(driver, first_pool, item.get("plates") or [item["plate"]], item["carrier"])
    except PlateChangedError as exc:
        # A placa só aparece na tela de alteração: volta à Consulta para excluir.
        go_home(driver, main_handle)
        select_company(driver, config.company)
        open_fob_query(driver)
        find_schedule(driver, old_number, date_from, date_to, config.vibra_base, _query_clients(item))
        change = f"Mudou a placa {exc.screen_plate} -> {' / '.join(exc.plates)}"
        return _delete_and_recreate(driver, main_handle, config, item, log_module, change)
    item["plate"] = filled["plate"]
    selected = {"pools": {first_pool: filled["pool"]}, "carrier": filled["carrier"]}

    # Liberada de novo sem mudança real (ex.: reabriu sem querer): não altera nada na Vibra.
    if compartments_already_match(driver, item["orders"]):
        print(f"[OK] Nada mudou em relação à programação {old_number} na Vibra - alteração não necessária.")
        detail = (
            f"{EDIT_REQUEST_LABEL} {item['editRequestId']} | Sem mudança: programação {old_number} mantida"
            f" | {SCHEDULED_LABEL} {current['date']} {current['time']} | Editado"
            f" | {SCHEDULE_NUMBER_LABEL} {old_number}"
        )
        return company, selected, STATUS_EDITED, detail, old_number, "", current["time"]

    # Só mexe no que mudou: esvazia (lixeira) os compartimentos divergentes, mantém os
    # que batem e carrega os novos.
    unloaded = unload_mismatched_compartments(driver, item["orders"])
    loaded, switched_pools = load_compartments(driver, item["orders"], first_pool)
    selected["pools"].update(switched_pools)
    schedule_number, vibra_message, scheduled_time = schedule_and_finalize(
        driver, current["date"], current["time"], edit=True
    )
    # A Vibra pode manter o número na alteração; sem ele na tela, vale o original.
    schedule_number = schedule_number or old_number
    detail = (
        f"{EDIT_REQUEST_LABEL} {item['editRequestId']} | Programação alterada: {old_number}"
        + (f" | Esvaziados: {'; '.join(unloaded)}" if unloaded else "")
        + " | Compartimentos: " + "; ".join(loaded)
        + f" | {SCHEDULED_LABEL} {current['date']} {scheduled_time} | Editado"
        + f" | {SCHEDULE_NUMBER_LABEL} {schedule_number}"
        + (f" | Vibra: {vibra_message}" if vibra_message else "")
    )
    return company, selected, STATUS_EDITED, detail, schedule_number, vibra_message, scheduled_time


def _delete_trip_schedule(driver, main_handle: str, config: BotConfig, item: dict) -> tuple:
    """Fila de exclusão: Consultar > acha a programação > lixeira > Excluir. Se ela
    não estiver na Consulta, já não existe na Vibra - conta como excluída."""
    number = item["scheduleNumber"]
    go_home(driver, main_handle)
    company = select_company(driver, config.company)
    open_fob_query(driver)
    clients = _query_clients(item)
    if clients:
        date_from, date_to = _edit_search_window(item["loadDate"], config.edit_search_days)
    else:
        # Sem os postos, find_schedule procura em todos os clientes da Consulta: só a fatia
        # de 5 dias em volta da data de carga, senão seriam dezenas de pesquisas por cliente.
        load_date = date.fromisoformat(item["loadDate"])
        date_from, date_to = load_date - timedelta(days=2), load_date + timedelta(days=2)
    try:
        find_schedule(driver, number, date_from, date_to, config.vibra_base, clients)
    except ScheduleNotFoundError:
        note = (
            f"Programação {number} não encontrada na Consulta da Vibra ({date_from:%d/%m/%Y} a {date_to:%d/%m/%Y}) "
            "- considerada já excluída."
        )
        print(f"[AVISO] {note}")
        detail = f"{DELETION_REQUEST_LABEL} {item['deletionRequestId']} | {note}"
        return company, {}, STATUS_SCHEDULE_DELETED, detail, number, note, ""
    delete_schedule(driver, number)
    detail = f"{DELETION_REQUEST_LABEL} {item['deletionRequestId']} | Programação {number} excluída na Vibra"
    return company, {}, STATUS_SCHEDULE_DELETED, detail, number, "", ""


def _query_clients(item: dict) -> List[dict]:
    """Postos da programação, o 1º Pool primeiro: na Consulta da Vibra o Cliente é o
    posto, e a programação só aparece com o Cliente dela selecionado (find_schedule
    tenta um por um). Edição: dos pedidos da viagem; exclusão: dos postos que o portal
    devolve no pedido de exclusão."""
    if item.get("isDeletion"):
        stations = item.get("stations") or []
    else:
        stations = [
            {
                "taxId": order.get("stationTaxId"),
                "name": order.get("stationName"),
                "distributorName": order.get("stationDistributorName"),
            }
            for part in item.get("pools") or []
            for order in part.get("orders", [])
        ]
    clients: List[dict] = []
    for station in stations:
        key = station.get("taxId") or station.get("distributorName") or station.get("name")
        if key and key not in [c.get("taxId") or c.get("distributorName") or c.get("name") for c in clients]:
            clients.append(station)
    return clients


def _schedule_change(item: dict, current: dict) -> str:
    """Descrição da mudança de data/horário entre a programação na Vibra e a viagem
    ("" = mantidos). O horário só conta como mudado se o carregamento foi alterado
    no portal (distributorScheduledTime nulo): a Vibra pode ter agendado no livre mais
    próximo do planejado, e isso não é mudança do usuário."""
    changes = []
    if current["date"] != item["loadDate"]:
        changes.append(f"data {current['date']} -> {item['loadDate']}")
    if not item.get("distributorScheduledTime") and current["time"] != item["loadTime"]:
        changes.append(f"horário {current['time']} -> {item['loadTime']}")
    old_plate = _registered_plate(item, current)
    if old_plate:
        changes.append(f"placa {old_plate} -> {' / '.join(item.get('plates') or [item['plate']])}")
    return ("Mudou " + " e ".join(changes)) if changes else ""


def _registered_plate(item: dict, current: dict) -> str:
    """Placa antiga da programação se ela for diferente da viagem ("" = mesma ou
    desconhecida). Fontes: placas na linha da Consulta (se a Vibra mostrar) e a placa
    gravada no histórico quando o agente cadastrou/editou. Desconhecida: a tela de
    alteração confere de novo (PlateChangedError)."""
    trip_plates = {p.replace("-", "") for p in (item.get("plates") or [item["plate"]])}
    if item.get("tractorPlate"):
        trip_plates.add(item["tractorPlate"].upper().replace("-", ""))
    row_plates = [p.replace("-", "") for p in current.get("plates") or []]
    if row_plates:
        return "" if trip_plates & set(row_plates) else row_plates[0]
    registered = item.get("registeredPlate") or ""
    return registered if registered and registered not in trip_plates else ""


def _delete_and_recreate(driver, main_handle: str, config: BotConfig, item: dict, log_module: LogModule, change: str) -> tuple:
    """A alteração da Vibra não muda data, horário nem placa: exclui a programação
    (a linha já está na Consulta, via find_schedule), marca EXCLUIDO no histórico -
    a próxima tentativa só cadastra - e cadastra a nova."""
    old_number = item["scheduleNumber"]
    print(f"[INFO] {change} - a alteração da Vibra não muda isso: excluindo a programação {old_number} e cadastrando outra.")
    report_step(f"{change}: excluindo a programação {old_number} para cadastrar outra")
    delete_schedule(driver, old_number)
    _write_result(
        log_module, item, STATUS_SCHEDULE_DELETED,
        detail=f"{SCHEDULE_DELETED_LABEL} {old_number} | {change}",
    )
    return _recreate_schedule(driver, main_handle, config, item, old_number, change)


def _recreate_schedule(
    driver, main_handle: str, config: BotConfig, item: dict, old_number: str, change: str = ""
) -> tuple:
    """Cadastra a programação nova depois de excluir a antiga. Erro aqui deixa a
    viagem sem programação na Vibra - a mensagem diz isso ao usuário."""
    report_step(f"Programação {old_number} excluída - cadastrando a nova")
    try:
        company, selected, _, detail, schedule_number, vibra_message, scheduled_time = _register_trip(
            driver, main_handle, config, item
        )
    except RegistrationError as exc:
        raise RegistrationError(
            f"A programação {old_number} foi excluída na Vibra ({_change_label(change)}), mas o cadastro da nova "
            f"falhou: {exc.user_message}",
            str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - relança com o contexto da exclusão já feita
        raise RuntimeError(
            f"Programação {old_number} excluída na Vibra, mas o cadastro da nova falhou (a próxima tentativa só "
            f"cadastra): {short_error(exc)}"
        ) from exc
    detail = (
        f"{EDIT_REQUEST_LABEL} {item['editRequestId']} | Programação {old_number} excluída ({_change_label(change)}) "
        f"e recriada | {detail}"
    )
    return company, selected, STATUS_EDITED, detail, schedule_number, vibra_message, scheduled_time


def _change_label(change: str) -> str:
    return change[0].lower() + change[1:] if change else "data/horário/placa mudou numa tentativa anterior"


def _shows_processing(item: dict) -> bool:
    # A exclusão não tem viagem (já foi apagada no portal); mock/CSV não falam com o portal.
    return not item.get("isDeletion") and bool(item.get("reportToPortal")) and bool(item.get("tripId"))


def _set_processing(config: BotConfig, item: dict, processing: bool) -> None:
    """Selo PROCESSANDO da viagem no quadro do portal e, enquanto ligado, a etapa atual
    (report_step do VibraPortal). Falha só avisa: o selo não pode impedir o cadastro."""
    set_step_reporter(None)
    if not _shows_processing(item):
        return
    action = "edit" if item.get("isEdit") else "register"
    first_step = "Iniciando a alteração da programação" if item.get("isEdit") else "Iniciando o cadastro da programação"
    try:
        report_processing(
            config.portal_api_url, config.portal_api_token, item["tripId"], processing, action,
            step=first_step if processing else None, start=processing,
        )
    except Exception as exc:  # noqa: BLE001 - só o selo do quadro
        state = "processando" if processing else "fim do processamento"
        print(f"[AVISO] Não foi possível marcar a viagem {item['tripId']} como {state} no portal: {short_error(exc)}")
        return
    if processing:
        set_step_reporter(_step_sender(config, item, action))


def _step_sender(config: BotConfig, item: dict, action: str):
    """Envia cada etapa ao portal. Depois de uma falha para de enviar nesta viagem:
    portal fora do ar não pode somar um timeout a cada etapa."""
    state = {"enabled": True}

    def send(text: str) -> None:
        if not state["enabled"]:
            return
        try:
            report_processing(
                config.portal_api_url, config.portal_api_token, item["tripId"], True, action,
                step=text, timeout=STEP_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            state["enabled"] = False
            raise

    return send


def _edit_search_window(load_date: str, search_days: int):
    """A programação a alterar está na data ANTIGA da viagem, que o portal não
    guarda: pesquisa de ontem até search_days à frente (ou até a data nova, se for
    depois disso)."""
    today = date.today()
    new_date = date.fromisoformat(load_date)
    return min(today - timedelta(days=1), new_date), max(today + timedelta(days=search_days), new_date)


def _write_result(log_module: LogModule, item: dict, status: str, selected: dict = None, detail: str = "") -> None:
    """Uma linha por posto da programação: o histórico (GetDataModule) é por viagem + posto.
    Na edição, uma linha só (histórico por pedido de edição, que vai no detail)."""
    selected = selected or {}
    if item.get("isDeletion"):
        if DELETION_REQUEST_LABEL not in detail:
            detail = f"{DELETION_REQUEST_LABEL} {item['deletionRequestId']} | {detail}"
        log_module.write_result(item, status, detail=detail)
        return
    if item.get("isEdit"):
        if EDIT_REQUEST_LABEL not in detail:
            detail = f"{EDIT_REQUEST_LABEL} {item['editRequestId']} | {detail}"
        log_module.write_result(
            item,
            status,
            " / ".join(selected.get("pools", {}).values()),
            selected.get("carrier", ""),
            detail=detail,
        )
        return
    for part in item.get("pools") or [item]:
        log_module.write_result(
            {**item, "pool": part["pool"], "orders": part.get("orders", item.get("orders", []))},
            status,
            selected.get("pools", {}).get(part["pool"], ""),
            selected.get("carrier", ""),
            detail=detail,
        )


def _handle_error(config: BotConfig, item: dict, exc: Exception, screenshot_path: str, log_module: LogModule) -> None:
    """Erro de dados vai direto ao portal. Erro técnico fica para o próximo ciclo
    até MAX_TECHNICAL_ATTEMPTS seguidas; aí também vai, para não ficar em loop."""
    technical_detail = f"{short_error(exc)} (screenshot: {screenshot_path})"
    attempts = item.get("technicalErrors", 0) + 1
    if isinstance(exc, RegistrationError):
        user_message = exc.user_message
    elif attempts >= MAX_TECHNICAL_ATTEMPTS:
        if item.get("isDeletion"):
            user_message = TECHNICAL_DELETION_ERROR_MESSAGE.format(
                attempts=attempts, schedule_number=item.get("scheduleNumber") or ""
            )
        elif item.get("isEdit"):
            user_message = TECHNICAL_EDIT_ERROR_MESSAGE.format(
                attempts=attempts, schedule_number=item.get("scheduleNumber") or ""
            )
        else:
            user_message = TECHNICAL_ERROR_MESSAGE.format(attempts=attempts)
    else:
        user_message = None

    print(f"[ERRO] Falha na programação da placa {item['plate']}: {user_message or short_error(exc)}")
    if not user_message or not item.get("reportToPortal"):
        if not isinstance(exc, RegistrationError):
            print(f"[INFO]   Erro técnico {attempts}/{MAX_TECHNICAL_ATTEMPTS} - nova tentativa no próximo ciclo.")
        _write_result(log_module, item, STATUS_ERROR, detail=technical_detail)
        return

    print(f"[INFO]   Detalhe técnico: {technical_detail}")
    if item.get("isDeletion"):
        _report_deletion_error(config, item, user_message, technical_detail, log_module)
        return
    if item.get("isEdit"):
        _report_edit_error(config, item, user_message, technical_detail, log_module)
        return
    try:
        order_results = [
            {
                "orderId": order["orderId"],
                "registered": False,
                "observation": user_message,
                "technicalDetail": technical_detail,
            }
            for order in item["orders"]
        ]
        response = report_registration(config.portal_api_url, config.portal_api_token, item["tripId"], order_results)
    except Exception as report_exc:  # noqa: BLE001 - registra no CSV e tenta de novo no próximo ciclo
        print(f"[ERRO] Falha ao informar o erro da placa {item['plate']} ao Portal Bindi Log: {report_exc}")
        _write_result(
            log_module, item, STATUS_ERROR, detail=f"{user_message} | {technical_detail} | Retorno ao portal falhou: {report_exc}"
        )
        return

    print(
        f"[OK] Erro informado ao Portal Bindi Log: erro na viagem {item['tripId']} ({response.get('failed', 0)} pedido(s)) - "
        "a viagem volta para o agente depois que o usuário corrigir e liberar de novo."
    )
    _write_result(log_module, item, STATUS_ERROR_REPORTED, detail=f"{user_message} | {technical_detail}")


def _report_deletion_error(
    config: BotConfig, item: dict, user_message: str, technical_detail: str, log_module: LogModule
) -> None:
    try:
        report_deletion(
            config.portal_api_url, config.portal_api_token, item["deletionRequestId"], False,
            observation=user_message, technical_detail=technical_detail,
        )
    except Exception as report_exc:  # noqa: BLE001 - registra no CSV e tenta de novo no próximo ciclo
        print(f"[ERRO] Falha ao informar o erro da exclusão {item['deletionRequestId']} ao Portal Bindi Log: {report_exc}")
        _write_result(
            log_module, item, STATUS_ERROR, detail=f"{user_message} | {technical_detail} | Retorno ao portal falhou: {report_exc}"
        )
        return
    print(f"[OK] Erro da exclusão {item['deletionRequestId']} informado ao Portal Bindi Log.")
    _write_result(log_module, item, STATUS_ERROR_REPORTED, detail=f"{user_message} | {technical_detail}")


def _report_deleted(config: BotConfig, item: dict, schedule_number: str, observation: str, _scheduled_time: str) -> None:
    report_deletion(config.portal_api_url, config.portal_api_token, item["deletionRequestId"], True, observation=observation)
    print(f"[OK] Resultado enviado ao Portal Bindi Log: programação {schedule_number} excluída na Vibra.")


def _resend_deleted_result(config: BotConfig, item: dict, log_module: LogModule) -> None:
    print("=" * 55)
    print(f"[INFO] Exclusão {item['deletionRequestId']}: já feita na Vibra - reenviando o resultado ao portal.")
    try:
        _report_deleted(config, item, item["scheduleNumber"], "", "")
    except Exception as exc:  # noqa: BLE001 - tenta de novo no próximo ciclo
        print(f"[ERRO] Retorno ao portal da exclusão {item['deletionRequestId']} falhou de novo: {exc}")
        return
    _write_result(log_module, item, STATUS_SCHEDULE_DELETED, detail="Resultado reenviado ao portal (já excluída na Vibra)")


def _report_edit_error(
    config: BotConfig, item: dict, user_message: str, technical_detail: str, log_module: LogModule
) -> None:
    try:
        report_edit(
            config.portal_api_url, config.portal_api_token, item["editRequestId"], item["tripId"], False,
            observation=user_message, technical_detail=technical_detail,
        )
    except Exception as report_exc:  # noqa: BLE001 - registra no CSV e tenta de novo no próximo ciclo
        print(f"[ERRO] Falha ao informar o erro da edição da viagem {item['tripId']} ao Portal Bindi Log: {report_exc}")
        _write_result(
            log_module, item, STATUS_ERROR, detail=f"{user_message} | {technical_detail} | Retorno ao portal falhou: {report_exc}"
        )
        return
    print(
        f"[OK] Erro informado ao Portal Bindi Log: edição da viagem {item['tripId']} - a viagem volta para a fila "
        "de edição depois que o usuário corrigir e liberar de novo (ou clicar 'Tentar de novo')."
    )
    _write_result(log_module, item, STATUS_ERROR_REPORTED, detail=f"{user_message} | {technical_detail}")


def _report_edited(
    config: BotConfig, item: dict, schedule_number: str, vibra_message: str, scheduled_time: str
) -> None:
    response = report_edit(
        config.portal_api_url, config.portal_api_token, item["editRequestId"], item["tripId"], True,
        distributor_message=vibra_message, schedule_number=schedule_number, scheduled_time=scheduled_time,
    )
    print(
        f"[OK] Resultado enviado ao Portal Bindi Log: viagem {item['tripId']} - programação {schedule_number} alterada"
        f" ({response.get('orders', 0)} pedido(s))."
    )


def _resend_edited_result(config: BotConfig, item: dict, log_module: LogModule) -> None:
    print("=" * 55)
    print(
        f"[INFO] Viagem {item['tripId']} (edição {item['editRequestId']}): já alterada na Vibra - "
        "reenviando o resultado ao portal."
    )
    number = item.get("scheduleNumber", "")
    scheduled_time = item.get("scheduledTime", "")
    try:
        _report_edited(config, item, number, "", scheduled_time)
    except Exception as exc:  # noqa: BLE001 - tenta de novo no próximo ciclo
        print(f"[ERRO] Retorno ao portal da edição da viagem {item['tripId']} falhou de novo: {exc}")
        return
    _write_result(
        log_module,
        item,
        STATUS_EDITED,
        detail="Resultado reenviado ao portal (já alterada na Vibra)"
        + (f" | {SCHEDULED_LABEL} {item['loadDate']} {scheduled_time}" if scheduled_time else "")
        + (f" | {SCHEDULE_NUMBER_LABEL} {number}" if number else ""),
    )


def _report_registered(
    config: BotConfig, item: dict, schedule_number: str, vibra_message: str, scheduled_time: str
) -> None:
    order_results = [{"orderId": order["orderId"], "registered": True} for order in item["orders"]]
    response = report_registration(
        config.portal_api_url, config.portal_api_token, item["tripId"], order_results, vibra_message,
        schedule_number, scheduled_time,
    )
    print(
        f"[OK] Resultado enviado ao Portal Bindi Log: viagem {item['tripId']} cadastrada"
        + (f" (programação {schedule_number})" if schedule_number else "")
        + f" - {response.get('registered', 0)} pedido(s)."
    )


def _resend_finalized_result(config: BotConfig, item: dict, log_module: LogModule) -> None:
    print("=" * 55)
    print(f"[INFO] Viagem {item['tripId']} | pool {item['pool']}: já finalizada na Vibra - reenviando o resultado ao portal.")
    try:
        _report_registered(config, item, item.get("scheduleNumber", ""), "", item.get("scheduledTime", ""))
    except Exception as exc:  # noqa: BLE001 - tenta de novo no próximo ciclo
        print(f"[ERRO] Retorno ao portal da viagem {item['tripId']} falhou de novo: {exc}")
        return
    number = item.get("scheduleNumber")
    scheduled_time = item.get("scheduledTime")
    _write_result(
        log_module,
        item,
        STATUS_FINALIZED,
        detail="Resultado reenviado ao portal (já finalizada na Vibra)"
        + (f" | {SCHEDULED_LABEL} {item['loadDate']} {scheduled_time}" if scheduled_time else "")
        + (f" | {SCHEDULE_NUMBER_LABEL} {number}" if number else ""),
    )


def short_error(exc: BaseException) -> str:
    """Mensagem de erro legível numa linha. Exceções do Selenium trazem o
    stacktrace interno do chromedriver no texto, que só polui o log."""
    message = exc.msg if isinstance(exc, WebDriverException) and exc.msg else str(exc)
    message = message.split("Stacktrace:")[0].strip()
    message = " ".join(message.split()) or type(exc).__name__
    return message if len(message) <= 400 else message[:400] + "..."


def _save_screenshot(driver, path: str) -> str:
    try:
        driver.save_screenshot(path)
        return path
    except Exception as exc:  # noqa: BLE001 - screenshot é diagnóstico, não pode mascarar o erro original
        print(f"[AVISO] Falha ao salvar screenshot {path}: {exc}")
        return "-"
