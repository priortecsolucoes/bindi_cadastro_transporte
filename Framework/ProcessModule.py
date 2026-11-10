"""PROCESS — login no Canal de Negócios Vibra e, para cada programação (viagem
planejada no Portal Bindi Log + posto), abre Programação FOB > Cadastrar e
preenche Pool, Placa e Transportador. Erro em uma programação não interrompe as
demais."""

import os
from typing import List

from Automation.VibraPortal import (
    fill_registration,
    load_compartments,
    login,
    open_fob_registration,
    schedule_and_finalize,
)
from Framework.InitModule import BotConfig
from Log.LogModule import STATUS_ERROR, STATUS_FILLED, STATUS_FINALIZED, LogModule
from Utils.DriverFactory import create_driver, load_cookies, save_cookies

DEFAULT_COOKIES_PATH = "vibra_cookies.json"


def process_items(items: List[dict], config: BotConfig, log_module: LogModule) -> None:

    if not items:
        print("[INFO] Nenhuma programação a registrar na Vibra - não abrindo o navegador.")
        return

    driver = create_driver(
        headless=config.headless,
        user_data_dir=config.chrome_user_data_dir,
        profile_directory=config.chrome_profile_directory,
        user_agent=config.chrome_user_agent,
        window_size=config.chrome_window_size,
        chrome_binary_path=config.chrome_binary_path,
    )

    cookies_path = os.environ.get("VIBRA_COOKIES_PATH", "").strip() or DEFAULT_COOKIES_PATH
    logged_in = False
    try:
        load_cookies(driver, cookies_path)
        login(
            driver,
            config.username,
            config.password,
            config.mfa_wait_seconds,
            token_api_url=config.token_api_url,
            token_api_token=config.token_api_token,
            token_timeout_seconds=config.token_timeout_seconds,
        )
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
                print(f"[INFO]   Pedido {order['orderNumber']} | Vibra {order['distributorOrderNumber']} | {products}")
            try:
                open_fob_registration(driver, main_handle)
                selected = fill_registration(driver, item["pool"], item["plate"], item["carrier"])
                if item.get("orders"):
                    loaded = load_compartments(driver, item["orders"])
                    vibra_message = schedule_and_finalize(driver, item["loadDate"], item["loadTime"])
                    status = STATUS_FINALIZED
                    detail = (
                        "Compartimentos: " + "; ".join(loaded)
                        + f" | Agendado {item['loadDate']} {item['loadTime']} | Finalizado"
                        + (f" | Vibra: {vibra_message}" if vibra_message else "")
                    )
                else:
                    status = STATUS_FILLED
                    detail = "Sem pedidos (CSV de teste) - compartimentos e agendamento não feitos"
                    print(f"[AVISO] {detail}.")
                log_module.write_result(item, status, selected["pool"], selected["carrier"], detail=detail)
                print(f"[OK] Programação da placa {item['plate']}: {status}.")
            except Exception as exc:  # noqa: BLE001 - erro por programação não pode parar o loop
                screenshot_path = _save_screenshot(driver, f"erro_programacao_{index}.png")
                print(f"[ERRO] Falha na programação da placa {item['plate']}: {exc}")
                log_module.write_result(item, STATUS_ERROR, detail=f"{exc} (screenshot: {screenshot_path})")
    finally:
        # Só com login confirmado: cookies de uma tentativa falha sobrescreveriam uma sessão boa.
        if logged_in:
            save_cookies(driver, cookies_path)
        try:
            driver.quit()
        except Exception as exc:  # noqa: BLE001 - falha ao encerrar não pode mascarar um erro anterior
            print(f"[AVISO] Falha ao encerrar o Chrome ao final da execução (ignorado): {exc}")


def _save_screenshot(driver, path: str) -> str:
    try:
        driver.save_screenshot(path)
        return path
    except Exception as exc:  # noqa: BLE001 - screenshot é diagnóstico, não pode mascarar o erro original
        print(f"[AVISO] Falha ao salvar screenshot {path}: {exc}")
        return "-"
