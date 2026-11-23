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
MAX_TECHNICAL_ATTEMPTS tentativas seguidas."""

import os
from typing import List

from selenium.common.exceptions import WebDriverException

from Automation.VibraPortal import (
    RegistrationError,
    fill_registration,
    go_home,
    load_compartments,
    login,
    open_fob_registration,
    schedule_and_finalize,
    select_company,
)
from Framework.InitModule import BotConfig
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import (
    SCHEDULE_NUMBER_LABEL,
    SCHEDULED_LABEL,
    STATUS_ERROR,
    STATUS_ERROR_REPORTED,
    STATUS_FILLED,
    STATUS_FINALIZED,
    LogModule,
)
from Utils.DriverFactory import create_driver, load_cookies, save_cookies
from Utils.PortalClient import report_registration

DEFAULT_COOKIES_PATH = "vibra_cookies.json"
MAX_TECHNICAL_ATTEMPTS = 3
TECHNICAL_ERROR_MESSAGE = (
    "Falha técnica do agente ao cadastrar a Programação FOB na Vibra ({attempts} tentativas seguidas). "
    "Cadastre manualmente se for urgente e acione o suporte da automação (veja 'Detalhes técnicos'). "
    "Para o agente tentar de novo, use 'Tentar de novo' na viagem."
)


def process_items(items: List[dict], config: BotConfig, log_module: LogModule) -> None:

    already_finalized = [item for item in items if item.get("alreadyFinalized")]
    items = [item for item in items if not item.get("alreadyFinalized")]
    for item in already_finalized:
        _resend_finalized_result(config, item, log_module)

    if not items:
        print("[INFO] Nenhuma programação a registrar na Vibra - não abrindo o navegador.")
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
            print(
                f"[INFO] Programação {index}/{len(items)}: {trip_label}pool {item['pool']} | "
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
                company, selected, status, detail, schedule_number, vibra_message, scheduled_time = _register_trip(
                    driver, main_handle, config, item
                )
                detail = f"Unidade Vibra: {company} | {detail}"
            except Exception as exc:  # noqa: BLE001 - erro por programação não pode parar o loop
                screenshot_path = _save_screenshot(driver, f"erro_programacao_{index}.png")
                _handle_error(config, item, exc, screenshot_path, log_module)
                continue

            print(f"[OK] Programação da placa {item['plate']}: {status}.")
            if status == STATUS_FINALIZED and item.get("reportToPortal"):
                try:
                    _report_registered(config, item, schedule_number, vibra_message, scheduled_time)
                except Exception as exc:  # noqa: BLE001 - já está na Vibra; o próximo ciclo reenvia
                    print(f"[ERRO] Placa {item['plate']} cadastrada na Vibra, mas o retorno ao portal falhou: {exc}")
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
    filled = fill_registration(driver, first_pool, item["plate"], item["carrier"])
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


def _write_result(log_module: LogModule, item: dict, status: str, selected: dict = None, detail: str = "") -> None:
    """Uma linha por posto da programação: o histórico (GetDataModule) é por viagem + posto."""
    selected = selected or {}
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
