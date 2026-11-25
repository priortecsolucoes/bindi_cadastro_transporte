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
    RegistrationError,
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
    schedule_and_finalize,
    select_company,
    unload_mismatched_compartments,
)
from Framework.InitModule import BotConfig
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import (
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
from Utils.PortalClient import report_edit, report_registration

DEFAULT_COOKIES_PATH = "vibra_cookies.json"
MAX_TECHNICAL_ATTEMPTS = 3
TECHNICAL_ERROR_MESSAGE = (
    "Falha técnica do agente ao cadastrar a Programação FOB na Vibra ({attempts} tentativas seguidas). "
    "Cadastre manualmente se for urgente e acione o suporte da automação (veja 'Detalhes técnicos'). "
    "Para o agente tentar de novo, use 'Tentar de novo' na viagem."
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
    # Cadastros antes das edições (o GetDataModule já devolve nessa ordem).
    items = [item for item in items if not item.get("alreadyFinalized") and not item.get("alreadyEdited")]
    items.sort(key=lambda item: bool(item.get("isEdit")))

    if not items:
        print("[INFO] Nenhuma programação a registrar ou editar na Vibra - não abrindo o navegador.")
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
            raise
        except Exception as exc:  # noqa: BLE001 - sem login nenhuma programação roda; tenta no próximo ciclo
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
            action = f"Edição da programação {item.get('scheduleNumber') or '(sem nº)'}" if item.get("isEdit") else "Programação"
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
            try:
                if item.get("isEdit"):
                    step_result = _edit_trip(driver, main_handle, config, item, log_module)
                else:
                    step_result = _register_trip(driver, main_handle, config, item)
                company, selected, status, detail, schedule_number, vibra_message, scheduled_time = step_result
                detail = f"Unidade Vibra: {company} | {detail}"
            except Exception as exc:  # noqa: BLE001 - erro por programação não pode parar o loop
                screenshot_name = f"erro_edicao_{index}.png" if item.get("isEdit") else f"erro_programacao_{index}.png"
                screenshot_path = _save_screenshot(driver, screenshot_name)
                _handle_error(config, item, exc, screenshot_path, log_module)
                continue

            print(f"[OK] Programação da placa {item['plate']}: {status}.")
            if status in (STATUS_FINALIZED, STATUS_EDITED) and item.get("reportToPortal"):
                report = _report_edited if item.get("isEdit") else _report_registered
                try:
                    report(config, item, schedule_number, vibra_message, scheduled_time)
                except Exception as exc:  # noqa: BLE001 - já está na Vibra; o próximo ciclo reenvia
                    print(f"[ERRO] Placa {item['plate']} {status.lower()} na Vibra, mas o retorno ao portal falhou: {exc}")
                    detail = f"{detail} | Retorno ao portal falhou (reenviado no próximo ciclo): {exc}"
            _write_result(log_module, item, status, selected, detail)
    finally:
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
    # Na Consulta o Cliente é a unidade ativa; o diálogo de troca mostra o nome sem o código.
    current = find_schedule(
        driver, old_number, date_from, date_to, config.vibra_base, config.company.split(" - ", 1)[-1].strip()
    )

    change = _schedule_change(item, current)
    if change:
        print(f"[INFO] {change} - a alteração da Vibra não muda isso: excluindo a programação {old_number} e cadastrando outra.")
        delete_schedule(driver, old_number)
        _write_result(
            log_module, item, STATUS_SCHEDULE_DELETED,
            detail=f"{SCHEDULE_DELETED_LABEL} {old_number} | {change}",
        )
        return _recreate_schedule(driver, main_handle, config, item, old_number)

    print("[INFO] Data e horário mantidos - alterando só os compartimentos.")
    open_schedule_for_edit(driver, old_number)
    first_pool = item["pools"][0]["pool"]
    # Placa e transportador ficam fixos na alteração: só confere a placa e seleciona o Pool.
    filled = fill_edit_registration(driver, first_pool, item.get("plates") or [item["plate"]], item["carrier"])
    item["plate"] = filled["plate"]
    selected = {"pools": {first_pool: filled["pool"]}, "carrier": filled["carrier"]}

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
    return ("Mudou " + " e ".join(changes)) if changes else ""


def _recreate_schedule(driver, main_handle: str, config: BotConfig, item: dict, old_number: str) -> tuple:
    """Cadastra a programação nova depois de excluir a antiga. Erro aqui deixa a
    viagem sem programação na Vibra - a mensagem diz isso ao usuário."""
    try:
        company, selected, _, detail, schedule_number, vibra_message, scheduled_time = _register_trip(
            driver, main_handle, config, item
        )
    except RegistrationError as exc:
        raise RegistrationError(
            f"A programação {old_number} foi excluída na Vibra para mudar a data/horário, mas o cadastro da nova "
            f"falhou: {exc.user_message}",
            str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - relança com o contexto da exclusão já feita
        raise RuntimeError(
            f"Programação {old_number} excluída na Vibra, mas o cadastro da nova falhou (a próxima tentativa só "
            f"cadastra): {short_error(exc)}"
        ) from exc
    detail = (
        f"{EDIT_REQUEST_LABEL} {item['editRequestId']} | Programação {old_number} excluída (data/horário mudou) "
        f"e recriada | {detail}"
    )
    return company, selected, STATUS_EDITED, detail, schedule_number, vibra_message, scheduled_time


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
        user_message = (
            TECHNICAL_EDIT_ERROR_MESSAGE.format(attempts=attempts, schedule_number=item.get("scheduleNumber") or "")
            if item.get("isEdit")
            else TECHNICAL_ERROR_MESSAGE.format(attempts=attempts)
        )
    else:
        user_message = None

    print(f"[ERRO] Falha na programação da placa {item['plate']}: {user_message or short_error(exc)}")
    if not user_message or not item.get("reportToPortal"):
        if not isinstance(exc, RegistrationError):
            print(f"[INFO]   Erro técnico {attempts}/{MAX_TECHNICAL_ATTEMPTS} - nova tentativa no próximo ciclo.")
        _write_result(log_module, item, STATUS_ERROR, detail=technical_detail)
        return

    print(f"[INFO]   Detalhe técnico: {technical_detail}")
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
